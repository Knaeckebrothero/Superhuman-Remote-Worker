"""Shared, diagnosable Semantic Scholar API transport.

The paper tools previously implemented the same HTTP call in two modules and
treated every non-429 failure as a generic ``aiohttp`` error.  In particular,
metadata lookups swallowed a rejected API key as "paper not found".  This
module owns the transport contract, classifies provider responses, and keeps a
secret-free health snapshot for agent status and operator probes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from .network import ProxyConfig, research_request

SEMANTIC_SCHOLAR_BASE_URL = "https://api.semanticscholar.org/graph/v1"
SEMANTIC_SCHOLAR_API_KEY_ENV = "SEMANTIC_SCHOLAR_API_KEY"


@dataclass(frozen=True)
class SemanticScholarHealth:
    """Secret-free result of the latest real provider request."""

    state: str
    key_configured: bool
    status_code: Optional[int] = None
    checked_at: Optional[str] = None
    message: str = "Provider has not been checked in this process."

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SemanticScholarProviderError(RuntimeError):
    """Classified provider failure safe to expose to an agent or operator."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retryable = retryable


def _configured_api_key() -> Optional[str]:
    """Return a normalized key without ever logging or serializing it."""

    value = os.getenv(SEMANTIC_SCHOLAR_API_KEY_ENV, "").strip()
    return value or None


_health = SemanticScholarHealth(
    state="unchecked",
    key_configured=_configured_api_key() is not None,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_health(
    state: str,
    *,
    status_code: Optional[int],
    message: str,
) -> SemanticScholarHealth:
    global _health
    _health = SemanticScholarHealth(
        state=state,
        key_configured=_configured_api_key() is not None,
        status_code=status_code,
        checked_at=_utc_now(),
        message=message,
    )
    return _health


def get_semantic_scholar_health() -> dict[str, Any]:
    """Return the latest provider health without credential material."""

    key_configured = _configured_api_key() is not None
    snapshot = _health
    if snapshot.key_configured != key_configured:
        snapshot = replace(
            snapshot,
            state="unchecked",
            key_configured=key_configured,
            status_code=None,
            checked_at=None,
            message="Credential presence changed; provider has not been rechecked.",
        )
    return snapshot.as_dict()


def _classified_http_error(
    status_code: int,
    *,
    key_configured: bool,
) -> SemanticScholarProviderError:
    if status_code in {401, 403}:
        credential = (
            f"configured {SEMANTIC_SCHOLAR_API_KEY_ENV} was rejected; "
            "rotate or verify the credential"
            if key_configured
            else f"request was refused without {SEMANTIC_SCHOLAR_API_KEY_ENV}"
        )
        message = (
            f"Semantic Scholar authentication failed (HTTP {status_code}): "
            f"{credential}. Repeating the request will not help until the "
            "configuration changes."
        )
        return SemanticScholarProviderError(
            message,
            category="authentication",
            status_code=status_code,
            retryable=False,
        )

    if status_code == 429:
        credential_hint = (
            "The configured key is being throttled."
            if key_configured
            else f"Configure {SEMANTIC_SCHOLAR_API_KEY_ENV} to avoid the shared anonymous pool."
        )
        return SemanticScholarProviderError(
            f"Semantic Scholar rate limit hit (HTTP 429). {credential_hint}",
            category="rate_limit",
            status_code=status_code,
            retryable=True,
        )

    if status_code >= 500:
        return SemanticScholarProviderError(
            f"Semantic Scholar is unavailable (HTTP {status_code}).",
            category="provider_unavailable",
            status_code=status_code,
            retryable=True,
        )

    return SemanticScholarProviderError(
        f"Semantic Scholar request failed (HTTP {status_code}).",
        category="request",
        status_code=status_code,
        retryable=False,
    )


async def semantic_scholar_request_json(
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    proxy: Optional[ProxyConfig] = None,
    timeout: float = 30,
    allow_not_found: bool = False,
) -> Optional[dict[str, Any]]:
    """Request and decode one Semantic Scholar response.

    HTTP failures are never retried by ``research_request``.  The typed error
    tells callers whether a retry could make sense and ensures 401/403 cannot
    be mistaken for an empty result or a missing paper.
    """

    api_key = _configured_api_key()
    headers = {"x-api-key": api_key} if api_key else {}
    url = f"{SEMANTIC_SCHOLAR_BASE_URL}/{path.lstrip('/')}"

    try:
        async with research_request(
            "GET",
            url,
            proxy=proxy,
            timeout=timeout,
            params=params or {},
            headers=headers,
        ) as response:
            status_code = response.status
            if status_code == 404 and allow_not_found:
                _record_health(
                    "ready",
                    status_code=status_code,
                    message="Provider accepted the request; paper was not found.",
                )
                return None
            if status_code >= 400:
                error = _classified_http_error(
                    status_code, key_configured=api_key is not None
                )
                _record_health(
                    error.category,
                    status_code=status_code,
                    message=str(error),
                )
                raise error

            try:
                data = await response.json()
            except (aiohttp.ClientError, json.JSONDecodeError, ValueError) as exc:
                error = SemanticScholarProviderError(
                    "Semantic Scholar returned an invalid JSON response.",
                    category="protocol",
                    status_code=status_code,
                    retryable=True,
                )
                _record_health(
                    error.category,
                    status_code=status_code,
                    message=str(error),
                )
                raise error from exc

            if not isinstance(data, dict):
                error = SemanticScholarProviderError(
                    "Semantic Scholar returned an unexpected response shape.",
                    category="protocol",
                    status_code=status_code,
                    retryable=True,
                )
                _record_health(
                    error.category,
                    status_code=status_code,
                    message=str(error),
                )
                raise error

            _record_health(
                "ready",
                status_code=status_code,
                message="Semantic Scholar request succeeded.",
            )
            return data
    except SemanticScholarProviderError:
        raise
    except ConnectionError as exc:
        error = SemanticScholarProviderError(
            "Semantic Scholar connection failed after bounded retries.",
            category="connection",
            retryable=True,
        )
        _record_health(error.category, status_code=None, message=str(error))
        raise error from exc
    except aiohttp.ClientError as exc:
        error = SemanticScholarProviderError(
            "Semantic Scholar HTTP transport failed.",
            category="connection",
            retryable=True,
        )
        _record_health(error.category, status_code=None, message=str(error))
        raise error from exc


async def search_semantic_scholar(
    query: str,
    max_results: int,
    *,
    fields: str,
    proxy: Optional[ProxyConfig] = None,
) -> dict[str, Any]:
    data = await semantic_scholar_request_json(
        "paper/search",
        proxy=proxy,
        params={"query": query, "limit": max_results, "fields": fields},
    )
    return data or {}


async def get_semantic_scholar_paper(
    paper_id: str,
    *,
    fields: str,
    proxy: Optional[ProxyConfig] = None,
) -> Optional[dict[str, Any]]:
    encoded_paper_id = quote(paper_id, safe=":")
    return await semantic_scholar_request_json(
        f"paper/{encoded_paper_id}",
        proxy=proxy,
        params={"fields": fields},
        allow_not_found=True,
    )


async def probe_semantic_scholar(
    *,
    proxy: Optional[ProxyConfig] = None,
    timeout: float = 15,
) -> dict[str, Any]:
    """Perform one real, low-payload provider handshake.

    The result is deliberately limited to status/category/key-presence fields.
    It is suitable for ``kubectl exec`` against the actual worker image and
    never prints the API key or provider response content.
    """

    try:
        await semantic_scholar_request_json(
            "paper/ArXiv:1706.03762",
            params={"fields": "paperId"},
            proxy=proxy,
            timeout=timeout,
        )
    except SemanticScholarProviderError:
        pass
    return get_semantic_scholar_health()
