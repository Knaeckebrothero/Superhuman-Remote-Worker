"""R1.B06 lane B — thread connector authorization, revalidation, resolution.

Every branch here is a refusal branch or the reason one is *not* taken. The
403 body is deliberately generic (no per-id reason) so the endpoint cannot be
used to enumerate connectors, and an acknowledged id is narrowed out only
while it is still denied so a recreated connector returns with no repair step.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from orchestrator.services import datasource_policy
from orchestrator.services import thread_datasource_authorization as tda
from orchestrator.services.datasource_policy import (
    DatasourceUnavailableError,
    DatasourceWorkspaceTierError,
    ItemVerdict,
)

D1 = "11111111-1111-4111-8111-111111111111"
D2 = "22222222-2222-4222-8222-222222222222"
P1 = "33333333-3333-4333-8333-333333333333"
OWNER = {"id": "u-1"}
UNAVAILABLE = "One or more selected connectors are unavailable"


def _deps(**store: Any) -> tda.ThreadDatasourceAuthorizationDependencies:
    defaults: dict[str, Any] = {
        "get_user": AsyncMock(return_value=OWNER),
        "resolve_datasources_for_thread": AsyncMock(return_value=[]),
    }
    defaults.update(store)
    return tda.ThreadDatasourceAuthorizationDependencies(
        store=SimpleNamespace(**defaults),
        thread_project_ids=AsyncMock(return_value=[P1]),
    )


class TestAuthorizeSelection:
    @pytest.mark.asyncio
    async def test_owner_id_is_derived_from_the_actor_when_not_given(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def _authorize(db, actor, owner_id, ids, projects, backend, **kw):
            seen.update(owner_id=owner_id, backend=backend, kw=kw)
            return list(ids), {}

        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", _authorize
        )
        selected, revisions = await tda.authorize_thread_datasource_selection(
            {"id": "u-9"}, [D1], workspace_backend="sandbox", dependencies=_deps()
        )
        assert (selected, revisions) == ([D1], {})
        assert seen["owner_id"] == "u-9"
        assert seen["kw"]["allow_admin_explicit_override"] is True

    @pytest.mark.asyncio
    async def test_unavailable_becomes_the_generic_403(self, monkeypatch):
        async def _boom(*_a, **_kw):
            raise DatasourceUnavailableError()

        monkeypatch.setattr(datasource_policy, "authorize_datasource_selection", _boom)
        with pytest.raises(HTTPException) as exc:
            await tda.authorize_thread_datasource_selection(
                OWNER, [D1], workspace_backend=None, dependencies=_deps()
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == UNAVAILABLE

    @pytest.mark.asyncio
    async def test_workspace_tier_violation_keeps_its_own_400_sentence(
        self, monkeypatch
    ):
        async def _boom(*_a, **_kw):
            raise DatasourceWorkspaceTierError("repository needs a shell workspace")

        monkeypatch.setattr(datasource_policy, "authorize_datasource_selection", _boom)
        with pytest.raises(HTTPException) as exc:
            await tda.authorize_thread_datasource_selection(
                OWNER, [D1], workspace_backend="none", dependencies=_deps()
            )
        assert exc.value.status_code == 400
        assert exc.value.detail == "repository needs a shell workspace"

    @pytest.mark.asyncio
    async def test_ids_wrapper_drops_the_revision_snapshot(self, monkeypatch):
        async def _authorize(*_a, **_kw):
            return [D1], {D1: 3}

        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", _authorize
        )
        assert await tda.authorize_thread_datasource_ids(
            OWNER, [D1], workspace_backend=None, dependencies=_deps()
        ) == [D1]


class TestStripStillDeniedAck:
    @pytest.mark.asyncio
    async def test_no_ack_map_means_no_classification_at_all(self, monkeypatch):
        classify = AsyncMock()
        monkeypatch.setattr(tda, "classify_datasource_selection", classify)
        selected = await tda.strip_still_denied_ack(
            {"metadata": {}},
            [D1],
            actor=OWNER,
            effective_work_owner_id="u-1",
            project_ids=[],
            dependencies=_deps(),
        )
        assert selected == [D1]
        classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_acknowledged_and_still_denied_is_dropped(self, monkeypatch):
        monkeypatch.setattr(
            tda,
            "classify_datasource_selection",
            AsyncMock(return_value=([ItemVerdict(D1, True, "revoked")], {})),
        )
        assert (
            await tda.strip_still_denied_ack(
                {"metadata": {"config_drift_ack": {f"connector:{D1}": {}}}},
                [D1],
                actor=OWNER,
                effective_work_owner_id="u-1",
                project_ids=[],
                dependencies=_deps(),
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_acknowledged_but_recreated_returns_automatically(self, monkeypatch):
        monkeypatch.setattr(
            tda,
            "classify_datasource_selection",
            AsyncMock(return_value=([ItemVerdict(D1, False, None)], {D1: 1})),
        )
        assert await tda.strip_still_denied_ack(
            {"metadata": {"config_drift_ack": {f"connector:{D1}": {}}}},
            [D1],
            actor=OWNER,
            effective_work_owner_id="u-1",
            project_ids=[],
            dependencies=_deps(),
        ) == [D1]

    @pytest.mark.asyncio
    async def test_denied_but_never_acknowledged_stays_for_the_authorizer(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            tda,
            "classify_datasource_selection",
            AsyncMock(
                return_value=(
                    [
                        ItemVerdict(D1, True, "revoked"),
                        ItemVerdict(D2, True, "deleted"),
                    ],
                    {},
                )
            ),
        )
        assert await tda.strip_still_denied_ack(
            {"metadata": {"config_drift_ack": {f"connector:{D1}": {}}}},
            [D1, D2],
            actor=OWNER,
            effective_work_owner_id="u-1",
            project_ids=[],
            dependencies=_deps(),
        ) == [D2]

    @pytest.mark.asyncio
    async def test_classification_failure_is_a_403_not_a_500(self, monkeypatch):
        async def _boom(*_a, **_kw):
            raise DatasourceUnavailableError()

        monkeypatch.setattr(tda, "classify_datasource_selection", _boom)
        with pytest.raises(HTTPException) as exc:
            await tda.strip_still_denied_ack(
                {"metadata": {"config_drift_ack": {f"connector:{D1}": {}}}},
                [D1],
                actor=OWNER,
                effective_work_owner_id="u-1",
                project_ids=[],
                dependencies=_deps(),
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == UNAVAILABLE


class TestRevalidate:
    @pytest.mark.asyncio
    async def test_empty_selection_short_circuits_before_any_read(self):
        deps = _deps()
        assert await tda.revalidate_thread_datasource_selection(
            {"id": "t", "user_id": "u-1"}, [], dependencies=deps
        ) == ([], {})
        deps.thread_project_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_project_scope_defaults_to_the_thread_mounts(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def _authorize(db, actor, owner_id, ids, projects, backend, **kw):
            seen["projects"] = projects
            seen["backend"] = backend
            return list(ids), {}

        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", _authorize
        )
        deps = _deps()
        await tda.revalidate_thread_datasource_selection(
            {"id": "t", "user_id": "u-1"}, [D1], dependencies=deps
        )
        deps.thread_project_ids.assert_awaited_once_with("t")
        assert seen["projects"] == [P1]
        # Revalidation is an access check only: the create-time lite/repository
        # tier rule must not be applied retroactively.
        assert seen["backend"] is None

    @pytest.mark.asyncio
    async def test_userless_thread_authorizes_with_trusted_inheritance(
        self, monkeypatch
    ):
        seen: dict[str, Any] = {}

        async def _authorize(db, actor, owner_id, ids, projects, backend, **kw):
            seen.update(actor=actor, owner_id=owner_id, kw=kw)
            return list(ids), {}

        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", _authorize
        )
        deps = _deps(get_user=AsyncMock())
        selected, _ = await tda.revalidate_thread_datasource_selection(
            {"id": "t", "user_id": None}, [D1, D1], dependencies=deps
        )
        assert selected == [D1]
        assert seen["actor"] is None and seen["owner_id"] is None
        assert seen["kw"]["trusted_system_inheritance"] is True
        deps.store.get_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vanished_owner_fails_closed_with_the_generic_403(self):
        deps = _deps(get_user=AsyncMock(return_value=None))
        with pytest.raises(HTTPException) as exc:
            await tda.revalidate_thread_datasource_selection(
                {"id": "t", "user_id": "gone"}, [D1], dependencies=deps
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == UNAVAILABLE

    @pytest.mark.asyncio
    async def test_everything_stripped_returns_an_empty_snapshot(self, monkeypatch):
        monkeypatch.setattr(
            tda,
            "classify_datasource_selection",
            AsyncMock(return_value=([ItemVerdict(D1, True, "revoked")], {})),
        )
        authorize = AsyncMock()
        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", authorize
        )
        assert await tda.revalidate_thread_datasource_selection(
            {
                "id": "t",
                "user_id": "u-1",
                "metadata": {"config_drift_ack": {f"connector:{D1}": {}}},
            },
            [D1],
            dependencies=_deps(),
        ) == ([], {})
        authorize.assert_not_awaited()


class TestResolveAuthorized:
    @pytest.mark.asyncio
    async def test_a_silent_reduction_fails_closed(self, monkeypatch):
        async def _authorize(*_a, **_kw):
            return [D1, D2], {D1: 1, D2: 1}

        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", _authorize
        )
        deps = _deps(
            resolve_datasources_for_thread=AsyncMock(
                return_value=[{"id": D1, "policy_revision": 1}]
            )
        )
        with pytest.raises(HTTPException) as exc:
            await tda.resolve_authorized_thread_datasources(
                {"id": "t", "user_id": "u-1"},
                [D1, D2],
                target_project_ids=[P1],
                dependencies=deps,
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == UNAVAILABLE

    @pytest.mark.asyncio
    async def test_exact_resolution_passes_through(self, monkeypatch):
        async def _authorize(*_a, **_kw):
            return [D1], {D1: 4}

        monkeypatch.setattr(
            datasource_policy, "authorize_datasource_selection", _authorize
        )
        rows = [{"id": D1, "policy_revision": 4}]
        deps = _deps(resolve_datasources_for_thread=AsyncMock(return_value=rows))
        assert (
            await tda.resolve_authorized_thread_datasources(
                {"id": "t", "user_id": "u-1"},
                [D1],
                target_project_ids=[P1],
                dependencies=deps,
            )
            == rows
        )
