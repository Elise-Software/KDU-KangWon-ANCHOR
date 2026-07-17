ARG OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.9.6
FROM ${OPEN_WEBUI_IMAGE}

ARG UID=0
ARG GID=0

USER root
COPY overlay/wonju-health-overlay.css /tmp/wonju-health-overlay.css
COPY overlay/wonju-health-overlay.js /tmp/wonju-health-overlay.js
COPY overlay/wonju-health-mark.svg /tmp/wonju-health-mark.svg
COPY overlay/manifest.json /tmp/wonju-health-manifest.json
COPY open-webui/install_overlay.sh /tmp/install_overlay.sh
RUN chmod 0755 /tmp/install_overlay.sh \
    && /tmp/install_overlay.sh /app/build \
    && rm -f /tmp/install_overlay.sh /tmp/wonju-health-overlay.css /tmp/wonju-health-overlay.js \
        /tmp/wonju-health-mark.svg /tmp/wonju-health-manifest.json

USER ${UID}:${GID}
