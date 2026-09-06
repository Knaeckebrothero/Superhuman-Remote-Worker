"""Strict database construction for the public Canvas viewer gateway.

The isolated-origin gateway is internet-facing and must never inherit the
orchestrator's broad application database identity.  This module therefore
accepts only the purpose-specific ``CANVAS_VIEWER_POSTGRES_*`` variables and
has no legacy ``DATABASE_URL`` or ``POSTGRES_*`` fallback.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import quote

from orchestrator.database.postgres import PostgresDB


CANVAS_VIEWER_POSTGRES_PREFIX = "CANVAS_VIEWER_POSTGRES"
_DEFAULT_MIN_CONNECTIONS = 1
_DEFAULT_MAX_CONNECTIONS = 4
_MAX_CONNECTIONS = 16
_DNS_HOST = re.compile(
    r"^(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class CanvasViewerDatabaseConfigurationError(ValueError):
    """Raised when the dedicated Canvas viewer database identity is incomplete."""


class CanvasViewerDatabasePrivilegeError(RuntimeError):
    """Raised when the gateway database identity exceeds its narrow contract."""


# Keep this contract synchronized with the gateway-only methods on
# CanvasViewerSessionService and with the provisioned database role.  These are
# effective column privileges: table-level grants also satisfy the checks.
#
# A SELECT entry buys plain reads only.  PostgreSQL requires UPDATE on at least
# one column for FOR SHARE/FOR UPDATE, so a gateway statement that row-locks an
# authoritative table below fails with "permission denied" no matter how
# complete this list is.  Gateway-only methods read those rows unlocked; adding
# an UPDATE column to widen a lock would fail the excess-privilege attestation
# further down, which is the intended answer, not an obstacle to route around.
_REQUIRED_COLUMN_PRIVILEGES: tuple[tuple[str, str, str], ...] = (
    ("users", "id", "SELECT"),
    ("users", "is_admin", "SELECT"),
    ("users", "is_approved", "SELECT"),
    ("threads", "id", "SELECT"),
    ("threads", "user_id", "SELECT"),
    ("threads", "metadata", "SELECT"),
    ("srw_sessions", "id", "SELECT"),
    ("srw_sessions", "user_id", "SELECT"),
    ("srw_sessions", "absolute_expires_at", "SELECT"),
    ("srw_sessions", "revoked_at", "SELECT"),
    ("canvases", "thread_id", "SELECT"),
    ("canvases", "canvas_id", "SELECT"),
    ("canvases", "source", "SELECT"),
    ("canvases", "title", "SELECT"),
    ("canvases", "renderer", "SELECT"),
    ("canvases", "editable", "SELECT"),
    ("canvases", "alt_text", "SELECT"),
    ("canvases", "presentation_revision", "SELECT"),
    ("canvases", "source_fingerprint", "SELECT"),
    ("canvases", "source_version", "SELECT"),
    ("canvases", "origin_generation", "SELECT"),
    ("canvases", "created_at", "SELECT"),
    ("canvases", "updated_at", "SELECT"),
    ("canvas_view_attachments", "id", "SELECT"),
    ("canvas_view_attachments", "user_id", "SELECT"),
    ("canvas_view_attachments", "thread_id", "SELECT"),
    ("canvas_view_attachments", "canvas_id", "SELECT"),
    ("canvas_view_attachments", "parent_srw_session_id", "SELECT"),
    ("canvas_view_attachments", "embedding_origin", "SELECT"),
    ("canvas_view_attachments", "cookie_mode", "SELECT"),
    ("canvas_view_attachments", "expires_at", "SELECT"),
    ("canvas_view_attachments", "closed_at", "SELECT"),
    ("canvas_view_attachments", "origin_session_id", "UPDATE"),
    ("canvas_view_attachments", "last_seen_at", "UPDATE"),
    ("canvas_view_bootstraps", "id", "SELECT"),
    ("canvas_view_bootstraps", "attachment_id", "SELECT"),
    (
        "canvas_view_bootstraps",
        "expected_presentation_revision",
        "SELECT",
    ),
    ("canvas_view_bootstraps", "source_fingerprint", "SELECT"),
    ("canvas_view_bootstraps", "workspace_generation", "SELECT"),
    ("canvas_view_bootstraps", "origin_generation", "SELECT"),
    ("canvas_view_bootstraps", "expires_at", "SELECT"),
    ("canvas_view_bootstraps", "challenge_hash", "SELECT"),
    ("canvas_view_bootstraps", "browser_binding_hash", "SELECT"),
    ("canvas_view_bootstraps", "ready_receipt_hash", "SELECT"),
    ("canvas_view_bootstraps", "exchange_token_hash", "SELECT"),
    ("canvas_view_bootstraps", "authorized_at", "SELECT"),
    ("canvas_view_bootstraps", "consumed_at", "SELECT"),
    ("canvas_view_bootstraps", "challenge_hash", "UPDATE"),
    ("canvas_view_bootstraps", "browser_binding_hash", "UPDATE"),
    ("canvas_view_bootstraps", "ready_receipt_hash", "UPDATE"),
    ("canvas_view_bootstraps", "consumed_at", "UPDATE"),
    ("canvas_view_bootstraps", "consumed_origin_session_id", "UPDATE"),
    ("canvas_origin_sessions", "id", "SELECT"),
    ("canvas_origin_sessions", "session_secret_hash", "SELECT"),
    ("canvas_origin_sessions", "user_id", "SELECT"),
    ("canvas_origin_sessions", "thread_id", "SELECT"),
    ("canvas_origin_sessions", "canvas_id", "SELECT"),
    ("canvas_origin_sessions", "parent_srw_session_id", "SELECT"),
    ("canvas_origin_sessions", "source_fingerprint", "SELECT"),
    ("canvas_origin_sessions", "workspace_generation", "SELECT"),
    ("canvas_origin_sessions", "origin_generation", "SELECT"),
    ("canvas_origin_sessions", "embedding_origin", "SELECT"),
    ("canvas_origin_sessions", "cookie_mode", "SELECT"),
    ("canvas_origin_sessions", "expires_at", "SELECT"),
    ("canvas_origin_sessions", "revoked_at", "SELECT"),
    ("canvas_origin_sessions", "id", "INSERT"),
    ("canvas_origin_sessions", "session_secret_hash", "INSERT"),
    ("canvas_origin_sessions", "user_id", "INSERT"),
    ("canvas_origin_sessions", "thread_id", "INSERT"),
    ("canvas_origin_sessions", "canvas_id", "INSERT"),
    ("canvas_origin_sessions", "parent_srw_session_id", "INSERT"),
    (
        "canvas_origin_sessions",
        "issued_presentation_revision",
        "INSERT",
    ),
    ("canvas_origin_sessions", "source_fingerprint", "INSERT"),
    ("canvas_origin_sessions", "workspace_generation", "INSERT"),
    ("canvas_origin_sessions", "origin_generation", "INSERT"),
    ("canvas_origin_sessions", "embedding_origin", "INSERT"),
    ("canvas_origin_sessions", "cookie_mode", "INSERT"),
    ("canvas_origin_sessions", "expires_at", "INSERT"),
    ("canvas_origin_sessions", "expires_at", "UPDATE"),
    ("canvas_origin_sessions", "last_renewed_at", "UPDATE"),
    ("canvas_origin_sessions", "updated_at", "UPDATE"),
    ("canvas_origin_sessions", "revoked_at", "UPDATE"),
    ("canvas_origin_sessions", "revocation_reason", "UPDATE"),
)

_AUTHORITATIVE_TABLES = ("users", "threads", "srw_sessions", "canvases")
_VIEWER_TABLES = (
    "canvas_view_attachments",
    "canvas_view_bootstraps",
    "canvas_origin_sessions",
)
_FORBIDDEN_RELATION_PRIVILEGES: tuple[tuple[str, str], ...] = tuple(
    (table_name, privilege)
    for table_name in (*_AUTHORITATIVE_TABLES, *_VIEWER_TABLES)
    for privilege in ("DELETE", "TRUNCATE", "TRIGGER")
)
_ALLOWED_RELATIONS = (*_AUTHORITATIVE_TABLES, *_VIEWER_TABLES)
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
_ALL_RELATION_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_ALL_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")

_IDENTITY_ATTESTATION_SQL = """
    SELECT current_user::text AS role_name,
           session_user::text AS session_role_name,
           current_user = session_user AS session_role_matches,
           pg_catalog.current_setting('search_path') =
               'pg_catalog, public, pg_temp' AS search_path_safe,
           pg_catalog.has_database_privilege(
               current_user, pg_catalog.current_database(), 'CONNECT'
           ) AS database_connect,
           pg_catalog.has_database_privilege(
               current_user, pg_catalog.current_database(), 'CREATE'
           ) AS database_create,
           COALESCE((
               SELECT pg_catalog.has_schema_privilege(
                   current_user, namespace.oid, 'USAGE'
               )
               FROM pg_catalog.pg_namespace AS namespace
               WHERE namespace.nspname = 'public'
           ), FALSE) AS public_schema_usage,
           COALESCE((
               SELECT pg_catalog.has_schema_privilege(
                   current_user, namespace.oid, 'CREATE'
               )
               FROM pg_catalog.pg_namespace AS namespace
               WHERE namespace.nspname = 'public'
           ), FALSE) AS public_schema_create,
           EXISTS (
               SELECT 1
               FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_user
                 AND (
                     candidate.rolsuper
                     OR candidate.rolcreaterole
                     OR candidate.rolcreatedb
                     OR candidate.rolreplication
                     OR candidate.rolbypassrls
                 )
           ) AS elevated_role,
           EXISTS (
               SELECT 1
               FROM pg_catalog.pg_auth_members AS membership
               JOIN pg_catalog.pg_roles AS member
                 ON member.oid = membership.member
               WHERE member.rolname = current_user
           ) AS direct_role_membership
"""

_REQUIRED_COLUMN_ATTESTATION_SQL = """
    SELECT requirement.table_name,
           requirement.column_name,
           requirement.privilege,
           COALESCE(
               relation.relkind IN ('r', 'p')
               AND attribute.attnum IS NOT NULL
               AND pg_catalog.has_column_privilege(
                   current_user,
                   relation.oid,
                   attribute.attnum,
                   requirement.privilege
               ),
               FALSE
           ) AS granted
    FROM pg_catalog.unnest($1::text[]) WITH ORDINALITY
         AS required_table(table_name, position)
    JOIN pg_catalog.unnest($2::text[]) WITH ORDINALITY
         AS required_column(column_name, position) USING (position)
    JOIN pg_catalog.unnest($3::text[]) WITH ORDINALITY
         AS required_privilege(privilege, position) USING (position)
    CROSS JOIN LATERAL (
        SELECT required_table.table_name,
               required_column.column_name,
               required_privilege.privilege
    ) AS requirement
    LEFT JOIN pg_catalog.pg_namespace AS namespace
           ON namespace.nspname = 'public'
    LEFT JOIN pg_catalog.pg_class AS relation
           ON relation.relnamespace = namespace.oid
          AND relation.relname = requirement.table_name
    LEFT JOIN pg_catalog.pg_attribute AS attribute
           ON attribute.attrelid = relation.oid
          AND attribute.attname = requirement.column_name
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
"""

_FORBIDDEN_RELATION_ATTESTATION_SQL = """
    SELECT requirement.table_name,
           requirement.privilege,
           COALESCE(
               pg_catalog.has_table_privilege(
                   current_user, relation.oid, requirement.privilege
               )
               OR (
                   requirement.privilege IN (
                       'SELECT', 'INSERT', 'UPDATE', 'REFERENCES'
                   )
                   AND pg_catalog.has_any_column_privilege(
                       current_user, relation.oid, requirement.privilege
                   )
               ),
               FALSE
           ) AS granted
    FROM pg_catalog.unnest($1::text[]) WITH ORDINALITY
         AS forbidden_table(table_name, position)
    JOIN pg_catalog.unnest($2::text[]) WITH ORDINALITY
         AS forbidden_privilege(privilege, position) USING (position)
    CROSS JOIN LATERAL (
        SELECT forbidden_table.table_name, forbidden_privilege.privilege
    ) AS requirement
    LEFT JOIN pg_catalog.pg_namespace AS namespace
           ON namespace.nspname = 'public'
    LEFT JOIN pg_catalog.pg_class AS relation
           ON relation.relnamespace = namespace.oid
          AND relation.relname = requirement.table_name
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
"""

_ALLOWED_COLUMN_ATTESTATION_SQL = """
    SELECT relation.relname AS table_name,
           attribute.attname AS column_name,
           privilege.name AS privilege
    FROM pg_catalog.pg_namespace AS namespace
    JOIN pg_catalog.pg_class AS relation
      ON relation.relnamespace = namespace.oid
     AND relation.relkind IN ('r', 'p')
    JOIN pg_catalog.pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    CROSS JOIN pg_catalog.unnest($2::text[]) AS privilege(name)
    WHERE namespace.nspname = 'public'
      AND relation.relname = ANY($1::text[])
      AND pg_catalog.has_column_privilege(
          current_user, relation.oid, attribute.attnum, privilege.name
      )
"""

_UNRELATED_RELATION_ATTESTATION_SQL = """
    SELECT relation.relname AS table_name,
           privilege.name AS privilege
    FROM pg_catalog.pg_namespace AS namespace
    JOIN pg_catalog.pg_class AS relation
      ON relation.relnamespace = namespace.oid
     AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
    CROSS JOIN pg_catalog.unnest($2::text[]) AS privilege(name)
    WHERE namespace.nspname = 'public'
      AND NOT relation.relname = ANY($1::text[])
      AND COALESCE(
          pg_catalog.has_table_privilege(
              current_user, relation.oid, privilege.name
          )
          OR (
              privilege.name IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES')
              AND pg_catalog.has_any_column_privilege(
                  current_user, relation.oid, privilege.name
              )
          ),
          FALSE
      )
"""

_SEQUENCE_ATTESTATION_SQL = """
    SELECT relation.relname AS sequence_name,
           privilege.name AS privilege
    FROM pg_catalog.pg_namespace AS namespace
    JOIN pg_catalog.pg_class AS relation
      ON relation.relnamespace = namespace.oid
     AND relation.relkind = 'S'
    CROSS JOIN pg_catalog.unnest($1::text[]) AS privilege(name)
    WHERE namespace.nspname = 'public'
      AND pg_catalog.has_sequence_privilege(
          current_user, relation.oid, privilege.name
      )
"""


def _required(name: str, *, preserve_whitespace: bool = False) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise CanvasViewerDatabaseConfigurationError(
            f"{name} is required for the Canvas viewer gateway"
        )
    if preserve_whitespace:
        return value
    if value != value.strip():
        raise CanvasViewerDatabaseConfigurationError(
            f"{name} must not contain surrounding whitespace"
        )
    return value


def _bounded_pool_size(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CanvasViewerDatabaseConfigurationError(
            f"{name} must be an integer"
        ) from exc
    if not 1 <= value <= _MAX_CONNECTIONS:
        raise CanvasViewerDatabaseConfigurationError(
            f"{name} must be between 1 and {_MAX_CONNECTIONS}"
        )
    return value


def _database_host(value: str) -> str:
    """Return a DSN-safe DNS/IP host without admitting URL components."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if _DNS_HOST.fullmatch(value) is None:
            raise CanvasViewerDatabaseConfigurationError(
                "CANVAS_VIEWER_POSTGRES_HOST must be a DNS name or IP address"
            )
        return value
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def _required_privilege_label(requirement: tuple[str, str, str]) -> str:
    table_name, column_name, privilege = requirement
    return f"{privilege} public.{table_name}({column_name})"


def _forbidden_privilege_label(requirement: tuple[str, str]) -> str:
    table_name, privilege = requirement
    return f"{privilege} public.{table_name}"


async def attest_canvas_viewer_database_privileges(conn: Any) -> None:
    """Prove the connected role is both sufficient and narrowly constrained.

    PostgreSQL privilege helper functions report effective privileges, including
    grants inherited through role membership and grants to ``PUBLIC``. Catalog
    joins keep a missing migration or optional sensitive table fail-closed
    without relying on installation-specific role names or server extensions.
    """

    identity_row = await conn.fetchrow(_IDENTITY_ATTESTATION_SQL)
    if identity_row is None:
        raise CanvasViewerDatabasePrivilegeError(
            "Canvas viewer database privilege attestation returned no identity"
        )
    identity = dict(identity_row)

    required_tables = [item[0] for item in _REQUIRED_COLUMN_PRIVILEGES]
    required_columns = [item[1] for item in _REQUIRED_COLUMN_PRIVILEGES]
    required_privileges = [item[2] for item in _REQUIRED_COLUMN_PRIVILEGES]
    required_rows = await conn.fetch(
        _REQUIRED_COLUMN_ATTESTATION_SQL,
        required_tables,
        required_columns,
        required_privileges,
    )
    reported_required = {
        (str(row["table_name"]), str(row["column_name"]), str(row["privilege"])): row[
            "granted"
        ]
        for row in required_rows
    }
    missing = [
        _required_privilege_label(requirement)
        for requirement in _REQUIRED_COLUMN_PRIVILEGES
        if reported_required.get(requirement) is not True
    ]
    allowed_column_rows = await conn.fetch(
        _ALLOWED_COLUMN_ATTESTATION_SQL,
        list(_ALLOWED_RELATIONS),
        list(_COLUMN_PRIVILEGES),
    )
    extra_columns = {
        (str(row["table_name"]), str(row["column_name"]), str(row["privilege"]))
        for row in allowed_column_rows
    } - set(_REQUIRED_COLUMN_PRIVILEGES)

    forbidden_tables = [item[0] for item in _FORBIDDEN_RELATION_PRIVILEGES]
    forbidden_privileges = [item[1] for item in _FORBIDDEN_RELATION_PRIVILEGES]
    forbidden_rows = await conn.fetch(
        _FORBIDDEN_RELATION_ATTESTATION_SQL,
        forbidden_tables,
        forbidden_privileges,
    )
    reported_forbidden = {
        (str(row["table_name"]), str(row["privilege"])): row["granted"]
        for row in forbidden_rows
    }
    incomplete_forbidden = [
        _forbidden_privilege_label(requirement)
        for requirement in _FORBIDDEN_RELATION_PRIVILEGES
        if requirement not in reported_forbidden
    ]
    dangerous = [
        _forbidden_privilege_label(requirement)
        for requirement in _FORBIDDEN_RELATION_PRIVILEGES
        if reported_forbidden.get(requirement) is True
    ]
    dangerous.extend(
        _required_privilege_label(requirement) for requirement in sorted(extra_columns)
    )
    unrelated_rows = await conn.fetch(
        _UNRELATED_RELATION_ATTESTATION_SQL,
        list(_ALLOWED_RELATIONS),
        list(_ALL_RELATION_PRIVILEGES),
    )
    dangerous.extend(
        _forbidden_privilege_label((str(row["table_name"]), str(row["privilege"])))
        for row in unrelated_rows
    )
    sequence_rows = await conn.fetch(
        _SEQUENCE_ATTESTATION_SQL,
        list(_ALL_SEQUENCE_PRIVILEGES),
    )
    dangerous.extend(
        f"{row['privilege']} public.{row['sequence_name']} sequence"
        for row in sequence_rows
    )

    if identity.get("database_connect") is not True:
        missing.append("CONNECT current_database")
    if identity.get("public_schema_usage") is not True:
        missing.append("USAGE public schema")
    if identity.get("elevated_role") is not False:
        dangerous.append("superuser-like role attributes")
    if identity.get("session_role_matches") is not True:
        dangerous.append("authenticated session role differs from current role")
    if identity.get("search_path_safe") is not True:
        dangerous.append("search_path differs from pg_catalog, public, pg_temp")
    if identity.get("direct_role_membership") is not False:
        dangerous.append("direct role membership")
    if identity.get("database_create") is not False:
        dangerous.append("CREATE current_database")
    if identity.get("public_schema_create") is not False:
        dangerous.append("CREATE public schema")
    if incomplete_forbidden:
        missing.extend(
            f"attestation result for {label}" for label in incomplete_forbidden
        )

    if missing or dangerous:
        details: list[str] = []
        if missing:
            details.append("missing required: " + ", ".join(sorted(missing)))
        if dangerous:
            details.append("forbidden effective: " + ", ".join(sorted(dangerous)))
        raise CanvasViewerDatabasePrivilegeError(
            "Canvas viewer database privilege attestation failed; " + "; ".join(details)
        )


def create_canvas_viewer_database() -> PostgresDB:
    """Build the gateway's small pool from its dedicated database identity.

    Every connection part is explicit.  In particular, a populated application
    ``DATABASE_URL`` or ``POSTGRES_*`` environment cannot rescue an incomplete
    viewer configuration.
    """

    prefix = CANVAS_VIEWER_POSTGRES_PREFIX
    user = _required(f"{prefix}_USER")
    password = _required(f"{prefix}_PASSWORD", preserve_whitespace=True)
    host = _database_host(_required(f"{prefix}_HOST"))
    database = _required(f"{prefix}_DB")
    port_raw = _required(f"{prefix}_PORT")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise CanvasViewerDatabaseConfigurationError(
            f"{prefix}_PORT must be an integer"
        ) from exc
    if not 1 <= port <= 65535:
        raise CanvasViewerDatabaseConfigurationError(
            f"{prefix}_PORT must be between 1 and 65535"
        )

    minimum = _bounded_pool_size(f"{prefix}_MIN_CONNECTIONS", _DEFAULT_MIN_CONNECTIONS)
    maximum = _bounded_pool_size(f"{prefix}_MAX_CONNECTIONS", _DEFAULT_MAX_CONNECTIONS)
    if maximum < minimum:
        raise CanvasViewerDatabaseConfigurationError(
            f"{prefix}_MAX_CONNECTIONS must be greater than or equal to "
            f"{prefix}_MIN_CONNECTIONS"
        )

    dsn = (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )
    return PostgresDB(
        connection_string=dsn,
        min_connections=minimum,
        max_connections=maximum,
        command_timeout=30.0,
        env_prefix=prefix,
        default_min_connections=_DEFAULT_MIN_CONNECTIONS,
        default_max_connections=_DEFAULT_MAX_CONNECTIONS,
        server_settings={"search_path": "pg_catalog, public, pg_temp"},
    )


__all__ = [
    "CANVAS_VIEWER_POSTGRES_PREFIX",
    "CanvasViewerDatabaseConfigurationError",
    "CanvasViewerDatabasePrivilegeError",
    "attest_canvas_viewer_database_privileges",
    "create_canvas_viewer_database",
]
