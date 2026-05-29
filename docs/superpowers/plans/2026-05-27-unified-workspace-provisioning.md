# Unified Workspace Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicated job/session workspace-provisioning code with one owner-keyed path + a shared `ensure_workspace` state machine, and give sessions a reconcile (event + periodic) so a wedged workspace self-heals — closing the stuck-session gap.

**Architecture:** New `orchestrator/services/workspace_lifecycle.py` holds a `WorkspaceOwner` value object and the idempotent `ensure_workspace()` state machine (extracted from the job dispatcher). New `orchestrator/services/session_provisioner.py` holds the session-side ensure entry points + a periodic safety-net. `container_provisioner.py` and `workspace_suspension.py` collapse their parallel `*_workspace`/`*_thread_workspace` methods behind `WorkspaceOwner`. `main.py` deletes the inlined logic and delegates. Spec: `docs/features/unified_workspace_provisioning.md`.

**Tech Stack:** Python 3.12 (CI gate), asyncio, FastAPI, Kubernetes client, pytest + `unittest.mock`. Tests are flat in `tests/test_*.py` (sys.path insert + `from orchestrator.services... import ...`).

---

## Setup

- [ ] **Branch off `develop`** (do not work on `develop`/`main` directly):
```bash
git checkout develop && git pull --ff-only
git checkout -b feat/unified-workspace-provisioning
```
- [ ] Confirm baseline tests pass for the files we touch (records the green starting point):
```bash
pytest tests/test_container_provisioner.py tests/test_workspace_suspension.py tests/test_persistent_provisioner.py -q
```
Expected: PASS (or the same pre-existing failures noted; CI is the real gate).

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `orchestrator/services/workspace_lifecycle.py` | **create** | `WorkspaceOwner` + `ensure_workspace()` state machine + `EnsureOutcome` |
| `orchestrator/services/session_provisioner.py` | **create** | `ensure_session_workspace()` + `reconcile_session_workspaces()` periodic tick |
| `orchestrator/services/container_provisioner.py` | edit | owner-keyed `create_workspace`/`delete_workspace`/`get_workspace_status`/`_set_context` (+ back-compat shims) |
| `orchestrator/services/workspace_suspension.py` | edit | owner-keyed `restore(owner)` wrapping `restore_workspace`/`restore_thread_workspace` |
| `orchestrator/main.py` | edit (net delete) | job dispatcher calls `ensure_workspace`; session blocks delegate to `session_provisioner`; register the periodic tick |
| `orchestrator/routers/sessions.py` | edit | `/prepare` calls `ensure_session_workspace` before binding the agent |
| `src/api/persistent_app.py` | edit (agent image) | `_attach_session` exits cleanly instead of raising when workspace absent |
| `tests/test_workspace_lifecycle.py` | **create** | unit tests for `WorkspaceOwner` + `ensure_workspace` |
| `tests/test_session_provisioner.py` | **create** | unit tests for session ensure + reconcile + the regression scenario |
| `tests/test_container_provisioner.py` | edit | owner-keyed method tests |

---

## Task 1: `WorkspaceOwner` value object

**Files:** Create `orchestrator/services/workspace_lifecycle.py`; Test `tests/test_workspace_lifecycle.py`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_workspace_lifecycle.py
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.workspace_lifecycle import WorkspaceOwner  # noqa: E402


def test_owner_job_naming_and_labels():
    o = WorkspaceOwner.job("abcdef0123456789")
    assert o.kind == "job"
    assert o.pod_name == "workspace-abcdef012345"      # 12-char id truncation
    assert o.label_key == "srw/job-id"
    assert o.component_label == "workspace"
    assert o.network_tier_kind == "job"


def test_owner_session_naming_and_labels():
    o = WorkspaceOwner.session("abcdef0123456789")
    assert o.pod_name == "ws-thread-abcdef012345"
    assert o.label_key == "srw/thread-id"
    assert o.component_label == "thread-workspace"
    assert o.network_tier_kind == "thread"


def test_owner_is_frozen_hashable():
    o = WorkspaceOwner.session("t1")
    with pytest.raises(Exception):
        o.id = "t2"  # frozen
    assert {o: 1}[WorkspaceOwner.session("t1")] == 1  # hashable by (kind, id)
```

- [ ] **Step 2: Run it — expect ImportError/FAIL**
```bash
pytest tests/test_workspace_lifecycle.py -q
```
Expected: FAIL (`No module named ...workspace_lifecycle`).

- [ ] **Step 3: Implement `WorkspaceOwner`**
```python
# orchestrator/services/workspace_lifecycle.py
"""Owner-keyed workspace lifecycle: one provisioning path for jobs and sessions.

See docs/features/unified_workspace_provisioning.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

OwnerKind = Literal["job", "session"]


@dataclass(frozen=True)
class WorkspaceOwner:
    """Identifies whose workspace this is. Collapses the job/thread split."""

    kind: OwnerKind
    id: str

    @classmethod
    def job(cls, job_id: str) -> "WorkspaceOwner":
        return cls("job", job_id)

    @classmethod
    def session(cls, thread_id: str) -> "WorkspaceOwner":
        return cls("session", thread_id)

    @property
    def pod_name(self) -> str:
        prefix = "workspace" if self.kind == "job" else "ws-thread"
        return f"{prefix}-{self.id[:12]}"

    @property
    def label_key(self) -> str:
        return "srw/job-id" if self.kind == "job" else "srw/thread-id"

    @property
    def component_label(self) -> str:
        return "workspace" if self.kind == "job" else "thread-workspace"

    @property
    def network_tier_kind(self) -> str:
        # Arg expected by ContainerProvisioner._resolve_network_tier / DB.
        return "job" if self.kind == "job" else "thread"
```

- [ ] **Step 4: Run — expect PASS**
```bash
pytest tests/test_workspace_lifecycle.py -q
```
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add orchestrator/services/workspace_lifecycle.py tests/test_workspace_lifecycle.py
git commit -m "feat(workspace): add WorkspaceOwner value object

First slice of unified job/session workspace provisioning.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Owner-keyed `ContainerProvisioner` (dedup, behavior-preserving)

**Files:** Modify `orchestrator/services/container_provisioner.py`; Test `tests/test_container_provisioner.py`.

Collapse `create_workspace(job_id)`/`create_thread_workspace(thread_id)`, `delete_*`, `get_workspace_status`, `_set_context`/`_set_thread_context` into owner-keyed methods. The current pod spec (`_build_pod_manifest`) is already shared. The only per-owner bits — pod name, label key/component, network-tier kind, and the DB writer (`merge_workspace_container_context` vs `merge_thread_workspace_context`) — come from `WorkspaceOwner`.

- [ ] **Step 1: Write failing tests** (`tests/test_container_provisioner.py`, append)
```python
@pytest.mark.asyncio
async def test_create_workspace_session_uses_thread_naming_and_store(monkeypatch):
    from orchestrator.services.workspace_lifecycle import WorkspaceOwner
    cp = _make_provisioner_with_mock_k8s()   # existing helper / build inline
    cp._db = AsyncMock()
    cp._core_api.create_namespaced_pod = MagicMock()
    monkeypatch.setattr(cp, "_wait_for_ready", AsyncMock(return_value="10.0.0.5"))

    ok = await cp.create_workspace(WorkspaceOwner.session("thread-abc"))

    assert ok is True
    body = cp._core_api.create_namespaced_pod.call_args.kwargs["body"]
    assert body["metadata"]["name"] == "ws-thread-thread-abc"  # ws-thread-<id[:12]>
    assert body["metadata"]["labels"]["srw/thread-id"] == "thread-abc"
    # session state goes to threads.metadata, not jobs.context
    cp._db.merge_thread_workspace_context.assert_awaited()
    cp._db.merge_workspace_container_context.assert_not_called()
```
(Mirror the existing `tests/test_container_provisioner.py` fixture style; reuse its mock-k8s setup.)

- [ ] **Step 2: Run — expect FAIL** (`create_workspace` doesn't accept an owner yet)
```bash
pytest tests/test_container_provisioner.py -k owner -q
```
Expected: FAIL.

- [ ] **Step 3: Implement owner-keyed methods.** Replace `create_workspace(self, job_id, ...)` (`:153`) and `create_thread_workspace` (`:809`) with a single owner-keyed method; keep the old signatures as shims.
```python
async def create_workspace(self, owner: "WorkspaceOwner", *, cpu="500m",
                           memory="1Gi", cpu_limit="2000m", memory_limit="4Gi",
                           image=None) -> bool:
    if not self._k8s_available:
        return False
    pod_name = owner.pod_name
    workspace_image = image or self._workspace_image
    network_tier = await self._resolve_network_tier(owner.id, kind=owner.network_tier_kind)
    pod_manifest = self._build_pod_manifest(
        pod_name=pod_name, owner=owner, image=workspace_image, cpu=cpu,
        memory=memory, cpu_limit=cpu_limit, memory_limit=memory_limit,
        network_tier=network_tier,
    )
    try:
        await asyncio.to_thread(self._core_api.create_namespaced_pod,
                                namespace=self._namespace, body=pod_manifest)
        await self._set_context(owner, {"status": "created", "pod_name": pod_name,
                                         "namespace": self._namespace})
        pod_ip = await self._wait_for_ready(pod_name, timeout=120)
        if pod_ip:
            await self._set_context(owner, {"status": "ready", "pod_ip": pod_ip, "port": 30022})
        else:
            await self._set_context(owner, {"status": "creating"})
        return True
    except Exception as e:
        logger.error("Failed to create workspace for %s %s: %s", owner.kind, owner.id, e)
        await self._set_context(owner, {"status": "failed", "error": str(e)})
        return False

async def _set_context(self, owner: "WorkspaceOwner", updates: dict) -> None:
    if not self._db:
        return
    try:
        if owner.kind == "job":
            await self._db.merge_workspace_container_context(owner.id, updates)
        else:
            await self._db.merge_thread_workspace_context(owner.id, updates)
    except Exception:
        logger.exception("Failed to update workspace context for %s %s", owner.kind, owner.id)
```
  - Update `_build_pod_manifest` to take `owner` and build labels via `owner.label_key`/`owner.component_label` (fold `_build_workspace_labels` to accept the owner; drop the `# Reuse job_id label slot` hack).
  - Collapse `delete_workspace`/`delete_thread_workspace` → `delete_workspace(owner)` and `get_workspace_status` → `get_workspace_status(owner)` the same way.
  - **Back-compat shims** (so existing callers keep working until Task 4/6):
```python
async def create_thread_workspace(self, thread_id: str, **kw) -> bool:
    from orchestrator.services.workspace_lifecycle import WorkspaceOwner
    return await self.create_workspace(WorkspaceOwner.session(thread_id), **kw)
# (and create_workspace_by_job_id shim if any caller passes job_id positionally)
```
  Note: existing job callers pass `create_workspace(job_id=...)`. Add a tiny adapter or update those call sites in this task (they're few: `main.py:2144`, `workspace_suspension.py:280`).

- [ ] **Step 4: Run — expect PASS** (new owner test + existing container tests green)
```bash
pytest tests/test_container_provisioner.py -q
```
Expected: PASS (behavior unchanged for both kinds).

- [ ] **Step 5: Commit**
```bash
git add orchestrator/services/container_provisioner.py tests/test_container_provisioner.py
git commit -m "refactor(workspace): collapse job/thread provisioning behind WorkspaceOwner

No behavior change; removes the create_thread_workspace copy-paste.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `ensure_workspace` state machine + job dispatcher delegates to it

**Files:** Modify `orchestrator/services/workspace_lifecycle.py`, `orchestrator/services/workspace_suspension.py`, `orchestrator/main.py`; Test `tests/test_workspace_lifecycle.py`.

Extract the container branch of the job dispatcher (`main.py:2119-2240`) into a reusable function. Add `WorkspaceSuspensionService.restore(owner)` wrapping the two existing restore methods.

- [ ] **Step 1: Write failing tests** for the transition table:
```python
# tests/test_workspace_lifecycle.py (append)
import pytest
from unittest.mock import AsyncMock
from orchestrator.services.workspace_lifecycle import ensure_workspace, EnsureOutcome, WorkspaceOwner

@pytest.mark.asyncio
@pytest.mark.parametrize("status,expect_call,outcome", [
    (None,        "create",  EnsureOutcome.PENDING),
    ("failed",    "create",  EnsureOutcome.PENDING),   # recreate, not stuck
    ("deleted",   "create",  EnsureOutcome.PENDING),
    ("suspended", "restore", EnsureOutcome.PENDING),
    ("creating",  None,      EnsureOutcome.PENDING),
    ("restoring", None,      EnsureOutcome.PENDING),
    ("ready",     None,      EnsureOutcome.READY),
])
async def test_ensure_transitions(status, expect_call, outcome):
    prov = AsyncMock(); prov.create_workspace = AsyncMock(return_value=True)
    prov.get_workspace_status = AsyncMock(return_value={"pod_name": "x"})  # pod exists
    susp = AsyncMock(); susp.restore = AsyncMock(return_value=True)
    res = await ensure_workspace(WorkspaceOwner.session("t1"), provisioner=prov,
                                 suspension=susp, current_status=status)
    assert res.outcome == outcome
    if expect_call == "create": prov.create_workspace.assert_awaited()
    elif expect_call == "restore": susp.restore.assert_awaited()
    else: prov.create_workspace.assert_not_called(); susp.restore.assert_not_called()

@pytest.mark.asyncio
async def test_ensure_ready_but_pod_missing_recreates():
    prov = AsyncMock(); prov.create_workspace = AsyncMock(return_value=True)
    prov.get_workspace_status = AsyncMock(return_value=None)  # drift: gone
    susp = AsyncMock()
    res = await ensure_workspace(WorkspaceOwner.session("t1"), provisioner=prov,
                                 suspension=susp, current_status="ready")
    prov.create_workspace.assert_awaited()
    assert res.outcome == EnsureOutcome.PENDING
```

- [ ] **Step 2: Run — expect FAIL.**
```bash
pytest tests/test_workspace_lifecycle.py -k ensure -q
```
Expected: FAIL (`ensure_workspace`/`EnsureOutcome` not defined).

- [ ] **Step 3: Implement `ensure_workspace` + `EnsureOutcome`** in `workspace_lifecycle.py`:
```python
class EnsureOutcome(Enum):
    READY = "ready"        # workspace usable now
    PENDING = "pending"    # in progress (created/restoring/creating) — retry later
    FAILED = "failed"      # creation failed — caller decides policy


@dataclass
class EnsureResult:
    outcome: EnsureOutcome
    status: Optional[str] = None
    error: Optional[str] = None


async def ensure_workspace(owner, *, provisioner, suspension, current_status,
                           ws_config=None) -> EnsureResult:
    """Idempotently drive owner's workspace toward 'ready'. Mirrors the job
    dispatcher's container branch (main.py:2119-2240), owner-agnostic."""
    s = current_status
    if s in (None, "none", "deleted", "failed"):
        ok = await provisioner.create_workspace(owner, **(ws_config or {}))
        return EnsureResult(EnsureOutcome.PENDING if ok else EnsureOutcome.FAILED,
                            status="creating" if ok else "failed")
    if s == "suspended":
        await suspension.restore(owner)          # dispatches docker/vm/k8s internally
        return EnsureResult(EnsureOutcome.PENDING, status="restoring")
    if s in ("creating", "restoring", "suspending"):
        return EnsureResult(EnsureOutcome.PENDING, status=s)
    if s == "ready":
        live = await provisioner.get_workspace_status(owner)
        if not live:                              # drift: pod vanished
            ok = await provisioner.create_workspace(owner, **(ws_config or {}))
            return EnsureResult(EnsureOutcome.PENDING if ok else EnsureOutcome.FAILED)
        return EnsureResult(EnsureOutcome.READY, status="ready")
    return EnsureResult(EnsureOutcome.PENDING, status=s)  # unknown — wait
```
  - Add `WorkspaceSuspensionService.restore(self, owner)` (`workspace_suspension.py`): `return await (self.restore_workspace(owner.id) if owner.kind=="job" else self.restore_thread_workspace(owner.id))`.

- [ ] **Step 4: Run — expect PASS.**
```bash
pytest tests/test_workspace_lifecycle.py -q
```

- [ ] **Step 5: Point the job dispatcher at `ensure_workspace`.** In `main.py`, replace the container branch (`:2119-2240`) with a call:
```python
elif _job_needs_sandbox(job):
    from orchestrator.services.workspace_lifecycle import ensure_workspace, EnsureOutcome, WorkspaceOwner
    ws_cfg = (job.get("config_override") or {}).get("workspace", {}).get("container", {})
    res = await ensure_workspace(
        WorkspaceOwner.job(job_id),
        provisioner=container_provisioner, suspension=workspace_suspension_service,
        current_status=_get_container_context(job).get("status"),
        ws_config={k: ws_cfg[k] for k in ("cpu","memory","cpu_limit","memory_limit","image") if k in ws_cfg},
    )
    if res.outcome is EnsureOutcome.FAILED:
        await postgres_db.update_job_status(job_id, status="failed",
            error_message=f"Workspace container failed: {res.error or 'see logs'}")
        continue
    if res.outcome is EnsureOutcome.PENDING:
        continue                       # wait for next cycle
    # READY → fall through to dispatch
```
  Preserve the docker-pool path: if `docker_provisioner.is_available and not container_provisioner.in_cluster`, keep the existing `assign_workspace` branch (move it into a `provisioner` selection helper used by `ensure_workspace`, or guard before the call). Keep VM branch untouched.

- [ ] **Step 6: Run dispatcher/job tests — expect PASS (job behavior unchanged).**
```bash
pytest tests/test_provision_or_assign_lifecycle.py tests/test_dispatch_phase_credentials.py tests/test_api_orchestrator.py -q
```

- [ ] **Step 7: Commit**
```bash
git add orchestrator/services/workspace_lifecycle.py orchestrator/services/workspace_suspension.py orchestrator/main.py tests/test_workspace_lifecycle.py
git commit -m "refactor(workspace): extract ensure_workspace; job dispatcher delegates

Behavior-preserving extraction of the dispatcher's container branch into a
shared, owner-agnostic state machine.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Session reconcile (the fix) + rewire session call sites

**Files:** Create `orchestrator/services/session_provisioner.py`; modify `orchestrator/main.py`, `orchestrator/routers/sessions.py`; Test `tests/test_session_provisioner.py`.

- [ ] **Step 1: Write failing tests** including the regression scenario:
```python
# tests/test_session_provisioner.py
@pytest.mark.asyncio
async def test_failed_session_workspace_is_recreated():
    """Regression for the stuck RAG session: status 'failed' must recreate."""
    db = AsyncMock(); db.get_thread = AsyncMock(return_value={
        "id": "t1", "status": "active",
        "metadata": {"workspace_container": {"status": "failed"}}})
    prov = AsyncMock(); prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    from orchestrator.services.session_provisioner import ensure_session_workspace
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    prov.create_workspace.assert_awaited()        # not stuck — recreated
    assert res.outcome.value in ("pending", "ready")

@pytest.mark.asyncio
async def test_reconcile_skips_ended_threads():
    db = AsyncMock(); db.list_threads_needing_workspace = AsyncMock(return_value=[])
    from orchestrator.services.session_provisioner import reconcile_session_workspaces
    n = await reconcile_session_workspaces(db=db, provisioner=AsyncMock(), suspension=AsyncMock())
    assert n == 0
```

- [ ] **Step 2: Run — expect FAIL.**
```bash
pytest tests/test_session_provisioner.py -q
```

- [ ] **Step 3: Implement `session_provisioner.py`:**
```python
"""Session-side workspace provisioning + reconcile (the dispatcher-equivalent
for persistent sessions). See docs/features/unified_workspace_provisioning.md."""
from orchestrator.services.workspace_lifecycle import ensure_workspace, WorkspaceOwner

def _ws_status(thread: dict) -> str | None:
    md = thread.get("metadata") or {}
    if isinstance(md, str):
        import json; md = json.loads(md or "{}")
    return (md.get("workspace_container") or {}).get("status")

async def ensure_session_workspace(thread_id, *, db, provisioner, suspension):
    thread = await db.get_thread(thread_id)
    if not thread or thread.get("status") == "ended":
        return None
    return await ensure_workspace(
        WorkspaceOwner.session(thread_id), provisioner=provisioner,
        suspension=suspension, current_status=_ws_status(thread))

async def reconcile_session_workspaces(*, db, provisioner, suspension) -> int:
    """Safety-net: re-ensure workspaces for connecting/active threads that are
    not 'ready'. Idempotent; ensure_workspace no-ops in-progress states."""
    threads = await db.list_threads_needing_workspace()   # status in (created,active) AND ws status NOT ready
    for t in threads:
        await ensure_session_workspace(t["id"], db=db, provisioner=provisioner, suspension=suspension)
    return len(threads)
```
  - Add `PostgresDB.list_threads_needing_workspace()` (mirror `workspace_suspension.py:677` query style) selecting threads in `('created','active')` whose `metadata->workspace_container->>status` is distinct from `'ready'` (and excluding in-progress `creating/restoring`). Add a unit test for the SQL via the existing mock-conn pattern.

- [ ] **Step 4: Rewire session call sites** in `main.py` to use `ensure_session_workspace`, replacing the `status == "suspended"`-only gates:
  - `_provision_thread_workspace` (`:11666`) and the standalone create (`:10663`) → `await ensure_session_workspace(...)`.
  - `resume_thread` (`:12381`), `_resolve_thread_for_forwarding` (`:12479`), `_phase5_wake_if_suspended` (`:13393`) → replace `if status=="suspended": restore_thread_workspace(...)` with `await ensure_session_workspace(...)` (now handles failed/none/suspended).
  - `routers/sessions.py` `/prepare` (`:73-228`) → `await ensure_session_workspace(thread_id, ...)` before binding the agent.

- [ ] **Step 5: Register the periodic safety-net.** In `main.py`, extend the existing `auto_assign_dispatcher` tick (or add a sibling `asyncio` task) to call `reconcile_session_workspaces(...)` each cycle. Log a one-line summary `{ensured: N}`.

- [ ] **Step 6: Run — expect PASS.**
```bash
pytest tests/test_session_provisioner.py tests/test_sessions_router_prepare.py tests/test_persistent_provisioner.py -q
```

- [ ] **Step 7: Commit**
```bash
git add orchestrator/services/session_provisioner.py orchestrator/main.py orchestrator/routers/sessions.py orchestrator/database/postgres.py tests/test_session_provisioner.py
git commit -m "feat(workspace): session-side ensure/reconcile (closes stuck-workspace gap)

Sessions now recreate failed/missing workspaces like jobs do, on prepare/resume
and via a periodic safety-net. Replaces the suspended-only restore gates.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: De-race — agent exits cleanly instead of crash-looping (agent image)

**Files:** Modify `src/api/persistent_app.py`; Test `tests/test_persistent_app.py`.

This ships in the **agent** image (separate deployable). `_attach_session` (`:643`) currently `raise`s when `_poll_workspace_ready` returns `None`, aborting uvicorn startup (exit 3). Change to a clean, non-error exit so the orchestrator's session reconcile re-binds once the workspace is ready.

- [ ] **Step 1: Write failing test** — patch `_poll_workspace_ready` to return None; assert `_attach_session` raises `WorkspaceNotReady` (typed), and the lifespan handler calls a graceful shutdown (no unhandled RuntimeError).
- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_persistent_app.py -k workspace_not_ready -q`
- [ ] **Step 3: Implement:** introduce `class WorkspaceNotReady(Exception)`, raise it instead of the bare `RuntimeError` at `:643`; in `lifespan` (`:497`), catch it, log a clear info line, best-effort notify the orchestrator (`deregister`/mark bind-failed so reconcile rebinds), and exit the process with status 0 (e.g. `os._exit(0)` after cleanup, or signal uvicorn to stop) — pod completes, not Error.
- [ ] **Step 4: Run — expect PASS.** `pytest tests/test_persistent_app.py -q`
- [ ] **Step 5: Commit**
```bash
git add src/api/persistent_app.py tests/test_persistent_app.py
git commit -m "fix(agent): exit cleanly when workspace not ready, let orchestrator rebind

Replaces the RuntimeError crash-loop (exit 3) on missing workspace with a
graceful exit; pairs with the session reconcile in the orchestrator.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Remove back-compat shims

**Files:** `orchestrator/services/container_provisioner.py`, `workspace_suspension.py`, any remaining callers.

- [ ] **Step 1:** `grep -rn "create_thread_workspace\|restore_thread_workspace" orchestrator/ --include=*.py` — confirm the only remaining references are the shims + the internal restore dispatch.
- [ ] **Step 2:** Migrate the internal restore callers (`workspace_suspension.py:581`, `lifecycle/*_manager.py`) to the owner API; delete the shims.
- [ ] **Step 3:** Run the full touched-area suite:
```bash
pytest tests/test_container_provisioner.py tests/test_workspace_suspension.py tests/test_workspace_lifecycle.py tests/test_session_provisioner.py tests/test_lifecycle_workspace_manager.py -q
```
- [ ] **Step 4: Commit** (`refactor(workspace): drop create_thread_workspace shims` + trailer).

---

## Final verification

- [ ] `pytest -q` for the full touched set (Task 6 list + dispatcher/job tests). CI (Py3.12) is the gate.
- [ ] `ruff check orchestrator/services/workspace_lifecycle.py orchestrator/services/session_provisioner.py` (push auto-runs ruff; pre-empt it).
- [ ] Grep confirms `main.py` shrank: the container branch (`~120 lines`) and the three suspended-gates are gone, replaced by `ensure_*` calls.
- [ ] Manual smoke (dev, after deploy): suspend a session → resume → workspace restores; force a `failed` workspace_container row → confirm the safety-net recreates it within one tick and the agent binds.

## Rollout / deploy note

Two deployables change: **orchestrator** (Tasks 2–4, 6) and the **agent image** (Task 5). They are independently safe — the orchestrator reconcile works with the old agent (agent still crashes but gets rebound), and the new agent works with the old orchestrator (just exits cleanly). Land orchestrator first; Fleet rolls both via the normal `develop` → CI → tag-bump path.

## Execution handoff

Two options:
1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (superpowers:subagent-driven-development).
2. **Inline** — execute here with checkpoints (superpowers:executing-plans).

Which approach?
