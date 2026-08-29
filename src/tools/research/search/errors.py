"""Typed provider failures raised at the search-adapter boundary."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Base class for a diagnosable provider failure."""

    failover_eligible = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderAuthError(ProviderError):
    """The provider rejected or could not use its credential."""

    failover_eligible = True


class ProviderQuotaError(ProviderError):
    """The provider account or plan has exhausted its allowance."""

    failover_eligible = True


class ProviderRateLimitError(ProviderError):
    """The provider is temporarily rate-limiting requests."""

    failover_eligible = True


class ProviderUnavailableError(ProviderError):
    """The configured provider endpoint cannot currently serve requests."""

    failover_eligible = True


class ProviderRequestError(ProviderError):
    """The provider rejected a request that the adapter constructed."""
