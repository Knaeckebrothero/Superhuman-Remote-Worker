"""U5-A HTTP contract for session-parent subagent ledgers.

The internal routes carry an exact session runtime authority rather than a
synthetic job identity.  The owner roster is deliberately separate and uses
the ordinary session ownership gate without accepting runtime credentials.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import orchestrator.main as m
from shared.persistent_input_delivery import InputDeliveryConflict


PARENT = uuid.UUID("10000000-0000-4000-8000-000000000001")
CHILD = uuid.UUID("20000000-0000-4000-8000-000000000002")
AGENT = uuid.UUID("30000000-0000-4000-8000-000000000003")
GENERATION = uuid.UUID("40000000-0000-4000-8000-000000000004")
NEXT_GENERATION = uuid.UUID("50000000-0000-4000-8000-000000000005")
ATTACH = uuid.UUID("60000000-0000-4000-8000-000000000006")
DELIVERY = uuid.UUID("70000000-0000-4000-8000-000000000007")
NOW = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)


def _pinned_authority(
    parent_thread_id: uuid.UUID = PARENT,
) -> m.AgentSessionSubagentAuthority:
    return m.AgentSessionSubagentAuthority(
        execution_lane="pinned",
        parent_thread_id=parent_thread_id,
        agent_id=AGENT,
        pod_uid="parent-pod-uid",
        session_runtime_generation=GENERATION,
        runtime_attach_token=ATTACH,
    )


def _stateless_authority() -> m.AgentSessionSubagentAuthority:
    return m.AgentSessionSubagentAuthority(
        execution_lane="stateless",
        parent_thread_id=PARENT,
        lease_token=17,
        executor_id="executor-1",
        executor_pod_uid="executor-pod-uid",
    )


def _row() -> dict:
    return {
        "id": CHILD,
        "kind": "subagent",
        "parent_job_id": None,
        "parent_thread_id": PARENT,
        "parent_tool_call_id": "call-1",
        "subagent_handle": "reviewer-0001",
        "subagent_type": "reviewer",
        "subagent_status": "running",
        "subagent_outcome": None,
        "subagent_error": None,
        "report_path": None,
        "status": "active",
        "total_turns": 0,
        "total_tokens": 0,
        "runtime_generation": GENERATION,
        "metadata": {
            "subagent": {
                "brief_description": "review this",
                "isolation": "shared",
                "write_policy": "none",
                "parent_iteration": 2,
                "fork": False,
                "run_in_background": False,
            }
        },
        "created_at": NOW,
        "last_activity": NOW,
        "ended_at": None,
    }


class TestSessionAuthorityBody:
    def test_pinned_and_stateless_shapes_are_disjoint_and_extra_fields_fail(self):
        assert _pinned_authority().execution_lane == "pinned"
        assert _stateless_authority().execution_lane == "stateless"
        with pytest.raises(ValidationError):
            m.AgentSessionSubagentAuthority(
                execution_lane="pinned",
                parent_thread_id=PARENT,
                agent_id=AGENT,
                pod_uid="pod",
                session_runtime_generation=GENERATION,
                runtime_attach_token=ATTACH,
                lease_token=9,
                executor_id="executor",
                executor_pod_uid="executor-pod",
            )
        with pytest.raises(ValidationError):
            m.AgentSessionSubagentAuthority(
                execution_lane="stateless",
                parent_thread_id=PARENT,
                lease_token=9,
                executor_id="executor",
            )
        with pytest.raises(ValidationError):
            m.AgentSessionSubagentAuthority(
                execution_lane="pinned",
                parent_thread_id=PARENT,
                agent_id=AGENT,
                pod_uid="pod",
                session_runtime_generation=GENERATION,
                runtime_attach_token=ATTACH,
                invented="not-authority",
            )


class TestSessionCreateRoute:
    @pytest.mark.asyncio
    async def test_internal_gate_precedes_the_database(self, monkeypatch):
        db = SimpleNamespace(create_session_subagent_thread=AsyncMock())
        gate = AsyncMock(side_effect=m.HTTPException(status_code=401, detail="no"))
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_internal", gate)
        body = m.AgentSessionSubagentCreateRequest(
            parent_authority=_pinned_authority(),
            handle="reviewer-0001",
            subagent_type="reviewer",
        )
        with pytest.raises(m.HTTPException) as excinfo:
            await m.agent_create_session_subagent_thread(
                SimpleNamespace(), str(PARENT), body
            )
        assert excinfo.value.status_code == 401
        db.create_session_subagent_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_every_create_field_and_exact_authority_reach_the_accessor(
        self, monkeypatch
    ):
        db = SimpleNamespace(
            create_session_subagent_thread=AsyncMock(
                return_value={
                    "thread_id": str(CHILD),
                    "runtime_generation": str(GENERATION),
                }
            )
        )
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_internal", AsyncMock(return_value=None))
        body = m.AgentSessionSubagentCreateRequest(
            parent_authority=_pinned_authority(),
            subagent_id=CHILD,
            handle="reviewer-0001",
            subagent_type="reviewer",
            parent_tool_call_id="call-1",
            parent_input_message_id=uuid.UUID("70000000-0000-4000-8000-000000000007"),
            parent_ai_message_id=uuid.UUID("80000000-0000-4000-8000-000000000008"),
            isolation="worktree",
            write_policy="owned_paths",
            owned_paths=["src/**"],
            brief_description="review this",
            parent_iteration=2,
            fork=True,
            run_in_background=True,
            initial_status="queued",
        )
        result = await m.agent_create_session_subagent_thread(
            SimpleNamespace(), str(PARENT), body
        )
        assert result == {
            "thread_id": str(CHILD),
            "runtime_generation": str(GENERATION),
            "status": "created",
        }
        db.create_session_subagent_thread.assert_awaited_once_with(
            parent_thread_id=str(PARENT),
            parent_authority=body.parent_authority.model_dump(mode="json"),
            thread_id=str(CHILD),
            handle="reviewer-0001",
            subagent_type="reviewer",
            parent_tool_call_id="call-1",
            parent_input_message_id="70000000-0000-4000-8000-000000000007",
            parent_ai_message_id="80000000-0000-4000-8000-000000000008",
            isolation="worktree",
            write_policy="owned_paths",
            owned_paths=["src/**"],
            brief_description="review this",
            parent_iteration=2,
            fork=True,
            run_in_background=True,
            initial_status="queued",
        )

    @pytest.mark.asyncio
    async def test_typed_authority_refusal_is_409(self, monkeypatch):
        db = SimpleNamespace(
            create_session_subagent_thread=AsyncMock(
                side_effect=m.SessionParentAuthorityRefused(
                    "stateless_background_unsupported"
                )
            )
        )
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_internal", AsyncMock(return_value=None))
        body = m.AgentSessionSubagentCreateRequest(
            parent_authority=_stateless_authority(),
            handle="reviewer-0001",
            subagent_type="reviewer",
            run_in_background=True,
            initial_status="queued",
        )
        with pytest.raises(m.HTTPException) as excinfo:
            await m.agent_create_session_subagent_thread(
                SimpleNamespace(), str(PARENT), body
            )
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == {
            "code": "session_parent_authority_refused",
            "reason": "stateless_background_unsupported",
        }


class TestSessionLifecycleRoutes:
    @pytest.mark.asyncio
    async def test_live_get_and_by_call_return_session_parent_rows(self, monkeypatch):
        row = _row()
        db = SimpleNamespace(
            list_live_session_subagent_threads=AsyncMock(return_value=[row]),
            get_session_subagent_thread=AsyncMock(return_value=row),
            get_session_subagent_thread_by_call=AsyncMock(return_value=row),
        )
        gate = AsyncMock(return_value=None)
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_internal", gate)
        query = m.AgentSessionSubagentQueryRequest(parent_authority=_pinned_authority())
        by_call = m.AgentSessionSubagentByCallRequest(
            parent_authority=_pinned_authority(), parent_tool_call_id="call-1"
        )

        live = await m.agent_list_live_session_subagent_threads(
            SimpleNamespace(), str(PARENT), query
        )
        exact = await m.agent_get_session_subagent_thread(
            SimpleNamespace(), str(PARENT), CHILD, query
        )
        replay = await m.agent_get_session_subagent_thread_by_call(
            SimpleNamespace(), str(PARENT), by_call
        )

        assert live["count"] == 1
        assert live["subagents"][0]["parent_job_id"] is None
        assert live["subagents"][0]["parent_thread_id"] == str(PARENT)
        assert exact["thread_id"] == str(CHILD)
        assert replay["parent_tool_call_id"] == "call-1"
        assert gate.await_count == 3

    @pytest.mark.asyncio
    async def test_reopen_returns_new_generation_and_stale_is_409(self, monkeypatch):
        db = SimpleNamespace(
            reopen_session_subagent_thread=AsyncMock(
                return_value={
                    "result": "reopened",
                    "thread_id": str(CHILD),
                    "runtime_generation": str(NEXT_GENERATION),
                }
            )
        )
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_internal", AsyncMock(return_value=None))
        body = m.AgentSessionSubagentReopenRequest(
            parent_authority=_pinned_authority(), runtime_generation=GENERATION
        )
        result = await m.agent_reopen_session_subagent_thread(
            SimpleNamespace(), str(PARENT), CHILD, body
        )
        assert result["runtime_generation"] == str(NEXT_GENERATION)
        db.reopen_session_subagent_thread.assert_awaited_once_with(
            parent_thread_id=str(PARENT),
            thread_id=str(CHILD),
            runtime_generation=str(GENERATION),
            parent_authority=body.parent_authority.model_dump(mode="json"),
        )

        db.reopen_session_subagent_thread.return_value = {"result": "stale"}
        with pytest.raises(m.HTTPException) as excinfo:
            await m.agent_reopen_session_subagent_thread(
                SimpleNamespace(), str(PARENT), CHILD, body
            )
        assert excinfo.value.status_code == 409

    @pytest.mark.asyncio
    async def test_terminal_passes_delivery_fields_and_conflict_is_typed_409(
        self, monkeypatch
    ):
        applied = {
            "result": "applied",
            "thread_id": str(CHILD),
            "runtime_generation": str(GENERATION),
            "delivery_id": str(DELIVERY),
            "delivery_state": "owned",
        }
        db = SimpleNamespace(
            terminalize_session_subagent_thread=AsyncMock(return_value=applied)
        )
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_internal", AsyncMock(return_value=None))
        body = m.AgentSessionSubagentTerminalRequest(
            parent_authority=_pinned_authority(),
            runtime_generation=GENERATION,
            subagent_status="completed",
            delivery_id=DELIVERY,
            message="review complete",
            outcome="completed",
            turns=2,
            tokens=50,
            report_path=".subagents/reviewer-0001/report.md",
            foreground_orphan_recovery=True,
        )
        assert (
            await m.agent_terminalize_session_subagent_thread(
                SimpleNamespace(), str(PARENT), CHILD, body
            )
            == applied
        )
        kwargs = db.terminalize_session_subagent_thread.await_args.kwargs
        assert kwargs["parent_thread_id"] == str(PARENT)
        assert kwargs["thread_id"] == str(CHILD)
        assert kwargs["runtime_generation"] == str(GENERATION)
        assert kwargs["delivery_id"] == str(DELIVERY)
        assert kwargs["message"] == "review complete"
        assert kwargs["turns"] == 2 and kwargs["tokens"] == 50
        assert kwargs["foreground_orphan_recovery"] is True

        db.terminalize_session_subagent_thread.side_effect = InputDeliveryConflict(
            "different transcript"
        )
        with pytest.raises(m.HTTPException) as excinfo:
            await m.agent_terminalize_session_subagent_thread(
                SimpleNamespace(), str(PARENT), CHILD, body
            )
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["code"] == "subagent_delivery_conflict"


class TestOwnerRosterRoute:
    @pytest.mark.asyncio
    async def test_owner_gate_precedes_roster_and_payload_has_parent(self, monkeypatch):
        db = SimpleNamespace(
            list_session_subagent_threads=AsyncMock(return_value=[_row()])
        )
        guard = AsyncMock(
            return_value=({"id": "owner"}, {"id": PARENT, "kind": "session"})
        )
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_thread_owner", guard)
        request = SimpleNamespace()
        result = await m.get_session_subagents(request, str(PARENT))
        assert result["parent_thread_id"] == str(PARENT)
        assert result["count"] == 1
        assert result["subagents"][0]["parent_thread_id"] == str(PARENT)
        guard.assert_awaited_once_with(request, db, str(PARENT))

    @pytest.mark.asyncio
    async def test_denial_and_non_session_never_read_children(self, monkeypatch):
        db = SimpleNamespace(list_session_subagent_threads=AsyncMock())
        guard = AsyncMock(side_effect=m.HTTPException(status_code=403, detail="no"))
        monkeypatch.setattr(m, "postgres_db", db)
        monkeypatch.setattr(m, "require_thread_owner", guard)
        with pytest.raises(m.HTTPException) as excinfo:
            await m.get_session_subagents(SimpleNamespace(), str(PARENT))
        assert excinfo.value.status_code == 403
        db.list_session_subagent_threads.assert_not_awaited()

        guard.side_effect = None
        guard.return_value = ({"id": "owner"}, {"id": PARENT, "kind": "subagent"})
        with pytest.raises(m.HTTPException) as excinfo:
            await m.get_session_subagents(SimpleNamespace(), str(PARENT))
        assert excinfo.value.status_code == 404
        db.list_session_subagent_threads.assert_not_awaited()
