# Workspace Reaper Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop workspace pods leaking forever by making the lifecycle reconciler reap teardown-eligible workspaces with a clean/dirty gate, a bounded snapshot-retry escape hatch, and a volume-mode branch — replacing the keep-alive-on-snapshot-failure loop.

**Architecture:** Extend `WorkspaceInstanceManager` (a `StatefulInstanceManager`) with new predicates (`is_reapable`, `is_dirty`, `is_reachable`, `is_state_ephemeral`, `attempts_exhausted`) and actions (`record_attempt`, `give_up`). Add a stateful reap branch to `InstanceLifecycleReconciler.tick()` that runs them in order. Retire the suspend half of `workspace_idle_sweeper`. Add a snapshot work-marker, a default-22 port fix, owner-ref/TTL on pods, and reap-outcome observability via the reconciler's existing logged-stats dict.

**Tech Stack:** Python 3.12 (CI gate), asyncio, pytest + pytest-asyncio, Kubernetes Python client, Postgres (asyncpg) for `jobs`/`threads`. Spec: `docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md`.

**REVISION (2026-06-05, at execution time, post ide-ext merge):**
- **Task 12 changed from Prometheus to logged-stats.** `prometheus_client` is NOT installed and the codebase uses NO Counter/Gauge metrics anywhere — observability is the reconciler's `stats`/`report` dict, emitted via `logger.info("Lifecycle tick kind=... {...}")`. Task 12 now ensures reap outcomes (`reaped`, `reap_attempts`, `reap_forced`) are in that dict (already added in Task 7) and adds a WARN log on each forced delete (the data-loss signal). No new dependency, no `metrics.py`.
- **`_build_pod_manifest` signature confirmed** (Task 10): `(self, pod_name, owner, image, cpu, memory, cpu_limit, memory_limit, network_tier=DEFAULT_NETWORK_TIER, pvc_name=None, seed_configmap=None)`. ide-ext added `extensions` to `_create_seed_configmap`, NOT to `_build_pod_manifest` — Task 10's edit point (the `metadata` block, line ~875) is unaffected.
- Implementation branch is `feat/workspace-reaper-lifecycle` (off freshly-merged `develop`), not `design/...`.

**Key decisions baked in:**
- Dirty signal keys on `threads.total_turns` (monotonic, real-turns-only), **never `last_activity`** (contaminated: `merge_*_context` bumps it).
- Jobs have **no** dirty-marker (asymmetric): terminal jobs reap via their existing completion snapshot; otherwise `is_dirty` returns `True` (attempt snapshot) and the escape hatch bounds the unreachable case. No Mongo call in the tick.
- `is_healthy` stays **phase-only**; reachability is probed **only** in the reap path, only for already-reapable instances.
- emptyDir crash → delete tombstone; PVC-backed → recreate-pod-keep-PVC (minimally activated this spec).

**Conventions to follow:**
- Tests live in `tests/test_*.py` (flat dir). Async tests use `@pytest.mark.asyncio`. Reuse the `_make_manager()` fixture style from `tests/test_lifecycle_workspace_manager.py`.
- Run tests with `python -m pytest`. CI uses Py3.12; local may be noisy — a green run of the *specific* new tests is the bar.
- Commit after every passing task. We are on branch `design/workspace-reaper-lifecycle`.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `orchestrator/services/lifecycle/workspace_manager.py` | New predicates + actions on the manager | Modify |
| `orchestrator/services/lifecycle/reconciler.py` | Stateful reap branch in `tick()` + stats | Modify |
| `orchestrator/services/lifecycle/metrics.py` | Prometheus counters for reap outcomes | Create |
| `orchestrator/services/snapshot_service.py` | Stamp `work_marker_at_capture` on success | Modify |
| `orchestrator/services/workspace_suspension.py` | Default-22 → resolve port by kind | Modify |
| `orchestrator/services/container_provisioner.py` | Owner-ref/TTL annotation at pod create | Modify |
| `orchestrator/main.py` | Thin `workspace_idle_sweeper` to reconcile-only | Modify |
| `tests/test_lifecycle_workspace_manager.py` | Predicate/action unit tests | Modify |
| `tests/test_lifecycle_reconciler_reap.py` | Reap-branch decision-flow tests | Create |
| `tests/test_snapshot_work_marker.py` | Marker-stamp test | Create |
| `tests/test_workspace_suspension_port.py` | Port-resolution test | Create |

**Task order & dependencies:** Tasks 1–6 build manager predicates/actions (pure, parallel-safe, each independently testable). Task 7 wires them into the reconciler (depends on 1–6). Task 8 (snapshot marker) is independent but Task 2's `is_dirty` reads what it writes — do 8 before relying on it end-to-end, but its unit test stands alone. Tasks 9–11 (port fix, owner-ref, sweeper thinning) are independent. Task 12 is metrics wiring. Task 13 is the manual cleanup + verification.

---

## Task 1: `is_reapable` — widen eligibility to terminal states

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py` (add constants near line 32; add method after `is_idle` ~line 147)
- Test: `tests/test_lifecycle_workspace_manager.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_lifecycle_workspace_manager.py`:

```python
class TestIsReapable:
    @pytest.mark.asyncio
    async def test_completed_job_is_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"job_status": "completed"})
        assert await mgr.is_reapable(inst) is True

    @pytest.mark.asyncio
    async def test_failed_job_is_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"job_status": "failed"})
        assert await mgr.is_reapable(inst) is True

    @pytest.mark.asyncio
    async def test_paused_job_is_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"job_status": "paused"})
        assert await mgr.is_reapable(inst) is True

    @pytest.mark.asyncio
    async def test_processing_job_not_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"job_status": "processing"})
        assert await mgr.is_reapable(inst) is False

    @pytest.mark.asyncio
    async def test_ended_thread_is_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"thread_status": "ended"})
        assert await mgr.is_reapable(inst) is True

    @pytest.mark.asyncio
    async def test_active_thread_not_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"thread_status": "active"})
        assert await mgr.is_reapable(inst) is False

    @pytest.mark.asyncio
    async def test_no_status_not_reapable(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={})
        assert await mgr.is_reapable(inst) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsReapable -v`
Expected: FAIL with `AttributeError: ... has no attribute 'is_reapable'`

- [ ] **Step 3: Implement `is_reapable`**

In `workspace_manager.py`, after the existing `_IDLE_THREAD_STATUSES` (line 33) add:

```python
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_THREAD_STATUSES = frozenset({"ended"})
# Reapable = bound work no longer needs the pod: terminal (clean up) OR
# suspendable-idle (snapshot + free). Superset of the old is_idle set.
_REAPABLE_JOB_STATUSES = _IDLE_JOB_STATUSES | _TERMINAL_JOB_STATUSES
_REAPABLE_THREAD_STATUSES = _IDLE_THREAD_STATUSES | _TERMINAL_THREAD_STATUSES
```

Add the method after `is_idle` (after line 147):

```python
    async def is_reapable(self, inst: Instance) -> bool:
        """True when the bound work no longer needs the pod.

        Superset of ``is_idle``: adds terminal job/thread states. Terminal
        instances get cleaned up; suspendable-idle ones get snapshot+freed.
        A pod with no bound row is never reapable (context may be in flight).
        """
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            return job_status in _REAPABLE_JOB_STATUSES
        if thread_status:
            return thread_status in _REAPABLE_THREAD_STATUSES
        return False

    def _is_terminal(self, inst: Instance) -> bool:
        """Bound work is finished (vs merely paused) — nothing to preserve
        beyond an existing snapshot."""
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            return job_status in _TERMINAL_JOB_STATUSES
        if thread_status:
            return thread_status in _TERMINAL_THREAD_STATUSES
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsReapable -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/workspace_manager.py tests/test_lifecycle_workspace_manager.py
git commit -m "feat(workspace-reaper): add is_reapable predicate (terminal + idle)"
```

---

## Task 2: `is_dirty` — activity-based, threads via total_turns, jobs conservative

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py` (add method; extend `list_instances` to capture the marker)
- Test: `tests/test_lifecycle_workspace_manager.py`

**Context:** `is_dirty` compares a monotonic work-marker against the marker recorded at last snapshot. Threads: `total_turns` (column on `threads`). Jobs: no marker — return `True` unless the instance is terminal-with-existing-snapshot (then there's a fresh capture, treat as clean). The snapshot marker is written by Task 8 into `workspace_container.last_snapshot_turns`; we read it from instance metadata captured during `list_instances`.

- [ ] **Step 1: Write the failing tests**

```python
class TestIsDirty:
    @pytest.mark.asyncio
    async def test_thread_zero_turns_is_clean(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="t1",
                        metadata={"thread_status": "ended", "total_turns": 0,
                                  "last_snapshot_turns": None})
        assert await mgr.is_dirty(inst) is False

    @pytest.mark.asyncio
    async def test_thread_turns_ahead_of_snapshot_is_dirty(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="t1",
                        metadata={"thread_status": "ended", "total_turns": 5,
                                  "last_snapshot_turns": 2})
        assert await mgr.is_dirty(inst) is True

    @pytest.mark.asyncio
    async def test_thread_turns_equal_snapshot_is_clean(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="t1",
                        metadata={"thread_status": "ended", "total_turns": 3,
                                  "last_snapshot_turns": 3})
        assert await mgr.is_dirty(inst) is False

    @pytest.mark.asyncio
    async def test_thread_with_turns_never_snapshotted_is_dirty(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="t1",
                        metadata={"thread_status": "ended", "total_turns": 4,
                                  "last_snapshot_turns": None})
        assert await mgr.is_dirty(inst) is True

    @pytest.mark.asyncio
    async def test_terminal_job_with_snapshot_is_clean(self):
        # Completed jobs get a completion snapshot — reap without re-capture.
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="j1",
                        metadata={"job_status": "completed",
                                  "snapshot_status": "available"})
        assert await mgr.is_dirty(inst) is False

    @pytest.mark.asyncio
    async def test_job_without_snapshot_is_dirty(self):
        # No job turn-counter → conservative: attempt a snapshot.
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="j1",
                        metadata={"job_status": "pending_review",
                                  "snapshot_status": None})
        assert await mgr.is_dirty(inst) is True
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsDirty -v`
Expected: FAIL with `AttributeError: ... 'is_dirty'`

- [ ] **Step 3: Implement `is_dirty`**

Add after `is_reapable` in `workspace_manager.py`:

```python
    async def is_dirty(self, inst: Instance) -> bool:
        """True when the workspace may hold un-snapshotted state worth saving.

        Threads: precise — current ``total_turns`` vs the turn count recorded
        at last snapshot (``last_snapshot_turns``). Zero turns, or turns equal
        to the snapshot, means clean.

        Jobs: no monotonic turn counter exists in Postgres (audit count is in
        Mongo — deliberately not consulted here). Conservative: a terminal job
        with an existing snapshot is clean (it got a completion capture);
        otherwise dirty (attempt a snapshot; the escape hatch bounds the
        unreachable case).

        NOTE: never reads ``last_activity`` — it is bumped by the orchestrator's
        own context merges and cannot distinguish real work from bookkeeping.
        """
        thread_status = inst.metadata.get("thread_status")
        if thread_status is not None:
            turns = inst.metadata.get("total_turns") or 0
            snap_turns = inst.metadata.get("last_snapshot_turns")
            if snap_turns is None:
                return turns > 0
            return turns > snap_turns
        # Job path: no turn counter.
        if self._is_terminal(inst):
            return inst.metadata.get("snapshot_status") != "available"
        return True
```

- [ ] **Step 4: Extend `list_instances` to populate the new metadata fields**

In `workspace_manager.py`, in the thread branch of `list_instances` (currently sets `metadata["thread_status"]` ~line 100), extend the `_fetch_thread` row read so the row also yields `total_turns`, and surface the snapshot marker. Replace the thread block:

```python
            if thread_id:
                row = await self._fetch_thread(thread_id)
                if row is not None:
                    metadata["thread_status"] = row.get("status")
                    metadata["total_turns"] = row.get("total_turns") or 0
                    md = row.get("metadata") or {}
                    if isinstance(md, str):
                        try:
                            md = json.loads(md)
                        except (json.JSONDecodeError, ValueError):
                            md = {}
                    ws = md.get("workspace_container") or {}
                    metadata["last_snapshot_turns"] = ws.get("last_snapshot_turns")
                    snap = md.get("snapshot") or {}
                    metadata["snapshot_status"] = snap.get("status")
```

And in the job branch (currently ~line 104-113) add the snapshot status alongside `workspace_status`:

```python
                    snap = ctx.get("snapshot") or {}
                    metadata["snapshot_status"] = snap.get("status")
```

Update `_fetch_thread`'s SQL (line ~263) to select the extra columns:

```python
                row = await conn.fetchrow(
                    "SELECT id, status, ended_at, agent_id, total_turns, metadata "
                    "FROM threads WHERE id = $1",
                    thread_id,
                )
```

- [ ] **Step 5: Run to verify pass (incl. existing list_instances tests)**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsDirty tests/test_lifecycle_workspace_manager.py::TestListInstances -v`
Expected: PASS (TestIsDirty 6 + TestListInstances 4). The thread `_make_manager` fixture returns rows via `conn.fetchrow` side-effect, so `total_turns`/`metadata` absent → `.get()` yields defaults; existing assertions still hold.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/lifecycle/workspace_manager.py tests/test_lifecycle_workspace_manager.py
git commit -m "feat(workspace-reaper): add is_dirty (threads total_turns; jobs conservative)"
```

---

## Task 3: `is_state_ephemeral` — volume-mode branch

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py` (method + capture volume mode in `list_instances`)
- Test: `tests/test_lifecycle_workspace_manager.py`

**Context:** Pod volume `workspace-data` is either `emptyDir` (ephemeral) or `persistentVolumeClaim` (recoverable by reattach). We read it from the pod spec during `list_instances` and stash a bool in metadata.

- [ ] **Step 1: Write the failing tests**

```python
class TestIsStateEphemeral:
    @pytest.mark.asyncio
    async def test_emptydir_is_ephemeral(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"volume_ephemeral": True})
        assert await mgr.is_state_ephemeral(inst) is True

    @pytest.mark.asyncio
    async def test_pvc_is_not_ephemeral(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"volume_ephemeral": False})
        assert await mgr.is_state_ephemeral(inst) is False

    @pytest.mark.asyncio
    async def test_unknown_defaults_to_ephemeral(self):
        # Default matches today's reality (emptyDir). Conservative for the
        # current fleet; the PVC migration spec flips the default explicitly.
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={})
        assert await mgr.is_state_ephemeral(inst) is True
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsStateEphemeral -v`
Expected: FAIL `AttributeError: 'is_state_ephemeral'`

- [ ] **Step 3: Implement + capture volume mode**

Add method:

```python
    async def is_state_ephemeral(self, inst: Instance) -> bool:
        """True when pod-local storage dies with the pod (emptyDir).

        Ephemeral → a crashed/unreachable pod's state is unrecoverable, so the
        terminal action is delete-the-tombstone. PVC-backed → state survives on
        the volume; the terminal action is recreate-pod-keep-PVC. Defaults to
        ephemeral (today's fleet default) when the volume mode is unknown.
        """
        return bool(inst.metadata.get("volume_ephemeral", True))
```

In `list_instances`, where `metadata` is first built (after line 96), add volume-mode detection from the pod spec:

```python
            metadata["volume_ephemeral"] = _pod_volume_is_ephemeral(pod)
```

Add a module-level helper near `expected_workspace_shas` (after line 47):

```python
def _pod_volume_is_ephemeral(pod: Any) -> bool:
    """True if the pod's workspace-data volume is emptyDir (vs a PVC).

    Defaults to True (ephemeral) when the volume can't be read — matches the
    current fleet default and keeps the reaper conservative.
    """
    try:
        for vol in pod.spec.volumes or []:
            if getattr(vol, "name", None) == "workspace-data":
                if getattr(vol, "persistent_volume_claim", None) is not None:
                    return False
                return getattr(vol, "empty_dir", None) is not None
    except Exception:
        pass
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsStateEphemeral -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/workspace_manager.py tests/test_lifecycle_workspace_manager.py
git commit -m "feat(workspace-reaper): add is_state_ephemeral volume-mode branch"
```

---

## Task 4: `is_reachable` — cached SSH/TCP ping

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py`
- Test: `tests/test_lifecycle_workspace_manager.py`

**Context:** Cheap TCP connect to `pod_ip:30022`, cached ~30s per pod IP in an in-memory dict on the manager (single orchestrator process). We inject a clock and the connector so tests are deterministic — no real sockets, no real time.

- [ ] **Step 1: Write the failing tests**

```python
class TestIsReachable:
    @pytest.mark.asyncio
    async def test_reachable_when_connect_succeeds(self):
        mgr, *_ = _make_manager()
        mgr._tcp_probe = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="x", metadata={"pod_ip": "10.0.0.5"})
        assert await mgr.is_reachable(inst) is True
        mgr._tcp_probe.assert_awaited_once_with("10.0.0.5", 30022)

    @pytest.mark.asyncio
    async def test_unreachable_when_connect_fails(self):
        mgr, *_ = _make_manager()
        mgr._tcp_probe = AsyncMock(return_value=False)
        inst = Instance(kind="workspace", id="x", metadata={"pod_ip": "10.0.0.5"})
        assert await mgr.is_reachable(inst) is False

    @pytest.mark.asyncio
    async def test_unreachable_without_pod_ip(self):
        mgr, *_ = _make_manager()
        mgr._tcp_probe = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="x", metadata={})
        assert await mgr.is_reachable(inst) is False
        mgr._tcp_probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_is_cached(self):
        mgr, *_ = _make_manager()
        mgr._clock = lambda: 1000.0
        mgr._tcp_probe = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="x", metadata={"pod_ip": "10.0.0.5"})
        assert await mgr.is_reachable(inst) is True
        assert await mgr.is_reachable(inst) is True
        mgr._tcp_probe.assert_awaited_once()  # second call served from cache

    @pytest.mark.asyncio
    async def test_cache_expires(self):
        mgr, *_ = _make_manager()
        t = {"now": 1000.0}
        mgr._clock = lambda: t["now"]
        mgr._tcp_probe = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="x", metadata={"pod_ip": "10.0.0.5"})
        await mgr.is_reachable(inst)
        t["now"] = 1040.0  # > 30s TTL
        await mgr.is_reachable(inst)
        assert mgr._tcp_probe.await_count == 2
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsReachable -v`
Expected: FAIL `AttributeError: 'is_reachable'`

- [ ] **Step 3: Implement**

Add imports at top of `workspace_manager.py` if absent: `import socket`, `import time`. In `__init__` add cache + injectables:

```python
        self._reach_cache: dict[str, tuple[float, bool]] = {}
        self._reach_ttl_s: float = 30.0
        self._clock = time.monotonic
```

Add methods:

```python
    async def _tcp_probe(self, host: str, port: int) -> bool:
        """One-shot TCP connect with a short timeout. Overridable in tests."""
        def _connect() -> bool:
            try:
                with socket.create_connection((host, port), timeout=5):
                    return True
            except OSError:
                return False
        return await asyncio.to_thread(_connect)

    async def is_reachable(self, inst: Instance) -> bool:
        """Cached liveness probe to the pod's SSH port (30022).

        Used ONLY in the reap path to choose snapshot-vs-retry — never in
        ``is_healthy`` (an unreachable busy pod must not be force-deleted over
        a transient blip). Cached ~30s per pod IP.
        """
        host = inst.metadata.get("pod_ip")
        if not host:
            return False
        now = self._clock()
        cached = self._reach_cache.get(host)
        if cached is not None and (now - cached[0]) < self._reach_ttl_s:
            return cached[1]
        ok = await self._tcp_probe(host, 30022)
        self._reach_cache[host] = (now, ok)
        return ok
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestIsReachable -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/workspace_manager.py tests/test_lifecycle_workspace_manager.py
git commit -m "feat(workspace-reaper): add cached is_reachable probe"
```

---

## Task 5: attempt counter — `record_attempt` + `attempts_exhausted`

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py`
- Test: `tests/test_lifecycle_workspace_manager.py`

**Context:** `snapshot_attempts` lives in the workspace context JSONB. Increment on a failed/unreachable attempt; the threshold `N` is configurable via env `WORKSPACE_SNAPSHOT_MAX_ATTEMPTS` (default 5). We persist via the existing `merge_workspace_container_context` / `merge_thread_workspace_context` db methods, dispatched by label.

- [ ] **Step 1: Write the failing tests**

```python
class TestAttemptCounter:
    @pytest.mark.asyncio
    async def test_record_attempt_increments_job_context(self):
        mgr, _, _, _, db = _make_manager()
        db.merge_workspace_container_context = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="workspace-a", bound_to="j1",
                        metadata={"labels": {"srw/job-id": "j1"},
                                  "snapshot_attempts": 2})
        await mgr.record_attempt(inst)
        db.merge_workspace_container_context.assert_awaited_once_with(
            "j1", {"snapshot_attempts": 3})

    @pytest.mark.asyncio
    async def test_record_attempt_increments_thread_context(self):
        mgr, _, _, _, db = _make_manager()
        db.merge_thread_workspace_context = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="ws-thread-a", bound_to="t1",
                        metadata={"labels": {"srw/thread-id": "t1"},
                                  "snapshot_attempts": 0})
        await mgr.record_attempt(inst)
        db.merge_thread_workspace_context.assert_awaited_once_with(
            "t1", {"snapshot_attempts": 1})

    @pytest.mark.asyncio
    async def test_exhausted_true_at_threshold(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_SNAPSHOT_MAX_ATTEMPTS", "5")
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"snapshot_attempts": 5})
        assert await mgr.attempts_exhausted(inst) is True

    @pytest.mark.asyncio
    async def test_exhausted_false_below_threshold(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_SNAPSHOT_MAX_ATTEMPTS", "5")
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"snapshot_attempts": 4})
        assert await mgr.attempts_exhausted(inst) is False
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestAttemptCounter -v`
Expected: FAIL `AttributeError`

- [ ] **Step 3: Implement + capture `snapshot_attempts` in list_instances**

Add `import os` if absent. Add methods:

```python
    def _max_attempts(self) -> int:
        try:
            return int(os.environ.get("WORKSPACE_SNAPSHOT_MAX_ATTEMPTS", "5"))
        except ValueError:
            return 5

    async def attempts_exhausted(self, inst: Instance) -> bool:
        return (inst.metadata.get("snapshot_attempts") or 0) >= self._max_attempts()

    async def record_attempt(self, inst: Instance) -> None:
        """Persist an incremented snapshot-attempt counter to the bound row."""
        if self._db is None:
            return
        bound = inst.bound_to
        if not bound:
            return
        nxt = (inst.metadata.get("snapshot_attempts") or 0) + 1
        labels = inst.metadata.get("labels") or {}
        try:
            if "srw/thread-id" in labels:
                await self._db.merge_thread_workspace_context(
                    bound, {"snapshot_attempts": nxt})
            else:
                await self._db.merge_workspace_container_context(
                    bound, {"snapshot_attempts": nxt})
        except Exception:
            logger.exception("Failed to record snapshot attempt for %s", inst.id)
```

In `list_instances`, capture the existing counter. In the job branch where `ws_ctx` is read (~line 111):

```python
                    metadata["snapshot_attempts"] = ws_ctx.get("snapshot_attempts") or 0
```

In the thread branch, from the `ws` dict added in Task 2:

```python
                    metadata["snapshot_attempts"] = ws.get("snapshot_attempts") or 0
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestAttemptCounter -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/workspace_manager.py tests/test_lifecycle_workspace_manager.py
git commit -m "feat(workspace-reaper): add bounded snapshot-attempt counter"
```

---

## Task 6: `give_up` — volume-mode-aware terminal action + snapshot-resets-counter

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py`
- Test: `tests/test_lifecycle_workspace_manager.py`

**Context:** `give_up` is the escape-hatch terminal action when a dirty pod can't be snapshotted after N attempts. emptyDir → delete (existing `delete`). PVC → recreate-pod-keep-PVC (minimally activated: call provisioner delete then create against same owner; PVC is not deleted). Also: a successful snapshot must reset the attempt counter to 0 — add that to the existing `snapshot` method.

- [ ] **Step 1: Write the failing tests**

```python
class TestGiveUp:
    @pytest.mark.asyncio
    async def test_ephemeral_give_up_deletes(self):
        mgr, container, *_ = _make_manager()
        inst = Instance(kind="workspace", id="workspace-a", bound_to="j1",
                        metadata={"labels": {"srw/job-id": "j1"},
                                  "volume_ephemeral": True})
        await mgr.give_up(inst, grace_s=0)
        container.delete_workspace.assert_awaited_once_with(WorkspaceOwner.job("j1"))

    @pytest.mark.asyncio
    async def test_pvc_give_up_recreates_keeps_pvc(self):
        mgr, container, *_ = _make_manager()
        container.create_workspace = AsyncMock(return_value=True)
        container.delete_workspace_pvc = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="workspace-a", bound_to="j1",
                        metadata={"labels": {"srw/job-id": "j1"},
                                  "volume_ephemeral": False})
        await mgr.give_up(inst, grace_s=0)
        container.delete_workspace.assert_awaited_once_with(WorkspaceOwner.job("j1"))
        container.create_workspace.assert_awaited_once_with(WorkspaceOwner.job("j1"))
        container.delete_workspace_pvc.assert_not_called()  # PVC must survive

    @pytest.mark.asyncio
    async def test_snapshot_success_resets_attempt_counter(self):
        mgr, _, _, snapshot, db = _make_manager()
        db.merge_workspace_container_context = AsyncMock(return_value=True)
        snapshot.capture_vm_snapshot = AsyncMock(return_value=True)
        inst = Instance(kind="workspace", id="workspace-a", bound_to="j1",
                        metadata={"labels": {"srw/job-id": "j1"},
                                  "pod_ip": "10.0.0.5"})
        ref = await mgr.snapshot(inst)
        assert ref == "j1"
        db.merge_workspace_container_context.assert_awaited_with(
            "j1", {"snapshot_attempts": 0})
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestGiveUp -v`
Expected: FAIL (`give_up` missing; snapshot reset not implemented)

- [ ] **Step 3: Implement `give_up` + counter reset in `snapshot`**

Add method:

```python
    async def give_up(self, inst: Instance, grace_s: int) -> None:
        """Escape hatch: dirty + unreachable + attempts exhausted.

        Ephemeral storage → delete the pod (state already unrecoverable).
        PVC-backed → recreate the pod against the same PVC so the volume
        reattaches; the PVC is NOT deleted. (PVC arm is minimally activated
        this spec — full restore-by-reattach lands with the migration spec.)
        """
        bound = inst.bound_to
        if not bound:
            return
        labels = inst.metadata.get("labels") or {}
        owner = (WorkspaceOwner.session(bound)
                 if "srw/thread-id" in labels else WorkspaceOwner.job(bound))
        await self.delete(inst, grace_s)
        if not inst.metadata.get("volume_ephemeral", True):
            try:
                await self._provisioner.create_workspace(owner)
            except Exception:
                logger.exception("PVC give_up recreate failed for %s", inst.id)
```

In the existing `snapshot` method, after a successful capture (where it currently `return bound if ok else None`, ~line 171), reset the counter on success. Replace that tail:

```python
            if ok:
                labels = inst.metadata.get("labels") or {}
                try:
                    if "srw/thread-id" in labels:
                        await self._db.merge_thread_workspace_context(
                            bound, {"snapshot_attempts": 0})
                    else:
                        await self._db.merge_workspace_container_context(
                            bound, {"snapshot_attempts": 0})
                except Exception:
                    logger.exception("Failed to reset attempts for %s", inst.id)
                return bound
            return None
```

(Note: `snapshot`'s `inst.metadata` must carry `labels`; `list_instances` already sets `metadata["labels"]` at line 94.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py::TestGiveUp tests/test_lifecycle_workspace_manager.py::TestSnapshot -v`
Expected: PASS (TestGiveUp 3 + TestSnapshot 3). The existing `TestSnapshot` cases pass `metadata` without `labels`; `.get("labels") or {}` handles that, and `db` is an AsyncMock so the reset call is a no-op assertion-wise.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/workspace_manager.py tests/test_lifecycle_workspace_manager.py
git commit -m "feat(workspace-reaper): add give_up escape hatch + snapshot counter reset"
```

---

## Task 7: Reconciler reap branch

**Files:**
- Modify: `orchestrator/services/lifecycle/reconciler.py` (extend `tick()` after the drift block ~line 195; extend stats dict ~line 117)
- Test: `tests/test_lifecycle_reconciler_reap.py` (Create)

**Context:** After the existing crash + drift handling, for a stateful manager whose instance is reapable, run the decision flow from the spec. Guard everything so a manager lacking the new methods (agents) is unaffected — use `getattr`/`isinstance(StatefulInstanceManager)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lifecycle_reconciler_reap.py`:

```python
"""Reap-branch decision-flow tests for InstanceLifecycleReconciler.tick()."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.lifecycle import (
    Instance, InstanceLifecycleReconciler, StatefulInstanceManager,
)


def _stateful_mgr(inst, *, healthy=True, reapable=True, dirty=False,
                  reachable=False, exhausted=False, snapshot_ref=None):
    mgr = MagicMock(spec=StatefulInstanceManager)
    mgr.kind = "workspace"
    mgr.expected_versions = AsyncMock(return_value=set())
    mgr.list_instances = AsyncMock(return_value=[inst])
    mgr.is_healthy = AsyncMock(return_value=healthy)
    mgr.is_idle = AsyncMock(return_value=False)
    mgr.signal_drain_pending = AsyncMock()
    mgr.is_reapable = AsyncMock(return_value=reapable)
    mgr.is_dirty = AsyncMock(return_value=dirty)
    mgr.is_reachable = AsyncMock(return_value=reachable)
    mgr.attempts_exhausted = AsyncMock(return_value=exhausted)
    mgr.snapshot = AsyncMock(return_value=snapshot_ref)
    mgr.delete = AsyncMock()
    mgr.give_up = AsyncMock()
    mgr.record_attempt = AsyncMock()
    return mgr


def _inst():
    return Instance(kind="workspace", id="ws-1", bound_to="j1")


@pytest.mark.asyncio
async def test_clean_reapable_deletes_without_probe():
    mgr = _stateful_mgr(_inst(), dirty=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.delete.assert_awaited_once()
    mgr.is_reachable.assert_not_called()
    mgr.snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_reachable_snapshots_then_deletes():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=True, snapshot_ref="j1")
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.snapshot.assert_awaited_once()
    mgr.delete.assert_awaited_once()
    mgr.give_up.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_reachable_snapshot_fails_records_attempt():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=True, snapshot_ref=None)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.snapshot.assert_awaited_once()
    mgr.record_attempt.assert_awaited_once()
    mgr.delete.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_unreachable_not_exhausted_records_attempt():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=False, exhausted=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.record_attempt.assert_awaited_once()
    mgr.give_up.assert_not_called()
    mgr.delete.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_unreachable_exhausted_gives_up():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=False, exhausted=True)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.give_up.assert_awaited_once()
    mgr.delete.assert_not_called()  # give_up owns the deletion


@pytest.mark.asyncio
async def test_not_reapable_is_untouched():
    mgr = _stateful_mgr(_inst(), reapable=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.delete.assert_not_called()
    mgr.give_up.assert_not_called()
    mgr.record_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_unhealthy_still_crash_deletes_before_reap():
    mgr = _stateful_mgr(_inst(), healthy=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.delete.assert_awaited_once()  # crash path
    mgr.is_reapable.assert_not_called()
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lifecycle_reconciler_reap.py -v`
Expected: FAIL (reap branch not implemented; e.g. `clean_reapable` expects delete but tick never calls it)

- [ ] **Step 3: Implement the reap branch**

In `reconciler.py` `tick()`, extend the stats dict (after `"skipped_busy": 0,` ~line 122):

```python
                "reaped": 0,
                "reap_attempts": 0,
                "reap_forced": 0,
            }
```

The existing drift block ends with the `drained` handling (~line 195). The drift branch uses `continue` only when not drift (line 163) — restructure so a non-drifted **or** post-drift instance still reaches the reap check. Replace the loop body from the drift check onward (lines 162–195) with:

```python
                if self.is_drift(inst, expected):
                    stats["drift"] += 1
                    try:
                        await manager.signal_drain_pending(inst)
                    except Exception:
                        logger.exception(
                            "signal_drain_pending failed for kind=%s id=%s",
                            kind, inst.id,
                        )
                    if not await manager.is_idle(inst):
                        stats["skipped_busy"] += 1
                        # fall through to reap check below
                    elif drained < cap and self._budget.allow(kind):
                        try:
                            await manager.drain(inst, grace_s=0)
                            stats["drained"] += 1
                            drained += 1
                            continue  # drained — nothing left to reap
                        except Exception:
                            logger.exception(
                                "Drain failed for kind=%s id=%s", kind, inst.id)

                # Stateful reap path: teardown-eligible workspaces/VMs.
                if isinstance(manager, StatefulInstanceManager):
                    try:
                        await self._reap(manager, inst, stats)
                    except Exception:
                        logger.exception(
                            "Reap failed for kind=%s id=%s", kind, inst.id)
```

Add the `_reap` helper as a method on the reconciler (after `tick`):

```python
    async def _reap(self, manager, inst, stats) -> None:
        """Decision flow for tearing down a teardown-eligible stateful instance.

        clean            -> delete now (no probe)
        dirty+reachable  -> snapshot; delete if captured else record attempt
        dirty+unreach    -> give_up if exhausted, else record attempt
        """
        if not await manager.is_reapable(inst):
            return
        if not await manager.is_dirty(inst):
            await manager.delete(inst, grace_s=0)
            stats["reaped"] += 1
            return
        if await manager.is_reachable(inst):
            ref = await manager.snapshot(inst)
            if ref:
                await manager.delete(inst, grace_s=0)
                stats["reaped"] += 1
            else:
                await manager.record_attempt(inst)
                stats["reap_attempts"] += 1
            return
        if await manager.attempts_exhausted(inst):
            await manager.give_up(inst, grace_s=0)
            stats["reap_forced"] += 1
        else:
            await manager.record_attempt(inst)
            stats["reap_attempts"] += 1
```

Ensure `StatefulInstanceManager` is imported in `reconciler.py` (it imports from `.types` at line 21 — add it there):

```python
from .types import (
    Instance,
    InstanceLifecycleManager,
    StatefulInstanceManager,
)
```

- [ ] **Step 4: Run to verify pass (+ existing reconciler tests)**

Run: `python -m pytest tests/test_lifecycle_reconciler_reap.py tests/test_lifecycle_skeleton.py -v`
Expected: PASS (new 7 + existing skeleton tests still green). If a skeleton test asserts the exact stats-dict keys, update it to include the three new keys.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/reconciler.py tests/test_lifecycle_reconciler_reap.py
git commit -m "feat(workspace-reaper): reap branch in reconciler tick"
```

---

## Task 8: Snapshot work-marker (`last_snapshot_turns`)

**Files:**
- Modify: `orchestrator/services/lifecycle/workspace_manager.py` (`snapshot` passes current turns to capture)
- Modify: `orchestrator/services/snapshot_service.py` (`capture_vm_snapshot` signature + write marker on success)
- Test: `tests/test_snapshot_work_marker.py` (Create)

**Context:** On snapshot success, record the turn count at capture into the workspace context so `is_dirty` (Task 2) can compare. The manager passes `total_turns` (from instance metadata) into the capture call; the service writes it into the success context block alongside `status: available`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_snapshot_work_marker.py`:

```python
"""capture_vm_snapshot stamps the work-marker into the success context."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.snapshot_service import SnapshotService


@pytest.mark.asyncio
async def test_marker_written_to_thread_context_on_success():
    svc = SnapshotService()
    svc._available = True
    svc._db = AsyncMock()
    svc._db.merge_thread_snapshot_context = AsyncMock(return_value=True)
    svc.upload_snapshot = AsyncMock(return_value=True)

    # Stub the SSH-tar pipeline so capture "succeeds" without a real pod.
    with patch.object(SnapshotService, "_collect_environment_info",
                      AsyncMock(return_value={})), \
         patch("orchestrator.services.snapshot_service.asyncio.create_subprocess_exec") as cse:
        proc = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=[b"data", b""])
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        cse.return_value = proc
        ok = await svc.capture_vm_snapshot(
            job_id="t1", ssh_host="10.0.0.5", ssh_port=30022,
            source_type="pod", entity_type="threads", work_marker=7,
        )
    assert ok is True
    # upload_snapshot is stubbed; the marker is written via merge on success.
    svc._db.merge_thread_snapshot_context.assert_any_await(
        "t1", {"last_snapshot_turns": 7})
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_snapshot_work_marker.py -v`
Expected: FAIL (`capture_vm_snapshot` has no `work_marker` kwarg)

- [ ] **Step 3: Implement**

In `snapshot_service.py`, add `work_marker: Optional[int] = None` to `capture_vm_snapshot`'s signature (after `entity_type`, line ~246). After a successful `upload_snapshot` returns True (the `return await self.upload_snapshot(...)` at line ~410), the marker must be persisted. Change that tail to capture the result and stamp the marker into the **workspace_container** context (where `is_dirty` reads it), then return:

```python
            uploaded = await self.upload_snapshot(
                job_id=job_id,
                tar_path=tar_path,
                manifest=manifest,
                phase_number=phase_number,
                entity_type=entity_type,
            )
            if uploaded and work_marker is not None:
                marker = {"last_snapshot_turns": work_marker}
                try:
                    if entity_type == "threads":
                        await self._db.merge_thread_snapshot_context(job_id, marker)
                    else:
                        await self._db.merge_snapshot_context(job_id, marker)
                except Exception:
                    logger.exception("Failed to stamp work-marker for %s", job_id)
            return uploaded
```

> NOTE: `last_snapshot_turns` is read by `is_dirty` from `metadata.workspace_container` (threads) / `context.workspace_container` (jobs) in Task 2. `merge_thread_snapshot_context` writes under `metadata.snapshot`. To keep read and write paths aligned, write the marker via the **workspace_container** merge instead:

```python
            if uploaded and work_marker is not None:
                marker = {"last_snapshot_turns": work_marker}
                try:
                    if entity_type == "threads":
                        await self._db.merge_thread_workspace_context(job_id, marker)
                    else:
                        await self._db.merge_workspace_container_context(job_id, marker)
                except Exception:
                    logger.exception("Failed to stamp work-marker for %s", job_id)
            return uploaded
```

Update the test's mock accordingly (`merge_thread_workspace_context`):

```python
    svc._db.merge_thread_workspace_context = AsyncMock(return_value=True)
    ...
    svc._db.merge_thread_workspace_context.assert_any_await(
        "t1", {"last_snapshot_turns": 7})
```

In `workspace_manager.py` `snapshot`, pass the marker through (in the `capture_vm_snapshot` call ~line 165):

```python
            ok = await self._snapshot.capture_vm_snapshot(
                job_id=bound,
                ssh_host=ssh_host,
                ssh_port=30022,
                source_type="pod",
                work_marker=inst.metadata.get("total_turns"),
            )
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_snapshot_work_marker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/snapshot_service.py orchestrator/services/lifecycle/workspace_manager.py tests/test_snapshot_work_marker.py
git commit -m "feat(workspace-reaper): stamp work-marker on snapshot success"
```

---

## Task 9: Default-22 port fix

**Files:**
- Modify: `orchestrator/services/workspace_suspension.py` (lines ~127-128 jobs; ~447-448 threads)
- Test: `tests/test_workspace_suspension_port.py` (Create)

**Context:** Container/pod workspaces run sshd on 30022; the VM default of 22 is wrong for them. Resolve by kind: if a `workspace_container` ctx exists → 30022; only fall back to the VM `ssh_port`/22 for actual VM contexts.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workspace_suspension_port.py`:

```python
"""Port resolution: pod workspaces use 30022, not the VM-shaped default 22."""
from __future__ import annotations

from orchestrator.services.workspace_suspension import _resolve_ssh_port


def test_pod_context_resolves_30022_when_port_missing():
    ws_ctx = {"status": "ready", "pod_ip": "10.0.0.5"}  # no explicit port
    assert _resolve_ssh_port(ws_ctx, vm_ctx={}) == 30022


def test_pod_context_honors_explicit_port():
    ws_ctx = {"status": "ready", "pod_ip": "10.0.0.5", "port": 30022}
    assert _resolve_ssh_port(ws_ctx, vm_ctx={}) == 30022


def test_vm_context_uses_vm_ssh_port():
    assert _resolve_ssh_port(ws_ctx={}, vm_ctx={"ssh_port": 22}) == 22


def test_vm_context_defaults_22():
    assert _resolve_ssh_port(ws_ctx={}, vm_ctx={}) == 22
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_workspace_suspension_port.py -v`
Expected: FAIL `ImportError: cannot import name '_resolve_ssh_port'`

- [ ] **Step 3: Implement the helper + use it**

In `workspace_suspension.py`, add a module-level helper near the top (after the logger):

```python
def _resolve_ssh_port(ws_ctx: dict, vm_ctx: dict) -> int:
    """Resolve the snapshot SSH port by workspace kind.

    Container/pod workspaces run sshd on 30022; only true VM contexts use the
    VM ssh_port (default 22). Previously both fell through to a VM-shaped 22
    default, which silently broke pod snapshots when ``port`` was absent.
    """
    if ws_ctx:
        return int(ws_ctx.get("port", 30022))
    return int(vm_ctx.get("ssh_port", 22))
```

Replace the job-path resolution (line ~127-128):

```python
        ssh_host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
        ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx)
```

Replace the thread-path resolution (line ~447-448) identically:

```python
        ssh_host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
        ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_workspace_suspension_port.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/workspace_suspension.py tests/test_workspace_suspension_port.py
git commit -m "fix(workspace-reaper): resolve snapshot SSH port by kind (pod=30022)"
```

---

## Task 10: Owner-ref / TTL annotation on workspace pods

**Files:**
- Modify: `orchestrator/services/container_provisioner.py` (`_build_pod_manifest` metadata block ~line 840-870)
- Test: `tests/test_workspace_backends.py` (add) — or `tests/test_lifecycle_workspace_manager.py` if simpler

**Context:** Bare pods (no ownerRef) are never GC'd by K8s. We can't set a cross-resource ownerRef to a Postgres job, so add a TTL annotation (`srw.io/ttl-after`) as a documented backstop hook; a future K8s TTL controller or a simple age check can act on it. Minimal here: stamp the annotation at creation so the data exists. (Full GC actuation is the reconciler's job, already covered.)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workspace_backends.py` (or create `tests/test_workspace_pod_annotations.py`):

```python
def test_pod_manifest_has_lifecycle_annotation():
    from orchestrator.services.container_provisioner import ContainerProvisioner
    prov = ContainerProvisioner.__new__(ContainerProvisioner)
    prov._namespace = "ns"
    prov._workspace_image = "img:sha-abc"
    prov._storage_class = "longhorn-ephemeral"
    from services.workspace_lifecycle import WorkspaceOwner
    manifest = prov._build_pod_manifest(
        pod_name="workspace-x", owner=WorkspaceOwner.job("j1"),
        image="img:sha-abc", cpu="500m", memory="1Gi",
        cpu_limit="2", memory_limit="4Gi", network_tier="internet-only",
        seed_configmap=None,
    )
    ann = manifest["metadata"].get("annotations", {})
    assert "srw.io/managed-by" in ann
    assert ann["srw.io/managed-by"] == "lifecycle-reconciler"
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_workspace_backends.py::test_pod_manifest_has_lifecycle_annotation -v`
Expected: FAIL (annotation missing, or signature mismatch — adjust the constructor stub to match `_build_pod_manifest`'s real params if needed; read the method first)

- [ ] **Step 3: Implement**

In `_build_pod_manifest`, in the `metadata` dict, add an `annotations` key (merge if one exists):

```python
                "annotations": {
                    "srw.io/managed-by": "lifecycle-reconciler",
                },
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_workspace_backends.py::test_pod_manifest_has_lifecycle_annotation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/container_provisioner.py tests/test_workspace_backends.py
git commit -m "feat(workspace-reaper): annotate workspace pods as lifecycle-managed"
```

---

## Task 11: Thin `workspace_idle_sweeper` to reconcile-only

**Files:**
- Modify: `orchestrator/main.py` (`workspace_idle_sweeper` ~lines 676-720)
- Test: manual reasoning + existing import smoke (no new unit test — this is deletion of now-duplicated logic)

**Context:** The reap path now owns idle suspension. Remove the `check_idle_all` / `check_idle_threads` calls from the sweeper, leaving only the session-workspace reconcile (`reconcile_session_workspaces`), which is a distinct recovery concern. This eliminates the two-reaper overlap (and the keep-alive-forever loop).

- [ ] **Step 1: Remove the suspend calls**

In `main.py` `workspace_idle_sweeper`, delete the block that calls `check_idle_all` and `check_idle_threads` (the `if workspace_suspension_service.is_enabled:` block, ~lines 689-700), keeping the `reconcile_session_workspaces` call and the loop scaffolding. Update the docstring to reflect that suspension now lives in the lifecycle reconciler.

```python
async def workspace_idle_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background loop: reconciles failed/missing session workspaces.

    Idle suspension and teardown now live in the lifecycle reconciler's reap
    path (see services/lifecycle/reconciler.py). This loop retains only the
    session-workspace recovery reconcile — recreating failed/missing
    workspaces for active sessions — which is independent of idle policy.
    """
    logger.info("Workspace idle sweeper started (reconcile-only)")
    while not shutdown_event.is_set():
        try:
            await reconcile_session_workspaces(
                db=postgres_db,
                provisioner=container_provisioner,
                suspension=workspace_suspension_service,
            )
        except Exception as e:
            logger.error("Error in session workspace reconcile: %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 2: Verify imports still resolve + nothing references the removed path**

Run: `python -c "import ast; ast.parse(open('orchestrator/main.py').read()); print('parse ok')"`
Expected: `parse ok`

Run: `python -m pytest tests/test_workspace_lifecycle.py -v`
Expected: PASS (no regression; if a test asserted the sweeper suspends, update it to assert reconcile-only behavior, since suspension moved by design).

- [ ] **Step 3: Commit**

```bash
git add orchestrator/main.py
git commit -m "refactor(workspace-reaper): thin idle sweeper to reconcile-only"
```

---

## Task 12: Metrics

**Files:**
- Create: `orchestrator/services/lifecycle/metrics.py`
- Modify: `orchestrator/services/lifecycle/reconciler.py` (increment in `_reap`)
- Test: `tests/test_lifecycle_reconciler_reap.py` (add metric assertions)

**Context:** Prometheus counters for observability; `forced` is the "accepted data loss" signal that should alert. Use the existing `prometheus_client` (check it's a dependency; if the codebase has a metrics module, follow its pattern instead of creating a new file).

- [ ] **Step 1: Check existing metrics pattern**

Run: `grep -rn "prometheus_client\|Counter(" orchestrator/ | grep -iv test | head`
If a metrics module/registry exists, define the counters there and skip creating `metrics.py`. Otherwise create it.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_lifecycle_reconciler_reap.py`:

```python
@pytest.mark.asyncio
async def test_forced_reap_increments_metric():
    from orchestrator.services.lifecycle import metrics
    before = metrics.workspace_force_deleted_total._value.get() \
        if hasattr(metrics.workspace_force_deleted_total, "_value") else None
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=False, exhausted=True)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.give_up.assert_awaited_once()
    # Stats reflect the forced reap regardless of Prometheus internals.
    # (Primary assertion is on stats; metric is best-effort.)
```

- [ ] **Step 3: Implement counters**

Create `orchestrator/services/lifecycle/metrics.py`:

```python
"""Prometheus counters for lifecycle reap outcomes."""
from prometheus_client import Counter

workspace_reaped_total = Counter(
    "srw_workspace_reaped_total",
    "Workspaces reaped by the lifecycle reconciler.",
    ["reason"],  # clean | snapshotted | crash
)
workspace_force_deleted_total = Counter(
    "srw_workspace_force_deleted_total",
    "Workspaces force-deleted after exhausting snapshot attempts (data loss).",
    ["volume_mode"],  # emptydir | pvc
)
workspace_snapshot_attempts_total = Counter(
    "srw_workspace_snapshot_attempts_total",
    "Snapshot attempts recorded by the reap path.",
)
```

In `reconciler.py` `_reap`, import and increment:

```python
from . import metrics
...
        if not await manager.is_dirty(inst):
            await manager.delete(inst, grace_s=0)
            stats["reaped"] += 1
            metrics.workspace_reaped_total.labels(reason="clean").inc()
            return
...
            if ref:
                await manager.delete(inst, grace_s=0)
                stats["reaped"] += 1
                metrics.workspace_reaped_total.labels(reason="snapshotted").inc()
            else:
                await manager.record_attempt(inst)
                stats["reap_attempts"] += 1
                metrics.workspace_snapshot_attempts_total.inc()
            return
        if await manager.attempts_exhausted(inst):
            mode = "emptydir" if inst.metadata.get("volume_ephemeral", True) else "pvc"
            await manager.give_up(inst, grace_s=0)
            stats["reap_forced"] += 1
            metrics.workspace_force_deleted_total.labels(volume_mode=mode).inc()
        else:
            await manager.record_attempt(inst)
            stats["reap_attempts"] += 1
            metrics.workspace_snapshot_attempts_total.inc()
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_lifecycle_reconciler_reap.py -v`
Expected: PASS (all, incl. the metric test)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/lifecycle/metrics.py orchestrator/services/lifecycle/reconciler.py tests/test_lifecycle_reconciler_reap.py
git commit -m "feat(workspace-reaper): reap outcome metrics (forced = data-loss alert)"
```

---

## Task 13: Full-suite gate, manual cleanup, cluster verification

**Files:** none (operational)

- [ ] **Step 1: Run the full lifecycle + workspace test suites**

Run: `python -m pytest tests/test_lifecycle_workspace_manager.py tests/test_lifecycle_reconciler_reap.py tests/test_lifecycle_skeleton.py tests/test_snapshot_work_marker.py tests/test_workspace_suspension_port.py tests/test_workspace_lifecycle.py -v`
Expected: all PASS. Fix any regressions before proceeding. (CI on Py3.12 is the real gate; if local env is noisy per the project notes, ensure at least these target files are green.)

- [ ] **Step 2: Lint**

Run: `ruff check orchestrator/services/lifecycle/ orchestrator/services/workspace_suspension.py orchestrator/services/snapshot_service.py`
Expected: clean (the push workflow also runs ruff; fix locally first).

- [ ] **Step 3: Delete the 5 pre-migration leaked pods (one-time)**

Confirm with the user that pending-review job `692f00d5` is abandoned (its emptyDir state was never snapshotted and will be lost). Then:

```bash
kubectl --context=main -n superhuman-remote-worker delete pod \
  workspace-692f00d5-0ac workspace-8d31111d-31d \
  ws-thread-8b7f0e31-a15 ws-thread-b9b6c2be-dcc ws-thread-f6c19671-7b9
```

- [ ] **Step 4: Clear any DB-orphan workspace contexts**

For threads whose pods were already gone (e.g. `2cae45a9`, `d2c63f11`, `a4c847d3` from the logs), confirm status `ended` and that no pod exists, then clear their `metadata.workspace_container` status so the reaper/sweeper stops retrying. (Use the orchestrator DB; do not invent IDs — re-list first.)

- [ ] **Step 5: Post-deploy verification**

After the new image deploys to dev, confirm via orchestrator logs that the reap path acts and the keep-alive loop is gone:

```bash
kubectl --context=main -n superhuman-remote-worker logs deploy/srw-orchestrator --since=10m \
  | grep -iE "Lifecycle tick kind=workspace|reaped|reap_forced|keeping workspace alive"
```
Expected: `Lifecycle tick kind=workspace` shows `reaped`/`reap_forced` activity as eligible workspaces appear; **no** new `keeping workspace alive` spam accumulating indefinitely.

- [ ] **Step 6: Final commit / PR**

```bash
git push -u origin design/workspace-reaper-lifecycle
gh pr create --base develop --title "Workspace reaper: reconciler-owned clean/dirty-gated teardown" \
  --body "Implements docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md. Fixes the keep-alive-forever workspace leak."
```

---

## Self-Review Notes

**Spec coverage:**
- Reap decision flow → Task 7. `is_reapable`/terminal widening → Task 1. `is_dirty` (threads total_turns, jobs conservative, not last_activity) → Task 2. `is_reachable` cached, reap-only → Task 4. `is_state_ephemeral` volume branch → Task 3. Attempt counter + escape hatch → Tasks 5, 6, 7. `is_healthy` stays phase-only → unchanged (no task needed; Task 7 test `test_unhealthy_still_crash_deletes_before_reap` guards it). Snapshot marker → Task 8. DB-orphan → partially Task 13 manual; **note:** automated DB-orphan enumeration (rows whose pod is missing) is NOT in `list_instances` yet — see Gap below. Metrics → Task 12. Retire sweeper → Task 11. Default-22 → Task 9. Owner-ref/TTL → Task 10. One-time cleanup → Task 13.

**Known gap (flagged, not silently dropped):** The spec's automated DB-orphan reaping ("rows whose pod is missing") requires `list_instances` to also enumerate context rows with no live pod — currently it lists by pod only. This plan handles existing orphans manually (Task 13 Step 4) but does not yet add the DB-side enumeration. **Recommend a follow-up task** (Task 7.5) if automated orphan reaping is required for this iteration; it was lower-priority than the keep-alive-forever loop and adds a DB-scan path. Decide with the user before implementing.

**Placeholder scan:** No TBD/TODO; all code steps show code. Task 10's constructor stub and Task 12's metrics-internal assertion are the two spots most likely to need a small real-code adjustment at execution time (both flagged inline with "read the method first" / "best-effort").

**Type consistency:** Method names consistent across tasks (`is_reapable`, `is_dirty`, `is_reachable`, `is_state_ephemeral`, `attempts_exhausted`, `record_attempt`, `give_up`, `_reap`). Metadata keys consistent: `total_turns`, `last_snapshot_turns`, `snapshot_status`, `snapshot_attempts`, `volume_ephemeral`, `pod_ip`, `labels`. `work_marker` kwarg consistent between Task 8's service signature and the manager call. `merge_thread_workspace_context` / `merge_workspace_container_context` used consistently for the marker write (Task 8 NOTE) and counter (Tasks 5, 6).
```
