#!/usr/bin/env bash
# Change values in the production env file and verify the result.
#
#   ./scripts/set-config.sh CADDY_DOMAIN=example.ru BASE_URL=https://example.ru
#
# The env file is owned by root and deploy has no usable sudo, so the edit runs
# in a throwaway container. That works because deploy is in the docker group --
# which is root-equivalent anyway, so this grants nothing new; it just makes the
# edit repeatable, backed up and validated instead of improvised under pressure.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [ "$#" -eq 0 ]; then
    echo "usage: $0 KEY=VALUE [KEY=VALUE ...]" >&2
    exit 2
fi

env_file="${ENV_FILE:-.env}"
env_target="$(readlink -f "$project_dir/$env_file")"
if [ ! -f "$env_target" ]; then
    echo "No env file at $env_target" >&2
    exit 1
fi

echo "Editing $env_target"
docker run --rm --network none \
    --volume "$project_dir/scripts/set_config.py:/set_config.py:ro" \
    --volume "$env_target:$env_target" \
    python:3.12-slim python /set_config.py "$env_target" "$@"

# A bad value here takes the site down on the next deploy, so check now.
echo
echo "Verifying the configuration..."
if python3 -m scripts.preflight; then
    echo
    echo "Config updated. Run ./scripts/deploy.sh (or push to main) to apply it."
else
    echo
    echo "Preflight rejected the new configuration. Restore the backup printed" >&2
    echo "above before deploying, or correct the values." >&2
    exit 1
fi
