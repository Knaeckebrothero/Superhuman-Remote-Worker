<?php

declare(strict_types=1);

require_once __DIR__ . '/common.php';

try {
    if (
        ($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET'
        || (($_SERVER['QUERY_STRING'] ?? '') !== '')
    ) {
        srw_effect_reject(405, 'invalid_capability_request');
    }
    $backendInstanceId = $_SERVER['HTTP_X_SRW_BACKEND_INSTANCE'] ?? null;
    if (!srw_effect_is_canonical_uuid($backendInstanceId)) {
        srw_effect_reject(400, 'invalid_backend_instance');
    }
    $capability = [
        'version' => SRW_EFFECT_VERSION,
        'backend_instance_id' => $backendInstanceId,
        'config_sha256' => srw_effect_config_sha256(),
        'queue_bound_seconds' => srw_effect_positive_int_env(
            'NEXTCLOUD_PROTECTED_EFFECT_QUEUE_BOUND_SECONDS',
        ),
        'handler_bound_seconds' => srw_effect_positive_int_env(
            'NEXTCLOUD_PROTECTED_EFFECT_HANDLER_BOUND_SECONDS',
        ),
        'clock_skew_bound_seconds' => srw_effect_positive_int_env(
            'NEXTCLOUD_PROTECTED_EFFECT_CLOCK_SKEW_BOUND_SECONDS',
        ),
        'safety_margin_seconds' => srw_effect_positive_int_env(
            'NEXTCLOUD_PROTECTED_EFFECT_SAFETY_MARGIN_SECONDS',
        ),
        'capability_max_age_seconds' => srw_effect_positive_int_env(
            'NEXTCLOUD_PROTECTED_EFFECT_CAPABILITY_MAX_AGE_SECONDS',
        ),
        'server_time' => srw_effect_now_wire(),
    ];
    $canonical = srw_effect_canonical_json($capability);
    $signature = hash_hmac(
        'sha256',
        SRW_EFFECT_CAPABILITY_DOMAIN . $canonical,
        srw_effect_key(),
    );
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode(
        ['capability' => $capability, 'signature' => $signature],
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
    );
} catch (SrwProtectedEffectReject $error) {
    srw_effect_write_error($error);
} catch (Throwable) {
    http_response_code(500);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo '{"error":"capability_failed"}';
}
