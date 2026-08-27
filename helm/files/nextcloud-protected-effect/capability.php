<?php

declare(strict_types=1);

require_once __DIR__ . '/common.php';

const SRW_MAIN_CLOUD_INSTALLATION_DOMAIN = "srw-main-cloud-installation-v1\0";
const SRW_INSTALLATION_ATTESTATION_DOMAIN = "srw-nextcloud-installation-attestation-v1\0";

function srw_effect_installation_proof_sha256(): string
{
    $configPath = '/var/www/html/config/config.php';
    if (!is_file($configPath) || !is_readable($configPath)) {
        srw_effect_reject(503, 'installation_identity_unavailable');
    }

    // Nextcloud's config.php assigns the provider-owned value to $CONFIG.
    // Keep the raw identity inside this function: only its domain-separated
    // digest enters the signed attestation or leaves the protected FPM lane.
    $CONFIG = null;
    require $configPath;
    $remoteIdentity = is_array($CONFIG) ? ($CONFIG['instanceid'] ?? null) : null;
    if (
        !is_string($remoteIdentity)
        || $remoteIdentity === ''
        || $remoteIdentity !== trim($remoteIdentity)
        || str_contains($remoteIdentity, "\0")
        || preg_match('/[\x00-\x1f]/D', $remoteIdentity) === 1
    ) {
        srw_effect_reject(503, 'installation_identity_unavailable');
    }
    $binding = [
        'version' => 1,
        'backend_id' => 'nextcloud',
        'remote_identity' => $remoteIdentity,
    ];
    return hash(
        'sha256',
        SRW_MAIN_CLOUD_INSTALLATION_DOMAIN . srw_effect_canonical_json($binding),
    );
}

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
    $configSha256 = srw_effect_config_sha256();
    $serverTime = srw_effect_now_wire();
    $capability = [
        'version' => SRW_EFFECT_VERSION,
        'backend_instance_id' => $backendInstanceId,
        'config_sha256' => $configSha256,
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
        'server_time' => $serverTime,
    ];
    $canonical = srw_effect_canonical_json($capability);
    $signature = hash_hmac(
        'sha256',
        SRW_EFFECT_CAPABILITY_DOMAIN . $canonical,
        srw_effect_key(),
    );
    $installationAttestation = [
        'version' => 1,
        'backend_instance_id' => $backendInstanceId,
        'config_sha256' => $configSha256,
        'installation_proof_sha256' => srw_effect_installation_proof_sha256(),
        'capability_sha256' => hash('sha256', $canonical),
        'server_time' => $serverTime,
    ];
    $installationSignature = hash_hmac(
        'sha256',
        SRW_INSTALLATION_ATTESTATION_DOMAIN
            . srw_effect_canonical_json($installationAttestation),
        srw_effect_key(),
    );
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode(
        [
            'capability' => $capability,
            'signature' => $signature,
            'installation_attestation' => $installationAttestation,
            'installation_signature' => $installationSignature,
        ],
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
