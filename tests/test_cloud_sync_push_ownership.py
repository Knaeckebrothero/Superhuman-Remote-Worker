"""Contracts for the push-ownership half of ``shared.cloud_sync_generations``.

Commit-then-effects (knowledge-base/knowledge/features/stateless_turn_resilience.md
step 4a): once the run_queue unit completes, the turn-end push is fenced by
the row's ``push_owner_token`` instead of the queue lease. These tests pin the
SQL shape and the Python contract with a recording fake connection; the
lock-manager semantics are proven against real Postgres in
``tests/test_cloud_sync_push_ownership_pg.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from shared import cloud_sync_generations as gen
from shared.cloud_sync_generations import (
    CloudSyncRequirement,
    acknowledge_cloud_sync_generation,
    adopt_push_ownership,
    cloud_sync_lease_is_current,
    cloud_sync_writer_is_current,
    encode_cloud_sync_baseline,
    hand_off_push_ownership,
    heartbeat_push_owner,
    normalize_push_progress,
    pending_push_state,
    record_push_failure,
    record_push_progress,
)

THREAD = uuid4()
WORKSPACE = "ws-gen-1"


class _Conn:
    """Records every statement; answers from queued results."""

    def __init__(self, *, fetch=None, fetchval=None):
        self.calls: list[tuple[str, str, tuple]] = []
        self._fetch = list(fetch or [])
        self._fetchval = list(fetchval or [])

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._fetch.pop(0) if self._fetch else []

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self._fetchval.pop(0) if self._fetchval else None


# --------------------------------------------------------------- progress


def test_normalize_push_progress_canonicalizes_and_validates():
    out = normalize_push_progress(
        {
            "planned": 3,
            "files": {
                "b.txt": {
                    "sha256": "b" * 64,
                    "size": 2,
                    "remote_etag": '"e2"',
                    "state": "uploaded",
                },
                "a.txt": {"state": "deleted"},
            },
        }
    )
    assert out["planned"] == 3
    assert list(out["files"]) == ["a.txt", "b.txt"]
    assert out["files"]["a.txt"] == {
        "sha256": "",
        "size": 0,
        "remote_etag": "",
        "state": "deleted",
    }
    assert out["files"]["b.txt"]["remote_etag"] == '"e2"'


def test_normalize_push_progress_accepts_json_text_and_empty():
    assert normalize_push_progress(None) == {"planned": None, "files": {}}
    assert normalize_push_progress("") == {"planned": None, "files": {}}
    assert normalize_push_progress(json.dumps({"files": {}})) == {
        "planned": None,
        "files": {},
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"files": {"x": {"state": "flying"}}},
        {"files": {"x": {"state": "uploaded", "sha256": "nope"}}},
        {"files": {"../x": {"state": "deleted"}}},
        {"files": {"x": {"state": "uploaded", "sha256": "a" * 64, "size": -1}}},
        {"planned": -1, "files": {}},
        {"planned": True, "files": {}},
        {"files": []},
    ],
)
def test_normalize_push_progress_rejects_malformed(bad):
    with pytest.raises(ValueError):
        normalize_push_progress(bad)


# --------------------------------------------------------------- requirements


def _row(**overrides):
    manifest, _encoded, sha = encode_cloud_sync_baseline(overrides.pop("baseline", {}))
    row = {
        "mount_id": "legacy-session",
        "required_generation": 7,
        "acknowledged_generation": 0,
        "required_lease_token": 7,
        "workspace_generation": WORKSPACE,
        "sync_scope_sha256": "c" * 64,
        "baseline_manifest": manifest,
        "baseline_sha256": sha,
    }
    row.update(overrides)
    return row


def test_requirements_read_push_columns_and_tolerate_their_absence():
    with_cols = gen._requirements(
        [
            _row(
                push_owner_token=3,
                push_progress={"planned": 2, "files": {"a": {"state": "deleted"}}},
            )
        ]
    )["legacy-session"]
    assert with_cols.push_owner_token == 3
    assert with_cols.push_progress["planned"] == 2
    assert "a" in with_cols.push_progress["files"]

    class _NoCols(dict):
        def __getitem__(self, key):
            if key in ("push_owner_token", "push_progress"):
                raise KeyError(key)
            return super().__getitem__(key)

    without = gen._requirements([_NoCols(_row())])["legacy-session"]
    assert without.push_owner_token == 0
    assert without.push_progress == {"planned": None, "files": {}}


# --------------------------------------------------------------- writer fence


@pytest.mark.asyncio
async def test_writer_check_dispatches_on_kind():
    conn = _Conn(fetchval=[True, True])
    assert await cloud_sync_writer_is_current(
        conn,
        kind="lease",
        thread_id=THREAD,
        workspace_generation=WORKSPACE,
        lease_token=4,
    )
    assert await cloud_sync_writer_is_current(
        conn,
        kind="push",
        thread_id=THREAD,
        workspace_generation=WORKSPACE,
        push_owner_token=9,
    )
    lease_sql, push_sql = conn.calls[0][1], conn.calls[1][1]
    assert "run_queue" in lease_sql and "lease_token = $2" in lease_sql
    assert "push_owner_token = $2" in push_sql
    assert "acknowledged_generation < generation.required_generation" in push_sql
    # every pending row must carry OUR token — a mixed thread is not ours
    assert "other.push_owner_token <> $2" in push_sql
    assert conn.calls[1][2] == (THREAD, 9, WORKSPACE)


@pytest.mark.asyncio
async def test_writer_check_push_kind_without_token_is_never_current():
    conn = _Conn(fetchval=[True])
    assert not await cloud_sync_writer_is_current(
        conn,
        kind="push",
        thread_id=THREAD,
        workspace_generation=WORKSPACE,
        push_owner_token=0,
    )
    assert conn.calls == []  # no round trip for an impossible fence


@pytest.mark.asyncio
async def test_writer_check_rejects_unknown_kind_and_missing_lease():
    conn = _Conn()
    with pytest.raises(ValueError):
        await cloud_sync_writer_is_current(
            conn, kind="lease", thread_id=THREAD, workspace_generation=WORKSPACE
        )
    with pytest.raises(ValueError):
        await cloud_sync_writer_is_current(
            conn,
            kind="banana",
            thread_id=THREAD,
            workspace_generation=WORKSPACE,
            lease_token=1,
        )


@pytest.mark.asyncio
async def test_lease_alias_keeps_the_old_contract():
    conn = _Conn(fetchval=[False])
    assert not await cloud_sync_lease_is_current(
        conn, thread_id=THREAD, lease_token=4, workspace_generation=WORKSPACE
    )
    assert "run_queue" in conn.calls[0][1]


# --------------------------------------------------------------- hand-off / adopt


@pytest.mark.asyncio
async def test_hand_off_is_lease_fenced_and_returns_the_single_new_token():
    conn = _Conn(
        fetch=[
            [
                {"mount_id": "a", "push_owner_token": 5},
                {"mount_id": "b", "push_owner_token": 5},
            ]
        ]
    )
    token = await hand_off_push_ownership(
        conn,
        thread_id=THREAD,
        lease_token=7,
        workspace_generation=WORKSPACE,
        pod_name="pod-1",
        pod_uid="uid-1",
    )
    assert token == 5
    _kind, sql, args = conn.calls[0]
    assert "queue.lease_token = $2" in sql and "queue.state = 'leased'" in sql
    assert "COALESCE(MAX(generation.push_owner_token), 0) + 1" in sql
    assert "generation.required_lease_token = $2" in sql
    assert "acknowledged_generation < generation.required_generation" in sql
    assert "FOR SHARE" in sql
    assert args == (THREAD, 7, WORKSPACE, "pod-1", "uid-1")


@pytest.mark.asyncio
async def test_hand_off_returns_none_when_nothing_pending_and_refuses_mixed_tokens():
    assert (
        await hand_off_push_ownership(
            _Conn(fetch=[[]]),
            thread_id=THREAD,
            lease_token=7,
            workspace_generation=WORKSPACE,
            pod_name="p",
            pod_uid="u",
        )
        is None
    )
    with pytest.raises(RuntimeError):
        await hand_off_push_ownership(
            _Conn(
                fetch=[
                    [
                        {"mount_id": "a", "push_owner_token": 5},
                        {"mount_id": "b", "push_owner_token": 6},
                    ]
                ]
            ),
            thread_id=THREAD,
            lease_token=7,
            workspace_generation=WORKSPACE,
            pod_name="p",
            pod_uid="u",
        )


@pytest.mark.asyncio
async def test_adopt_bumps_every_pending_row_and_returns_normalized_progress():
    conn = _Conn(
        fetch=[
            [
                {
                    "mount_id": "legacy-session",
                    "push_owner_token": 8,
                    "push_progress": json.dumps(
                        {
                            "planned": 4,
                            "files": {
                                "z.txt": {
                                    "state": "uploaded",
                                    "sha256": "f" * 64,
                                    "size": 1,
                                    "remote_etag": "e",
                                }
                            },
                        }
                    ),
                }
            ]
        ]
    )
    adopted = await adopt_push_ownership(
        conn,
        thread_id=THREAD,
        lease_token=9,
        workspace_generation=WORKSPACE,
        pod_name="pod-2",
        pod_uid="uid-2",
    )
    assert adopted["legacy-session"]["planned"] == 4
    assert adopted["legacy-session"]["files"]["z.txt"]["remote_etag"] == "e"
    sql = conn.calls[0][1]
    # unconditional for the thread's new claimant: no required_lease_token filter
    assert "generation.required_lease_token" not in sql
    assert "queue.lease_token = $2" in sql
    assert "acknowledged_generation < generation.required_generation" in sql


# --------------------------------------------------------------- progress / heartbeat / failure


@pytest.mark.asyncio
async def test_record_push_progress_merges_files_and_sets_planned_once():
    conn = _Conn(fetchval=[1, None])
    ok = await record_push_progress(
        conn,
        thread_id=THREAD,
        mount_id="legacy-session",
        push_owner_token=5,
        files={
            "a.txt": {
                "sha256": "a" * 64,
                "size": 3,
                "remote_etag": "x",
                "state": "uploaded",
            }
        },
        planned=7,
    )
    assert ok is True
    _kind, sql, args = conn.calls[0]
    assert "push_owner_token = $3" in sql and "|| $4::jsonb" in sql
    assert "'{planned}'" in sql
    assert args[:3] == (THREAD, "legacy-session", 5)
    assert json.loads(args[3]) == {
        "a.txt": {
            "sha256": "a" * 64,
            "size": 3,
            "remote_etag": "x",
            "state": "uploaded",
        }
    }
    assert args[4] == "7"
    # lost ownership → False, planned None → SQL NULL keeps the stored value
    lost = await record_push_progress(
        conn, thread_id=THREAD, mount_id="legacy-session", push_owner_token=5, files={}
    )
    assert lost is False
    assert conn.calls[1][2][4] is None


@pytest.mark.asyncio
async def test_record_push_progress_rejects_malformed_entries_before_any_sql():
    conn = _Conn()
    with pytest.raises(ValueError):
        await record_push_progress(
            conn,
            thread_id=THREAD,
            mount_id="m",
            push_owner_token=1,
            files={"x": {"state": "?"}},
        )
    assert conn.calls == []


@pytest.mark.asyncio
async def test_heartbeat_and_failure_are_token_scoped():
    conn = _Conn(fetch=[[{"mount_id": "m"}], [], [{"mount_id": "m"}]])
    assert await heartbeat_push_owner(conn, thread_id=THREAD, push_owner_token=5)
    assert not await heartbeat_push_owner(conn, thread_id=THREAD, push_owner_token=5)
    assert await record_push_failure(
        conn, thread_id=THREAD, push_owner_token=5, error="boom"
    )
    for _k, sql, args in conn.calls:
        assert "push_owner_token = $2" in sql
        assert args[:2] == (THREAD, 5)
    assert "push_failed_at = now()" in conn.calls[2][1]


@pytest.mark.asyncio
async def test_pending_push_state_aggregates_mounts():
    fresh = datetime.now(timezone.utc)
    conn = _Conn(
        fetch=[
            [
                {
                    "mount_id": "a",
                    "push_progress": {
                        "planned": 5,
                        "files": {
                            "x": {
                                "state": "uploaded",
                                "sha256": "a" * 64,
                                "size": 1,
                                "remote_etag": "",
                            }
                        },
                    },
                    "push_heartbeat_at": fresh,
                    "push_owner_pod": "p",
                    "push_failed_at": None,
                    "push_error": None,
                    "owner_alive": True,
                },
                {
                    "mount_id": "b",
                    "push_progress": {"planned": 2, "files": {}},
                    "push_heartbeat_at": fresh - timedelta(minutes=5),
                    "push_owner_pod": "p",
                    "push_failed_at": fresh,
                    "push_error": "x",
                    "owner_alive": False,
                },
            ]
        ]
    )
    state = await pending_push_state(conn, thread_id=THREAD)
    assert state == {
        "pending": 2,
        "uploaded": 1,
        "total": 7,
        "owner_alive": True,
        "failed": True,
    }
    assert conn.calls[0][2] == (THREAD, gen.PUSH_OWNER_STALE_AFTER_SECONDS)
    empty = await pending_push_state(_Conn(fetch=[[]]), thread_id=THREAD)
    assert empty == {
        "pending": 0,
        "uploaded": 0,
        "total": None,
        "owner_alive": False,
        "failed": False,
    }


# --------------------------------------------------------------- acknowledgement


@pytest.mark.asyncio
async def test_acknowledge_uses_the_push_fence_when_a_token_is_given():
    conn = _Conn(fetchval=[7, 7])
    req = CloudSyncRequirement(
        mount_id="legacy-session",
        required_generation=7,
        acknowledged_generation=0,
        required_lease_token=7,
        workspace_generation=WORKSPACE,
        sync_scope_sha256="c" * 64,
    )
    assert await acknowledge_cloud_sync_generation(
        conn,
        thread_id=THREAD,
        lease_token=7,
        mount_id=req.mount_id,
        generation=req.required_generation,
        workspace_generation=req.workspace_generation,
        sync_scope_sha256=req.sync_scope_sha256,
        baseline_sha256=req.baseline_sha256,
        push_owner_token=5,
    )
    push_sql, push_args = conn.calls[0][1], conn.calls[0][2]
    assert "generation.push_owner_token = $2" in push_sql
    assert "run_queue" not in push_sql
    assert push_args[1] == 5 and push_args[2] == "legacy-session"
    assert await acknowledge_cloud_sync_generation(
        conn,
        thread_id=THREAD,
        lease_token=7,
        mount_id=req.mount_id,
        generation=7,
        workspace_generation=req.workspace_generation,
        sync_scope_sha256=req.sync_scope_sha256,
        baseline_sha256=req.baseline_sha256,
    )
    assert "run_queue" in conn.calls[1][1]


def test_reserve_and_load_return_the_push_columns_and_reset_on_rearm():
    assert "push_owner_token" in gen._LOAD_SQL and "push_progress" in gen._LOAD_SQL
    assert "push_progress = '{}'::jsonb" in gen._RESERVE_SQL
    assert "push_heartbeat_at = NULL" in gen._RESERVE_SQL
    # the token itself is NOT reset: it stays monotonic across generations
    assert "push_owner_token = 0" not in gen._RESERVE_SQL
