"""OC-02/BP-09 shared runtime actor authorization contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from orchestrator.services import runtime_actor as service
from src.shared.runtime_actor import (
    RUNTIME_ACTOR_BOOTSTRAP_HEADER,
    RUNTIME_ACTOR_HEADER,
    SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY,
    RuntimeActorContext,
)


PROJECT_A = str(uuid4())
PROJECT_B = str(uuid4())
OFFICER_THREAD = str(uuid4())
SUCCESSOR_THREAD = str(uuid4())
USER_ID = str(uuid4())
ACCESS_TOKEN = "sra_" + ("A" * 43)


def _request(*values: str) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = Headers(
        raw=[
            (RUNTIME_ACTOR_HEADER.lower().encode(), value.encode()) for value in values
        ]
    )
    request.url.path = "/api/runtime-actors/authorize"
    request.client = None
    return request


def _audit_db() -> MagicMock:
    db = MagicMock()
    db.record_security_event = AsyncMock()
    return db


def _bootstrap_request(*values: str) -> MagicMock:
    request = MagicMock()
    request.headers = Headers(
        raw=[
            (RUNTIME_ACTOR_BOOTSTRAP_HEADER.lower().encode(), value.encode())
            for value in values
        ]
    )
    return request


def _actor(
    *,
    kind: str = "officer",
    project_id: str = PROJECT_A,
    role: str | None = "owner",
    thread_id: str | None = OFFICER_THREAD,
    incarnation: int | None = 0,
) -> RuntimeActorContext:
    return RuntimeActorContext(
        caller_kind=kind,
        project_id=project_id,
        project_role=role,
        thread_id=thread_id,
        officer_incarnation=incarnation,
        user_id=USER_ID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ((), "missing_credential"),
        (("bad-token",), "malformed_credential"),
        ((ACCESS_TOKEN, ACCESS_TOKEN), "duplicate_credential"),
        ((ACCESS_TOKEN, ""), "duplicate_credential"),
    ],
)
async def test_bad_actor_credentials_fail_closed_and_are_audited(headers, code):
    db = _audit_db()
    with pytest.raises(HTTPException) as exc:
        await service.authorize_runtime_actor_request(
            db,
            _request(*headers),
            action="officer_message",
            project_id=PROJECT_A,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == code
    db.record_security_event.assert_awaited_once()


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (("bad-bootstrap",), "malformed_bootstrap"),
        (("srb_" + ("A" * 43), ""), "duplicate_bootstrap"),
    ],
)
def test_malformed_or_duplicate_bootstrap_is_never_an_actor_credential(values, code):
    with pytest.raises(service.RuntimeActorCredentialError) as exc:
        service.request_bootstrap_token(_bootstrap_request(*values))
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_expired_actor_credential_fails_closed_and_audits_actor():
    db = _audit_db()
    actor = _actor()
    error = service.RuntimeActorCredentialError(
        "expired_credential", "expired", actor=actor
    )
    with patch.object(service, "_actor_for_access", AsyncMock(side_effect=error)):
        with pytest.raises(HTTPException) as exc:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="officer_message",
                project_id=PROJECT_A,
            )
    assert exc.value.detail["code"] == "expired_credential"
    assert exc.value.detail["actor"]["thread_id"] == OFFICER_THREAD
    db.record_security_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["officer_message", "machine_tags", "charter"])
async def test_project_a_credential_cannot_act_on_project_b(action):
    db = _audit_db()
    actor = _actor(project_id=PROJECT_A)
    current = AsyncMock(return_value=actor)
    with (
        patch.object(service, "_actor_for_access", AsyncMock(return_value=actor)),
        patch.object(service, "_current_actor", current),
    ):
        with pytest.raises(HTTPException) as exc:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action=action,
                project_id=PROJECT_B,
            )
    assert exc.value.detail["code"] == "project_scope_mismatch"
    current.assert_not_awaited()
    db.record_security_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "role", "allowed"),
    [
        ("worker", None, False),
        ("human", "viewer", False),
        ("human", "editor", False),
        ("human", "owner", True),
        ("human", "admin", True),
        ("officer", "owner", True),
        ("conference", "viewer", False),
        ("conference", "editor", False),
        ("conference", "owner", True),
        ("conference", "admin", True),
    ],
)
async def test_sensitive_knowledge_policy_matrix(kind, role, allowed):
    db = _audit_db()
    actor = _actor(
        kind=kind,
        role=role,
        thread_id=None if kind == "worker" else OFFICER_THREAD,
        incarnation=0 if kind == "officer" else None,
    )
    with (
        patch.object(service, "_actor_for_access", AsyncMock(return_value=actor)),
        patch.object(service, "_current_actor", AsyncMock(return_value=actor)),
    ):
        if allowed:
            result = await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="machine_tags",
                project_id=PROJECT_A,
            )
            assert result is actor
            db.record_security_event.assert_not_awaited()
        else:
            with pytest.raises(HTTPException) as exc:
                await service.authorize_runtime_actor_request(
                    db,
                    _request(ACCESS_TOKEN),
                    action="machine_tags",
                    project_id=PROJECT_A,
                )
            assert exc.value.detail["code"] == "project_role_denied"
            db.record_security_event.assert_awaited_once()


def test_named_human_policy_constant_documents_safe_default():
    assert dict(SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY) == {
        "admin": True,
        "owner": True,
        "editor": False,
        "viewer": False,
    }


class _CurrentStateDB:
    def __init__(self) -> None:
        self.post = {
            "thread_id": OFFICER_THREAD,
            "incarnations": [],
        }
        self.record_security_event = AsyncMock()

    async def get_thread(self, thread_id):
        return {
            "id": thread_id,
            "status": "active",
            "project_id": PROJECT_A,
            "user_id": USER_ID,
            "metadata": {},
        }

    async def list_thread_mounts(self, _thread_id):
        return [
            {
                "mount_kind": "project",
                "source_ref": PROJECT_A,
            }
        ]

    async def get_user(self, _user_id):
        return {"id": USER_ID, "is_admin": False}

    async def get_user_role_in_project(self, _project_id, _user_id):
        return "owner"

    async def get_project_officer(self, _project_id):
        return self.post


@pytest.mark.asyncio
async def test_recommission_invalidates_old_incarnation_immediately():
    db = _CurrentStateDB()
    old_actor = _actor()

    current = await service._current_actor(db, old_actor)
    assert current.caller_kind == "officer"

    # Decommission/recommission changes the authoritative post immediately;
    # the still-unexpired old credential no longer validates.
    db.post = {
        "thread_id": SUCCESSOR_THREAD,
        "incarnations": [{"thread_id": OFFICER_THREAD}],
    }
    with pytest.raises(service.RuntimeActorCredentialError) as exc:
        await service._current_actor(db, old_actor)
    assert exc.value.code == "runtime_not_current"

    with patch.object(service, "_actor_for_access", AsyncMock(return_value=old_actor)):
        with pytest.raises(HTTPException) as denied:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="machine_tags",
                project_id=PROJECT_A,
            )
    assert denied.value.detail["code"] == "runtime_not_current"
    assert denied.value.detail["actor"]["thread_id"] == OFFICER_THREAD
    db.record_security_event.assert_awaited_once()
