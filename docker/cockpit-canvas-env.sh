#!/bin/sh
set -eu

# Helm mounts its complete env.js ConfigMap. Compose uses the image's default
# env.js and may opt into only this public, non-secret host suffix.
master="$(printf '%s' "${CANVAS_LIVE_PREVIEW_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
enabled="$(printf '%s' "${CANVAS_VIEWER_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
suffix="${CANVAS_VIEWER_HOST_SUFFIX:-}"
case "$master" in
  1|true|yes|on) ;;
  *) exit 0 ;;
esac
case "$enabled" in
  1|true|yes|on) ;;
  *) exit 0 ;;
esac
if [ -z "$suffix" ]; then
  exit 0
fi

if [ "${#suffix}" -gt 253 ] || ! printf '%s' "$suffix" | grep -Eq '^\.([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$'; then
  echo "CANVAS_VIEWER_HOST_SUFFIX is not a valid dotted ASCII DNS suffix" >&2
  exit 1
fi

env_file=/usr/share/nginx/html/assets/env.js
if ! grep -Fq "window['env']['canvasViewerHostSuffix']" "$env_file"; then
  echo "Cockpit env.js has no Canvas viewer setting" >&2
  exit 1
fi

sed -i "/canvasViewerHostSuffix/c\  window['env']['canvasViewerHostSuffix'] = '$suffix';" "$env_file"
