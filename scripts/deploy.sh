#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

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

run_python -m scripts.preflight
run_python -m scripts.backup_db
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
