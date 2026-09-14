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
from urllib.parse import parse_qs, quote, unquote, urlsplit


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


def postgres_database_name(dsn: str) -> str:
    """Read an explicit database target using asyncpg's URI precedence.

    The path wins over query ``dbname`` and ``database``. This is operational
    data, not a logging sanitizer: query parameters can themselves be secrets.
    An absent explicit target stays empty; database creation must not guess a
    name from an OS user or a password fragment.
    """
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("Invalid PostgreSQL DSN scheme")
    # A slash in unquoted userinfo is not an asyncpg URI. Do not mistake its
    # remainder for a database to create. Multi-host authorities remain valid.
    if "@" not in parts.netloc and ":" in parts.netloc and "," not in parts.netloc:
        try:
            parts.port
        except ValueError:
            raise ValueError("PostgreSQL DSN userinfo must be URL-encoded") from None
    if parts.path:
        return unquote(parts.path.removeprefix("/"))
    query = parse_qs(parts.query, strict_parsing=True)
    return (query.get("dbname") or query.get("database") or [""])[-1]


def describe_postgres_dsn(dsn: Optional[str]) -> dict:
    """Describe a DSN without exposing userinfo or arbitrary query values.

    Query boundaries are parsed before userinfo, so ``@`` in a query password
    never becomes an authority delimiter. The legacy raw ``p/a@ss:w0rd``
    password shape is tolerated for description only; operational connections
    retain asyncpg's requirement for URL-encoded credentials.

    This is metadata, not a guarantee that an operator supplied no secret as a
    database name. Logs use independent configuration labels instead.
    """
    empty = {"host": "", "port": "", "database": ""}
    if not dsn:
        return empty
    try:
        parts = urlsplit(dsn)
        if parts.scheme not in ("postgres", "postgresql"):
            return empty
        try:
            port = parts.port
        except ValueError:
            # Recover only malformed unquoted userinfo, never a valid URI with
            # an @ in its path/query. Query content is excluded before slicing.
            raw = dsn.partition("?")[0].partition("#")[0]
            authority = raw.partition("://")[2]
            userinfo, marker, location = authority.rpartition("@")
            if not marker or "@" in parts.netloc or ":" not in userinfo:
                return empty
            parts = urlsplit("postgresql://" + location)
            port = parts.port
            return {
                "host": parts.hostname or "",
                "port": str(port) if port else "",
                "database": unquote(parts.path.removeprefix("/")),
            }
        return {
            "host": parts.hostname or "",
            "port": str(port) if port else "",
            "database": postgres_database_name(dsn),
        }
    except ValueError:
        return empty


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


def resolve_fenced_checkpoint_url() -> Optional[str]:
    """Resolve the only valid DSN for lease-fenced worker checkpoints.

    The fenced saver locks ``run_queue`` and writes the LangGraph checkpoint
    in one transaction. Both tables must therefore be in the authoritative
    application Postgres database. A dedicated checkpoint database remains
    supported for pinned workers, but stateless workers deliberately ignore
    it instead of fencing against a missing or non-authoritative queue copy.
    """

    return build_postgres_url("POSTGRES", fallback_env="DATABASE_URL")
