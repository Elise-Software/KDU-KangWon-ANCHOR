#!/usr/bin/env bash
set -euo pipefail

readonly DEPLOY_ROOT="/home/elise/Desktop/KDU-KangWon-ANCHOR/deploy/open-webui"
readonly UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"

if [[ ! -f "${DEPLOY_ROOT}/.env" ]]; then
  echo "Create ${DEPLOY_ROOT}/.env from .env.example before installing services." >&2
  exit 1
fi

install -d -m 0755 "${UNIT_DIR}"
install -m 0644 "${DEPLOY_ROOT}/systemd/wonju-vllm.service" "${UNIT_DIR}/wonju-vllm.service"
install -m 0644 "${DEPLOY_ROOT}/systemd/wonju-health-ai.service" "${UNIT_DIR}/wonju-health-ai.service"
install -m 0755 "${DEPLOY_ROOT}/systemd/run-wonju-vllm.sh" "${UNIT_DIR}/run-wonju-vllm.sh"
install -m 0755 "${DEPLOY_ROOT}/systemd/run-wonju-health-ai.sh" "${UNIT_DIR}/run-wonju-health-ai.sh"

sudo loginctl enable-linger "${USER}"
systemctl --user daemon-reload
systemctl --user enable --now wonju-vllm.service wonju-health-ai.service
systemctl --user --no-pager status wonju-vllm.service wonju-health-ai.service
