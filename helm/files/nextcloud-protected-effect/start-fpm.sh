#!/bin/sh

set -eu

die() {
    echo "[srw-protected-effect] $1" >&2
    exit 1
}

positive_int() {
    value="$1"
    case "$value" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$value" -gt 0 ] && [ "$value" -le 86400 ]
}

: "${NEXTCLOUD_PROTECTED_EFFECT_HMAC_KEY:?protected-effect HMAC key is required}"
: "${NEXTCLOUD_PROTECTED_EFFECT_CONFIG_SHA256:?protected-effect config digest is required}"
: "${NEXTCLOUD_PROTECTED_EFFECT_HANDLER_BOUND_SECONDS:?handler bound is required}"
: "${NEXTCLOUD_PROTECTED_EFFECT_MAX_CHILDREN:?max children is required}"

[ "${#NEXTCLOUD_PROTECTED_EFFECT_HMAC_KEY}" -ge 32 ] \
    || die "HMAC key must contain at least 32 bytes"
echo "$NEXTCLOUD_PROTECTED_EFFECT_CONFIG_SHA256" \
    | grep -Eq '^[0-9a-f]{64}$' \
    || die "config digest must be lowercase SHA-256"
positive_int "$NEXTCLOUD_PROTECTED_EFFECT_HANDLER_BOUND_SECONDS" \
    || die "handler bound must be a positive bounded integer"
positive_int "$NEXTCLOUD_PROTECTED_EFFECT_MAX_CHILDREN" \
    || die "max children must be a positive bounded integer"

# The ordinary Apache container initializes the shared volume.  This sidecar
# never runs Nextcloud's entrypoint and therefore cannot race an install or
# update; it merely waits until the immutable front controllers are present.
i=0
while [ ! -f /var/www/html/index.php ] || [ ! -f /var/www/html/ocs/v2.php ]; do
    i=$((i + 1))
    [ "$i" -le 300 ] || die "Nextcloud front controllers did not appear"
    sleep 1
done

mkdir -p /run/srw-nextcloud
cat > /run/srw-nextcloud/protected-effect-fpm.conf <<EOF
[global]
daemonize = no
error_log = /proc/self/fd/2

[srw-protected-effect]
user = www-data
group = www-data
listen = /run/srw-nextcloud/protected-effect.sock
listen.owner = www-data
listen.group = www-data
listen.mode = 0666
pm = ondemand
pm.max_children = ${NEXTCLOUD_PROTECTED_EFFECT_MAX_CHILDREN}
pm.process_idle_timeout = 10s
pm.max_requests = 100
clear_env = no
catch_workers_output = yes
decorate_workers_output = no
request_terminate_timeout = ${NEXTCLOUD_PROTECTED_EFFECT_HANDLER_BOUND_SECONDS}s
request_terminate_timeout_track_finished = yes
security.limit_extensions = .php
php_admin_value[auto_prepend_file] = /opt/srw-protected-effect/prepend.php
php_admin_value[max_execution_time] = 0
php_admin_value[log_errors] = On
php_admin_flag[display_errors] = Off
EOF

exec php-fpm -F -y /run/srw-nextcloud/protected-effect-fpm.conf
