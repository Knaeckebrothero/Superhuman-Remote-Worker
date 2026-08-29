"""Shared fixed-destination HTTP helpers for search adapters."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)


def configured_endpoint(base_url: str | None, suffix: str) -> str:
    """Append a provider API path without consulting model-supplied input."""

    configured = str(base_url or "").strip()
    parsed = urlsplit(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderRequestError("Provider base URL is not configured correctly")
    suffix_path = "/" + suffix.strip("/")
    path = parsed.path.rstrip("/")
    if not path.endswith(suffix_path):
        path += suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def raise_for_provider_status(provider: str, response: httpx.Response) -> None:
    """Raise the typed taxonomy for a non-success provider response."""

    status = response.status_code
    if status < 400:
        return
    message = f"{provider} request failed (HTTP {status})"
    if status in {401, 403}:
        raise ProviderAuthError(message, status_code=status)
    if status in {402, 432}:
        raise ProviderQuotaError(message, status_code=status)
    if status == 429:
        raise ProviderRateLimitError(message, status_code=status)
    if status >= 500:
        raise ProviderUnavailableError(message, status_code=status)
    raise ProviderRequestError(message, status_code=status)


def transport_error(provider: str, error: Exception) -> ProviderError:
    """Classify client transport/protocol failures without exposing secrets."""

    if isinstance(error, ProviderError):
        return error
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return ProviderUnavailableError(f"{provider} provider is unavailable")
    return ProviderUnavailableError(f"{provider} returned an invalid response")
