#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

env_file="${ENV_FILE:-.env}"

run_python() {
    if command -v python3 >/dev/null 2>&1; then
        python3 "$@"
    elif command -v python >/dev/null 2>&1; then
        python "$@"
    else
        docker run --rm --network none \
            --user "$(id -u):$(id -g)" \
            --volume "$project_dir:/workspace" \
            --workdir /workspace \
            python:3.12-slim python "$@"
    fi
}

# Last assignment wins, the way the app itself reads the file. These keys are
# optional, so fall back to the same defaults scripts/preflight.py uses.
read_env() {
    local value
    value="$(sed -n "s/^$1=//p" "$env_file" | tail -n 1 | tr -d "\"'")"
    printf '%s\n' "${value:-$2}"
}

absolute() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s\n' "$project_dir/$1" ;;
    esac
}

app_uid="$(read_env APP_UID 1000)"
app_gid="$(read_env APP_GID 1000)"
data_dir="$(absolute "$(read_env HOST_DATA_DIR backend/data)")"
backup_dir="$(absolute "$(read_env BACKUP_DIR backend/data/backups)")"
env_target="$(readlink -f "$project_dir/$env_file")"

# SQLite creates -wal/-shm sidecars owned by whoever opens the database. The
# app runs as APP_UID:APP_GID and cannot use sidecars left by the deploying
# user, so take the pre-deploy backup as the service account -- the same one
# judge-helper-backup.service uses for the scheduled backup.
run_backup() {
    if [ "$(id -u)" = "$app_uid" ] && [ "$(id -g)" = "$app_gid" ]; then
        run_python -m scripts.backup_db
        return
    fi

    # A bind mount of a missing host path is created by the daemon as root,
    # which the service account could not then write to. Insist instead.
    for required in "$data_dir" "$backup_dir"; do
        if [ ! -d "$required" ]; then
            echo "Deployment failed: $required does not exist." >&2
            exit 1
        fi
    done

    echo "Taking the pre-deploy backup as ${app_uid}:${app_gid}..."
    docker run --rm --network none \
        --user "${app_uid}:${app_gid}" \
        --volume "$project_dir:/workspace" \
        --volume "$data_dir:$data_dir" \
        --volume "$backup_dir:$backup_dir" \
        --volume "$env_target:$env_target:ro" \
        --workdir /workspace \
        python:3.12-slim python -m scripts.backup_db
}

run_python -m scripts.preflight
run_backup
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans

echo "Waiting for the backend readiness check..."
for attempt in $(seq 1 30); do
    if docker compose exec -T judge-helper python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)"; then
        docker compose ps
        echo "Deployment completed successfully."
        exit 0
    fi
    sleep 2
done

docker compose logs --tail=100 judge-helper
echo "Deployment failed: backend did not become ready." >&2
exit 1
