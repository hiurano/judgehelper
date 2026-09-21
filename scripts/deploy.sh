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

app_image="judge-helper:latest"
rollback_image="judge-helper:rollback"

reload_caddy() {
    # The Caddyfile is a bind mount: compose sees no reason to recreate the
    # container when only the file's contents change, and Caddy does not watch
    # it. Without this a proxy change deploys to disk and nowhere else, while
    # the rollout reports success because the backend answers fine.
    if [ -z "$(docker compose ps --quiet caddy 2>/dev/null)" ]; then
        echo "Caddy is not running; it will pick the config up when it starts."
        return 0
    fi
    echo "Reloading the Caddy configuration..."
    if docker compose exec -T caddy \
        caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
        return 0
    fi
    # A rejected config leaves the previous one serving, so the site stays up.
    echo "Caddy refused the new configuration and kept the previous one." >&2
    return 1
}

wait_for_ready() {
    local attempt
    for attempt in $(seq 1 30); do
        if docker compose exec -T judge-helper python -c \
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)" \
            >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

run_python -m scripts.preflight
run_backup
docker compose config --quiet

# Keep whatever is serving right now, so a bad rollout has somewhere to return
# to. Resolve it from the running container rather than from a tag: the tag may
# not exist yet, and the container is the honest answer to what is live.
rollback_available=0
current_container="$(docker compose ps --quiet judge-helper 2>/dev/null | head -n 1)"
if [ -n "$current_container" ]; then
    current_image="$(docker inspect --format '{{.Image}}' "$current_container" 2>/dev/null || true)"
    if [ -n "$current_image" ]; then
        docker tag "$current_image" "$rollback_image"
        rollback_available=1
    fi
fi
if [ "$rollback_available" = "0" ]; then
    echo "Nothing is running yet; this deploy cannot be rolled back."
fi

docker compose build --pull

# compose exits non-zero when a dependency never turns healthy. Under set -e
# that would abort the script before it can diagnose or roll back, so keep
# going and let the readiness check below decide.
docker compose up -d --remove-orphans || true

echo "Waiting for the backend readiness check..."
if wait_for_ready; then
    # The application is healthy at this point. A proxy config that failed to
    # load is worth failing the deploy over, but not worth rolling the
    # application back for: the old proxy config is still serving it.
    if ! reload_caddy; then
        docker compose ps
        echo "Deployment failed: the application is live but Caddy kept its old configuration." >&2
        exit 1
    fi
    docker compose ps
    echo "Deployment completed successfully."
    exit 0
fi

echo "Deployment failed: backend did not become ready." >&2
docker compose logs --tail=100 judge-helper >&2

if [ "$rollback_available" = "0" ]; then
    echo "No previous image to roll back to; the service is down." >&2
    exit 1
fi

echo "Rolling back to the previous image..." >&2
docker tag "$rollback_image" "$app_image"
docker compose up -d --force-recreate --no-build >&2 || true

if wait_for_ready; then
    reload_caddy || true
    docker compose ps
    echo "Rolled back to the previous image. The new build was NOT deployed." >&2
    exit 1
fi

echo "Rollback failed as well; the service is down and needs a human." >&2
docker compose logs --tail=100 judge-helper >&2
exit 1
