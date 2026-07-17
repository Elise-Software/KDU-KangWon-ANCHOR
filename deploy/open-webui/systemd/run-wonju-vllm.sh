#!/usr/bin/env bash
set -euo pipefail

readonly CONTAINER_NAME="wonju-vllm"
readonly NETWORK_NAME="wonju-health-internal"
readonly RECIPE_ROOT="/home/elise/spark-vllm-docker"

# The user service can start before Docker during boot. Keep waiting here so
# systemd does not burn through restart attempts while the daemon initializes.
until /usr/bin/docker info >/dev/null 2>&1; do
  sleep 5
done

if ! /usr/bin/docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
  /usr/bin/docker network create "${NETWORK_NAME}" >/dev/null
fi

if ! /usr/bin/docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
  cd "${RECIPE_ROOT}"
  ./run-recipe.sh gemma4-31b-nvfp4-512k \
    --solo \
    --daemon \
    --name "${CONTAINER_NAME}" \
    --host 0.0.0.0 \
    -p 127.0.0.1:8000:8000
elif [[ "$(/usr/bin/docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}")" != "true" ]]; then
  /usr/bin/docker start "${CONTAINER_NAME}" >/dev/null
fi

# The recipe connects the default bridge for the loopback host publication.
# P1 and the raw proxy additionally reach it by Docker DNS on this network.
if ! /usr/bin/docker inspect -f '{{json .NetworkSettings.Networks}}' "${CONTAINER_NAME}" \
  | /usr/bin/grep -q "\"${NETWORK_NAME}\""; then
  /usr/bin/docker network connect "${NETWORK_NAME}" "${CONTAINER_NAME}"
fi

# Attach systemd to the daemonized recipe container. Any unexpected container
# exit makes this helper fail so Restart=on-failure launches the recipe again.
/usr/bin/docker wait "${CONTAINER_NAME}" >/dev/null || true
exit 1
