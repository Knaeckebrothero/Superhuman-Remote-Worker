"""Orchestrator imports for the shared VM lifecycle HMAC protocol.

Wire encoding, verification and guest-token derivation live in the
standard-library-only ``shared.vm_lifecycle_auth`` module. Application
admission, replay tracking and lifecycle ownership remain with callers.
"""

from shared.vm_lifecycle_auth import (
    AUTH_FIELD as AUTH_FIELD,
    AUTH_VERSION as AUTH_VERSION,
    GUEST_KDF_LABEL as GUEST_KDF_LABEL,
    MAX_FUTURE_SKEW_SECONDS as MAX_FUTURE_SKEW_SECONDS,
    MAX_MESSAGE_AGE_SECONDS as MAX_MESSAGE_AGE_SECONDS,
    MIN_SECRET_BYTES as MIN_SECRET_BYTES,
    Direction as Direction,
    LifecycleAuthConfigurationError as LifecycleAuthConfigurationError,
    configured_secret as configured_secret,
    guest_token as guest_token,
    sign_payload as sign_payload,
    signature as signature,
    unsigned_payload as unsigned_payload,
    verify_payload as verify_payload,
)
