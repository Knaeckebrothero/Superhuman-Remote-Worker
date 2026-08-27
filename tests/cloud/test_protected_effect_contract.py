from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from orchestrator.services.cloud.protected_effect_contract import (
    MAX_PROTECTED_EFFECT_TIMING_SECONDS,
    PROTECTED_EFFECT_CAPABILITY_HMAC_DOMAIN,
    PROTECTED_EFFECT_REQUEST_HMAC_DOMAIN,
    NextcloudEffectCapability,
    NextcloudEffectFenceIntent,
    NextcloudEffectHorizon,
    NextcloudEffectRequestAuthority,
    ValidatedNextcloudEffectCapability,
    adopt_protected_effect_capability,
    calculate_protected_effect_safe_after,
    normalize_protected_effect_path,
    sign_protected_effect_capability,
    sign_protected_effect_request,
    verify_protected_effect_capability_signature,
    verify_protected_effect_request_signature,
)


BACKEND_INSTANCE_ID = "99999999-9999-4999-8999-aaaaaaaaaaaa"
OTHER_BACKEND_INSTANCE_ID = "88888888-8888-4888-8888-bbbbbbbbbbbb"
ENGAGE_ATTEMPT = "11111111-1111-4111-8111-aaaaaaaaaaaa"
OTHER_ENGAGE_ATTEMPT = "22222222-2222-4222-8222-bbbbbbbbbbbb"
CONFIG_SHA256 = "a" * 64
BODY_SHA256 = "b" * 64
HMAC_KEY = bytes(range(32))
SERVER_TIME = datetime(2026, 8, 26, 12, 34, 56, 123456, tzinfo=timezone.utc)


def _capability(**overrides: Any) -> NextcloudEffectCapability:
    values: dict[str, Any] = {
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "config_sha256": CONFIG_SHA256,
        "queue_bound_seconds": 30,
        "handler_bound_seconds": 10,
        "clock_skew_bound_seconds": 2,
        "safety_margin_seconds": 3,
        "capability_max_age_seconds": 5,
        "server_time": SERVER_TIME,
    }
    values.update(overrides)
    return NextcloudEffectCapability(**values)


def _request(**overrides: Any) -> NextcloudEffectRequestAuthority:
    values: dict[str, Any] = {
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "config_sha256": CONFIG_SHA256,
        "engage_attempt": ENGAGE_ATTEMPT,
        "method": "POST",
        "path": "/ocs/v2.php/apps/srw/protected-reader",
        "body_sha256": BODY_SHA256,
        "effect_not_after": SERVER_TIME + timedelta(seconds=20),
    }
    values.update(overrides)
    return NextcloudEffectRequestAuthority(**values)


def _validated_capability(
    capability: NextcloudEffectCapability | None = None,
    *,
    db_before: datetime = SERVER_TIME - timedelta(seconds=1),
    db_after: datetime = SERVER_TIME + timedelta(seconds=1),
    signature: str | None = None,
    expected_backend_instance_id: str = BACKEND_INSTANCE_ID,
    expected_config_sha256: str = CONFIG_SHA256,
) -> ValidatedNextcloudEffectCapability:
    wire_capability = capability or _capability()
    validated = adopt_protected_effect_capability(
        wire_capability.binding,
        signature=signature
        if signature is not None
        else sign_protected_effect_capability(wire_capability, key=HMAC_KEY),
        key=HMAC_KEY,
        db_before=db_before,
        db_after=db_after,
        expected_backend_instance_id=expected_backend_instance_id,
        expected_config_sha256=expected_config_sha256,
    )
    assert validated is not None
    return validated


def _adopt_capability(
    capability: NextcloudEffectCapability,
    *,
    db_before: datetime,
    db_after: datetime,
    signature: str | None = None,
    expected_backend_instance_id: str = BACKEND_INSTANCE_ID,
    expected_config_sha256: str = CONFIG_SHA256,
) -> ValidatedNextcloudEffectCapability | None:
    return adopt_protected_effect_capability(
        capability.binding,
        signature=signature
        if signature is not None
        else sign_protected_effect_capability(capability, key=HMAC_KEY),
        key=HMAC_KEY,
        db_before=db_before,
        db_after=db_after,
        expected_backend_instance_id=expected_backend_instance_id,
        expected_config_sha256=expected_config_sha256,
    )


def _intent(
    request: NextcloudEffectRequestAuthority | None = None,
    *,
    capability: ValidatedNextcloudEffectCapability | None = None,
    request_signature: str | None = None,
    db_dispatched_at: datetime = SERVER_TIME + timedelta(seconds=1),
) -> NextcloudEffectFenceIntent:
    authority = request or _request()
    return NextcloudEffectFenceIntent.capture(
        capability=capability or _validated_capability(),
        request=authority,
        request_signature=request_signature
        if request_signature is not None
        else sign_protected_effect_request(authority, key=HMAC_KEY),
        key=HMAC_KEY,
        db_dispatched_at=db_dispatched_at,
    )


def _horizon(
    *,
    intent: NextcloudEffectFenceIntent | None = None,
    db_dispatch_closed_at: datetime = SERVER_TIME + timedelta(seconds=5),
) -> NextcloudEffectHorizon:
    return NextcloudEffectHorizon.capture(
        intent=intent or _intent(),
        db_dispatch_closed_at=db_dispatch_closed_at,
    )


def _parse_horizon(
    binding: dict[str, Any] | None,
    *,
    expected_engage_attempt: str = ENGAGE_ATTEMPT,
    expected_request_authority_sha256: str | None = None,
) -> NextcloudEffectHorizon | None:
    return NextcloudEffectHorizon.from_binding(
        binding,
        key=HMAC_KEY,
        expected_backend_instance_id=BACKEND_INSTANCE_ID,
        expected_config_sha256=CONFIG_SHA256,
        expected_engage_attempt=expected_engage_attempt,
        expected_request_authority_sha256=expected_request_authority_sha256
        or _request().authority_sha256,
    )


def test_capability_has_exact_canonical_v1_wire_shape() -> None:
    capability = _capability()

    assert capability.canonical_json == (
        '{"backend_instance_id":"99999999-9999-4999-8999-aaaaaaaaaaaa",'
        '"capability_max_age_seconds":5,"clock_skew_bound_seconds":2,'
        '"config_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"handler_bound_seconds":10,"queue_bound_seconds":30,'
        '"safety_margin_seconds":3,'
        '"server_time":"2026-08-26T12:34:56.123456Z","version":1}'
    )
    assert NextcloudEffectCapability.from_binding(capability.binding) == capability


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("version", True),
        ("version", 1.0),
        ("version", 0),
        ("backend_instance_id", BACKEND_INSTANCE_ID.upper()),
        ("backend_instance_id", BACKEND_INSTANCE_ID.replace("-", "")),
        ("backend_instance_id", "00000000-0000-0000-0000-000000000000"),
        ("config_sha256", CONFIG_SHA256.upper()),
        ("config_sha256", "a" * 63),
        ("queue_bound_seconds", True),
        ("queue_bound_seconds", 1.0),
        ("handler_bound_seconds", 0),
        ("clock_skew_bound_seconds", -1),
        (
            "safety_margin_seconds",
            MAX_PROTECTED_EFFECT_TIMING_SECONDS + 1,
        ),
        ("capability_max_age_seconds", False),
        ("server_time", "2026-08-26T12:34:56.123456+00:00"),
        ("server_time", "2026-08-26T12:34:56Z"),
    ],
)
def test_capability_binding_rejects_normalizable_or_unbounded_mutations(
    field: str,
    replacement: object,
) -> None:
    binding = _capability().binding
    binding[field] = replacement

    assert NextcloudEffectCapability.from_binding(binding) is None


def test_capability_rejects_naive_or_non_utc_server_time() -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        _capability(server_time=SERVER_TIME.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UTC-aware"):
        _capability(server_time=SERVER_TIME.astimezone(timezone(timedelta(hours=1))))


def test_effect_timestamps_reject_non_round_trippable_short_years() -> None:
    short_year = SERVER_TIME.replace(year=1)

    with pytest.raises(ValueError, match="four-digit UTC year"):
        _capability(server_time=short_year)
    with pytest.raises(ValueError, match="four-digit UTC year"):
        _request(effect_not_after=short_year)
    with pytest.raises(ValueError, match="four-digit UTC year"):
        calculate_protected_effect_safe_after(
            dispatch_closed_at=short_year,
            max_effect_not_after=SERVER_TIME,
            handler_bound_seconds=1,
            clock_skew_bound_seconds=1,
            safety_margin_seconds=1,
        )


def test_capability_binding_rejects_null_legacy_or_extended_shape() -> None:
    assert NextcloudEffectCapability.from_binding(None) is None
    assert NextcloudEffectCapability.from_binding({}) is None
    assert (
        NextcloudEffectCapability.from_binding(
            {**_capability().binding, "endpoint_url": "https://secret.invalid"}
        )
        is None
    )


def test_capability_validates_a_fresh_postgres_clock_window() -> None:
    capability = _capability()

    validated = _adopt_capability(
        capability,
        db_before=SERVER_TIME - timedelta(seconds=1),
        db_after=SERVER_TIME + timedelta(seconds=1),
    )
    assert validated is not None
    assert validated.capability == capability
    assert validated.db_after == SERVER_TIME + timedelta(seconds=1)
    assert validated.fresh_until == SERVER_TIME + timedelta(seconds=3)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            SERVER_TIME - timedelta(seconds=6),
            SERVER_TIME,
        ),
        (
            SERVER_TIME + timedelta(seconds=1),
            SERVER_TIME,
        ),
    ],
)
def test_capability_rejects_stale_or_backwards_database_windows(
    before: datetime,
    after: datetime,
) -> None:
    assert (
        _adopt_capability(
            _capability(),
            db_before=before,
            db_after=after,
        )
        is None
    )


@pytest.mark.parametrize(
    "server_time",
    [
        SERVER_TIME - timedelta(seconds=4),
        SERVER_TIME + timedelta(seconds=4),
    ],
)
def test_capability_rejects_server_time_outside_db_window_and_skew(
    server_time: datetime,
) -> None:
    capability = _capability(server_time=server_time)

    assert (
        _adopt_capability(
            capability,
            db_before=SERVER_TIME,
            db_after=SERVER_TIME + timedelta(seconds=1),
        )
        is None
    )


@pytest.mark.parametrize(
    ("expected_instance", "expected_fingerprint"),
    [
        (OTHER_BACKEND_INSTANCE_ID, CONFIG_SHA256),
        (BACKEND_INSTANCE_ID, "c" * 64),
    ],
)
def test_capability_rejects_a_different_pinned_identity(
    expected_instance: str,
    expected_fingerprint: str,
) -> None:
    assert (
        _adopt_capability(
            _capability(),
            db_before=SERVER_TIME,
            db_after=SERVER_TIME + timedelta(seconds=1),
            expected_backend_instance_id=expected_instance,
            expected_config_sha256=expected_fingerprint,
        )
        is None
    )


def test_capability_rejects_non_utc_database_timestamps() -> None:
    for replacement in (
        SERVER_TIME.replace(tzinfo=None),
        SERVER_TIME.astimezone(timezone(timedelta(hours=-3))),
    ):
        assert (
            _adopt_capability(
                _capability(),
                db_before=replacement,
                db_after=SERVER_TIME + timedelta(seconds=1),
            )
            is None
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "/ocs//v2.php/./apps/srw/protected-reader/",
            "/ocs/v2.php/apps/srw/protected-reader",
        ),
        ("/ocs/v2.php/apps/srw", "/ocs/v2.php/apps/srw"),
    ],
)
def test_effect_path_has_one_explicit_normalization_rule(
    value: str,
    expected: str,
) -> None:
    assert normalize_protected_effect_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "ocs/v2.php/apps/srw",
        "https://cloud.invalid/ocs/v2.php",
        "//cloud.invalid/ocs/v2.php",
        "/ocs/../admin",
        "/ocs/%2e%2e/admin",
        "/ocs/v2.php?admin=true",
        "/ocs/v2.php#fragment",
        "/ocs\\v2.php",
        "/ocs/v2.php/\N{SNOWMAN}",
        "/",
    ],
)
def test_effect_path_rejects_urls_queries_and_ambiguous_paths(value: str) -> None:
    with pytest.raises(ValueError, match="path"):
        normalize_protected_effect_path(value)


def test_request_has_exact_canonical_loggable_shape() -> None:
    request = _request(path="/ocs//v2.php/./apps/srw/protected-reader/")

    assert request.path == "/ocs/v2.php/apps/srw/protected-reader"
    assert request.canonical_json == (
        '{"backend_instance_id":"99999999-9999-4999-8999-aaaaaaaaaaaa",'
        '"body_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        '"config_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"effect_not_after":"2026-08-26T12:35:16.123456Z",'
        '"engage_attempt":"11111111-1111-4111-8111-aaaaaaaaaaaa",'
        '"method":"POST","path":"/ocs/v2.php/apps/srw/protected-reader",'
        '"version":1}'
    )
    for excluded in (
        "https://",
        "?",
        "credential",
        "password",
        "request_body",
        "super-secret-body",
    ):
        assert excluded not in request.canonical_json


def test_request_persisted_and_wire_parsers_require_canonical_bytes() -> None:
    request = _request()

    assert NextcloudEffectRequestAuthority.from_binding(request.binding) == request
    assert (
        NextcloudEffectRequestAuthority.from_canonical_json(request.canonical_json)
        == request
    )
    assert (
        NextcloudEffectRequestAuthority.from_binding(
            {**request.binding, "path": "/ocs//v2.php/apps/srw/protected-reader"}
        )
        is None
    )
    assert (
        NextcloudEffectRequestAuthority.from_canonical_json(
            json.dumps(request.binding, indent=2)
        )
        is None
    )
    reordered = json.dumps(request.binding, separators=(",", ":"))
    assert reordered != request.canonical_json
    assert NextcloudEffectRequestAuthority.from_canonical_json(reordered) is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("version", True),
        ("backend_instance_id", BACKEND_INSTANCE_ID.upper()),
        ("config_sha256", CONFIG_SHA256.upper()),
        ("engage_attempt", ENGAGE_ATTEMPT.replace("-", "")),
        ("method", "post"),
        ("method", "DELETE"),
        ("body_sha256", BODY_SHA256.upper()),
        ("body_sha256", "b" * 63),
        ("effect_not_after", "2026-08-26T12:35:16.123456+00:00"),
    ],
)
def test_request_binding_rejects_every_noncanonical_authority_coordinate(
    field: str,
    replacement: object,
) -> None:
    binding = _request().binding
    binding[field] = replacement

    assert NextcloudEffectRequestAuthority.from_binding(binding) is None


def test_request_rejects_naive_deadline_and_extra_or_legacy_fields() -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        _request(effect_not_after=SERVER_TIME.replace(tzinfo=None))
    assert NextcloudEffectRequestAuthority.from_binding(None) is None
    assert (
        NextcloudEffectRequestAuthority.from_binding(
            {**_request().binding, "url": "https://cloud.invalid/ocs"}
        )
        is None
    )


def test_request_dispatch_validation_binds_instance_and_queue_deadline() -> None:
    capability = _validated_capability(
        db_before=SERVER_TIME - timedelta(seconds=1),
        db_after=SERVER_TIME,
    )
    request = _request(effect_not_after=SERVER_TIME + timedelta(seconds=30))

    assert (
        request.validate_dispatch(capability, db_dispatched_at=SERVER_TIME) is request
    )
    with pytest.raises(ValueError, match="capability identity does not match"):
        replace(
            request, backend_instance_id=OTHER_BACKEND_INSTANCE_ID
        ).validate_dispatch(
            capability,
            db_dispatched_at=SERVER_TIME,
        )
    with pytest.raises(ValueError, match="capability identity does not match"):
        replace(request, config_sha256="c" * 64).validate_dispatch(
            capability,
            db_dispatched_at=SERVER_TIME,
        )
    with pytest.raises(ValueError, match="runs backwards"):
        replace(request, effect_not_after=SERVER_TIME).validate_dispatch(
            capability,
            db_dispatched_at=SERVER_TIME,
        )
    with pytest.raises(ValueError, match="exceeds queue bound"):
        replace(
            request,
            effect_not_after=SERVER_TIME + timedelta(seconds=31),
        ).validate_dispatch(capability, db_dispatched_at=SERVER_TIME)


def test_request_refuses_bare_expired_or_clone_forged_capability() -> None:
    request = _request()
    parsed_but_unvalidated = NextcloudEffectCapability.from_binding(
        _capability().binding
    )
    assert parsed_but_unvalidated is not None
    with pytest.raises(ValueError, match="was not validated"):
        request.validate_dispatch(  # type: ignore[arg-type]
            parsed_but_unvalidated,
            db_dispatched_at=SERVER_TIME,
        )

    validated = _validated_capability()
    with pytest.raises(ValueError, match="has expired"):
        request.validate_dispatch(
            validated,
            db_dispatched_at=SERVER_TIME + timedelta(seconds=4),
        )
    with pytest.raises(TypeError):
        replace(validated, fresh_until=SERVER_TIME + timedelta(days=1))
    with pytest.raises(AttributeError, match="immutable"):
        validated._fresh_until = SERVER_TIME + timedelta(days=1)  # type: ignore[misc]
    with pytest.raises(ValueError, match="was not validated"):
        ValidatedNextcloudEffectCapability(
            capability=_capability(),
            signature="0" * 64,
            db_before=SERVER_TIME,
            db_after=SERVER_TIME,
            fresh_until=SERVER_TIME + timedelta(days=1),
            _validation_marker=object(),
        )


def test_hmac_has_a_fixed_known_vector_and_retains_no_key_field() -> None:
    request = _request()
    signature = sign_protected_effect_request(request, key=HMAC_KEY)

    assert (
        signature == "61193a6619bbb8e69c497e2c9d995c1d1f1dd9c6bcdbcd56a446aec54f056b4e"
    )
    assert verify_protected_effect_request_signature(
        request,
        signature=signature,
        key=HMAC_KEY,
    )
    assert not hasattr(request, "key")
    assert HMAC_KEY.hex() not in request.canonical_json


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: replace(
            request,
            backend_instance_id=OTHER_BACKEND_INSTANCE_ID,
        ),
        lambda request: replace(request, config_sha256="c" * 64),
        lambda request: replace(request, engage_attempt=OTHER_ENGAGE_ATTEMPT),
        lambda request: replace(request, method="PUT"),
        lambda request: replace(request, path="/ocs/v2.php/apps/srw/other"),
        lambda request: replace(request, body_sha256="c" * 64),
        lambda request: replace(
            request,
            effect_not_after=request.effect_not_after + timedelta(microseconds=1),
        ),
    ],
)
def test_hmac_rejects_each_mutated_authority_coordinate(
    mutate: Callable[
        [NextcloudEffectRequestAuthority],
        NextcloudEffectRequestAuthority,
    ],
) -> None:
    request = _request()
    signature = sign_protected_effect_request(request, key=HMAC_KEY)

    assert not verify_protected_effect_request_signature(
        mutate(request),
        signature=signature,
        key=HMAC_KEY,
    )


@pytest.mark.parametrize(
    "signature",
    [
        "0" * 64,
        "EE9A7376AE52088A462B01DA296CFDF5F69AD602165852537653296AC835C55C",
        "e" * 63,
        "not-a-digest",
        "",
    ],
)
def test_hmac_rejects_wrong_or_noncanonical_signatures(signature: str) -> None:
    assert not verify_protected_effect_request_signature(
        _request(),
        signature=signature,
        key=HMAC_KEY,
    )


def test_capability_hmac_has_a_fixed_vector_and_retains_no_key_field() -> None:
    capability = _capability()
    signature = sign_protected_effect_capability(capability, key=HMAC_KEY)

    assert (
        signature == "86f00a364c0e60faafa85570d33afe4d6b5af0cd085a7bf286f4d9520d99f8a9"
    )
    assert verify_protected_effect_capability_signature(
        capability,
        signature=signature,
        key=HMAC_KEY,
    )
    assert not hasattr(capability, "key")
    assert HMAC_KEY.hex() not in capability.canonical_json


@pytest.mark.parametrize(
    "mutate",
    [
        lambda capability: replace(
            capability,
            backend_instance_id=OTHER_BACKEND_INSTANCE_ID,
        ),
        lambda capability: replace(capability, config_sha256="c" * 64),
        lambda capability: replace(capability, queue_bound_seconds=31),
        lambda capability: replace(capability, handler_bound_seconds=11),
        lambda capability: replace(capability, clock_skew_bound_seconds=3),
        lambda capability: replace(capability, safety_margin_seconds=4),
        lambda capability: replace(capability, capability_max_age_seconds=6),
        lambda capability: replace(
            capability,
            server_time=capability.server_time + timedelta(microseconds=1),
        ),
    ],
)
def test_capability_hmac_rejects_every_mutated_attestation_coordinate(
    mutate: Callable[[NextcloudEffectCapability], NextcloudEffectCapability],
) -> None:
    capability = _capability()
    signature = sign_protected_effect_capability(capability, key=HMAC_KEY)

    assert not verify_protected_effect_capability_signature(
        mutate(capability),
        signature=signature,
        key=HMAC_KEY,
    )


def test_hmac_domains_prevent_capability_request_signature_substitution() -> None:
    capability = _capability()
    request = _request()
    capability_signature = sign_protected_effect_capability(
        capability,
        key=HMAC_KEY,
    )
    request_signature = sign_protected_effect_request(request, key=HMAC_KEY)

    assert PROTECTED_EFFECT_CAPABILITY_HMAC_DOMAIN.endswith(b"\0")
    assert PROTECTED_EFFECT_REQUEST_HMAC_DOMAIN.endswith(b"\0")
    assert (
        PROTECTED_EFFECT_CAPABILITY_HMAC_DOMAIN != PROTECTED_EFFECT_REQUEST_HMAC_DOMAIN
    )
    assert capability_signature != request_signature
    assert not verify_protected_effect_capability_signature(
        capability,
        signature=request_signature,
        key=HMAC_KEY,
    )
    assert not verify_protected_effect_request_signature(
        request,
        signature=capability_signature,
        key=HMAC_KEY,
    )


@pytest.mark.parametrize("key", [b"short", bytearray(range(32)), "x" * 32])
def test_hmac_rejects_weak_or_non_bytes_keys(key: object) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        sign_protected_effect_request(_request(), key=key)  # type: ignore[arg-type]


def test_pre_effect_intent_and_horizon_bind_one_authenticated_request() -> None:
    request = _request()
    intent = _intent(request)
    dispatch_closed_at = SERVER_TIME + timedelta(seconds=5)
    horizon = NextcloudEffectHorizon.capture(
        intent=intent,
        db_dispatch_closed_at=dispatch_closed_at,
    )

    assert horizon.backend_instance_id == BACKEND_INSTANCE_ID
    assert horizon.config_sha256 == CONFIG_SHA256
    assert horizon.engage_attempt == ENGAGE_ATTEMPT
    assert horizon.request_authority_sha256 == request.authority_sha256
    assert horizon.max_effect_not_after == request.effect_not_after
    assert horizon.safe_after == request.effect_not_after + timedelta(seconds=15)
    assert horizon.handler_bound_seconds == 10
    assert horizon.clock_skew_bound_seconds == 2
    assert horizon.safety_margin_seconds == 3
    assert horizon.binding["intent_sha256"] == intent.sha256
    parsed = _parse_horizon(horizon.binding)
    assert parsed is not None
    assert parsed.binding == horizon.binding
    assert calculate_protected_effect_safe_after(
        dispatch_closed_at=dispatch_closed_at,
        max_effect_not_after=request.effect_not_after,
        handler_bound_seconds=10,
        clock_skew_bound_seconds=2,
        safety_margin_seconds=3,
    ) == request.effect_not_after + timedelta(seconds=15)


def test_lower_current_bounds_cannot_shorten_a_captured_horizon() -> None:
    captured = _horizon()
    lower_capability = _capability(
        config_sha256="d" * 64,
        handler_bound_seconds=1,
        clock_skew_bound_seconds=1,
        safety_margin_seconds=1,
    )
    lower_validated = _validated_capability(
        lower_capability,
        expected_config_sha256="d" * 64,
    )
    lower_request = _request(config_sha256="d" * 64)
    lower_intent = _intent(lower_request, capability=lower_validated)

    assert lower_intent.capability.handler_bound_seconds == 1
    assert captured.safe_after == SERVER_TIME + timedelta(seconds=35)
    reparsed = _parse_horizon(captured.binding)
    assert reparsed is not None
    assert reparsed.safe_after == SERVER_TIME + timedelta(seconds=35)


def test_intent_rejects_bare_capability_and_forged_request_coordinates() -> None:
    request = _request()
    signature = sign_protected_effect_request(request, key=HMAC_KEY)
    with pytest.raises(ValueError, match="was not validated"):
        NextcloudEffectFenceIntent.capture(
            capability=_capability(),  # type: ignore[arg-type]
            request=request,
            request_signature=signature,
            key=HMAC_KEY,
            db_dispatched_at=SERVER_TIME + timedelta(seconds=1),
        )

    validated = _validated_capability()
    for forged in (
        replace(request, backend_instance_id=OTHER_BACKEND_INSTANCE_ID),
        replace(request, config_sha256="c" * 64),
        replace(
            request,
            effect_not_after=request.effect_not_after + timedelta(seconds=1),
        ),
        replace(request, body_sha256="c" * 64),
    ):
        with pytest.raises(ValueError):
            NextcloudEffectFenceIntent.capture(
                capability=validated,
                request=forged,
                request_signature=signature,
                key=HMAC_KEY,
                db_dispatched_at=SERVER_TIME + timedelta(seconds=1),
            )


def test_horizon_refuses_cross_attempt_transplant_and_backwards_close() -> None:
    horizon = _horizon()

    assert (
        _parse_horizon(
            horizon.binding,
            expected_engage_attempt=OTHER_ENGAGE_ATTEMPT,
        )
        is None
    )
    with pytest.raises(ValueError, match="closure runs backwards"):
        NextcloudEffectHorizon.capture(
            intent=horizon.intent,
            db_dispatch_closed_at=SERVER_TIME,
        )
    with pytest.raises(ValueError, match="intent was not validated"):
        NextcloudEffectHorizon.capture(
            intent=_request(),  # type: ignore[arg-type]
            db_dispatch_closed_at=SERVER_TIME + timedelta(seconds=5),
        )


def test_resigned_substitute_cannot_change_an_already_captured_intent() -> None:
    original = _request()
    intent = _intent(original)
    substitute = replace(
        original,
        engage_attempt=OTHER_ENGAGE_ATTEMPT,
        body_sha256="c" * 64,
        effect_not_after=original.effect_not_after + timedelta(seconds=1),
    )
    substitute_signature = sign_protected_effect_request(substitute, key=HMAC_KEY)

    assert verify_protected_effect_request_signature(
        substitute,
        signature=substitute_signature,
        key=HMAC_KEY,
    )
    assert intent.request == original
    assert intent.request.authority_sha256 != substitute.authority_sha256
    horizon = NextcloudEffectHorizon.capture(
        intent=intent,
        db_dispatch_closed_at=SERVER_TIME + timedelta(seconds=5),
    )
    assert horizon.engage_attempt == ENGAGE_ATTEMPT
    assert horizon.request_authority_sha256 == original.authority_sha256


def test_each_mutating_request_gets_its_own_proven_horizon_for_durable_max() -> None:
    first = _horizon()
    second_request = _request(
        method="PUT",
        path="/ocs/v2.php/apps/srw/protected-reader/group",
        body_sha256="c" * 64,
        effect_not_after=SERVER_TIME + timedelta(seconds=25),
    )
    second = _horizon(
        intent=_intent(second_request),
        db_dispatch_closed_at=SERVER_TIME + timedelta(seconds=6),
    )

    assert first.request_authority_sha256 != second.request_authority_sha256
    assert max(first.safe_after, second.safe_after) == second.safe_after
    assert second.safe_after == SERVER_TIME + timedelta(seconds=40)
    assert (
        _parse_horizon(
            second.binding,
            expected_request_authority_sha256=first.request_authority_sha256,
        )
        is None
    )
    assert (
        _parse_horizon(
            second.binding,
            expected_request_authority_sha256=second.request_authority_sha256,
        )
        is not None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda binding: binding.update(version=0),
        lambda binding: binding.update(version=True),
        lambda binding: binding.update(version=1.0),
        lambda binding: binding.update(intent_sha256="0" * 64),
        lambda binding: binding.update(
            dispatch_closed_at="2026-08-26T12:35:01.123456+00:00"
        ),
        lambda binding: binding.update(safe_after="2026-08-26T12:35:30.123456Z"),
        lambda binding: binding.update(client_timeout_seconds=1),
        lambda binding: binding["intent"].update(version=True),
        lambda binding: binding["intent"].update(version=1.0),
        lambda binding: binding["intent"]["capability"].update(handler_bound_seconds=1),
        lambda binding: binding["intent"]["request"].update(
            engage_attempt=OTHER_ENGAGE_ATTEMPT
        ),
        lambda binding: binding["intent"]["request"].update(
            effect_not_after="2026-08-26T12:35:17.123456Z"
        ),
    ],
)
def test_horizon_rejects_legacy_null_or_mismatched_persisted_values(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    horizon = _horizon()
    binding = horizon.binding
    mutate(binding)

    assert _parse_horizon(binding) is None


def test_horizon_rejects_missing_or_naive_timestamps() -> None:
    assert _parse_horizon(None) is None
    assert _parse_horizon({}) is None
    with pytest.raises(ValueError, match="UTC-aware"):
        calculate_protected_effect_safe_after(
            dispatch_closed_at=SERVER_TIME.replace(tzinfo=None),
            max_effect_not_after=SERVER_TIME + timedelta(seconds=1),
            handler_bound_seconds=1,
            clock_skew_bound_seconds=1,
            safety_margin_seconds=1,
        )


def test_horizon_uses_later_dispatch_closure_when_deadline_has_passed() -> None:
    dispatch_closed_at = SERVER_TIME + timedelta(seconds=2)

    assert calculate_protected_effect_safe_after(
        dispatch_closed_at=dispatch_closed_at,
        max_effect_not_after=SERVER_TIME + timedelta(seconds=1),
        handler_bound_seconds=1,
        clock_skew_bound_seconds=1,
        safety_margin_seconds=1,
    ) == dispatch_closed_at + timedelta(seconds=3)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("handler_bound_seconds", False),
        ("clock_skew_bound_seconds", 1.0),
        ("safety_margin_seconds", MAX_PROTECTED_EFFECT_TIMING_SECONDS + 1),
    ],
)
def test_horizon_calculation_rejects_bool_float_or_unbounded_timing(
    field: str,
    replacement: object,
) -> None:
    values: dict[str, Any] = {
        "dispatch_closed_at": SERVER_TIME,
        "max_effect_not_after": SERVER_TIME + timedelta(seconds=1),
        "handler_bound_seconds": 1,
        "clock_skew_bound_seconds": 1,
        "safety_margin_seconds": 1,
    }
    values[field] = replacement

    with pytest.raises(ValueError, match="positive bounded integer"):
        calculate_protected_effect_safe_after(**values)
