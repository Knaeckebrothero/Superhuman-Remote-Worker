"""Unit tests for the archiver's Postgres backend branch (PR2).

DB-free: a ``FakeWriter`` captures the row dicts ``LLMArchiver`` builds, so these
run in the normal ``pytest`` suite and assert the field assembly, datetime->ISO-Z
serialization, chat cascade, two-phase audit payloads, and ``archive_error``
folding without standing up Postgres. The writer itself is exercised against a
real server in ``tests/test_audit_writer.py``.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.archiver import LLMArchiver


class FakeWriter:
    """Captures writer calls; mimics SyncAuditWriter's return contract."""

    def __init__(self, ready: bool = True):
        self.ready = ready
        self.llm_rows: list[dict] = []
        self.pre_rows: list[dict] = []
        self.posts: list[tuple] = []
        self.chat_rows: list[dict] = []
        self._id = 0

    def ensure_ready(self) -> bool:
        return self.ready

    def insert_llm_request(self, row):
        self._id += 1
        self.llm_rows.append(row)
        return self._id

    def insert_audit_pre(self, row):
        self._id += 1
        self.pre_rows.append(row)
        return self._id

    def insert_audit_post(self, pre_id, payload, latency_ms, request_id=None):
        if pre_id is None:
            return False
        self.posts.append((pre_id, payload, latency_ms, request_id))
        return True

    def insert_chat_entry(self, row):
        self.chat_rows.append(row)

    def close(self):
        pass


@pytest.fixture
def fw():
    return FakeWriter()


@pytest.fixture
def archiver(fw):
    return LLMArchiver(writer=fw)


def test_archive_builds_row_and_cascades_chat(archiver, fw):
    job = str(uuid4())
    messages = [SystemMessage("sys"), HumanMessage("hello")]
    response = AIMessage("hi there")
    rid = archiver.archive(
        job,
        "universal",
        messages,
        response,
        "gpt-x",
        latency_ms=12,
        iteration=3,
        call_type="main",
        metadata={"u": uuid4()},
    )
    assert isinstance(rid, int) and rid >= 1
    assert len(fw.llm_rows) == 1
    row = fw.llm_rows[0]
    assert row["job_id"] == job
    assert row["call_type"] == "main"
    assert row["model"] == "gpt-x"
    assert row["iteration"] == 3
    assert row["request"]["message_count"] == 2
    assert row["response"]["content"] == "hi there"
    assert "token_usage" in row["metrics"]
    # UUID in metadata is serialized to a string for JSONB.
    assert isinstance(row["metadata"]["u"], str)
    # call_type == 'main' cascades a chat row carrying the int request_id.
    assert len(fw.chat_rows) == 1
    assert fw.chat_rows[0]["request_id"] == rid
    assert fw.chat_rows[0]["inputs"][0]["content"] == "hello"  # system excluded


def test_archive_non_main_skips_chat(archiver, fw):
    archiver.archive(
        str(uuid4()),
        "vision",
        [HumanMessage("x")],
        AIMessage("y"),
        "m",
        call_type="vision",
    )
    assert len(fw.llm_rows) == 1
    assert fw.chat_rows == []


def test_audit_tool_call_serializes_datetimes(archiver, fw):
    pre_id = archiver.audit_tool_call(
        str(uuid4()), "universal", 1, "read_file", "call-1", {"path": "/x"}
    )
    assert isinstance(pre_id, int)
    row = fw.pre_rows[-1]
    assert row["step_type"] == "tool"
    assert row["node_name"] == "tools"
    assert row["payload"]["tool"]["name"] == "read_file"
    # started_at datetime -> ISO-8601 'Z' string in the JSONB payload.
    assert isinstance(row["payload"]["started_at"], str)
    assert row["payload"]["started_at"].endswith("Z")
    assert row["payload"]["completed_at"] is None


def test_update_tool_result_builds_post_payload(archiver, fw):
    pre_id = archiver.audit_tool_call(str(uuid4()), "universal", 1, "t", "c", {"a": 1})
    ok = archiver.update_tool_result(pre_id, "the result", True, 50)
    assert ok is True
    sent_pre, payload, latency, request_id = fw.posts[-1]
    assert sent_pre == pre_id
    assert latency == 50
    assert request_id is None
    assert payload["tool"]["result_preview"] == "the result"
    assert payload["tool"]["success"] is True
    assert payload["completed_at"].endswith("Z")


def test_update_llm_response_threads_request_id(archiver, fw):
    audit_id = archiver.audit_llm_call(str(uuid4()), "universal", 1, "gpt", 5, 10)
    ok = archiver.update_llm_response(
        audit_id,
        request_id=999,
        response_preview="hi",
        tool_calls=[{"id": "a", "name": "t"}],
        output_chars=2,
        latency_ms=20,
    )
    assert ok is True
    sent_pre, payload, latency, request_id = fw.posts[-1]
    assert sent_pre == audit_id
    assert request_id == 999  # hard column
    assert payload["llm"]["request_id"] == 999  # wire field
    assert payload["llm"]["metrics"]["tool_call_count"] == 1


def test_update_tool_result_none_pre_id_returns_false(archiver):
    assert archiver.update_tool_result(None, "x", True, 1) is False


def test_archive_error_folds_status_into_metadata(archiver, fw):
    rid = archiver.archive_error(
        str(uuid4()),
        "universal",
        [HumanMessage("q")],
        "gpt",
        "boom",
        "TimeoutError",
        call_type="auxiliary",
    )
    assert isinstance(rid, int)
    row = fw.llm_rows[-1]
    assert row["response"] == {}  # NOT NULL satisfied with an empty response
    assert row["metadata"]["status"] == "error"
    assert row["metadata"]["error"]["type"] == "TimeoutError"
    assert row["metrics"]["output_chars"] == 0


def test_unready_writer_short_circuits(archiver, fw):
    fw.ready = False
    assert archiver.audit_step(str(uuid4()), "u", "tool", "tools", 1) is None
    assert fw.pre_rows == []


def test_from_env_backend_gating(monkeypatch):
    for k in (
        "AUDIT_BACKEND",
        "MONGODB_URL",
        "AUDIT_DB_URL",
        "AUDIT_POSTGRES_USER",
        "AUDIT_POSTGRES_PASSWORD",
        "AUDIT_POSTGRES_HOST",
        "AUDIT_POSTGRES_DB",
    ):
        monkeypatch.delenv(k, raising=False)
    # Default backend is mongo; no MONGODB_URL -> disabled.
    assert LLMArchiver.from_env() is None
    # Postgres backend selected but no credentials -> disabled.
    monkeypatch.setenv("AUDIT_BACKEND", "postgres")
    assert LLMArchiver.from_env() is None
    # Postgres backend with a DSN -> a Postgres-mode archiver (no connection yet).
    monkeypatch.setenv("AUDIT_DB_URL", "postgresql://u:p@localhost:5599/db")
    arch = LLMArchiver.from_env()
    assert arch is not None and arch._is_postgres is True


def test_postgres_mode_does_not_import_pymongo(fw):
    # Constructing with a writer must not require/instantiate the Mongo client.
    arch = LLMArchiver(writer=fw)
    assert arch._is_postgres is True
    assert arch._mongo_db is None
    assert os.environ.get("MONGODB_URL") in (None, "")  # sanity: unset under pytest
