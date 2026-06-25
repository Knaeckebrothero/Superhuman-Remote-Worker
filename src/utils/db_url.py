"""Compose Postgres DSNs from discrete env-var parts.

The Helm chart used to ship a single ``DATABASE_URL`` Vault key alongside
``POSTGRES_USER`` / ``POSTGRES_PASSWORD`` — redundant, and a footgun when a
generated password happened to contain ``/`` (which truncates the netloc
under ``urllib.parse.urlsplit``). The chart now injects only the discrete
parts (user/password from Secret, host/port/db from ConfigMap) and this
helper assembles a URL-quoted DSN at runtime.
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote


def build_postgres_url(
    prefix: str = "POSTGRES",
    *,
    fallback_env: Optional[str] = None,
    default_host: Optional[str] = None,
    default_port: int = 5432,
    default_db: Optional[str] = None,
) -> Optional[str]:
    """Assemble ``postgresql://user:pass@host:port/db`` from env vars.

    Reads ``<prefix>_USER`` / ``<prefix>_PASSWORD`` (Secret-sourced) and
    ``<prefix>_HOST`` / ``<prefix>_PORT`` / ``<prefix>_DB`` (ConfigMap-
    sourced). Username and password are URL-quoted with ``safe=""`` so
    ``/``, ``@`` and ``:`` round-trip through ``urlsplit`` correctly.

    Falls back to ``$<fallback_env>`` if user+password aren't both set,
    so a stack still running on the old layout keeps working.

    Returns ``None`` if neither layout is configured.
    """
    user = os.getenv(f"{prefix}_USER")
    password = os.getenv(f"{prefix}_PASSWORD")
    if user and password:
        host = os.getenv(f"{prefix}_HOST", default_host)
        port = os.getenv(f"{prefix}_PORT") or str(default_port)
        db = os.getenv(f"{prefix}_DB", default_db)
        if host and db:
            return (
                f"postgresql://{quote(user, safe='')}:"
                f"{quote(password, safe='')}@{host}:{port}/{db}"
            )
    if fallback_env:
        return os.getenv(fallback_env)
    return None


def checkpointer_backend() -> str:
    """Which LangGraph checkpointer the worker uses: ``postgres`` (shared,
    cross-pod resume) or ``sqlite`` (legacy pod-local). Default ``sqlite`` for a
    safe, flag-gated rollout."""
    return os.getenv("CHECKPOINTER_BACKEND", "sqlite").strip().lower()


def resolve_checkpoint_url() -> Optional[str]:
    """Resolve the Postgres checkpoint DSN.

    Prefers a dedicated store — a full ``CHECKPOINT_DB_URL`` DSN, then discrete
    ``CHECKPOINT_*`` parts — and falls back to the app DB (``POSTGRES_*`` /
    ``DATABASE_URL``). Returns ``None`` if nothing is configured.
    """
    return (
        os.getenv("CHECKPOINT_DB_URL")
        or build_postgres_url("CHECKPOINT")
        or build_postgres_url("POSTGRES", fallback_env="DATABASE_URL")
    )
