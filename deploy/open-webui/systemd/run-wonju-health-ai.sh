#!/usr/bin/env bash
set -euo pipefail

# DGX Spark deployment root. This is intentionally separate from the vLLM
# recipe root: Compose owns only the gateway, Open WebUI and P1 adapters.
readonly DEPLOY_ROOT="/home/elise/Desktop/KDU-KangWon-ANCHOR/deploy/open-webui"
readonly ENV_FILE="${DEPLOY_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing required environment file: ${ENV_FILE}" >&2
  exit 1
fi

# Docker may not be ready when the user manager starts at boot.
until /usr/bin/docker info >/dev/null 2>&1; do
  sleep 5
done

cd "${DEPLOY_ROOT}"

# Do not rebuild on every boot. `--wait` makes systemd mark this unit active
# only after Caddy, Open WebUI, P1 API and the permission synchronizer are up.
/usr/bin/docker compose --env-file "${ENV_FILE}" up -d --remove-orphans --wait --wait-timeout 900
