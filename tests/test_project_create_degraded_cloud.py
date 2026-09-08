"""Characterization: ``POST /api/projects`` on an installation with no cloud.

R1.B01 recorded that a minimal profile — no Gitea, no main cloud — answers
``POST /api/projects`` with a **500 after the project and owner-membership rows
are already written**, and explicitly refused to count that as a passing
project-create gate. R1.B03 owns the endpoint, so it owns the defect.

This file pins the behaviour that exists *today*, at ``develop`` ``2b5f0e23a``,
before any B03 extraction moves the code. It is deliberately written against
the current owner (``orchestrator.main``) so it can be re-pointed at the
extracted owner and keep meaning the same thing on both sides of the move.

The failure is not the forge. With Gitea disabled the endpoint simply skips
knowledge-repo provisioning — that branch is already guarded by
``gitea_client.is_initialized``. The 500 comes from the *cloud* step:
``_ensure_project_cloud_resources`` calls ``main_cloud_router.for_owner()``
outside its own ``try``, and that raises ``FeatureNotAvailable`` when no active
backend instance has been bound. Every other remote effect in that helper is
already wrapped and logs instead of raising, and the sibling
``for_project_optional`` path returns the project unchanged rather than failing
— so this one unguarded call is out of step with its own module.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from orchestrator import main
from orchestrator.schemas.projects import ProjectCreate
from orchestrator.services.cloud.errors import FeatureNotAvailable


OWNER_ID = str(uuid4())
PROJECT_ID = str(uuid4())


def _owner() -> dict[str, Any]:
    return {"id": OWNER_ID, "email": "owner@example.test", "is_admin": False}


def _store() -> MagicMock:
    """A store that records the writes ``create_project`` performs."""
    db = MagicMock()
    row = {
        "id": PROJECT_ID,
        "name": "Minimal profile project",
        "description": None,
        "goal": None,
        "is_default": False,
        "main_cloud_backend": None,
        "main_cloud_folder_handle": None,
        "nextcloud_folder_id": None,
    }
    db.create_project = AsyncMock(return_value=dict(row))
    db.add_project_member = AsyncMock(return_value={"role": "owner"})
    db.get_project = AsyncMock(return_value=dict(row))
    db.get_project_members = AsyncMock(return_value=[{"user_id": OWNER_ID}])
    db.get_user = AsyncMock(return_value=_owner())
    db.update_project = AsyncMock(return_value=dict(row))
    return db


def _uninitialised_cloud_router() -> MagicMock:
    """A router with no bound active backend instance.

    This is the exact production shape when no cloud is configured: the active
    backend is never bound, so ``for_owner`` refuses rather than guessing an
    installation. Refusing is correct; the caller's handling of the refusal is
    what this file is about.
    """
    router = MagicMock()
    router.for_owner.side_effect = FeatureNotAvailable(
        "durable active backend-instance authority", backend="nextcloud"
    )
    router.for_project_optional.return_value = None
    return router


def _patched(db: MagicMock, cloud: MagicMock) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        patch(
            "orchestrator.main.require_approved_user", AsyncMock(return_value=_owner())
        )
    )
    stack.enter_context(patch("orchestrator.main.postgres_db", db))
    stack.enter_context(patch("orchestrator.main.main_cloud_router", cloud))
    # The forge is off in the minimal profile, exactly as `values-e2e.yaml`
    # ships it: `is_initialized` is False, so no knowledge repo is provisioned.
    stack.enter_context(
        patch("orchestrator.main.gitea_client", SimpleNamespace(is_initialized=False))
    )
    stack.enter_context(
        patch(
            "orchestrator.main.keycloak_groups", SimpleNamespace(is_initialized=False)
        )
    )
    return stack


def _body() -> ProjectCreate:
    return ProjectCreate(name="Minimal profile project", user_id=OWNER_ID)


@pytest.mark.asyncio
async def test_create_project_500s_after_writing_rows_when_no_cloud_is_configured() -> (
    None
):
    """The recorded defect, pinned exactly.

    Both rows are written and then the request fails, so the caller sees a 500
    while the project and its owner membership exist. Nothing rolls them back:
    the two writes are separate statements, not one transaction.
    """
    db = _store()
    cloud = _uninitialised_cloud_router()
    request = MagicMock()

    with _patched(db, cloud):
        with pytest.raises(HTTPException) as raised:
            await main.create_project(_body(), request)

    assert raised.value.status_code == 500
    # The message is the stringified FeatureNotAvailable, which is how we know
    # the 500 is the cloud step and not the forge.
    assert "backend-instance authority" in str(raised.value.detail)

    # ...and the rows the caller cannot see are already there.
    db.create_project.assert_awaited_once()
    db.add_project_member.assert_awaited_once()
    assert db.add_project_member.await_args.kwargs["role"] == "owner"
    assert db.add_project_member.await_args.kwargs["project_id"] == PROJECT_ID


@pytest.mark.asyncio
async def test_disabled_forge_alone_does_not_fail_project_create() -> None:
    """Isolate the forge: with a working cloud, a disabled Gitea is fine.

    This is the control for the test above. It is what makes "the 500 is the
    cloud step, not the forge" a measured claim rather than a reading of the
    source.
    """
    db = _store()
    cloud = MagicMock()
    backend = MagicMock()
    backend.is_initialized = False
    cloud.for_owner.return_value = backend
    cloud.for_project_optional.return_value = backend
    request = MagicMock()

    with _patched(db, cloud):
        project = await main.create_project(_body(), request)

    assert project["id"] == PROJECT_ID
    db.create_project.assert_awaited_once()
    db.add_project_member.assert_awaited_once()
