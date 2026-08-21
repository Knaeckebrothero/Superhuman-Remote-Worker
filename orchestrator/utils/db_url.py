"""Compose Postgres DSNs from discrete env-var parts.

The Helm chart used to ship a single ``DATABASE_URL`` Vault key alongside
``POSTGRES_USER`` / ``POSTGRES_PASSWORD`` — redundant, and a footgun when a
generated password happened to contain ``/`` (which truncates the netloc
under ``urllib.parse.urlsplit``). The chart now injects only the discrete
parts (user/password from Secret, host/port/db from ConfigMap) and this
helper assembles a URL-quoted DSN at runtime.

Mirrors ``src/utils/db_url.py`` — kept duplicated so the orchestrator
container image doesn't need to bundle the agent ``src/`` tree.
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

    ``<prefix>_SSLMODE`` and ``<prefix>_SSLROOTCERT``, when set, are appended
    as libpq query parameters. asyncpg accepts ``disable``, ``allow``,
    ``prefer``, ``require``, ``verify-ca`` and ``verify-full``; with none set
    it defaults to ``prefer`` (encrypted if offered, never verified). They are
    NOT appended to a ``fallback_env`` DSN, which may carry its own query
    string.

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
            dsn = (
                f"postgresql://{quote(user, safe='')}:"
                f"{quote(password, safe='')}@{host}:{port}/{db}"
            )
            # TLS is expressed as libpq query parameters, which asyncpg parses.
            # Appended ONLY here: a fallback DSN may already carry its own query
            # string, and merging the two is a footgun for no benefit.
            params = []
            sslmode = os.getenv(f"{prefix}_SSLMODE")
            if sslmode:
                params.append(f"sslmode={quote(sslmode, safe='')}")
            sslrootcert = os.getenv(f"{prefix}_SSLROOTCERT")
            if sslrootcert:
                params.append(f"sslrootcert={quote(sslrootcert, safe='')}")
            if params:
                dsn = f"{dsn}?{'&'.join(params)}"
            return dsn
    if fallback_env:
        return os.getenv(fallback_env)
    return None
