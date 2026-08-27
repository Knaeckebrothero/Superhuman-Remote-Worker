#!/bin/sh
set -eu

# Helm mounts its complete env.js ConfigMap. Compose uses the image's default
# env.js and may opt into either public, non-secret Canvas origin setting.
env_file=/usr/share/nginx/html/assets/env.js

configure_viewer_origin() {
  master="$(printf '%s' "${CANVAS_LIVE_PREVIEW_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
  enabled="$(printf '%s' "${CANVAS_VIEWER_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
  suffix="${CANVAS_VIEWER_HOST_SUFFIX:-}"
  case "$master" in
    1|true|yes|on) ;;
    *) return ;;
  esac
  case "$enabled" in
    1|true|yes|on) ;;
    *) return ;;
  esac
  if [ -z "$suffix" ]; then
    return
  fi
  if [ "${#suffix}" -gt 253 ] || ! printf '%s' "$suffix" | grep -Eq '^\.([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$'; then
    echo "CANVAS_VIEWER_HOST_SUFFIX is not a valid dotted ASCII DNS suffix" >&2
    exit 1
  fi
  if ! grep -Fq "window['env']['canvasViewerHostSuffix']" "$env_file"; then
    echo "Cockpit env.js has no Canvas viewer setting" >&2
    exit 1
  fi
  sed -i "/canvasViewerHostSuffix/c\  window['env']['canvasViewerHostSuffix'] = '$suffix';" "$env_file"
}

configure_office_origin() {
  enabled="$(printf '%s' "${COLLABORA_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
  office_url="${COLLABORA_PUBLIC_URL:-}"
  case "$enabled" in
    1|true|yes|on) ;;
    *) return ;;
  esac
  if [ -z "$office_url" ] || [ "${#office_url}" -gt 300 ] ||
     ! printf '%s' "$office_url" | grep -Eq '^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$'; then
    echo "COLLABORA_PUBLIC_URL is not a valid exact HTTP(S) origin" >&2
    exit 1
  fi
  if ! grep -Fq "window['env']['canvasOfficeOrigin']" "$env_file"; then
    echo "Cockpit env.js has no Canvas Office setting" >&2
    exit 1
  fi
  sed -i "/canvasOfficeOrigin/c\  window['env']['canvasOfficeOrigin'] = '$office_url';" "$env_file"
}

configure_viewer_origin
configure_office_origin
