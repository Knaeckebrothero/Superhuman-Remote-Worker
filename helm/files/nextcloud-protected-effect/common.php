<?php

declare(strict_types=1);

/*
 * Shared wire verifier for SRW's bounded Nextcloud effect lane.
 *
 * This file intentionally has no dependency on Nextcloud.  The capability
 * endpoint signs the same compact JSON representation as the Python client,
 * while the FPM auto-prepend hook authenticates and expires a request before
 * Nextcloud's front controller can perform a mutation.
 */

const SRW_EFFECT_CAPABILITY_DOMAIN = "srw-nextcloud-effect-capability-v1\0";
const SRW_EFFECT_REQUEST_DOMAIN = "srw-nextcloud-effect-request-v1\0";
const SRW_EFFECT_VERSION = 1;

final class SrwProtectedEffectReject extends RuntimeException
{
    public function __construct(
        public readonly int $httpStatus,
        public readonly string $errorCode,
    ) {
        parent::__construct($errorCode);
    }
}
/** @return never */
function srw_effect_reject(int $status, string $code): void
{
    throw new SrwProtectedEffectReject($status, $code);
}

function srw_effect_env(string $name): string
{
    $value = getenv($name);
    if (!is_string($value) || $value === '') {
        srw_effect_reject(503, 'lane_unconfigured');
    }
    return $value;
}

function srw_effect_positive_int_env(string $name): int
{
    $value = srw_effect_env($name);
    if (!preg_match('/^[1-9][0-9]{0,4}$/D', $value)) {
        srw_effect_reject(503, 'lane_unconfigured');
    }
    $parsed = (int) $value;
    if ($parsed <= 0 || $parsed > 86400) {
        srw_effect_reject(503, 'lane_unconfigured');
    }
    return $parsed;
}

function srw_effect_key(): string
{
    $key = srw_effect_env('NEXTCLOUD_PROTECTED_EFFECT_HMAC_KEY');
    if (strlen($key) < 32) {
        srw_effect_reject(503, 'lane_unconfigured');
    }
    return $key;
}

function srw_effect_config_sha256(): string
{
    $digest = srw_effect_env('NEXTCLOUD_PROTECTED_EFFECT_CONFIG_SHA256');
    if (!preg_match('/^[0-9a-f]{64}$/D', $digest)) {
        srw_effect_reject(503, 'lane_unconfigured');
    }
    return $digest;
}

function srw_effect_is_object_array(array $value): bool
{
    return !array_is_list($value);
}

/** @return mixed */
function srw_effect_canonical_value(mixed $value): mixed
{
    if (!is_array($value)) {
        return $value;
    }
    if (srw_effect_is_object_array($value)) {
        ksort($value, SORT_STRING);
    }
    foreach ($value as $key => $member) {
        $value[$key] = srw_effect_canonical_value($member);
    }
    return $value;
}

function srw_effect_canonical_json(array $value): string
{
    try {
        return json_encode(
            srw_effect_canonical_value($value),
            JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR,
        );
    } catch (JsonException) {
        srw_effect_reject(400, 'malformed_authority');
    }
}

function srw_effect_is_canonical_uuid(mixed $value): bool
{
    if (!is_string($value)) {
        return false;
    }
    if (!preg_match(
        '/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/D',
        $value,
    )) {
        return false;
    }
    return $value !== '00000000-0000-0000-0000-000000000000';
}

function srw_effect_is_sha256(mixed $value): bool
{
    return is_string($value) && preg_match('/^[0-9a-f]{64}$/D', $value) === 1;
}

function srw_effect_now_wire(): string
{
    $parts = explode(' ', microtime());
    if (count($parts) !== 2 || !preg_match('/^0\.[0-9]{8}$/D', $parts[0])) {
        srw_effect_reject(503, 'clock_unavailable');
    }
    $seconds = (int) $parts[1];
    $microseconds = substr($parts[0], 2, 6);
    return gmdate('Y-m-d\TH:i:s', $seconds) . '.' . $microseconds . 'Z';
}

function srw_effect_parse_wire_time(mixed $value): DateTimeImmutable
{
    if (!is_string($value) || !preg_match(
        '/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$/D',
        $value,
    )) {
        srw_effect_reject(400, 'malformed_deadline');
    }
    $parsed = DateTimeImmutable::createFromFormat(
        '!Y-m-d\TH:i:s.u\Z',
        $value,
        new DateTimeZone('UTC'),
    );
    $errors = DateTimeImmutable::getLastErrors();
    if (
        !$parsed
        || (is_array($errors) && ($errors['warning_count'] > 0 || $errors['error_count'] > 0))
        || $parsed->format('Y-m-d\TH:i:s.u\Z') !== $value
    ) {
        srw_effect_reject(400, 'malformed_deadline');
    }
    return $parsed;
}

/** @return array<string, string> */
function srw_effect_parse_form(string $body): array
{
    if ($body === '' || strlen($body) > 65536) {
        srw_effect_reject(400, 'malformed_body');
    }
    $result = [];
    foreach (explode('&', $body) as $part) {
        if ($part === '' || substr_count($part, '=') !== 1) {
            srw_effect_reject(400, 'malformed_body');
        }
        [$encodedKey, $encodedValue] = explode('=', $part, 2);
        $key = urldecode($encodedKey);
        $value = urldecode($encodedValue);
        if (
            $key === ''
            || str_contains($key, '[')
            || str_contains($key, ']')
            || array_key_exists($key, $result)
        ) {
            srw_effect_reject(400, 'malformed_body');
        }
        $result[$key] = $value;
    }
    ksort($result, SORT_STRING);
    return $result;
}

/** @param array<string, string> $form */
function srw_effect_require_form_keys(array $form, array $expected): void
{
    sort($expected, SORT_STRING);
    if (array_keys($form) !== $expected) {
        srw_effect_reject(403, 'effect_not_allowlisted');
    }
}

/**
 * Bind every authority-creating path and identifier to the engage attempt.
 * The normal Nextcloud service remains the cleanup/read lane; this service
 * accepts only the five POST effects used to construct one RO reader grant.
 *
 * @param array<string, mixed> $request
 */
function srw_effect_require_allowlisted_request(array $request, string $body): void
{
    if ($request['method'] !== 'POST') {
        srw_effect_reject(403, 'effect_not_allowlisted');
    }
    $attemptHex = str_replace('-', '', $request['engage_attempt']);
    $reader = 'srw-reader-a-' . $attemptHex;
    $group = 'srw-rog-a-' . $attemptHex;
    $path = $request['path'];
    $form = srw_effect_parse_form($body);

    if (preg_match('#^(?:/[A-Za-z0-9._~-]+)*/ocs/v2\.php/cloud/users$#D', $path)) {
        srw_effect_require_form_keys($form, ['password', 'userid']);
        if ($form['userid'] !== $reader || $form['password'] === '' || strlen($form['password']) > 512) {
            srw_effect_reject(403, 'effect_not_allowlisted');
        }
        return;
    }
    if (preg_match('#^(?:/[A-Za-z0-9._~-]+)*/ocs/v2\.php/cloud/groups$#D', $path)) {
        srw_effect_require_form_keys($form, ['groupid']);
        if ($form['groupid'] !== $group) {
            srw_effect_reject(403, 'effect_not_allowlisted');
        }
        return;
    }
    if (preg_match(
        '#^(?:/[A-Za-z0-9._~-]+)*/index\.php/apps/groupfolders/folders/[1-9][0-9]*/groups$#D',
        $path,
    )) {
        srw_effect_require_form_keys($form, ['group']);
        if ($form['group'] !== $group) {
            srw_effect_reject(403, 'effect_not_allowlisted');
        }
        return;
    }
    if (preg_match(
        '#^(?:/[A-Za-z0-9._~-]+)*/index\.php/apps/groupfolders/folders/[1-9][0-9]*/groups/' . preg_quote($group, '#') . '$#D',
        $path,
    )) {
        srw_effect_require_form_keys($form, ['permissions']);
        if ($form['permissions'] !== '1') {
            srw_effect_reject(403, 'effect_not_allowlisted');
        }
        return;
    }
    if (preg_match(
        '#^(?:/[A-Za-z0-9._~-]+)*/ocs/v2\.php/cloud/users/' . preg_quote($reader, '#') . '/groups$#D',
        $path,
    )) {
        srw_effect_require_form_keys($form, ['groupid']);
        if ($form['groupid'] !== $group) {
            srw_effect_reject(403, 'effect_not_allowlisted');
        }
        return;
    }
    srw_effect_reject(403, 'effect_not_allowlisted');
}

/**
 * @param array<string, mixed> $server
 * @return array<string, mixed>
 */
function srw_effect_verify_request(
    array $server,
    string $body,
    DateTimeImmutable $serverNow,
): array {
    $authority = $server['HTTP_X_SRW_PROTECTED_EFFECT_AUTHORITY'] ?? null;
    $signature = $server['HTTP_X_SRW_PROTECTED_EFFECT_SIGNATURE'] ?? null;
    $instanceHeader = $server['HTTP_X_SRW_BACKEND_INSTANCE'] ?? null;
    if (
        !is_string($authority)
        || $authority === ''
        || strlen($authority) > 4096
        || !is_string($signature)
        || preg_match('/^[0-9a-f]{64}$/D', $signature) !== 1
        || !is_string($instanceHeader)
        || !srw_effect_is_canonical_uuid($instanceHeader)
    ) {
        srw_effect_reject(401, 'missing_authority');
    }
    try {
        $request = json_decode($authority, true, 16, JSON_THROW_ON_ERROR);
    } catch (JsonException) {
        srw_effect_reject(400, 'malformed_authority');
    }
    if (!is_array($request) || array_is_list($request)) {
        srw_effect_reject(400, 'malformed_authority');
    }
    $expectedKeys = [
        'backend_instance_id',
        'body_sha256',
        'config_sha256',
        'effect_not_after',
        'engage_attempt',
        'method',
        'path',
        'version',
    ];
    $keys = array_keys($request);
    sort($keys, SORT_STRING);
    if ($keys !== $expectedKeys || srw_effect_canonical_json($request) !== $authority) {
        srw_effect_reject(400, 'malformed_authority');
    }
    if (
        $request['version'] !== SRW_EFFECT_VERSION
        || !srw_effect_is_canonical_uuid($request['backend_instance_id'])
        || $request['backend_instance_id'] !== $instanceHeader
        || !srw_effect_is_canonical_uuid($request['engage_attempt'])
        || !srw_effect_is_sha256($request['body_sha256'])
        || !srw_effect_is_sha256($request['config_sha256'])
        || $request['config_sha256'] !== srw_effect_config_sha256()
        || !is_string($request['method'])
        || !in_array($request['method'], ['POST', 'PUT'], true)
        || !is_string($request['path'])
        || $request['path'] === ''
        || strlen($request['path']) > 2048
    ) {
        srw_effect_reject(403, 'authority_mismatch');
    }
    $actualMethod = $server['REQUEST_METHOD'] ?? null;
    $requestUri = $server['REQUEST_URI'] ?? null;
    $query = $server['QUERY_STRING'] ?? '';
    if (
        $actualMethod !== $request['method']
        || !is_string($requestUri)
        || $requestUri !== $request['path']
        || !is_string($query)
        || $query !== ''
        || str_contains($requestUri, '?')
        || str_contains($requestUri, '#')
    ) {
        srw_effect_reject(403, 'request_target_mismatch');
    }
    if (!hash_equals($request['body_sha256'], hash('sha256', $body))) {
        srw_effect_reject(403, 'body_mismatch');
    }
    $expectedSignature = hash_hmac(
        'sha256',
        SRW_EFFECT_REQUEST_DOMAIN . $authority,
        srw_effect_key(),
    );
    if (!hash_equals($expectedSignature, $signature)) {
        srw_effect_reject(401, 'invalid_signature');
    }

    $deadline = srw_effect_parse_wire_time($request['effect_not_after']);
    $serverNow = $serverNow->setTimezone(new DateTimeZone('UTC'));
    if ($serverNow > $deadline) {
        srw_effect_reject(409, 'effect_expired');
    }
    $maximumFuture = $serverNow->modify(
        '+' . (
            srw_effect_positive_int_env('NEXTCLOUD_PROTECTED_EFFECT_QUEUE_BOUND_SECONDS')
            + srw_effect_positive_int_env('NEXTCLOUD_PROTECTED_EFFECT_CLOCK_SKEW_BOUND_SECONDS')
        ) . ' seconds',
    );
    if ($deadline > $maximumFuture) {
        srw_effect_reject(403, 'deadline_out_of_bounds');
    }

    srw_effect_require_allowlisted_request($request, $body);
    return $request;
}

function srw_effect_write_error(SrwProtectedEffectReject $error): void
{
    http_response_code($error->httpStatus);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode(
        ['error' => $error->errorCode],
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE,
    );
}
