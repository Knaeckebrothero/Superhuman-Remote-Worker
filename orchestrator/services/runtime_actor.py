"""Opaque runtime-actor credential minting and authorization.

``MCP_INTERNAL_KEY`` authenticates transport only.  This module is the single
actor boundary for OC-02 officer message actions and BP-09 sensitive knowledge
writes: identity is derived at dispatch/attach, stored behind opaque tokens,
and revalidated against current durable state on every authorization.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from src.shared.runtime_actor import (
    RUNTIME_ACTOR_BOOTSTRAP_HEADER,
    RUNTIME_ACTOR_HEADER,
    RUNTIME_ACTOR_REFRESH_HEADER,
    SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY,
    RuntimeActorContext,
)


ACCESS_TTL_SECONDS = int(os.environ.get("RUNTIME_ACTOR_ACCESS_TTL_SECONDS", "300"))
REFRESH_TTL_SECONDS = int(os.environ.get("RUNTIME_ACTOR_REFRESH_TTL_SECONDS", "86400"))
BOOTSTRAP_TTL_SECONDS = int(
    os.environ.get("RUNTIME_ACTOR_BOOTSTRAP_TTL_SECONDS", "900")
)
# A dedicated session pod exchanges its bootstrap within seconds of booting, so
# 15 minutes is generous. A warm pool pod holds its bootstrap for as long as it
# sits idle waiting for a session, which is a pod lifetime — a short TTL there
# would silently turn identity into a function of how busy the cluster is,
# which is exactly the nondeterminism this credential exists to remove.
POD_BOOTSTRAP_TTL_SECONDS = int(
    os.environ.get("RUNTIME_ACTOR_POD_BOOTSTRAP_TTL_SECONDS", str(7 * 24 * 3600))
)
# Heartbeat-driven slide throttle: a grant is only pushed forward once it is
# inside the *second half* of its window. A 60s heartbeat therefore costs one
# UPDATE per grant per half-TTL (two a day at the default 24h) instead of one
# per beat, while still leaving a full half-TTL of runway to recover from an
# orchestrator outage before anything expires.
LIVENESS_SLIDE_BELOW_SECONDS = int(
    os.environ.get(
        "RUNTIME_ACTOR_LIVENESS_SLIDE_BELOW_SECONDS", str(REFRESH_TTL_SECONDS // 2)
    )
)

_TOKEN_RE = re.compile(r"^sr(?:a|r|b)_[A-Za-z0-9_-]{32,128}$")
_SENSITIVE_ACTIONS = frozenset({"machine_tags", "charter"})


class RuntimeActorCredentialError(Exception):
    """Closed-vocabulary credential/identity refusal."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        actor: RuntimeActorContext | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.actor = actor
        super().__init__(message)


def _token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _metadata(thread: dict[str, Any]) -> dict[str, Any]:
    value = thread.get("metadata") or {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def _thread_project_ids(db: Any, thread: dict[str, Any]) -> list[str]:
    """Derive the same native-project ordering as the attach boundary."""

    rows = await db.list_thread_mounts(str(thread["id"]))
    project_ids = [
        str(row["source_ref"])
        for row in rows
        if row.get("mount_kind") in {"project", "project_default"}
        and row.get("source_ref")
    ]
    if project_ids:
        return list(dict.fromkeys(project_ids))
    metadata = _metadata(thread)
    fallback = [
        *([str(thread["project_id"])] if thread.get("project_id") else []),
        *(str(value) for value in metadata.get("project_ids") or []),
    ]
    return list(dict.fromkeys(fallback))


async def derive_runtime_actor(
    db: Any,
    *,
    thread_id: str,
    project_ids: list[str] | tuple[str, ...] | None = None,
) -> RuntimeActorContext:
    """Derive a persistent runtime's actor solely from durable server state."""

    thread = await db.get_thread(str(thread_id))
    if not thread or str(thread.get("status") or "") == "ended":
        raise RuntimeActorCredentialError(
            "runtime_not_current", "The bound session is not current."
        )
    durable_projects = await _thread_project_ids(db, thread)
    if project_ids is not None:
        requested = list(dict.fromkeys(str(value) for value in project_ids if value))
        if any(value not in durable_projects for value in requested):
            raise RuntimeActorCredentialError(
                "runtime_scope_changed",
                "The delivered project scope is not attached to the thread.",
            )
    else:
        requested = durable_projects
    project_id = requested[0] if requested else None
    user_id = str(thread["user_id"]) if thread.get("user_id") else None

    project_role: str | None = None
    if user_id and project_id:
        user = await db.get_user(user_id)
        if user and bool(user.get("is_admin")):
            project_role = "admin"
        elif user:
            raw_role = await db.get_user_role_in_project(project_id, user_id)
            project_role = str(raw_role) if raw_role else None

    caller_kind = "human"
    officer_incarnation: int | None = None
    if project_id:
        post = await db.get_project_officer(project_id)
        if post and str(post.get("thread_id") or "") == str(thread_id):
            caller_kind = "officer"
            incarnations = post.get("incarnations") or []
            officer_incarnation = (
                len(incarnations) if isinstance(incarnations, list) else 0
            )
        else:
            config_override = _metadata(thread).get("config_override") or {}
            officer_config = (
                config_override.get("officer")
                if isinstance(config_override, dict)
                else None
            )
            if (
                isinstance(officer_config, dict)
                and officer_config.get("conference") is True
            ):
                caller_kind = "conference"

    return RuntimeActorContext(
        caller_kind=caller_kind,
        project_id=project_id,
        project_role=project_role,
        thread_id=str(thread_id),
        officer_incarnation=officer_incarnation,
        user_id=user_id,
    )


def worker_runtime_actor(
    *, project_id: str | None, user_id: str | None = None
) -> RuntimeActorContext:
    """Build the server-side worker identity placed in a job start bundle."""

    return RuntimeActorContext(
        caller_kind="worker",
        project_id=str(project_id) if project_id else None,
        project_role=None,
        thread_id=None,
        officer_incarnation=None,
        user_id=str(user_id) if user_id else None,
    )


async def _insert_access_token(
    conn: Any, grant_id: Any, *, now: datetime
) -> tuple[str, datetime]:
    access_token = _token("sra")
    access_expires_at = now + timedelta(seconds=ACCESS_TTL_SECONDS)
    await conn.execute(
        """
        INSERT INTO runtime_actor_access_tokens
            (token_hash, grant_id, expires_at)
        VALUES ($1, $2, $3)
        """,
        _digest(access_token),
        grant_id,
        access_expires_at,
    )
    return access_token, access_expires_at


async def mint_runtime_actor(
    db: Any, actor: RuntimeActorContext
) -> RuntimeActorContext:
    """Mint opaque access/refresh credentials for a derived actor."""

    refresh_token = _token("srr")
    now = datetime.now(timezone.utc)
    refresh_expires_at = now + timedelta(seconds=REFRESH_TTL_SECONDS)
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO runtime_actor_grants (
                    refresh_token_hash, caller_kind, user_id, project_id,
                    project_role, thread_id, officer_incarnation,
                    refresh_expires_at
                )
                VALUES ($1, $2, $3::uuid, $4::uuid, $5, $6::uuid, $7, $8)
                RETURNING id
                """,
                _digest(refresh_token),
                actor.caller_kind,
                actor.user_id,
                actor.project_id,
                actor.project_role,
                actor.thread_id,
                actor.officer_incarnation,
                refresh_expires_at,
            )
            access_token, access_expires_at = await _insert_access_token(
                conn, row["id"], now=now
            )
    actor.access_credential = access_token
    actor.refresh_credential = refresh_token
    actor.access_expires_at = access_expires_at
    actor.refresh_expires_at = refresh_expires_at
    return actor


async def mint_thread_runtime_actor(
    db: Any,
    *,
    thread_id: str,
    project_ids: list[str] | tuple[str, ...] | None = None,
) -> RuntimeActorContext:
    actor = await derive_runtime_actor(db, thread_id=thread_id, project_ids=project_ids)
    return await mint_runtime_actor(db, actor)


async def mint_worker_runtime_actor(
    db: Any, *, project_id: str | None, user_id: str | None = None
) -> RuntimeActorContext:
    return await mint_runtime_actor(
        db, worker_runtime_actor(project_id=project_id, user_id=user_id)
    )


async def issue_runtime_actor_bootstrap(db: Any, thread_id: str) -> str:
    """Create one unique, short-lived workload bootstrap for a session pod."""

    token = _token("srb")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=BOOTSTRAP_TTL_SECONDS)
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_actor_bootstraps (token_hash, thread_id, expires_at)
            VALUES ($1, $2::uuid, $3)
            """,
            _digest(token),
            str(thread_id),
            expires_at,
        )
    return token


async def issue_runtime_actor_pod_bootstrap(db: Any) -> str:
    """Create one pod-scoped bootstrap for a warm pool agent.

    Thread-less by construction: the pod is provisioned before any session
    exists, so there is nothing to bind to yet. The binding happens in
    ``exchange_runtime_actor_pod_bootstrap``, which reads it from durable
    server state instead of accepting it from the caller.
    """

    token = _token("srb")
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=POD_BOOTSTRAP_TTL_SECONDS
    )
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_actor_bootstraps (token_hash, thread_id, expires_at)
            VALUES ($1, NULL, $2)
            """,
            _digest(token),
            expires_at,
        )
    return token


async def exchange_runtime_actor_pod_bootstrap(
    db: Any, *, agent_id: str, thread_id: str, bootstrap_token: str
) -> RuntimeActorContext:
    """Bind a warm pool pod's bootstrap to the session it was just given.

    Two independent facts must hold, and neither is taken from the caller's
    word: the presenter holds a secret that only exists inside one provisioned
    pod, and the orchestrator's own ``agents`` row already says that pod is
    the agent serving this thread. A pod that lies about ``thread_id`` gets
    nothing, because the binding is read from the row, not the request.

    Unlike the dedicated-pod exchange this is deliberately repeatable: one pool
    pod serves a succession of sessions over its life, and each attach must
    mint a fresh, correctly-scoped actor.
    """

    if not _valid_token(bootstrap_token, "srb"):
        raise RuntimeActorCredentialError(
            "malformed_bootstrap", "Runtime actor bootstrap is malformed."
        )
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE runtime_actor_bootstraps
               SET last_used_at = now()
             WHERE token_hash = $1
               AND thread_id IS NULL
               AND expires_at > now()
            RETURNING token_hash
            """,
            _digest(bootstrap_token),
        )
        if row is None:
            raise RuntimeActorCredentialError(
                "invalid_bootstrap",
                "Runtime actor pod bootstrap is invalid or expired.",
            )
        bound = await conn.fetchrow(
            """
            SELECT 1
              FROM agents
             WHERE id = $1::uuid
               AND thread_id = $2::uuid
            """,
            str(agent_id),
            str(thread_id),
        )
    if bound is None:
        raise RuntimeActorCredentialError(
            "runtime_not_current",
            "This agent is not the bound agent for the requested session.",
        )
    return await mint_thread_runtime_actor(db, thread_id=str(thread_id))


async def exchange_runtime_actor_bootstrap(
    db: Any, *, thread_id: str, bootstrap_token: str
) -> RuntimeActorContext:
    """Exchange a pod-unique bootstrap after the registration bind succeeds."""

    if not _valid_token(bootstrap_token, "srb"):
        raise RuntimeActorCredentialError(
            "malformed_bootstrap", "Runtime actor bootstrap is malformed."
        )
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE runtime_actor_bootstraps
               SET last_used_at = now()
             WHERE token_hash = $1
               AND thread_id = $2::uuid
               AND expires_at > now()
            RETURNING thread_id
            """,
            _digest(bootstrap_token),
            str(thread_id),
        )
    if row is None:
        raise RuntimeActorCredentialError(
            "invalid_bootstrap", "Runtime actor bootstrap is invalid or expired."
        )
    return await mint_thread_runtime_actor(db, thread_id=str(thread_id))


def _valid_token(token: Any, prefix: str) -> bool:
    return (
        isinstance(token, str)
        and token.startswith(f"{prefix}_")
        and _TOKEN_RE.fullmatch(token) is not None
    )


def _header_values(request: Any, name: str) -> list[str]:
    headers = getattr(request, "headers", None)
    if headers is None:
        return []
    getter = getattr(headers, "getlist", None)
    if callable(getter):
        raw_values = getter(name)
        return [part.strip() for value in raw_values for part in str(value).split(",")]
    try:
        value = headers.get(name)
    except Exception:
        return []
    if not isinstance(value, str):
        return []
    # A combined duplicate header is just as ambiguous as two physical fields.
    return [part.strip() for part in value.split(",")]


def request_bootstrap_token(request: Any) -> str | None:
    values = _header_values(request, RUNTIME_ACTOR_BOOTSTRAP_HEADER)
    if not values:
        return None
    if len(values) != 1:
        raise RuntimeActorCredentialError(
            "duplicate_bootstrap",
            "Duplicate runtime actor bootstraps are refused.",
        )
    value = values[0]
    if not _valid_token(value, "srb"):
        raise RuntimeActorCredentialError(
            "malformed_bootstrap", "Runtime actor bootstrap is malformed."
        )
    return value


def _required_request_token(request: Any, name: str, prefix: str) -> str:
    values = _header_values(request, name)
    if not values:
        raise RuntimeActorCredentialError(
            "missing_credential", "Runtime actor credential is required."
        )
    if len(values) != 1:
        raise RuntimeActorCredentialError(
            "duplicate_credential", "Duplicate runtime actor credentials are refused."
        )
    value = values[0]
    if not _valid_token(value, prefix):
        raise RuntimeActorCredentialError(
            "malformed_credential", "Runtime actor credential is malformed."
        )
    return value


def _actor_from_row(row: Any) -> RuntimeActorContext:
    return RuntimeActorContext(
        caller_kind=str(row["caller_kind"]),
        project_id=str(row["project_id"]) if row.get("project_id") else None,
        project_role=str(row["project_role"]) if row.get("project_role") else None,
        thread_id=str(row["thread_id"]) if row.get("thread_id") else None,
        officer_incarnation=row.get("officer_incarnation"),
        user_id=str(row["user_id"]) if row.get("user_id") else None,
        access_expires_at=_as_utc(row.get("access_expires_at")),
        refresh_expires_at=_as_utc(row.get("refresh_expires_at")),
    )


async def _actor_for_access(db: Any, token: str) -> RuntimeActorContext:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT g.caller_kind, g.user_id, g.project_id, g.project_role,
                   g.thread_id, g.officer_incarnation, g.refresh_expires_at,
                   g.revoked_at, a.expires_at AS access_expires_at
              FROM runtime_actor_access_tokens a
              JOIN runtime_actor_grants g ON g.id = a.grant_id
             WHERE a.token_hash = $1
            """,
            _digest(token),
        )
        if row is not None:
            await conn.execute(
                "UPDATE runtime_actor_access_tokens SET last_used_at = now() "
                "WHERE token_hash = $1",
                _digest(token),
            )
    if row is None:
        raise RuntimeActorCredentialError(
            "invalid_credential", "Runtime actor credential is not recognized."
        )
    actor = _actor_from_row(row)
    now = datetime.now(timezone.utc)
    if row.get("revoked_at") is not None:
        raise RuntimeActorCredentialError(
            "revoked_credential", "Runtime actor credential was revoked.", actor=actor
        )
    if actor.access_expires_at is None or actor.access_expires_at <= now:
        raise RuntimeActorCredentialError(
            "expired_credential", "Runtime actor credential has expired.", actor=actor
        )
    if actor.refresh_expires_at is None or actor.refresh_expires_at <= now:
        raise RuntimeActorCredentialError(
            "expired_credential", "Runtime actor grant has expired.", actor=actor
        )
    return actor


async def _current_actor(db: Any, actor: RuntimeActorContext) -> RuntimeActorContext:
    if actor.caller_kind == "worker":
        return worker_runtime_actor(
            project_id=actor.project_id,
            user_id=actor.user_id,
        )
    if not actor.thread_id:
        raise RuntimeActorCredentialError(
            "runtime_not_current", "Runtime actor has no bound session.", actor=actor
        )
    try:
        current = await derive_runtime_actor(
            db,
            thread_id=actor.thread_id,
            project_ids=[actor.project_id] if actor.project_id else [],
        )
    except RuntimeActorCredentialError as exc:
        exc.actor = actor
        raise
    if current.audit_payload() != actor.audit_payload():
        raise RuntimeActorCredentialError(
            "runtime_not_current",
            "The runtime actor no longer matches current post or membership state.",
            actor=actor,
        )
    return current


async def _audit_denial(
    db: Any,
    request: Any,
    error: RuntimeActorCredentialError,
    *,
    action: str,
    project_id: str | None,
) -> None:
    try:
        from security.access import log_security_event
    except ImportError:  # pragma: no cover - package-style test imports
        from orchestrator.security.access import log_security_event

    actor = error.actor
    user = {
        "id": actor.user_id if actor else None,
        "auth_method": "runtime_actor",
        "is_admin": bool(actor and actor.project_role == "admin"),
    }
    actor_bits = actor.audit_payload() if actor else {"caller_kind": "unresolved"}
    await log_security_event(
        db,
        request=request,
        event_type="runtime_actor_denied",
        resource_type="runtime_actor",
        resource_id=project_id or (actor.project_id if actor else None),
        user=user,
        detail=f"{error.code}: action={action}; actor={actor_bits}",
    )


def _http_denial(error: RuntimeActorCredentialError, *, action: str) -> HTTPException:
    actor = (
        error.actor.audit_payload() if error.actor else {"caller_kind": "unresolved"}
    )
    return HTTPException(
        status_code=403,
        detail={
            "authorized": False,
            "code": error.code,
            "action": action,
            "actor": actor,
            "message": error.message,
        },
    )


async def authorize_runtime_actor_request(
    db: Any,
    request: Any,
    *,
    action: str,
    project_id: str | None,
) -> RuntimeActorContext:
    """Authorize one route or knowledge mutation from the hidden credential."""

    try:
        token = _required_request_token(request, RUNTIME_ACTOR_HEADER, "sra")
        actor = await _actor_for_access(db, token)
        if project_id is None or actor.project_id != str(project_id):
            raise RuntimeActorCredentialError(
                "project_scope_mismatch",
                "Runtime actor project does not match the target project.",
                actor=actor,
            )
        await _current_actor(db, actor)
        if action in {"officer_message", "redispatch_livelock_ack"}:
            if actor.caller_kind != "officer":
                raise RuntimeActorCredentialError(
                    "officer_required",
                    "Only the commissioned background officer may perform this action.",
                    actor=actor,
                )
        elif action in _SENSITIVE_ACTIONS:
            if actor.caller_kind == "officer":
                pass
            elif actor.caller_kind in {"human", "conference"} and bool(
                SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY.get(
                    str(actor.project_role or ""), False
                )
            ):
                pass
            else:
                raise RuntimeActorCredentialError(
                    "project_role_denied",
                    "The current caller kind/project role may not perform this write.",
                    actor=actor,
                )
        else:
            raise RuntimeActorCredentialError(
                "unknown_action", "Runtime actor action is not recognized.", actor=actor
            )
        return actor
    except RuntimeActorCredentialError as error:
        await _audit_denial(db, request, error, action=action, project_id=project_id)
        raise _http_denial(error, action=action) from error


async def refresh_runtime_actor_request(db: Any, request: Any) -> RuntimeActorContext:
    """Mint a fresh short-lived access token from a stable opaque refresh grant."""

    action = "refresh"
    try:
        token = _required_request_token(request, RUNTIME_ACTOR_REFRESH_HEADER, "srr")
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, caller_kind, user_id, project_id, project_role,
                       thread_id, officer_incarnation, refresh_expires_at,
                       revoked_at
                  FROM runtime_actor_grants
                 WHERE refresh_token_hash = $1
                """,
                _digest(token),
            )
        if row is None:
            raise RuntimeActorCredentialError(
                "invalid_credential", "Runtime actor refresh is not recognized."
            )
        actor = _actor_from_row(row)
        now = datetime.now(timezone.utc)
        if row.get("revoked_at") is not None:
            raise RuntimeActorCredentialError(
                "revoked_credential", "Runtime actor grant was revoked.", actor=actor
            )
        if actor.refresh_expires_at is None or actor.refresh_expires_at <= now:
            raise RuntimeActorCredentialError(
                "expired_credential", "Runtime actor refresh has expired.", actor=actor
            )
        await _current_actor(db, actor)

        # The grant's lifetime is an IDLE timeout, not a fixed lease. Sliding it
        # forward on each successful refresh is what lets an agent that is
        # SPECIFIED to run indefinitely — a Centurion on its wake/sleep cycle —
        # keep working. The previous absolute wall made officer capability a
        # function of when its pod last started, the same nondeterminism the
        # POD_BOOTSTRAP_TTL comment above exists to remove: officer 6ce5bc4c
        # went silently unauthorized 24h after boot and burned ~52 turns being
        # refused (knowledge/issues/
        # officer_runtime_grant_expires_after_24h_and_dies_silently.md).
        #
        # Liveness, not trust, licenses the extension: reaching this line means
        # _current_actor re-derived authority from the live thread, and
        # derive_runtime_actor refuses an `ended` one. An active thread is
        # therefore the signal, so a thread-bound grant needs no absolute cap
        # while its thread lives. Authority itself is never cached — it is
        # recomputed on every access and every refresh — so the window bounds
        # only how long a stolen refresh token stays useful, and a stolen token
        # can only be used while the thread it names is still alive.
        #
        # Workers are deliberately excluded: they are job-scoped, _current_actor
        # short-circuits them without any thread check, and they have no
        # liveness to justify an open-ended credential.
        slides = actor.caller_kind != "worker" and bool(row["thread_id"])
        next_refresh_expires_at = (
            now + timedelta(seconds=REFRESH_TTL_SECONDS) if slides else None
        )
        async with db.acquire() as conn:
            async with conn.transaction():
                access_token, access_expires_at = await _insert_access_token(
                    conn, row["id"], now=now
                )
                if next_refresh_expires_at is not None:
                    await conn.execute(
                        "UPDATE runtime_actor_grants SET last_refreshed_at = now(), "
                        "refresh_expires_at = $2 WHERE id = $1",
                        row["id"],
                        next_refresh_expires_at,
                    )
                else:
                    await conn.execute(
                        "UPDATE runtime_actor_grants SET last_refreshed_at = now() "
                        "WHERE id = $1",
                        row["id"],
                    )
        actor.access_credential = access_token
        actor.refresh_credential = token
        actor.access_expires_at = access_expires_at
        if next_refresh_expires_at is not None:
            actor.refresh_expires_at = next_refresh_expires_at
        return actor
    except RuntimeActorCredentialError as error:
        await _audit_denial(
            db,
            request,
            error,
            action=action,
            project_id=error.actor.project_id if error.actor else None,
        )
        raise _http_denial(error, action=action) from error


async def slide_thread_grant_on_liveness(db: Any, thread_id: str) -> bool:
    """Slide a live thread's grants forward because the thread is still ALIVE.

    ``refresh_runtime_actor_request`` already slides the window, but only from
    inside a refresh — and the runtime only refreshes when it needs an access
    token for a PRIVILEGED call. That keys an IDLE timeout on MUTATIONS rather
    than on liveness, which inverts the risk: a busy officer is safe and a quiet
    one starves. Officer 6ce5bc4c woke every 10 minutes for 24h reading SITREPs,
    made no privileged call, never refreshed, and so walked into the wall while
    demonstrably alive (knowledge/issues/
    officer_runtime_grant_expires_after_24h_and_dies_silently.md).

    An agent heartbeat from a thread-bound runtime is exactly the liveness
    signal the refresh-path comment describes, so it licenses the same
    extension. This is deliberately server-side: persistent thread pods are
    bare pods no rollout ever updates, so an agent-side fix would never reach
    an officer that is already running.

    Nothing here widens the documented threat model. The window bounds only how
    long a *stolen* refresh token stays useful, and the refresh path already
    licenses "no absolute cap while its thread lives"; authority itself is never
    cached — it is recomputed on every access and every refresh.

    Returns True when at least one grant was slid.
    """

    now = datetime.now(timezone.utc)
    async with db.acquire() as conn:
        # Everything that must NOT be slid is excluded in SQL, so the common
        # case (a grant nowhere near its wall) costs one indexed read and no
        # write at all:
        #   * workers — job-scoped, no thread liveness to claim (the refresh
        #     path excludes them identically);
        #   * revoked grants;
        #   * ALREADY-EXPIRED grants: expiry stays terminal. This fix prevents
        #     reaching the wall, it never resurrects a credential past it;
        #   * grants still in the first half of their window — the throttle.
        rows = await conn.fetch(
            """
            SELECT id, caller_kind, user_id, project_id, project_role,
                   thread_id, officer_incarnation, refresh_expires_at,
                   revoked_at
              FROM runtime_actor_grants
             WHERE thread_id = $1::uuid
               AND revoked_at IS NULL
               AND caller_kind <> 'worker'
               AND refresh_expires_at > now()
               AND refresh_expires_at < now() + make_interval(secs => $2::int)
            """,
            str(thread_id),
            LIVENESS_SLIDE_BELOW_SECONDS,
        )
    if not rows:
        return False

    next_refresh_expires_at = now + timedelta(seconds=REFRESH_TTL_SECONDS)
    slid = False
    for row in rows:
        actor = _actor_from_row(row)
        try:
            # The SAME authority the refresh path applies before it slides:
            # _current_actor re-derives from durable state via
            # derive_runtime_actor, which refuses an `ended` thread and any
            # grant that no longer matches current post/membership. A heartbeat
            # is evidence of a live POD; this is what makes it evidence of a
            # live, still-authorized THREAD.
            await _current_actor(db, actor)
        except RuntimeActorCredentialError:
            # Fail closed: a grant the refresh path would refuse is a grant
            # this path must leave to expire.
            continue
        async with db.acquire() as conn:
            await conn.execute(
                """
                UPDATE runtime_actor_grants
                   SET last_refreshed_at = now(),
                       refresh_expires_at = $2
                 WHERE id = $1
                   AND revoked_at IS NULL
                   AND refresh_expires_at > now()
                """,
                row["id"],
                next_refresh_expires_at,
            )
        slid = True
    return slid
