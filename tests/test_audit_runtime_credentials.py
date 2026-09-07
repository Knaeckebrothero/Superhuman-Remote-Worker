"""Synthetic runtime bearer values through actual audit write/read projections."""

from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.core.archiver import LLMArchiver
from agent.database.audit_writer import SyncAuditWriter
from orchestrator.database.audit_store import AuditStore, _audit_row_to_doc
from shared.runtime_actor import RuntimeActorContext

ACCESS = "synthetic-audit-access-not-a-real-credential"
REFRESH = "synthetic-audit-refresh-not-a-real-credential"
JOB = "00000000-0000-0000-0000-000000000001"
IDENTITY = {
    "caller_kind": "worker",
    "project_id": "project-synthetic",
    "project_role": "viewer",
    "thread_id": "thread-synthetic",
    "officer_incarnation": None,
    "user_id": "user-synthetic",
}
ACTOR = {
    **IDENTITY,
    "access_credential": ACCESS,
    "refresh_credential": REFRESH,
    "access_expires_at": "2026-09-06T00:00:00+00:00",
    "refresh_expires_at": "2026-09-07T00:00:00+00:00",
}


def metadata(actor=None):
    return {
        "runtime_actor": deepcopy(ACTOR if actor is None else actor),
        "description": "keep",
        "nested": {"marker": [1, 2]},
    }


def assert_identity_only(value):
    assert value["runtime_actor"] == IDENTITY
    assert ACCESS not in repr(value) and REFRESH not in repr(value)
    assert value["description"] == "keep"


class CaptureWriter:
    def __init__(self):
        self.rows = []

    def ensure_ready(self):
        return True

    def insert_llm_request(self, row):
        self.rows.append(row)
        return 17

    def insert_audit_pre(self, row):
        self.rows.append(row)
        return 18

    def insert_chat_entry(self, row):
        pass


@pytest.mark.parametrize("operation", ("llm", "audit", "error"))
@pytest.mark.parametrize("actor_object", (False, True))
def test_archiver_metadata_keeps_identity_not_credentials(operation, actor_object):
    supplied = metadata()
    if actor_object:
        supplied["runtime_actor"] = RuntimeActorContext.from_payload(ACTOR)
    before = deepcopy(supplied)
    supplied["resolved_config"] = {"large": "boot-only"}
    before["resolved_config"] = deepcopy(supplied["resolved_config"])
    writer = CaptureWriter()
    archiver = LLMArchiver(writer=writer)
    if operation == "llm":
        result = archiver.archive(
            JOB,
            "worker",
            [HumanMessage("input")],
            AIMessage("output"),
            "synthetic-model",
            metadata=supplied,
            auxiliary_metadata=supplied,
        )
        assert_identity_only(writer.rows[0]["metadata"])
        assert "resolved_config" not in writer.rows[0]["metadata"]
        assert_identity_only(writer.rows[0]["auxiliary_metadata"])
    elif operation == "audit":
        result = archiver.audit_step(
            JOB,
            "worker",
            "routing",
            "process",
            1,
            data={"decision": "keep"},
            metadata=supplied,
        )
        assert_identity_only(writer.rows[0]["metadata"])
        assert writer.rows[0]["payload"] == {"decision": "keep"}
        assert "resolved_config" not in writer.rows[0]["metadata"]
    else:
        result = archiver.archive_error(
            JOB,
            "worker",
            [HumanMessage("input")],
            "synthetic-model",
            "synthetic failure",
            "ValueError",
            auxiliary_metadata=supplied,
        )
        assert_identity_only(writer.rows[0]["auxiliary_metadata"])
        assert writer.rows[0]["metadata"]["error"] == {
            "type": "ValueError",
            "message": "synthetic failure",
        }
    assert result in (17, 18)
    assert supplied == before
    if actor_object:
        assert supplied["runtime_actor"].access_credential == ACCESS
        assert supplied["runtime_actor"].refresh_credential == REFRESH


def pool_for(conn):
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=conn)
    manager.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = manager
    return pool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,index", (("llm", 9), ("auxiliary", 10), ("audit", 10))
)
async def test_direct_writer_metadata_sql_argument_is_redacted(operation, index):
    supplied = metadata()
    before = deepcopy(supplied)
    row = {
        "job_id": JOB,
        "model": "synthetic-model",
        "timestamp": datetime(2026, 9, 5, tzinfo=timezone.utc),
        "request": {"messages": []},
        "response": {},
        "step_type": "routing",
        "metadata": supplied,
        "auxiliary_metadata": supplied,
    }
    row_before = deepcopy(row)
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=23)
    writer = SyncAuditWriter("postgresql://synthetic/unused")
    writer._pool = pool_for(conn)
    if operation == "audit":
        result = await writer._insert_audit_pre(row)
    else:
        result = await writer._insert_llm_request(row)
    assert result == 23
    arguments = conn.fetchval.call_args.args[1:]
    assert_identity_only(arguments[index])
    assert supplied == before and row == row_before


def audit_row(*, payload=None):
    return {
        "id": 11,
        "job_id": UUID(JOB),
        "agent_type": "worker",
        "iteration": 1,
        "step_type": "routing",
        "node_name": "process",
        "step_number": 1,
        "timestamp": "2026-09-05T00:00:00Z",
        "phase": "strategic",
        "phase_number": 1,
        "latency_ms": None,
        "metadata": metadata(),
        "payload": payload or {},
    }


@pytest.mark.parametrize("shadow", (False, True))
def test_audit_detail_read_redacts_after_existing_payload_shadow(shadow):
    row = audit_row(
        payload={
            "metadata": {**metadata(), "description": "shadow"},
            "phase": {"keep": True},
        }
        if shadow
        else {"decision": "keep"}
    )
    before = deepcopy(row)
    result = _audit_row_to_doc(row)
    assert result["metadata"]["runtime_actor"] == IDENTITY
    assert ACCESS not in repr(result) and REFRESH not in repr(result)
    assert result["metadata"]["description"] == ("shadow" if shadow else "keep")
    if shadow:
        assert result["phase"] == {"keep": True}
    assert row == before


@pytest.mark.asyncio
async def test_llm_detail_read_redacts_both_existing_metadata_columns():
    row = {
        "id": 12,
        "job_id": UUID(JOB),
        "agent_type": "worker",
        "call_type": "main",
        "timestamp": "2026-09-05T00:00:00Z",
        "model": "synthetic-model",
        "request": {"messages": []},
        "response": {"content": "keep"},
        "metrics": {},
        "iteration": 1,
        "latency_ms": 10,
        "metadata": metadata(),
        "auxiliary_metadata": metadata(),
    }
    before = deepcopy(row)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    reader = AuditStore("postgresql://synthetic/unused")
    reader._pool = pool_for(conn)
    reader._available = True
    result = await reader.get_request(12)
    assert_identity_only(result["metadata"])
    assert_identity_only(result["auxiliary_metadata"])
    assert result["request"] == row["request"] and result["response"] == row["response"]
    assert row == before


@pytest.mark.parametrize(
    "value", (None, {}, {"description": "keep"}, {"runtime_actor": None})
)
def test_projector_missing_or_null_actor_preserves_existing_object(value):
    from shared.runtime_actor import audit_metadata_payload

    assert audit_metadata_payload(value) is value


@pytest.mark.parametrize(
    "actor",
    (
        ACCESS,
        [ACTOR],
        {"caller_kind": "invalid", "access_credential": ACCESS},
        {"access_credential": ACCESS},
    ),
)
def test_projector_malformed_actor_is_null_without_touching_other_metadata(actor):
    from shared.runtime_actor import audit_metadata_payload

    source = {"runtime_actor": deepcopy(actor), "description": "keep"}
    before = deepcopy(source)
    assert audit_metadata_payload(source) == {
        "runtime_actor": None,
        "description": "keep",
    }
    assert source == before


@pytest.mark.parametrize("value", (ACCESS, [ACTOR], 12))
def test_projector_malformed_metadata_is_omitted(value):
    from shared.runtime_actor import audit_metadata_payload

    assert audit_metadata_payload(value) is None


def test_projector_uses_existing_identity_contract_and_preserves_other_namespaces():
    from shared.runtime_actor import audit_metadata_payload

    source = {
        **metadata(),
        "business": {"access_credential": "ordinary-business-field"},
        "runtime_actor_label": "unchanged",
    }
    before = deepcopy(source)
    result = audit_metadata_payload(source)
    assert_identity_only(result)
    assert result["business"] is source["business"]
    assert result["runtime_actor_label"] == "unchanged"
    assert audit_metadata_payload(result) is result
    assert source == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seam",
    (
        "archiver",
        "error-auxiliary",
        "writer-audit",
        "writer-llm",
        "reader-audit-shadow",
        "reader-llm",
    ),
)
async def test_malformed_actor_is_not_serialized_through_actual_seams(seam):
    supplied = {"runtime_actor": ACCESS, "description": "keep"}
    before = deepcopy(supplied)
    expected = {"runtime_actor": None, "description": "keep"}
    if seam in ("archiver", "error-auxiliary"):
        writer = CaptureWriter()
        archiver = LLMArchiver(writer=writer)
        if seam == "archiver":
            assert (
                archiver.audit_step(
                    JOB, "worker", "routing", "process", 1, metadata=supplied
                )
                == 18
            )
            assert writer.rows[0]["metadata"] == expected
        else:
            assert (
                archiver.archive_error(
                    JOB,
                    "worker",
                    [],
                    "synthetic-model",
                    "failure",
                    "ValueError",
                    auxiliary_metadata=supplied,
                )
                == 17
            )
            assert writer.rows[0]["auxiliary_metadata"] == expected
    elif seam.startswith("writer"):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=23)
        writer = SyncAuditWriter("postgresql://synthetic/unused")
        writer._pool = pool_for(conn)
        row = {
            "job_id": JOB,
            "step_type": "routing",
            "timestamp": "synthetic",
            "model": "synthetic-model",
            "request": {},
            "response": {},
            "metadata": supplied,
            "auxiliary_metadata": supplied,
        }
        if seam == "writer-audit":
            await writer._insert_audit_pre(row)
            assert conn.fetchval.call_args.args[11] == expected
        else:
            await writer._insert_llm_request(row)
            assert conn.fetchval.call_args.args[10] == expected
            assert conn.fetchval.call_args.args[11] == expected
    elif seam == "reader-audit-shadow":
        row = audit_row(payload={"metadata": supplied})
        assert _audit_row_to_doc(row)["metadata"] == expected
        assert row["payload"]["metadata"] is supplied
    else:
        row = {
            "id": 12,
            "job_id": UUID(JOB),
            "agent_type": "worker",
            "timestamp": "synthetic",
            "model": "synthetic-model",
            "call_type": "main",
            "request": {},
            "response": {},
            "metrics": {},
            "iteration": None,
            "latency_ms": None,
            "metadata": supplied,
            "auxiliary_metadata": supplied,
        }
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=row)
        reader = AuditStore("postgresql://synthetic/unused")
        reader._pool = pool_for(conn)
        reader._available = True
        result = await reader.get_request(12)
        assert result["metadata"] == result["auxiliary_metadata"] == expected
    assert supplied == before


def test_projector_preserves_live_actor_object_and_snapshots_its_identity():
    from shared.runtime_actor import audit_metadata_payload

    actor = RuntimeActorContext.from_payload(ACTOR)
    source = {"runtime_actor": actor, "description": "keep"}
    result = audit_metadata_payload(source)
    assert source["runtime_actor"] is actor
    assert actor.to_payload() == ACTOR
    assert_identity_only(result)
    actor.access_credential = "synthetic-later-access"
    actor.refresh_credential = "synthetic-later-refresh"
    actor.project_role = "editor"
    assert_identity_only(result)


@pytest.mark.parametrize(
    "field,value",
    (
        ("user_id", {"access_credential": ACCESS}),
        ("project_id", [REFRESH]),
        ("officer_incarnation", float("inf")),
        ("officer_incarnation", True),
    ),
)
def test_projector_refuses_nonscalar_or_invalid_identity_without_coercion(field, value):
    from shared.runtime_actor import audit_metadata_payload

    actor = {**ACTOR, field: value}
    source = {"runtime_actor": actor, "description": "keep"}
    assert audit_metadata_payload(source) == {
        "runtime_actor": None,
        "description": "keep",
    }
    assert source["runtime_actor"] is actor


def test_projector_never_parses_expiry_or_stringifies_discarded_credentials():
    from shared.runtime_actor import audit_metadata_payload

    class OpaqueCredential:
        def __str__(self):
            raise AssertionError("credential was stringified")

        __repr__ = __str__

    actor = {
        **ACTOR,
        "access_expires_at": "0001-01-01T00:00:00+14:00",
        "refresh_expires_at": "9999-12-31T23:59:59-14:00",
        "access_credential": OpaqueCredential(),
        "refresh_credential": OpaqueCredential(),
    }
    source = {"runtime_actor": actor, "description": "keep"}
    assert_identity_only(audit_metadata_payload(source))
    assert source["runtime_actor"] is actor


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("archiver", "writer-pre", "writer-post"))
async def test_known_audit_payload_metadata_shadow_is_redacted_before_persistence(seam):
    payload = {
        "metadata": metadata(),
        "phase": {"keep": True},
        "tool": {"arguments": {"access_credential": "ordinary-business-field"}},
    }
    before = deepcopy(payload)
    if seam == "archiver":
        writer = CaptureWriter()
        assert (
            LLMArchiver(writer=writer).audit_step(
                JOB, "worker", "routing", "process", 1, data=payload
            )
            == 18
        )
        persisted = writer.rows[0]["payload"]
    else:
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=23)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        writer = SyncAuditWriter("postgresql://synthetic/unused")
        writer._pool = pool_for(conn)
        if seam == "writer-pre":
            await writer._insert_audit_pre(
                {
                    "job_id": JOB,
                    "step_type": "routing",
                    "timestamp": "synthetic",
                    "payload": payload,
                }
            )
            persisted = conn.fetchval.call_args.args[10]
        else:
            assert await writer._insert_audit_post(11, payload, 10, 17) is True
            persisted = conn.execute.call_args.args[4]
    assert_identity_only(persisted["metadata"])
    assert persisted["phase"] == payload["phase"]
    assert persisted["tool"] == payload["tool"]
    assert payload == before
