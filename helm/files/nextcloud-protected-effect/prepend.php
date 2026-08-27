<?php

declare(strict_types=1);

require_once __DIR__ . '/common.php';

// The capability endpoint runs in the same hard-bounded FPM pool but creates
// no Nextcloud authority. Nginx supplies this non-HTTP FastCGI marker only in
// its exact GET-only capability location; request headers cannot forge it.
if (($_SERVER['SRW_PROTECTED_EFFECT_CAPABILITY'] ?? '') === '1') {
    return;
}
try {
    $body = file_get_contents('php://input');
    if (!is_string($body)) {
        srw_effect_reject(400, 'malformed_body');
    }
    srw_effect_verify_request(
        $_SERVER,
        $body,
        new DateTimeImmutable('now', new DateTimeZone('UTC')),
    );
} catch (SrwProtectedEffectReject $error) {
    srw_effect_write_error($error);
    exit;
} catch (Throwable) {
    http_response_code(500);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo '{"error":"verification_failed"}';
    exit;
}
