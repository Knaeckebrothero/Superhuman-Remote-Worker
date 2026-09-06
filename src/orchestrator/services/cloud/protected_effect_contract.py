"""Pure wire and persistence contracts for protected Nextcloud effects.

The protected reader lane cannot treat an HTTP timeout as proof that a remote
authority-creating request did not run.  Its causal fence is instead built
from three deliberately separate values:

* a short-lived, installation-specific capability attestation;
* an HMAC-authenticated request with an absolute server-enforced deadline; and
* a durable horizon captured from that request and the attested timing bounds.

This module performs no I/O and retains no signing key.  Its compact, sorted
UTF-8 JSON and fixed-width UTC timestamps are intentionally simple enough for
the Nextcloud PHP endpoint to reproduce byte for byte.

The wire authority deliberately carries the globally unique engage-attempt
UUID, not a thread id or runtime generation.  Before adopting or persisting an
intent, the database boundary must prove that the append-once attempt belongs
to the expected thread and generation; a self-consistent object from this
module is not that database proof.  A multi-effect attempt persists one intent
and closed horizon for every authority-creating POST/PUT, then derives its
aggregate fence only as the durable maximum of that nonempty exact set.

PHP interoperability is normative: recursively sort object keys with
``ksort(..., SORT_STRING)``, encode with ``JSON_UNESCAPED_SLASHES`` and
``JSON_UNESCAPED_UNICODE`` (no whitespace), and call ``hash_hmac('sha256',
DOMAIN . canonical_json, raw_key_bytes)``.  ``DOMAIN`` includes its terminating
NUL byte.  Request ``path`` is the exact origin-form HTTP path, including a
Nextcloud installation prefix when present; the sender must dispatch that
canonical path and no query, and the receiver must compare the actual path and
independently reject a nonempty query before interpreting the body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID


PROTECTED_EFFECT_CAPABILITY_VERSION = 1
PROTECTED_EFFECT_INSTALLATION_ATTESTATION_VERSION = 1
PROTECTED_EFFECT_REQUEST_VERSION = 1
PROTECTED_EFFECT_FENCE_INTENT_VERSION = 1
PROTECTED_EFFECT_HORIZON_VERSION = 1

# Public protocol constants: the PHP verifier must prepend the matching bytes
# before canonical UTF-8 JSON.  The NUL terminator makes concatenation
# unambiguous and the separate labels prevent cross-protocol substitution.
PROTECTED_EFFECT_CAPABILITY_HMAC_DOMAIN = b"srw-nextcloud-effect-capability-v1\0"
PROTECTED_EFFECT_INSTALLATION_ATTESTATION_HMAC_DOMAIN = (
    b"srw-nextcloud-installation-attestation-v1\0"
)
PROTECTED_EFFECT_REQUEST_HMAC_DOMAIN = b"srw-nextcloud-effect-request-v1\0"

# These are protocol hard limits, not deployment defaults.  An attested
# installation may advertise any strictly positive value up to one day.
MAX_PROTECTED_EFFECT_TIMING_SECONDS = 86_400

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@-]+$")
_CAPABILITY_KEYS = frozenset(
    {
        "version",
        "backend_instance_id",
        "config_sha256",
        "queue_bound_seconds",
        "handler_bound_seconds",
        "clock_skew_bound_seconds",
        "safety_margin_seconds",
        "capability_max_age_seconds",
        "server_time",
    }
)
_INSTALLATION_ATTESTATION_KEYS = frozenset(
    {
        "version",
        "backend_instance_id",
        "config_sha256",
        "installation_proof_sha256",
        "capability_sha256",
        "server_time",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "version",
        "backend_instance_id",
        "config_sha256",
        "engage_attempt",
        "method",
        "path",
        "body_sha256",
        "effect_not_after",
    }
)
_HORIZON_KEYS = frozenset(
    {
        "version",
        "intent",
        "intent_sha256",
        "dispatch_closed_at",
        "safe_after",
    }
)
_FENCE_INTENT_KEYS = frozenset(
    {
        "version",
        "capability",
        "capability_signature",
        "request",
        "request_signature",
        "db_before",
        "db_after",
        "fresh_until",
        "db_dispatched_at",
    }
)
_AUTHORITY_METHODS = frozenset({"POST", "PUT"})
_VALIDATED_CAPABILITY_MARKER = object()
_FENCE_INTENT_MARKER = object()
_HORIZON_MARKER = object()


def _canonical_json(binding: Mapping[str, Any]) -> str:
    return json.dumps(
        binding,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical_uuid(value: Any, *, coordinate: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"protected effect {coordinate} is not a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"protected effect {coordinate} is not a canonical UUID"
        ) from exc
    canonical = str(parsed)
    if not parsed.int or value != canonical:
        raise ValueError(f"protected effect {coordinate} is not a canonical UUID")
    return canonical


def _lowercase_sha256(value: Any, *, coordinate: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(
            f"protected effect {coordinate} is not a lowercase SHA-256 digest"
        )
    return value


def _positive_timing_seconds(value: Any, *, coordinate: str) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > MAX_PROTECTED_EFFECT_TIMING_SECONDS
    ):
        raise ValueError(
            f"protected effect {coordinate} must be a positive bounded integer"
        )
    return value


def _utc_datetime(value: Any, *, coordinate: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"protected effect {coordinate} must be UTC-aware")
    canonical = value.astimezone(timezone.utc)
    # ``datetime.strftime('%Y')`` is platform-dependent for years below 1000
    # and may emit one to three digits.  Such an object would be constructible
    # and signable here but impossible to re-adopt through the fixed-width wire
    # parser.  Keep every in-memory authority round-trippable by construction.
    if canonical.year < 1000:
        raise ValueError(
            f"protected effect {coordinate} must use a four-digit UTC year"
        )
    return canonical


def _format_utc(value: datetime, *, coordinate: str) -> str:
    canonical = _utc_datetime(value, coordinate=coordinate)
    return canonical.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_canonical_utc(value: Any, *, coordinate: str) -> datetime:
    if not isinstance(value, str) or not _CANONICAL_UTC_RE.fullmatch(value):
        raise ValueError(
            f"protected effect {coordinate} is not a canonical UTC timestamp"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(
            f"protected effect {coordinate} is not a canonical UTC timestamp"
        ) from exc
    if _format_utc(parsed, coordinate=coordinate) != value:
        raise ValueError(
            f"protected effect {coordinate} is not a canonical UTC timestamp"
        )
    return parsed


def normalize_protected_effect_path(value: Any) -> str:
    """Return one absolute, query-free ASCII request path.

    Empty segments, ``.`` segments, and a trailing slash are normalized away.
    Parent traversal, percent escapes, backslashes, query/fragment text, URLs,
    control characters, and an authority-form leading ``//`` are rejected.
    The caller must dispatch the returned path, never the unnormalized input.
    """

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "?" in value
        or "#" in value
        or "\\" in value
        or "%" in value
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
    ):
        raise ValueError("protected effect path is not a safe absolute path")

    segments: list[str] = []
    for segment in value.split("/"):
        if segment in {"", "."}:
            continue
        if segment == ".." or not _PATH_SEGMENT_RE.fullmatch(segment):
            raise ValueError("protected effect path is not safely normalizable")
        segments.append(segment)
    if not segments:
        raise ValueError("protected effect path cannot name the server root")
    return "/" + "/".join(segments)


@dataclass(frozen=True, slots=True)
class NextcloudEffectCapability:
    """One strict v1 timing/configuration attestation from Nextcloud."""

    backend_instance_id: str
    config_sha256: str
    queue_bound_seconds: int
    handler_bound_seconds: int
    clock_skew_bound_seconds: int
    safety_margin_seconds: int
    capability_max_age_seconds: int
    server_time: datetime
    version: int = PROTECTED_EFFECT_CAPABILITY_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != PROTECTED_EFFECT_CAPABILITY_VERSION
        ):
            raise ValueError("protected effect capability version is unsupported")
        object.__setattr__(
            self,
            "backend_instance_id",
            _canonical_uuid(
                self.backend_instance_id,
                coordinate="capability backend instance",
            ),
        )
        object.__setattr__(
            self,
            "config_sha256",
            _lowercase_sha256(
                self.config_sha256,
                coordinate="capability configuration fingerprint",
            ),
        )
        for field_name in (
            "queue_bound_seconds",
            "handler_bound_seconds",
            "clock_skew_bound_seconds",
            "safety_margin_seconds",
            "capability_max_age_seconds",
        ):
            _positive_timing_seconds(
                getattr(self, field_name),
                coordinate=field_name,
            )
        object.__setattr__(
            self,
            "server_time",
            _utc_datetime(self.server_time, coordinate="capability server time"),
        )

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend_instance_id": self.backend_instance_id,
            "config_sha256": self.config_sha256,
            "queue_bound_seconds": self.queue_bound_seconds,
            "handler_bound_seconds": self.handler_bound_seconds,
            "clock_skew_bound_seconds": self.clock_skew_bound_seconds,
            "safety_margin_seconds": self.safety_margin_seconds,
            "capability_max_age_seconds": self.capability_max_age_seconds,
            "server_time": _format_utc(
                self.server_time,
                coordinate="capability server time",
            ),
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.binding)

    @property
    def capability_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
    ) -> NextcloudEffectCapability | None:
        """Adopt only the exact, non-legacy v1 capability representation."""

        if not isinstance(binding, Mapping) or set(binding) != _CAPABILITY_KEYS:
            return None
        try:
            parsed = cls(
                version=binding.get("version"),
                backend_instance_id=binding.get("backend_instance_id"),
                config_sha256=binding.get("config_sha256"),
                queue_bound_seconds=binding.get("queue_bound_seconds"),
                handler_bound_seconds=binding.get("handler_bound_seconds"),
                clock_skew_bound_seconds=binding.get("clock_skew_bound_seconds"),
                safety_margin_seconds=binding.get("safety_margin_seconds"),
                capability_max_age_seconds=binding.get("capability_max_age_seconds"),
                server_time=_parse_canonical_utc(
                    binding.get("server_time"),
                    coordinate="capability server time",
                ),
            )
        except (AttributeError, TypeError, ValueError):
            return None
        return parsed if parsed.binding == dict(binding) else None

    def _validated_clock_window(
        self,
        *,
        trusted_before: datetime,
        trusted_after: datetime,
        clock_name: str,
        expected_backend_instance_id: str,
        expected_config_sha256: str,
    ) -> tuple[datetime, datetime, datetime]:
        """Validate freshness against trusted timestamps around the fetch.

        Effect authority supplies PostgreSQL timestamps; the startup-only
        installation probe supplies the orchestrator's UTC timestamps. The
        response must fit both the maximum fetch age and the attested
        cross-host skew. The caller-supplied pinned installation/configuration
        identity is mandatory so a self-consistent response from another
        installation cannot be adopted.
        """

        if clock_name not in {"database", "client"}:
            raise ValueError("protected effect trusted clock is unsupported")
        before = _utc_datetime(
            trusted_before,
            coordinate=f"{clock_name} time before fetch",
        )
        after = _utc_datetime(
            trusted_after,
            coordinate=f"{clock_name} time after fetch",
        )
        expected_instance = _canonical_uuid(
            expected_backend_instance_id,
            coordinate="expected backend instance",
        )
        expected_fingerprint = _lowercase_sha256(
            expected_config_sha256,
            coordinate="expected configuration fingerprint",
        )
        if (
            self.backend_instance_id != expected_instance
            or self.config_sha256 != expected_fingerprint
        ):
            raise ValueError("protected effect capability identity does not match")
        if after < before:
            raise ValueError(f"protected effect {clock_name} window runs backwards")

        fetch_age = after - before
        maximum_age = timedelta(seconds=self.capability_max_age_seconds)
        skew = timedelta(seconds=self.clock_skew_bound_seconds)
        if fetch_age > maximum_age:
            raise ValueError("protected effect capability fetch is stale")
        if self.server_time < before - skew or self.server_time > after + skew:
            raise ValueError(
                "protected effect capability server clock is out of bounds"
            )
        try:
            # If the remote clock is ahead by the full allowed skew, the
            # effect capability was really issued ``skew`` earlier.  This is
            # the conservative trusted-clock expiry.
            fresh_until = self.server_time + maximum_age - skew
        except OverflowError as exc:
            raise ValueError(
                "protected effect capability freshness exceeds UTC range"
            ) from exc
        if after > fresh_until:
            raise ValueError("protected effect capability server time is stale")
        return before, after, fresh_until


@dataclass(frozen=True, slots=True)
class NextcloudInstallationAttestation:
    """A signed startup-only installation proof bound to one capability."""

    backend_instance_id: str
    config_sha256: str
    installation_proof_sha256: str
    capability_sha256: str
    server_time: datetime
    version: int = PROTECTED_EFFECT_INSTALLATION_ATTESTATION_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != PROTECTED_EFFECT_INSTALLATION_ATTESTATION_VERSION
        ):
            raise ValueError(
                "protected effect installation attestation version is unsupported"
            )
        object.__setattr__(
            self,
            "backend_instance_id",
            _canonical_uuid(
                self.backend_instance_id,
                coordinate="installation attestation backend instance",
            ),
        )
        for field_name, coordinate in (
            ("config_sha256", "installation attestation configuration"),
            ("installation_proof_sha256", "installation attestation proof"),
            ("capability_sha256", "installation attestation capability"),
        ):
            object.__setattr__(
                self,
                field_name,
                _lowercase_sha256(
                    getattr(self, field_name),
                    coordinate=coordinate,
                ),
            )
        object.__setattr__(
            self,
            "server_time",
            _utc_datetime(
                self.server_time,
                coordinate="installation attestation server time",
            ),
        )

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend_instance_id": self.backend_instance_id,
            "config_sha256": self.config_sha256,
            "installation_proof_sha256": self.installation_proof_sha256,
            "capability_sha256": self.capability_sha256,
            "server_time": _format_utc(
                self.server_time,
                coordinate="installation attestation server time",
            ),
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.binding)

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
    ) -> NextcloudInstallationAttestation | None:
        if (
            not isinstance(binding, Mapping)
            or set(binding) != _INSTALLATION_ATTESTATION_KEYS
        ):
            return None
        try:
            parsed = cls(
                version=binding.get("version"),
                backend_instance_id=binding.get("backend_instance_id"),
                config_sha256=binding.get("config_sha256"),
                installation_proof_sha256=binding.get("installation_proof_sha256"),
                capability_sha256=binding.get("capability_sha256"),
                server_time=_parse_canonical_utc(
                    binding.get("server_time"),
                    coordinate="installation attestation server time",
                ),
            )
        except (AttributeError, TypeError, ValueError):
            return None
        return parsed if parsed.binding == dict(binding) else None


class ValidatedNextcloudEffectCapability:
    """Authenticated capability plus its exact PostgreSQL-time validity.

    Instances are produced only by :func:`adopt_protected_effect_capability`.
    The private marker prevents a merely parsed or directly constructed wire
    capability from being mistaken for authorization by dispatch/horizon APIs.
    """

    __slots__ = (
        "_capability",
        "_signature",
        "_db_before",
        "_db_after",
        "_fresh_until",
        "_validation_marker",
        "_sealed",
    )

    def __init__(
        self,
        *,
        capability: NextcloudEffectCapability,
        signature: str,
        db_before: datetime,
        db_after: datetime,
        fresh_until: datetime,
        _validation_marker: object,
    ) -> None:
        if _validation_marker is not _VALIDATED_CAPABILITY_MARKER:
            raise ValueError("protected effect capability was not validated")
        object.__setattr__(self, "_capability", capability)
        object.__setattr__(self, "_signature", signature)
        object.__setattr__(self, "_db_before", db_before)
        object.__setattr__(self, "_db_after", db_after)
        object.__setattr__(self, "_fresh_until", fresh_until)
        object.__setattr__(self, "_validation_marker", _validation_marker)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("validated protected effect capability is immutable")
        object.__setattr__(self, name, value)

    @property
    def capability(self) -> NextcloudEffectCapability:
        return self._capability

    @property
    def signature(self) -> str:
        return self._signature

    @property
    def db_before(self) -> datetime:
        return self._db_before

    @property
    def db_after(self) -> datetime:
        return self._db_after

    @property
    def fresh_until(self) -> datetime:
        return self._fresh_until

    @property
    def backend_instance_id(self) -> str:
        return self.capability.backend_instance_id

    @property
    def config_sha256(self) -> str:
        return self.capability.config_sha256

    def require_fresh(
        self,
        *,
        db_now: datetime,
    ) -> ValidatedNextcloudEffectCapability:
        """Refuse replay before the bracket closes or after signed freshness."""

        if self._validation_marker is not _VALIDATED_CAPABILITY_MARKER:
            raise ValueError("protected effect capability was not validated")
        now = _utc_datetime(db_now, coordinate="database dispatch time")
        if now < self.db_after:
            raise ValueError("protected effect capability replay runs backwards")
        if now > self.fresh_until:
            raise ValueError("protected effect capability has expired")
        return self


def adopt_protected_effect_capability(
    binding: Mapping[str, Any] | None,
    *,
    signature: str,
    key: bytes,
    db_before: datetime,
    db_after: datetime,
    expected_backend_instance_id: str,
    expected_config_sha256: str,
) -> ValidatedNextcloudEffectCapability | None:
    """Atomically parse, authenticate, pin, and freshness-check a capability."""

    capability = NextcloudEffectCapability.from_binding(binding)
    if capability is None or not verify_protected_effect_capability_signature(
        capability,
        signature=signature,
        key=key,
    ):
        return None
    try:
        before, after, fresh_until = capability._validated_clock_window(
            trusted_before=db_before,
            trusted_after=db_after,
            clock_name="database",
            expected_backend_instance_id=expected_backend_instance_id,
            expected_config_sha256=expected_config_sha256,
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    return ValidatedNextcloudEffectCapability(
        capability=capability,
        signature=signature,
        db_before=before,
        db_after=after,
        fresh_until=fresh_until,
        _validation_marker=_VALIDATED_CAPABILITY_MARKER,
    )


def adopt_protected_effect_installation_attestation(
    capability_binding: Mapping[str, Any] | None,
    *,
    capability_signature: str,
    attestation_binding: Mapping[str, Any] | None,
    attestation_signature: str,
    key: bytes,
    client_before: datetime,
    client_after: datetime,
    expected_backend_instance_id: str,
    expected_config_sha256: str,
) -> str | None:
    """Authenticate one fresh startup proof without granting effect authority.

    Protected mutations use PostgreSQL's clock and receive the sealed
    :class:`ValidatedNextcloudEffectCapability` marker. Startup installation
    attestation happens before the adapter is registered with the database, so
    it instead brackets the fetch with the orchestrator's UTC clock and returns
    only the signed, non-secret installation digest. The result can never be
    passed to the mutation/fence APIs as a validated capability.
    """

    capability = NextcloudEffectCapability.from_binding(capability_binding)
    if capability is None or not verify_protected_effect_capability_signature(
        capability,
        signature=capability_signature,
        key=key,
    ):
        return None
    attestation = NextcloudInstallationAttestation.from_binding(attestation_binding)
    if (
        attestation is None
        or not verify_protected_effect_installation_attestation_signature(
            attestation,
            signature=attestation_signature,
            key=key,
        )
        or attestation.backend_instance_id != capability.backend_instance_id
        or attestation.config_sha256 != capability.config_sha256
        or attestation.capability_sha256 != capability.capability_sha256
        or attestation.server_time != capability.server_time
    ):
        return None
    try:
        capability._validated_clock_window(
            trusted_before=client_before,
            trusted_after=client_after,
            clock_name="client",
            expected_backend_instance_id=expected_backend_instance_id,
            expected_config_sha256=expected_config_sha256,
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    return attestation.installation_proof_sha256


@dataclass(frozen=True, slots=True)
class NextcloudEffectRequestAuthority:
    """The complete loggable authority covered by one request signature.

    No URL, query, credential, HMAC key, or request body is retained.  The
    receiver acts only on ``canonical_json`` and separately supplied body bytes
    whose SHA-256 equals ``body_sha256``.
    """

    backend_instance_id: str
    config_sha256: str
    engage_attempt: str
    method: str
    path: str
    body_sha256: str
    effect_not_after: datetime
    version: int = PROTECTED_EFFECT_REQUEST_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != PROTECTED_EFFECT_REQUEST_VERSION
        ):
            raise ValueError("protected effect request version is unsupported")
        object.__setattr__(
            self,
            "backend_instance_id",
            _canonical_uuid(
                self.backend_instance_id,
                coordinate="request backend instance",
            ),
        )
        object.__setattr__(
            self,
            "engage_attempt",
            _canonical_uuid(self.engage_attempt, coordinate="engage attempt"),
        )
        object.__setattr__(
            self,
            "config_sha256",
            _lowercase_sha256(
                self.config_sha256,
                coordinate="request configuration fingerprint",
            ),
        )
        if not isinstance(self.method, str) or self.method not in _AUTHORITY_METHODS:
            raise ValueError("protected effect request method is not allowlisted")
        object.__setattr__(self, "path", normalize_protected_effect_path(self.path))
        object.__setattr__(
            self,
            "body_sha256",
            _lowercase_sha256(self.body_sha256, coordinate="request body digest"),
        )
        object.__setattr__(
            self,
            "effect_not_after",
            _utc_datetime(
                self.effect_not_after,
                coordinate="request effect deadline",
            ),
        )

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend_instance_id": self.backend_instance_id,
            "config_sha256": self.config_sha256,
            "engage_attempt": self.engage_attempt,
            "method": self.method,
            "path": self.path,
            "body_sha256": self.body_sha256,
            "effect_not_after": _format_utc(
                self.effect_not_after,
                coordinate="request effect deadline",
            ),
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.binding)

    @property
    def authority_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
    ) -> NextcloudEffectRequestAuthority | None:
        """Parse persisted JSONB without normalizing an unsafe legacy value."""

        if not isinstance(binding, Mapping) or set(binding) != _REQUEST_KEYS:
            return None
        try:
            parsed = cls(
                version=binding.get("version"),
                backend_instance_id=binding.get("backend_instance_id"),
                config_sha256=binding.get("config_sha256"),
                engage_attempt=binding.get("engage_attempt"),
                method=binding.get("method"),
                path=binding.get("path"),
                body_sha256=binding.get("body_sha256"),
                effect_not_after=_parse_canonical_utc(
                    binding.get("effect_not_after"),
                    coordinate="request effect deadline",
                ),
            )
        except (AttributeError, TypeError, ValueError):
            return None
        return parsed if parsed.binding == dict(binding) else None

    @classmethod
    def from_canonical_json(
        cls,
        value: str,
    ) -> NextcloudEffectRequestAuthority | None:
        """Parse only the exact bytes/text representation covered by HMAC."""

        if not isinstance(value, str) or not value:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        parsed = cls.from_binding(decoded)
        if parsed is None or parsed.canonical_json != value:
            return None
        return parsed

    def validate_dispatch(
        self,
        capability: ValidatedNextcloudEffectCapability,
        *,
        db_dispatched_at: datetime,
    ) -> NextcloudEffectRequestAuthority:
        """Bind the deadline to the already validated captured capability."""

        if (
            not isinstance(capability, ValidatedNextcloudEffectCapability)
            or capability._validation_marker is not _VALIDATED_CAPABILITY_MARKER
        ):
            raise ValueError("protected effect capability was not validated")
        dispatch_time = _utc_datetime(
            db_dispatched_at,
            coordinate="request dispatch time",
        )
        capability.require_fresh(db_now=dispatch_time)
        if (
            self.backend_instance_id != capability.backend_instance_id
            or self.config_sha256 != capability.config_sha256
        ):
            raise ValueError(
                "protected effect request capability identity does not match"
            )
        if self.effect_not_after <= dispatch_time:
            raise ValueError("protected effect request deadline runs backwards")
        latest_deadline = dispatch_time + timedelta(
            seconds=capability.capability.queue_bound_seconds
        )
        if self.effect_not_after > latest_deadline:
            raise ValueError("protected effect request deadline exceeds queue bound")
        return self


def _hmac_key(value: Any) -> bytes:
    # A protocol key is never embedded in an authority object or error.  A
    # 256-bit minimum also avoids silently accepting an operator typo as a key.
    if type(value) is not bytes or len(value) < hashlib.sha256().digest_size:
        raise ValueError("protected effect HMAC key must contain at least 32 bytes")
    return value


def _hmac_sha256(*, domain: bytes, canonical_json: str, key: bytes) -> str:
    signing_key = _hmac_key(key)
    return hmac.new(
        signing_key,
        domain + canonical_json.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_protected_effect_capability(
    capability: NextcloudEffectCapability,
    *,
    key: bytes,
) -> str:
    """Sign one capability using its distinct v1 HMAC domain."""

    if not isinstance(capability, NextcloudEffectCapability):
        raise ValueError("protected effect capability is missing")
    return _hmac_sha256(
        domain=PROTECTED_EFFECT_CAPABILITY_HMAC_DOMAIN,
        canonical_json=capability.canonical_json,
        key=key,
    )


def verify_protected_effect_capability_signature(
    capability: NextcloudEffectCapability,
    *,
    signature: str,
    key: bytes,
) -> bool:
    """Constant-time verify a strict lowercase capability signature."""

    if (
        not isinstance(capability, NextcloudEffectCapability)
        or not isinstance(signature, str)
        or not _SHA256_RE.fullmatch(signature)
    ):
        return False
    expected = sign_protected_effect_capability(capability, key=key)
    return hmac.compare_digest(expected, signature)


def sign_protected_effect_installation_attestation(
    attestation: NextcloudInstallationAttestation,
    *,
    key: bytes,
) -> str:
    """Sign the startup-only proof under its own HMAC domain."""

    if not isinstance(attestation, NextcloudInstallationAttestation):
        raise ValueError("protected effect installation attestation is missing")
    return _hmac_sha256(
        domain=PROTECTED_EFFECT_INSTALLATION_ATTESTATION_HMAC_DOMAIN,
        canonical_json=attestation.canonical_json,
        key=key,
    )


def verify_protected_effect_installation_attestation_signature(
    attestation: NextcloudInstallationAttestation,
    *,
    signature: str,
    key: bytes,
) -> bool:
    """Constant-time verify a strict lowercase startup-proof signature."""

    if (
        not isinstance(attestation, NextcloudInstallationAttestation)
        or not isinstance(signature, str)
        or not _SHA256_RE.fullmatch(signature)
    ):
        return False
    expected = sign_protected_effect_installation_attestation(
        attestation,
        key=key,
    )
    return hmac.compare_digest(expected, signature)


def sign_protected_effect_request(
    request: NextcloudEffectRequestAuthority,
    *,
    key: bytes,
) -> str:
    """Return the lowercase HMAC-SHA256 of the canonical UTF-8 request."""

    if not isinstance(request, NextcloudEffectRequestAuthority):
        raise ValueError("protected effect request authority is missing")
    return _hmac_sha256(
        domain=PROTECTED_EFFECT_REQUEST_HMAC_DOMAIN,
        canonical_json=request.canonical_json,
        key=key,
    )


def verify_protected_effect_request_signature(
    request: NextcloudEffectRequestAuthority,
    *,
    signature: str,
    key: bytes,
) -> bool:
    """Constant-time verify a strict lowercase signature; retain no key."""

    if (
        not isinstance(request, NextcloudEffectRequestAuthority)
        or not isinstance(signature, str)
        or not _SHA256_RE.fullmatch(signature)
    ):
        return False
    expected = sign_protected_effect_request(request, key=key)
    return hmac.compare_digest(expected, signature)


class NextcloudEffectFenceIntent:
    """Authenticated timing authority captured durably before remote dispatch."""

    __slots__ = (
        "_capability",
        "_capability_signature",
        "_request",
        "_request_signature",
        "_db_before",
        "_db_after",
        "_fresh_until",
        "_db_dispatched_at",
        "_validation_marker",
        "_sealed",
    )

    def __init__(
        self,
        *,
        capability: NextcloudEffectCapability,
        capability_signature: str,
        request: NextcloudEffectRequestAuthority,
        request_signature: str,
        db_before: datetime,
        db_after: datetime,
        fresh_until: datetime,
        db_dispatched_at: datetime,
        _validation_marker: object,
    ) -> None:
        if _validation_marker is not _FENCE_INTENT_MARKER:
            raise ValueError("protected effect fence intent was not validated")
        object.__setattr__(self, "_capability", capability)
        object.__setattr__(self, "_capability_signature", capability_signature)
        object.__setattr__(self, "_request", request)
        object.__setattr__(self, "_request_signature", request_signature)
        object.__setattr__(self, "_db_before", db_before)
        object.__setattr__(self, "_db_after", db_after)
        object.__setattr__(self, "_fresh_until", fresh_until)
        object.__setattr__(self, "_db_dispatched_at", db_dispatched_at)
        object.__setattr__(self, "_validation_marker", _validation_marker)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("protected effect fence intent is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def capture(
        cls,
        *,
        capability: ValidatedNextcloudEffectCapability,
        request: NextcloudEffectRequestAuthority,
        request_signature: str,
        key: bytes,
        db_dispatched_at: datetime,
    ) -> NextcloudEffectFenceIntent:
        """Build the record that must commit before the first remote effect."""

        if (
            not isinstance(capability, ValidatedNextcloudEffectCapability)
            or capability._validation_marker is not _VALIDATED_CAPABILITY_MARKER
        ):
            raise ValueError("protected effect capability was not validated")
        if not isinstance(request, NextcloudEffectRequestAuthority):
            raise ValueError("protected effect request authority is missing")
        dispatch_time = _utc_datetime(
            db_dispatched_at,
            coordinate="request dispatch time",
        )
        request.validate_dispatch(
            capability,
            db_dispatched_at=dispatch_time,
        )
        if not verify_protected_effect_request_signature(
            request,
            signature=request_signature,
            key=key,
        ):
            raise ValueError("protected effect request signature does not match")
        return cls(
            capability=capability.capability,
            capability_signature=capability.signature,
            request=request,
            request_signature=request_signature,
            db_before=capability.db_before,
            db_after=capability.db_after,
            fresh_until=capability.fresh_until,
            db_dispatched_at=dispatch_time,
            _validation_marker=_FENCE_INTENT_MARKER,
        )

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
        *,
        key: bytes,
        expected_backend_instance_id: str,
        expected_config_sha256: str,
        expected_engage_attempt: str,
        expected_request_authority_sha256: str,
    ) -> NextcloudEffectFenceIntent | None:
        """Re-adopt persisted pre-effect evidence using both HMAC proofs."""

        if not isinstance(binding, Mapping) or set(binding) != _FENCE_INTENT_KEYS:
            return None
        if (
            type(binding.get("version")) is not int
            or binding.get("version") != PROTECTED_EFFECT_FENCE_INTENT_VERSION
        ):
            return None
        try:
            before = _parse_canonical_utc(
                binding.get("db_before"),
                coordinate="database time before capability fetch",
            )
            after = _parse_canonical_utc(
                binding.get("db_after"),
                coordinate="database time after capability fetch",
            )
            persisted_fresh_until = _parse_canonical_utc(
                binding.get("fresh_until"),
                coordinate="capability freshness deadline",
            )
            dispatch_time = _parse_canonical_utc(
                binding.get("db_dispatched_at"),
                coordinate="request dispatch time",
            )
        except (AttributeError, TypeError, ValueError):
            return None
        capability_signature = binding.get("capability_signature")
        request_signature = binding.get("request_signature")
        if not isinstance(capability_signature, str) or not isinstance(
            request_signature, str
        ):
            return None
        validated = adopt_protected_effect_capability(
            binding.get("capability"),
            signature=capability_signature,
            key=key,
            db_before=before,
            db_after=after,
            expected_backend_instance_id=expected_backend_instance_id,
            expected_config_sha256=expected_config_sha256,
        )
        request = NextcloudEffectRequestAuthority.from_binding(binding.get("request"))
        try:
            expected_request_digest = _lowercase_sha256(
                expected_request_authority_sha256,
                coordinate="expected request authority digest",
            )
            expected_attempt = _canonical_uuid(
                expected_engage_attempt,
                coordinate="expected attempt",
            )
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            validated is None
            or request is None
            or validated.fresh_until != persisted_fresh_until
            or request.engage_attempt != expected_attempt
            or request.authority_sha256 != expected_request_digest
        ):
            return None
        try:
            parsed = cls.capture(
                capability=validated,
                request=request,
                request_signature=request_signature,
                key=key,
                db_dispatched_at=dispatch_time,
            )
        except (AttributeError, OverflowError, TypeError, ValueError):
            return None
        return parsed if parsed.binding == dict(binding) else None

    @property
    def capability(self) -> NextcloudEffectCapability:
        return self._capability

    @property
    def request(self) -> NextcloudEffectRequestAuthority:
        return self._request

    @property
    def request_signature(self) -> str:
        return self._request_signature

    @property
    def db_dispatched_at(self) -> datetime:
        return self._db_dispatched_at

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": PROTECTED_EFFECT_FENCE_INTENT_VERSION,
            "capability": self._capability.binding,
            "capability_signature": self._capability_signature,
            "request": self._request.binding,
            "request_signature": self._request_signature,
            "db_before": _format_utc(
                self._db_before,
                coordinate="database time before capability fetch",
            ),
            "db_after": _format_utc(
                self._db_after,
                coordinate="database time after capability fetch",
            ),
            "fresh_until": _format_utc(
                self._fresh_until,
                coordinate="capability freshness deadline",
            ),
            "db_dispatched_at": _format_utc(
                self._db_dispatched_at,
                coordinate="request dispatch time",
            ),
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.binding)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def calculate_protected_effect_safe_after(
    *,
    dispatch_closed_at: datetime,
    max_effect_not_after: datetime,
    handler_bound_seconds: int,
    clock_skew_bound_seconds: int,
    safety_margin_seconds: int,
) -> datetime:
    """Calculate the earliest time an ambiguous remote effect is fenced.

    All inputs are captured values.  A missing/legacy deadline is rejected
    rather than guessed from an HTTP client timeout.  The three bounds are
    added exactly once to the later of dispatch closure and signed deadline.
    """

    closed_at = _utc_datetime(
        dispatch_closed_at,
        coordinate="dispatch closure time",
    )
    effect_deadline = _utc_datetime(
        max_effect_not_after,
        coordinate="maximum effect deadline",
    )
    handler = _positive_timing_seconds(
        handler_bound_seconds,
        coordinate="captured handler bound",
    )
    skew = _positive_timing_seconds(
        clock_skew_bound_seconds,
        coordinate="captured clock skew bound",
    )
    safety = _positive_timing_seconds(
        safety_margin_seconds,
        coordinate="captured safety margin",
    )
    try:
        return max(closed_at, effect_deadline) + timedelta(
            seconds=handler + skew + safety
        )
    except OverflowError as exc:
        raise ValueError(
            "protected effect horizon exceeds UTC timestamp range"
        ) from exc


class NextcloudEffectHorizon:
    """Closed horizon for exactly one authenticated mutating request.

    A grant attempt with several remote mutations persists one fence intent and
    one closed horizon per request.  Its aggregate recovery fence is the
    durable maximum of those rows' ``safe_after`` values; callers never supply
    a detached, unproven maximum deadline.
    """

    __slots__ = ("_intent", "_dispatch_closed_at", "_validation_marker", "_sealed")

    def __init__(
        self,
        *,
        intent: NextcloudEffectFenceIntent,
        dispatch_closed_at: datetime,
        _validation_marker: object,
    ) -> None:
        if _validation_marker is not _HORIZON_MARKER:
            raise ValueError("protected effect horizon was not validated")
        object.__setattr__(self, "_intent", intent)
        object.__setattr__(self, "_dispatch_closed_at", dispatch_closed_at)
        object.__setattr__(self, "_validation_marker", _validation_marker)
        object.__setattr__(self, "_sealed", True)
        # Eagerly prove timestamp range while construction still has context.
        self.safe_after

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("protected effect horizon is immutable")
        object.__setattr__(self, name, value)

    @classmethod
    def capture(
        cls,
        *,
        intent: NextcloudEffectFenceIntent,
        db_dispatch_closed_at: datetime,
    ) -> NextcloudEffectHorizon:
        """Close an already durable pre-effect intent using PostgreSQL time."""

        if (
            not isinstance(intent, NextcloudEffectFenceIntent)
            or intent._validation_marker is not _FENCE_INTENT_MARKER
        ):
            raise ValueError("protected effect fence intent was not validated")
        closed_at = _utc_datetime(
            db_dispatch_closed_at,
            coordinate="dispatch closure time",
        )
        if closed_at < intent.db_dispatched_at:
            raise ValueError("protected effect dispatch closure runs backwards")
        return cls(
            intent=intent,
            dispatch_closed_at=closed_at,
            _validation_marker=_HORIZON_MARKER,
        )

    @property
    def intent(self) -> NextcloudEffectFenceIntent:
        return self._intent

    @property
    def backend_instance_id(self) -> str:
        return self._intent.request.backend_instance_id

    @property
    def config_sha256(self) -> str:
        return self._intent.request.config_sha256

    @property
    def engage_attempt(self) -> str:
        return self._intent.request.engage_attempt

    @property
    def request_authority_sha256(self) -> str:
        return self._intent.request.authority_sha256

    @property
    def dispatch_closed_at(self) -> datetime:
        return self._dispatch_closed_at

    @property
    def max_effect_not_after(self) -> datetime:
        # This record covers exactly one request.  SQL takes MAX across the
        # nonempty per-effect set for the grant-attempt aggregate.
        return self._intent.request.effect_not_after

    @property
    def handler_bound_seconds(self) -> int:
        return self._intent.capability.handler_bound_seconds

    @property
    def clock_skew_bound_seconds(self) -> int:
        return self._intent.capability.clock_skew_bound_seconds

    @property
    def safety_margin_seconds(self) -> int:
        return self._intent.capability.safety_margin_seconds

    @property
    def safe_after(self) -> datetime:
        return calculate_protected_effect_safe_after(
            dispatch_closed_at=self.dispatch_closed_at,
            max_effect_not_after=self.max_effect_not_after,
            handler_bound_seconds=self.handler_bound_seconds,
            clock_skew_bound_seconds=self.clock_skew_bound_seconds,
            safety_margin_seconds=self.safety_margin_seconds,
        )

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": PROTECTED_EFFECT_HORIZON_VERSION,
            "intent": self._intent.binding,
            "intent_sha256": self._intent.sha256,
            "dispatch_closed_at": _format_utc(
                self.dispatch_closed_at,
                coordinate="dispatch closure time",
            ),
            "safe_after": _format_utc(
                self.safe_after,
                coordinate="protected effect safe-after horizon",
            ),
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.binding)

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
        *,
        key: bytes,
        expected_backend_instance_id: str,
        expected_config_sha256: str,
        expected_engage_attempt: str,
        expected_request_authority_sha256: str,
    ) -> NextcloudEffectHorizon | None:
        """Verify both signed authorities and the recomputed closed horizon."""

        if not isinstance(binding, Mapping) or set(binding) != _HORIZON_KEYS:
            return None
        if (
            type(binding.get("version")) is not int
            or binding.get("version") != PROTECTED_EFFECT_HORIZON_VERSION
        ):
            return None
        try:
            closed_at = _parse_canonical_utc(
                binding.get("dispatch_closed_at"),
                coordinate="dispatch closure time",
            )
            persisted_safe_after = _parse_canonical_utc(
                binding.get("safe_after"),
                coordinate="protected effect safe-after horizon",
            )
        except (AttributeError, TypeError, ValueError):
            return None
        intent = NextcloudEffectFenceIntent.from_binding(
            binding.get("intent"),
            key=key,
            expected_backend_instance_id=expected_backend_instance_id,
            expected_config_sha256=expected_config_sha256,
            expected_engage_attempt=expected_engage_attempt,
            expected_request_authority_sha256=expected_request_authority_sha256,
        )
        if (
            intent is None
            or binding.get("intent_sha256") != intent.sha256
            or not isinstance(binding.get("intent_sha256"), str)
            or not _SHA256_RE.fullmatch(binding["intent_sha256"])
        ):
            return None
        try:
            parsed = cls.capture(
                intent=intent,
                db_dispatch_closed_at=closed_at,
            )
        except (AttributeError, OverflowError, TypeError, ValueError):
            return None
        if persisted_safe_after != parsed.safe_after:
            return None
        return parsed if parsed.binding == dict(binding) else None


__all__ = [
    "MAX_PROTECTED_EFFECT_TIMING_SECONDS",
    "PROTECTED_EFFECT_CAPABILITY_HMAC_DOMAIN",
    "PROTECTED_EFFECT_CAPABILITY_VERSION",
    "PROTECTED_EFFECT_INSTALLATION_ATTESTATION_HMAC_DOMAIN",
    "PROTECTED_EFFECT_INSTALLATION_ATTESTATION_VERSION",
    "PROTECTED_EFFECT_FENCE_INTENT_VERSION",
    "PROTECTED_EFFECT_HORIZON_VERSION",
    "PROTECTED_EFFECT_REQUEST_HMAC_DOMAIN",
    "PROTECTED_EFFECT_REQUEST_VERSION",
    "NextcloudEffectCapability",
    "NextcloudEffectFenceIntent",
    "NextcloudEffectHorizon",
    "NextcloudEffectRequestAuthority",
    "NextcloudInstallationAttestation",
    "ValidatedNextcloudEffectCapability",
    "adopt_protected_effect_capability",
    "adopt_protected_effect_installation_attestation",
    "calculate_protected_effect_safe_after",
    "normalize_protected_effect_path",
    "sign_protected_effect_capability",
    "sign_protected_effect_installation_attestation",
    "sign_protected_effect_request",
    "verify_protected_effect_capability_signature",
    "verify_protected_effect_installation_attestation_signature",
    "verify_protected_effect_request_signature",
]
