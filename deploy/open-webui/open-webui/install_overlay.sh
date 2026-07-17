#!/usr/bin/env sh
set -eu

BUILD_DIR="${1:-/app/build}"
INDEX="${BUILD_DIR}/index.html"

if [ ! -f "$INDEX" ]; then
  echo "Open WebUI index not found: $INDEX" >&2
  exit 1
fi

cp /tmp/wonju-health-overlay.css "${BUILD_DIR}/wonju-health-overlay.css"
cp /tmp/wonju-health-overlay.js "${BUILD_DIR}/wonju-health-overlay.js"
cp /tmp/wonju-health-mark.svg "${BUILD_DIR}/wonju-health-mark.svg"
cp /tmp/wonju-health-manifest.json "${BUILD_DIR}/wonju-health-manifest.json"

# Remove stock Open WebUI identity from the first paint and installed PWA.
# The runtime overlay repeats the title assignment as a defensive fallback,
# but the static document must already be branded before JavaScript executes.
sed -i \
  -e 's#<html lang="en">#<html lang="ko">#' \
  -e 's#<title>Open WebUI</title>#<title>원주시 생활건강 안내 AI</title>#' \
  -e 's|content="#171717"|content="#155247"|' \
  -e 's#type="image/png"#type="image/svg+xml"#g' \
  -e 's#href="/static/favicon.png"#href="/wonju-health-mark.svg"#g' \
  -e 's#href="/static/favicon-96x96.png"#href="/wonju-health-mark.svg"#g' \
  -e 's#href="/static/favicon.svg"#href="/wonju-health-mark.svg"#g' \
  -e 's#href="/static/favicon.ico"#href="/wonju-health-mark.svg"#g' \
  -e 's#href="/static/apple-touch-icon.png"#href="/wonju-health-mark.svg"#g' \
  -e 's#href="/manifest.json"#href="/wonju-health-manifest.json"#g' \
  -e "s#'/static/splash-dark.png'#'/wonju-health-mark.svg'#g" \
  -e "s#'/static/splash.png'#'/wonju-health-mark.svg'#g" \
  "$INDEX"

# A content-derived query version prevents browsers from keeping an old overlay
# after a deployment while remaining deterministic for identical assets.
VERSION="$(cat /tmp/wonju-health-overlay.css /tmp/wonju-health-overlay.js | sha256sum | cut -c1-12)"

# The insertion is idempotent so rebuilding on top of a customized image is safe.
if ! grep -q 'wonju-health-overlay.css' "$INDEX"; then
  sed -i "s#</head>#<link rel=\"stylesheet\" href=\"/wonju-health-overlay.css?v=${VERSION}\">\n<script defer src=\"/wonju-health-overlay.js?v=${VERSION}\"></script>\n</head>#" "$INDEX"
fi

grep -q 'wonju-health-overlay.js' "$INDEX"
