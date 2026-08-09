# Session Config Drift on Resume — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the silent 403 that permanently bricks a session whose connector, project, or grant disappeared with a dialog listing everything that drifted and a "resume without them" acknowledgment.

**Architecture:** The policy layer gains per-item *reporting* entry points; today's raising functions become thin wrappers over them, so the rules keep exactly one implementation. A new `collect_config_drift()` composes those reports across three families (connectors, projects, grants). `POST /resume` returns **428** carrying the drift list; the client re-POSTs with `acknowledge`, which is stored in `metadata.config_drift_ack` and honored at attach — the stored config is never rewritten.

**Tech Stack:** Python 3.12 (CI gate) / FastAPI / asyncpg / pytest + pytest-asyncio; Angular + signals / vitest / transloco.

**Spec:** `docs/features/session_config_drift_resume.md` (commit `03f9aefd`)

## Global Constraints

- Work on `develop`. No sub-branches. **Never push without asking the user.**
- `develop` has concurrent agent writers — re-check `HEAD` before any commit; never `--amend` without re-checking.
- Next free app migration number is **0115**. Re-verify it is still free immediately before committing — duplicate prefixes hard-fail the migration runner at boot.
- Local pytest is noisy under Python 3.14; run the specific test files named in each task. CI (3.12) is the real gate.
- `ruff` runs automatically on push. Keep lines ≤ 88 chars.
- asyncpg returns JSONB columns as **raw JSON strings** — `json.loads` every read; there is no global codec.
- Cockpit: `tsc -p tsconfig.json` checks **nothing**. Use `npx tsc -p cockpit/tsconfig.app.json --noEmit`.
- `PersistentChatComponent` is unmountable in specs (NG0951). Any logic that needs a test must be a **pure exported function**, not a method.
- Both `cockpit/src/assets/i18n/en.json` and `de-DE.json` must gain every new key.
- The generic denial string `GENERIC_UNAVAILABLE_DETAIL` must never be replaced with an enumerating message on the create/PATCH paths — that is a deliberate anti-enumeration property.

---

### Task 1: Per-item verdicts in the connector policy layer

Refactor `authorize_datasource_selection` into a wrapper over a new reporting function. **No behavior change** — this task is pure groundwork, and its tests exist to prove nothing changed.

**Files:**
- Modify: `orchestrator/services/datasource_policy.py:122-218`
- Test: `tests/test_datasource_policy.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ItemVerdict:
      datasource_id: str
      denied: bool
      reason: str | None   # "deleted" | "revoked" | "out_of_scope" | "workspace_tier" | None

  async def classify_datasource_selection(
      db, actor, effective_work_owner_id, datasource_ids, target_project_ids,
      workspace_backend, allow_admin_explicit_override=False,
      trusted_system_inheritance=False, legacy_job_id=None,
  ) -> tuple[list[ItemVerdict], dict[str, int]]
  ```

**Ordering hazard — read before writing.** Precedence has *two* tiers, and
conflating them is a real regression (it was caught in review on the first
attempt at this task):

1. A **missing row** was rejected by a `len(by_id) != len(normalized_ids)` check
   that ran *before* the per-item loop. So `deleted` is **position-independent**
   and outranks everything.
2. Every other failure — scope mismatch, not-authorized, lite-tier repository,
   bad `policy_revision` — raised from *inside* the loop, so it is strictly
   **first-in-list-order**. `out_of_scope` and `revoked` are on equal footing
   with `workspace_tier`, **not** senior to it.

Treating all non-tier denials as globally senior turns a 400 into a 403 for
`[repository-on-lite-tier, out-of-scope]` — an already-shipped create/PATCH
path. `policy_revision` corruption must therefore also become a verdict rather
than an in-loop raise, or it jumps the queue the same way. Only
`_normalized_ids` (malformed uuid) and `_validate_effective_owner` (missing
owner) keep raising early, because they ran before the loop originally.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_datasource_policy.py`:

```python
from orchestrator.services.datasource_policy import (
    ItemVerdict,
    classify_datasource_selection,
)


@pytest.mark.asyncio
async def test_classify_reports_missing_row_as_deleted():
    db = _db([_row(DS_OWNED)])

    verdicts, _revisions = await classify_datasource_selection(
        db,
        {"id": OWNER, "is_admin": False},
        OWNER,
        [DS_OWNED, DS_SHARED],
        [],
        None,
    )

    assert verdicts == [
        ItemVerdict(DS_OWNED, False, None),
        ItemVerdict(DS_SHARED, True, "deleted"),
    ]


@pytest.mark.asyncio
async def test_classify_reports_unauthorized_row_as_revoked():
    db = _db([_row(DS_SHARED, owner=OTHER)])

    verdicts, _revisions = await classify_datasource_selection(
        db,
        {"id": OWNER, "is_admin": False},
        OWNER,
        [DS_SHARED],
        [],
        None,
    )

    assert verdicts == [ItemVerdict(DS_SHARED, True, "revoked")]


@pytest.mark.asyncio
async def test_classify_reports_scope_mismatch_as_out_of_scope():
    db = _db([_row(DS_OWNED, scope="projects", projects=(PROJECT_B,))])

    verdicts, _revisions = await classify_datasource_selection(
        db,
        {"id": OWNER, "is_admin": False},
        OWNER,
        [DS_OWNED],
        [PROJECT_A],
        None,
    )

    assert verdicts == [ItemVerdict(DS_OWNED, True, "out_of_scope")]


@pytest.mark.asyncio
async def test_deleted_outranks_tier_error_even_when_it_comes_second():
    """The one position-independent case: the pre-refactor len() check ran
    before the loop, so a missing id wins from any position."""
    db = _db([_row(DS_REPOSITORY, ds_type="repository")])

    with pytest.raises(DatasourceUnavailableError):
        await authorize_datasource_selection(
            db,
            {"id": OWNER, "is_admin": False},
            OWNER,
            [DS_REPOSITORY, DS_SHARED],
            [],
            "virtual",
        )


@pytest.mark.asyncio
async def test_tier_error_wins_when_the_repository_comes_first():
    """In-loop failures are first-in-order. A `deleted`-only precedence test
    cannot catch a wrapper that wrongly promotes out_of_scope above tier,
    because `deleted` is position-independent either way — these two ordered
    pairs are what actually discriminate."""
    db = _db([
        _row(DS_REPOSITORY, ds_type="repository"),
        _row(DS_OWNED, scope="projects", projects=(PROJECT_B,)),
    ])

    with pytest.raises(DatasourceWorkspaceTierError):
        await authorize_datasource_selection(
            db,
            {"id": OWNER, "is_admin": False},
            OWNER,
            [DS_REPOSITORY, DS_OWNED],
            [PROJECT_A],
            "virtual",
        )


@pytest.mark.asyncio
async def test_out_of_scope_wins_when_it_comes_first():
    db = _db([
        _row(DS_OWNED, scope="projects", projects=(PROJECT_B,)),
        _row(DS_REPOSITORY, ds_type="repository"),
    ])

    with pytest.raises(DatasourceUnavailableError):
        await authorize_datasource_selection(
            db,
            {"id": OWNER, "is_admin": False},
            OWNER,
            [DS_OWNED, DS_REPOSITORY],
            [PROJECT_A],
            "virtual",
        )


@pytest.mark.asyncio
async def test_authorize_still_raises_generic_error_for_missing_row():
    """The non-enumeration property create/PATCH depend on."""
    db = _db([_row(DS_OWNED)])

    with pytest.raises(DatasourceUnavailableError) as excinfo:
        await authorize_datasource_selection(
            db,
            {"id": OWNER, "is_admin": False},
            OWNER,
            [DS_OWNED, DS_SHARED],
            [],
            None,
        )

    assert str(excinfo.value) == "One or more selected connectors are unavailable"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_datasource_policy.py -k "classify or beats_workspace or still_raises_generic" -v`
Expected: FAIL with `ImportError: cannot import name 'ItemVerdict'`

- [ ] **Step 3: Add the dataclass and the reporting function**

In `orchestrator/services/datasource_policy.py`, add `from dataclasses import dataclass` to the imports, then insert above `authorize_datasource_selection`:

```python
@dataclass(frozen=True)
class ItemVerdict:
    """One connector's availability decision, for callers that must report
    rather than deny. ``reason`` is None exactly when ``denied`` is False."""

    datasource_id: str
    denied: bool
    reason: str | None = None


async def classify_datasource_selection(
    db,
    actor: dict[str, Any] | None,
    effective_work_owner_id: str | None,
    datasource_ids: list[str] | None,
    target_project_ids: list[str] | None,
    workspace_backend: str | None,
    allow_admin_explicit_override: bool = False,
    trusted_system_inheritance: bool = False,
    legacy_job_id: str | None = None,
) -> tuple[list[ItemVerdict], dict[str, int]]:
    """Per-item availability verdicts plus the policy snapshot of allowed rows.

    The reporting half of :func:`authorize_datasource_selection`, which is now a
    thin wrapper over this.

    Malformed uuids and a vanished owner row still raise from here: both were
    checked *before* the per-item loop pre-refactor, so raising early is
    faithful. An unreadable ``policy_revision`` instead becomes a
    ``corrupt_revision`` verdict, so it keeps its position in list order; the
    wrapper turns it into the same generic denial the original raised. It is
    deliberately not an acknowledgeable drift reason — corruption is not drift.

    ``workspace_tier`` is likewise returned as a verdict rather than raised, so
    the caller controls precedence; see the wrapper.
    """
    normalized_ids = _normalized_ids(datasource_ids)
    normalized_projects = _normalized_ids(target_project_ids)
    try:
        normalized_legacy_job_id = (
            str(UUID(str(legacy_job_id))) if legacy_job_id else None
        )
    except (TypeError, ValueError) as exc:
        raise DatasourceUnavailableError() from exc
    if not normalized_ids:
        return [], {}

    owner_is_authoritative = await _validate_effective_owner(
        db, effective_work_owner_id, normalized_projects
    )
    owner_id = (
        str(UUID(str(effective_work_owner_id)))
        if owner_is_authoritative and effective_work_owner_id
        else None
    )
    target_set = set(normalized_projects)
    if normalized_legacy_job_id is None:
        rows = await db.get_datasource_policy_rows(normalized_ids)
    else:
        rows = await db.get_datasource_policy_rows(
            normalized_ids,
            legacy_job_id=normalized_legacy_job_id,
        )
    by_id = {str(row["id"]): row for row in rows}

    actor_is_admin = bool(actor and actor.get("is_admin"))
    admin_override = allow_admin_explicit_override and actor_is_admin

    verdicts: list[ItemVerdict] = []
    revisions: dict[str, int] = {}
    for datasource_id in normalized_ids:
        row = by_id.get(datasource_id)
        if row is None:
            verdicts.append(ItemVerdict(datasource_id, True, "deleted"))
            continue
        if not _scope_matches(row, target_set):
            verdicts.append(ItemVerdict(datasource_id, True, "out_of_scope"))
            continue

        linked_to_every_target = bool(target_set) and target_set.issubset(
            _row_project_ids(row)
        )
        native_target = _native_project_id(row)
        native_access = bool(
            owner_is_authoritative
            and native_target is not None
            and native_target in target_set
        )
        legacy_job_binding = bool(
            normalized_legacy_job_id
            and str(row.get("job_id") or "") == normalized_legacy_job_id
        )
        execution_authorized = (
            trusted_system_inheritance
            or legacy_job_binding
            or (owner_id is not None and str(row.get("created_by") or "") == owner_id)
            or bool(row.get("is_global"))
            or (owner_is_authoritative and linked_to_every_target)
            or native_access
            or admin_override
        )
        if not execution_authorized:
            verdicts.append(ItemVerdict(datasource_id, True, "revoked"))
            continue
        if _is_lite_repository(row, workspace_backend):
            verdicts.append(ItemVerdict(datasource_id, True, "workspace_tier"))
            continue
        try:
            policy_revision = int(row["policy_revision"])
            if policy_revision < 1:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            # A verdict, not a raise: raising here would jump ahead of an
            # EARLIER item's workspace_tier verdict, which the original never
            # did. Not an acknowledgeable drift reason — corruption is not
            # drift — so config_drift.py filters it out.
            verdicts.append(ItemVerdict(datasource_id, True, "corrupt_revision"))
            continue
        revisions[datasource_id] = policy_revision
        verdicts.append(ItemVerdict(datasource_id, False, None))
    return verdicts, revisions
```

- [ ] **Step 4: Rewrite the enforcing function as a wrapper**

Replace the body of `authorize_datasource_selection` — **keep its existing docstring verbatim** — with:

```python
    verdicts, revisions = await classify_datasource_selection(
        db,
        actor,
        effective_work_owner_id,
        datasource_ids,
        target_project_ids,
        workspace_backend,
        allow_admin_explicit_override=allow_admin_explicit_override,
        trusted_system_inheritance=trusted_system_inheritance,
        legacy_job_id=legacy_job_id,
    )
    # Missing rows were rejected by a len() check that ran BEFORE the per-item
    # loop, so "deleted" is position-independent and outranks everything else.
    if any(v.reason == "deleted" for v in verdicts):
        raise DatasourceUnavailableError()
    # Every other failure raised from inside that loop, so it is strictly
    # first-in-list-order: the first denied item decides which error surfaces.
    # out_of_scope and revoked are NOT senior to workspace_tier — whichever
    # item comes first wins.
    for verdict in verdicts:
        if not verdict.denied:
            continue
        if verdict.reason == "workspace_tier":
            raise DatasourceWorkspaceTierError(
                "Repository connectors require a workspace with filesystem support"
            )
        raise DatasourceUnavailableError()
    return [v.datasource_id for v in verdicts], revisions
```

Add `ItemVerdict` and `classify_datasource_selection` to the module's `__all__` list.

- [ ] **Step 5: Run the full policy suite**

Run: `python -m pytest tests/test_datasource_policy.py -v`
Expected: PASS — every pre-existing test included. Any pre-existing failure means the refactor changed behavior; fix it before continuing.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/datasource_policy.py tests/test_datasource_policy.py
git commit -m "refactor(datasources): report per-item verdicts behind the generic denial"
```

---

### Task 2: Per-item verdicts for project attachments

Same inversion for project mounts.

**Files:**
- Modify: `orchestrator/main.py:27576-27597` (`_authorize_thread_project_ids`)
- Test: `tests/test_thread_project_verdicts.py` (create)

**Interfaces:**
- Consumes: `ItemVerdict` pattern from Task 1 (this task defines its own type; they are not shared).
- Produces:
  ```python
  @dataclass(frozen=True)
  class ProjectVerdict:
      project_id: str
      denied: bool
      reason: str | None   # "deleted" | "revoked" | None

  async def _classify_thread_project_ids(
      user: dict[str, Any], project_ids: list[str] | None
  ) -> list[ProjectVerdict]
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_thread_project_verdicts.py`:

```python
"""Per-item project attachment verdicts (config-drift reporting layer)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import ProjectVerdict, _classify_thread_project_ids


USER = {"id": "11111111-1111-4111-8111-111111111111", "is_admin": False}
PROJECT_ALIVE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_GONE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROJECT_NO_ROLE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


@pytest.mark.asyncio
async def test_classify_projects_reports_deleted_and_revoked():
    projects = {
        PROJECT_ALIVE: {"id": PROJECT_ALIVE},
        PROJECT_NO_ROLE: {"id": PROJECT_NO_ROLE},
    }
    roles = {PROJECT_ALIVE: "owner"}

    with patch("orchestrator.main.postgres_db") as db:
        db.get_project = AsyncMock(side_effect=lambda pid: projects.get(pid))
        db.get_user_role_in_project = AsyncMock(
            side_effect=lambda pid, uid: roles.get(pid)
        )

        verdicts = await _classify_thread_project_ids(
            USER, [PROJECT_ALIVE, PROJECT_GONE, PROJECT_NO_ROLE]
        )

    assert verdicts == [
        ProjectVerdict(PROJECT_ALIVE, False, None),
        ProjectVerdict(PROJECT_GONE, True, "deleted"),
        ProjectVerdict(PROJECT_NO_ROLE, True, "revoked"),
    ]


@pytest.mark.asyncio
async def test_admin_is_allowed_on_any_existing_project():
    with patch("orchestrator.main.postgres_db") as db:
        db.get_project = AsyncMock(return_value={"id": PROJECT_ALIVE})
        db.get_user_role_in_project = AsyncMock(return_value=None)

        verdicts = await _classify_thread_project_ids(
            {"id": USER["id"], "is_admin": True}, [PROJECT_ALIVE]
        )

    assert verdicts == [ProjectVerdict(PROJECT_ALIVE, False, None)]


@pytest.mark.asyncio
async def test_admin_still_denied_on_deleted_project():
    with patch("orchestrator.main.postgres_db") as db:
        db.get_project = AsyncMock(return_value=None)
        db.get_user_role_in_project = AsyncMock(return_value=None)

        verdicts = await _classify_thread_project_ids(
            {"id": USER["id"], "is_admin": True}, [PROJECT_GONE]
        )

    assert verdicts == [ProjectVerdict(PROJECT_GONE, True, "deleted")]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_thread_project_verdicts.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProjectVerdict'`

- [ ] **Step 3: Add the dataclass and reporting function**

In `orchestrator/main.py`, immediately above `_authorize_thread_project_ids`:

```python
@dataclass(frozen=True)
class ProjectVerdict:
    """One project attachment's availability decision."""

    project_id: str
    denied: bool
    reason: str | None = None


async def _classify_thread_project_ids(
    user: dict[str, Any], project_ids: list[str] | None
) -> list[ProjectVerdict]:
    """Per-item project verdicts. Reporting half of
    :func:`_authorize_thread_project_ids`, which wraps this."""
    selected = list(dict.fromkeys(str(value) for value in project_ids or []))
    verdicts: list[ProjectVerdict] = []
    for project_id in selected:
        project = await postgres_db.get_project(project_id)
        if not project:
            verdicts.append(ProjectVerdict(project_id, True, "deleted"))
            continue
        if user.get("is_admin"):
            verdicts.append(ProjectVerdict(project_id, False, None))
            continue
        role = await postgres_db.get_user_role_in_project(project_id, str(user["id"]))
        if not role:
            verdicts.append(ProjectVerdict(project_id, True, "revoked"))
            continue
        verdicts.append(ProjectVerdict(project_id, False, None))
    return verdicts
```

If `orchestrator/main.py` does not already import `dataclass`, add `from dataclasses import dataclass` to its imports.

- [ ] **Step 4: Rewrite the authorizer as a wrapper**

Replace the body of `_authorize_thread_project_ids` — **keep its docstring verbatim** — with:

```python
    selected = list(dict.fromkeys(str(value) for value in project_ids or []))
    if not selected:
        return []
    verdicts = await _classify_thread_project_ids(user, selected)
    if any(v.denied for v in verdicts):
        raise HTTPException(
            status_code=403,
            detail="One or more attached projects are unavailable",
        )
    return selected
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_thread_project_verdicts.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add orchestrator/main.py tests/test_thread_project_verdicts.py
git commit -m "refactor(projects): report per-item attachment verdicts"
```

---

### Task 3: Surface grant violations through the resolve status

`GrantDenied` already carries `violations: list[str]`; `_resolve_session_config` already takes a `status` out-parameter. Record one into the other, so the drift collector reads violations from the exact code path attach enforces — no second merge implementation.

**Files:**
- Modify: `orchestrator/main.py:2552-2556` (the `except GrantDenied` block)
- Test: `tests/test_session_resolve_grant_status.py` (create)

**Interfaces:**
- Produces: `_resolve_session_config(..., status=status)` sets `status["grant_violations"] = list[str]` alongside the existing `status["state"] = "denied"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_resolve_grant_status.py`:

```python
"""_resolve_session_config records grant violations for the drift collector."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import GrantDenied, _resolve_session_config


THREAD = {"id": "11111111-1111-4111-8111-111111111111", "user_id": "u1"}


@pytest.mark.asyncio
async def test_grant_denied_records_violations_in_status():
    violations = ["shell_tools: tools.shell requires the shell_tools grant"]
    status: dict = {}

    with (
        patch("orchestrator.main._is_experts_db_enabled", return_value=True),
        patch("orchestrator.main._user_experts_enabled", AsyncMock(return_value=True)),
        patch(
            "orchestrator.main._enforce_dispatch_grants",
            AsyncMock(side_effect=GrantDenied(violations)),
        ),
        patch("orchestrator.main.postgres_db"),
    ):
        with pytest.raises(GrantDenied):
            await _resolve_session_config(THREAD, {}, status=status)

    assert status["state"] == "denied"
    assert status["grant_violations"] == violations
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_session_resolve_grant_status.py -v`
Expected: FAIL with `KeyError: 'grant_violations'`

If it instead fails earlier (the resolve throws before reaching the grant call), extend the `patch` list with whatever `_resolve_session_config` calls before `_enforce_dispatch_grants` — read `orchestrator/main.py:2470-2530` and mock each collaborator. Do **not** weaken the assertion.

- [ ] **Step 3: Record the violations**

In `orchestrator/main.py`, change the `except GrantDenied:` block inside `_resolve_session_config`:

```python
    except GrantDenied as gd:
        if status is not None:
            status["state"] = "denied"
            # The drift collector reads these rather than re-merging the config
            # itself — one merge implementation, so the dialog can never promise
            # something different from what attach enforces.
            status["grant_violations"] = list(gd.violations)
        raise
```

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_session_resolve_grant_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_session_resolve_grant_status.py
git commit -m "feat(sessions): record grant violations on the resolve status"
```

---

### Task 4: The drift collector

**Files:**
- Create: `orchestrator/services/config_drift.py`
- Test: `tests/test_config_drift.py` (create)

**Interfaces:**
- Consumes: `classify_datasource_selection` / `ItemVerdict` (Task 1), `_classify_thread_project_ids` / `ProjectVerdict` (Task 2), `status["grant_violations"]` (Task 3).
- Produces:
  ```python
  @dataclass(frozen=True)
  class DriftItem:
      id: str      # "connector:<uuid>" | "project:<uuid>" | "grant:<key>"
      kind: str    # "connector" | "project" | "grant"
      reason: str  # "deleted" | "revoked" | "out_of_scope"
      label: str

  def drift_labels(items: list[DriftItem]) -> list[dict[str, Any]]
  async def collect_config_drift(
      db, thread, *, owner, project_ids, datasource_ids, grant_violations,
      tombstones=None,
  ) -> list[DriftItem]
  ```

`collect_config_drift` is deliberately **pure with respect to I/O it does not own**: the caller passes already-computed verdict inputs. This keeps it unit-testable without mocking the FastAPI module and stops it becoming a second place that decides policy.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_drift.py`:

```python
"""Drift enumeration across connectors, projects and grants."""

from __future__ import annotations

import pytest

from orchestrator.services.config_drift import (
    DriftItem,
    collect_config_drift,
    drift_labels,
)
from orchestrator.services.datasource_policy import ItemVerdict


DS_GONE = "d7555d5d-ce46-49e2-b1fa-8235d720badc"
DS_OK = "2991589e-249d-4cca-98ce-780db69b2520"
DS_REVOKED = "33333333-3333-4333-8333-333333333333"
PROJECT_GONE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class _ProjectVerdict:
    def __init__(self, project_id, denied, reason):
        self.project_id = project_id
        self.denied = denied
        self.reason = reason


@pytest.mark.asyncio
async def test_no_drift_returns_empty():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_OK, False, None)],
        grant_violations=[],
    )
    assert items == []


@pytest.mark.asyncio
async def test_deleted_connector_named_from_tombstone():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_GONE, True, "deleted")],
        grant_violations=[],
        tombstones={DS_GONE: "KurortEngine"},
    )
    assert items == [
        DriftItem(f"connector:{DS_GONE}", "connector", "deleted", "KurortEngine")
    ]


@pytest.mark.asyncio
async def test_deleted_connector_without_tombstone_falls_back_to_uuid():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_GONE, True, "deleted")],
        grant_violations=[],
    )
    assert items[0].label == DS_GONE


@pytest.mark.asyncio
async def test_revoked_connector_is_not_named():
    """Naming a revoked connector would confirm it still exists and reveal its
    current name — a genuine enumeration oracle. Deleted rows carry no such
    risk, which is why only they are named."""
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_REVOKED, True, "revoked")],
        grant_violations=[],
        tombstones={DS_REVOKED: "Should Not Appear"},
    )
    assert items[0].label == "a connector you no longer have access to"
    assert "Should Not Appear" not in items[0].label


@pytest.mark.asyncio
async def test_out_of_scope_connector_is_reported_generically():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_REVOKED, True, "out_of_scope")],
        grant_violations=[],
    )
    assert items[0].reason == "out_of_scope"
    assert items[0].label == "a connector you no longer have access to"


@pytest.mark.asyncio
async def test_workspace_tier_verdict_is_not_drift():
    """A lite-tier repository conflict is a config incompatibility, not
    something an acknowledgment can resolve. It must keep raising 400."""
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_OK, True, "workspace_tier")],
        grant_violations=[],
    )
    assert items == []


@pytest.mark.asyncio
async def test_corrupt_revision_verdict_is_not_drift():
    """Corruption is not drift: no acknowledgment can make a bad
    policy_revision safe, so it must keep failing closed."""
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_OK, True, "corrupt_revision")],
        grant_violations=[],
    )
    assert items == []


@pytest.mark.asyncio
async def test_undenied_connector_is_not_drift_even_with_an_ack_reason():
    """Pins the `denied` half of the guard INDEPENDENTLY of the reason half.
    Without this, dropping `not verdict.denied` from the guard passes every
    other test in this file — the no-drift cases all reach their empty result
    through the reason check alone."""
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[ItemVerdict(DS_OK, False, "deleted")],
        grant_violations=[],
        tombstones={DS_OK: "Should Not Appear"},
    )
    assert items == []


@pytest.mark.asyncio
async def test_undenied_project_is_not_drift_even_with_an_ack_reason():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[_ProjectVerdict(PROJECT_GONE, False, "deleted")],
        datasource_ids=[],
        grant_violations=[],
    )
    assert items == []


@pytest.mark.asyncio
async def test_deleted_project_reported():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[_ProjectVerdict(PROJECT_GONE, True, "deleted")],
        datasource_ids=[],
        grant_violations=[],
    )
    assert items == [
        DriftItem(
            f"project:{PROJECT_GONE}",
            "project",
            "deleted",
            "a project that no longer exists",
        )
    ]


@pytest.mark.asyncio
async def test_grant_violation_parsed_into_item():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[],
        grant_violations=[
            "shell_tools: tools.shell requires the shell_tools grant"
        ],
    )
    assert items == [
        DriftItem(
            "grant:shell_tools",
            "grant",
            "revoked",
            "tools.shell requires the shell_tools grant",
        )
    ]


@pytest.mark.asyncio
async def test_malformed_grant_violation_still_yields_an_item():
    items = await collect_config_drift(
        None,
        {},
        owner={"id": "u1"},
        project_ids=[],
        datasource_ids=[],
        grant_violations=["no colon here"],
    )
    assert items[0].id == "grant:no colon here"
    assert items[0].label == "no colon here"


def test_drift_labels_collapses_duplicate_labels_with_a_count():
    items = [
        DriftItem("connector:a", "connector", "revoked",
                  "a connector you no longer have access to"),
        DriftItem("connector:b", "connector", "revoked",
                  "a connector you no longer have access to"),
        DriftItem("grant:shell_tools", "grant", "revoked", "shell tools"),
    ]

    rendered = drift_labels(items)

    assert rendered == [
        {
            "label": "a connector you no longer have access to",
            "count": 2,
            "kind": "connector",
            "reason": "revoked",
            "ids": ["connector:a", "connector:b"],
        },
        {
            "label": "shell tools",
            "count": 1,
            "kind": "grant",
            "reason": "revoked",
            "ids": ["grant:shell_tools"],
        },
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_config_drift.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.config_drift'`

- [ ] **Step 3: Write the module**

Create `orchestrator/services/config_drift.py`:

```python
"""Enumerate the parts of a session's stored config that are no longer usable.

Deliberately free of FastAPI and of policy decisions: callers hand in verdicts
already produced by the code that *enforces* them, so this module can never
drift from the enforcer. See docs/features/session_config_drift_resume.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Revoked and out-of-scope items are described without naming them: they still
#: exist and belong to someone, so naming them would confirm their existence and
#: current name. Deleted rows carry no such risk.
GENERIC_CONNECTOR_LABEL = "a connector you no longer have access to"
GENERIC_PROJECT_LABEL = "a project you no longer have access to"
DELETED_PROJECT_LABEL = "a project that no longer exists"

#: Verdict reasons that an acknowledgment can resolve. ``workspace_tier`` is
#: absent on purpose — a lite-tier repository conflict is a config
#: incompatibility that keeps raising 400.
ACKNOWLEDGEABLE_REASONS = frozenset({"deleted", "revoked", "out_of_scope"})


@dataclass(frozen=True)
class DriftItem:
    """One unusable configuration element.

    ``id`` is the stable acknowledgment key and is namespaced by kind so the
    three families cannot collide.
    """

    id: str
    kind: str
    reason: str
    label: str


async def collect_config_drift(
    db,
    thread: dict[str, Any],
    *,
    owner: dict[str, Any],
    project_ids: list[Any],
    datasource_ids: list[Any],
    grant_violations: list[str],
    tombstones: dict[str, str] | None = None,
) -> list[DriftItem]:
    """Every acknowledgeable drift item for one thread, in a stable order:
    connectors, then projects, then grants.

    ``project_ids`` and ``datasource_ids`` are verdict objects, not raw ids —
    they come from ``_classify_thread_project_ids`` and
    ``classify_datasource_selection`` respectively. ``grant_violations`` are the
    strings ``evaluate()`` produced, carried out of the resolve status.
    """
    names = tombstones or {}
    items: list[DriftItem] = []

    for verdict in datasource_ids:
        if not verdict.denied or verdict.reason not in ACKNOWLEDGEABLE_REASONS:
            continue
        if verdict.reason == "deleted":
            label = names.get(verdict.datasource_id, verdict.datasource_id)
        else:
            label = GENERIC_CONNECTOR_LABEL
        items.append(
            DriftItem(
                id=f"connector:{verdict.datasource_id}",
                kind="connector",
                reason=verdict.reason,
                label=label,
            )
        )

    for verdict in project_ids:
        if not verdict.denied or verdict.reason not in ACKNOWLEDGEABLE_REASONS:
            continue
        label = (
            DELETED_PROJECT_LABEL
            if verdict.reason == "deleted"
            else GENERIC_PROJECT_LABEL
        )
        items.append(
            DriftItem(
                id=f"project:{verdict.project_id}",
                kind="project",
                reason=verdict.reason,
                label=label,
            )
        )

    for violation in grant_violations or []:
        key, _, message = violation.partition(": ")
        items.append(
            DriftItem(
                id=f"grant:{key}",
                kind="grant",
                reason="revoked",
                label=message or key,
            )
        )

    return items


def drift_labels(items: list[DriftItem]) -> list[dict[str, Any]]:
    """Collapse items sharing a label into one row with a count.

    Revoked items all render the same generic string, so two of them would
    otherwise produce two identical lines. Every id is preserved, because the
    acknowledgment stays per-item.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        row = grouped.get(item.label)
        if row is None:
            grouped[item.label] = {
                "label": item.label,
                "count": 1,
                "kind": item.kind,
                "reason": item.reason,
                "ids": [item.id],
            }
            continue
        row["count"] += 1
        row["ids"].append(item.id)
    return list(grouped.values())
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_config_drift.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/config_drift.py tests/test_config_drift.py
git commit -m "feat(sessions): enumerate drifted config across connectors, projects, grants"
```

---

### Task 5: Connector tombstones and delete-time reference cleanup

Gives deleted connectors a readable name, and stops new dangling uuids accumulating.

**Files:**
- Create: `orchestrator/database/migrations/app/0115_datasource_tombstones.sql`
- Modify: `orchestrator/database/postgres.py:9260-9323` (`delete_datasource`)
- Test: `tests/test_datasource_delete_cleanup.py` (create)

**Interfaces:**
- Produces: `PostgresDB.get_datasource_tombstones(ids: list[str]) -> dict[str, str]` mapping id → name.

- [ ] **Step 1: Verify the migration number is still free**

Run: `ls orchestrator/database/migrations/app | grep -c '^0115'`
Expected: `0`. If not, use the next free number and adjust the filename everywhere in this task.

- [ ] **Step 2: Write the migration**

Create `orchestrator/database/migrations/app/0115_datasource_tombstones.sql`:

```sql
-- Deleted connectors leave a name behind so a session that still references
-- one can say WHICH connector vanished. Append-only; never joined in a hot
-- path. See docs/features/session_config_drift_resume.md §5.1.
CREATE TABLE IF NOT EXISTS datasource_tombstones (
    id          UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_by  UUID
);
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_datasource_delete_cleanup.py`:

```python
"""delete_datasource writes a tombstone and scrubs thread references."""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


async def test_delete_writes_tombstone_and_scrubs_thread_references(pg_db, seed_user):
    ds_id = await seed_user.create_datasource(name="KurortEngine")
    thread_id = await seed_user.create_thread(datasource_ids=[ds_id, seed_user.other_ds])

    assert await pg_db.delete_datasource(str(ds_id)) is True

    tombstones = await pg_db.get_datasource_tombstones([str(ds_id)])
    assert tombstones == {str(ds_id): "KurortEngine"}

    row = await pg_db.get_thread(str(thread_id))
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["datasource_ids"] == [str(seed_user.other_ds)]
```

This test needs a real Postgres. Follow the pattern in the repo's existing DB-gated tests — see `tests/` for the `pg_db` fixture convention, and `srw_canvas_db_gated_tests_locally` notes for running a local pgvector container. **If no such fixture exists**, convert this task's verification to Step 7's live check against dev and mark the test `@pytest.mark.skipif` on the absence of `DATABASE_URL`, rather than mocking the DB — a mocked client validates nothing about SQL.

- [ ] **Step 4: Run the test to verify it fails**

Run: `python -m pytest tests/test_datasource_delete_cleanup.py -v`
Expected: FAIL — `get_datasource_tombstones` does not exist.

- [ ] **Step 5: Implement tombstone + scrub inside the existing transaction**

In `orchestrator/database/postgres.py`, replace the `result = await conn.execute(...)` block at the end of `delete_datasource` with:

```python
                doomed = await conn.fetchrow(
                    "SELECT name FROM datasources WHERE id = $1",
                    uuid_val,
                )
                result = await conn.execute(
                    "DELETE FROM datasources WHERE id = $1",
                    uuid_val,
                )
                if result == "DELETE 1":
                    # A deleted row cannot supply its own name later, so keep one
                    # for sessions that still reference it.
                    await conn.execute(
                        """
                        INSERT INTO datasource_tombstones (id, name)
                        VALUES ($1, $2)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        uuid_val,
                        (doomed or {}).get("name") or str(uuid_val),
                    )
                    # Scrub the reference so new dangling ids stop accumulating.
                    # Sessions already carrying one are recovered by the
                    # acknowledgment flow, not by this.
                    await conn.execute(
                        """
                        UPDATE threads
                        SET metadata = jsonb_set(
                                metadata,
                                '{datasource_ids}',
                                COALESCE(
                                    (
                                        SELECT jsonb_agg(value)
                                        FROM jsonb_array_elements_text(
                                            metadata->'datasource_ids'
                                        ) AS value
                                        WHERE value <> $1::text
                                    ),
                                    '[]'::jsonb
                                )
                            )
                        WHERE jsonb_typeof(metadata->'datasource_ids') = 'array'
                          AND metadata->'datasource_ids' ? $1::text
                        """,
                        str(uuid_val),
                    )
```

Note the explicit `$1::text` casts: an untyped parameter used only inside a comparison makes asyncpg's PREPARE fail on **every** call, which surfaces in the browser as a CORS error rather than a SQL error.

The whole block must stay inside the existing `async with _transaction_if(...)`. If `authority_scope_uuid` is None there is no transaction today — wrap the delete/tombstone/scrub trio in `async with conn.transaction():` so a partial delete cannot happen.

- [ ] **Step 6: Add the tombstone reader**

Add to `PostgresDB`, near the other datasource readers:

```python
    async def get_datasource_tombstones(self, ids: list[str]) -> dict[str, str]:
        """Names of deleted connectors, for labelling drifted session config."""
        if not ids:
            return {}
        try:
            uuids = [UUID(str(value)) for value in ids]
        except (TypeError, ValueError):
            return {}
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name FROM datasource_tombstones WHERE id = ANY($1::uuid[])",
                uuids,
            )
        return {str(row["id"]): row["name"] for row in rows}
```

- [ ] **Step 7: Run the test**

Run: `python -m pytest tests/test_datasource_delete_cleanup.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add orchestrator/database/migrations/app/0115_datasource_tombstones.sql \
        orchestrator/database/postgres.py tests/test_datasource_delete_cleanup.py
git commit -m "feat(datasources): tombstone deleted connectors and scrub thread references"
```

---

### Task 6: Resume returns 428 and stores the acknowledgment

**Files:**
- Modify: `orchestrator/main.py:29880-29910` (`resume_thread`)
- Test: `tests/test_resume_config_drift.py` (create)

**Interfaces:**
- Consumes: `collect_config_drift`, `drift_labels` (Task 4); `classify_datasource_selection` (Task 1); `_classify_thread_project_ids` (Task 2); `status["grant_violations"]` (Task 3); `get_datasource_tombstones` (Task 5).
- Produces:
  ```python
  class ThreadResumeRequest(BaseModel):
      acknowledge: list[str] | None = None

  async def _thread_config_drift(thread, metadata, *, owner) -> list[DriftItem]
  ```
  and the 428 response body `{"code": "config_drift", "detail": str, "drift": [...], "summary": [...]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resume_config_drift.py`:

```python
"""POST /resume reports drifted config as 428 and accepts an acknowledgment."""

from __future__ import annotations

import pytest

from orchestrator.services.config_drift import DriftItem


DS_GONE = "d7555d5d-ce46-49e2-b1fa-8235d720badc"
THREAD = "1930dec9-181d-4fd5-a030-90b3d0b363d6"


@pytest.mark.asyncio
async def test_resume_returns_428_with_drift_and_does_not_mutate(
    client, ended_thread, monkeypatch
):
    monkeypatch.setattr(
        "orchestrator.main._thread_config_drift",
        _fake_drift([DriftItem(f"connector:{DS_GONE}", "connector",
                               "deleted", "KurortEngine")]),
    )

    response = await client.post(f"/api/persistent/threads/{ended_thread}/resume")

    assert response.status_code == 428
    body = response.json()
    assert body["code"] == "config_drift"
    assert body["drift"] == [
        {
            "id": f"connector:{DS_GONE}",
            "kind": "connector",
            "reason": "deleted",
            "label": "KurortEngine",
        }
    ]
    # Nothing was mutated: the thread is still ended.
    assert await _thread_status(ended_thread) == "ended"


@pytest.mark.asyncio
async def test_full_acknowledgment_resumes(client, ended_thread, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.main._thread_config_drift",
        _fake_drift([DriftItem(f"connector:{DS_GONE}", "connector",
                               "deleted", "KurortEngine")]),
    )

    response = await client.post(
        f"/api/persistent/threads/{ended_thread}/resume",
        json={"acknowledge": [f"connector:{DS_GONE}"]},
    )

    assert response.status_code == 200
    assert await _thread_status(ended_thread) == "created"
    assert await _thread_ack(ended_thread) == {f"connector:{DS_GONE}": "deleted"}


@pytest.mark.asyncio
async def test_partial_acknowledgment_is_rejected(client, ended_thread, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.main._thread_config_drift",
        _fake_drift([
            DriftItem(f"connector:{DS_GONE}", "connector", "deleted", "KurortEngine"),
            DriftItem("grant:shell_tools", "grant", "revoked", "shell tools"),
        ]),
    )

    response = await client.post(
        f"/api/persistent/threads/{ended_thread}/resume",
        json={"acknowledge": [f"connector:{DS_GONE}"]},
    )

    assert response.status_code == 428
    assert await _thread_status(ended_thread) == "ended"


@pytest.mark.asyncio
async def test_superset_acknowledgment_is_accepted(client, ended_thread, monkeypatch):
    """An item that recovered between prompt and confirm must not force a
    pointless re-prompt."""
    monkeypatch.setattr(
        "orchestrator.main._thread_config_drift",
        _fake_drift([DriftItem("grant:shell_tools", "grant", "revoked", "shell")]),
    )

    response = await client.post(
        f"/api/persistent/threads/{ended_thread}/resume",
        json={"acknowledge": ["grant:shell_tools", f"connector:{DS_GONE}"]},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_drift_resumes_exactly_as_before(client, ended_thread, monkeypatch):
    monkeypatch.setattr("orchestrator.main._thread_config_drift", _fake_drift([]))

    response = await client.post(f"/api/persistent/threads/{ended_thread}/resume")

    assert response.status_code == 200
    assert await _thread_ack(ended_thread) == {}


@pytest.mark.asyncio
async def test_non_ended_thread_still_409s(client, active_thread, monkeypatch):
    monkeypatch.setattr("orchestrator.main._thread_config_drift", _fake_drift([]))

    response = await client.post(f"/api/persistent/threads/{active_thread}/resume")

    assert response.status_code == 409
```

Implement `_fake_drift`, `_thread_status`, `_thread_ack`, and the `client` / `ended_thread` / `active_thread` fixtures following the conventions in the repo's existing endpoint tests — start from `tests/test_resume_endpoint_delegation.py`, which already exercises this endpoint and has the fixture wiring you need.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_resume_config_drift.py -v`
Expected: FAIL — `_thread_config_drift` does not exist.

- [ ] **Step 3: Add the request model and the drift assembler**

In `orchestrator/main.py`, near the other thread request models:

```python
class ThreadResumeRequest(BaseModel):
    """Optional body for POST /resume. ``acknowledge`` carries the drift item
    ids the user accepted losing."""

    acknowledge: list[str] | None = None
```

And above `resume_thread`:

```python
async def _thread_config_drift(
    thread: dict[str, Any],
    metadata: dict[str, Any],
    *,
    owner: dict[str, Any],
) -> list[DriftItem]:
    """Everything in this thread's stored config that is no longer usable.

    Runs the same classifiers the enforcers wrap, so the dialog can never
    promise something different from what attach will do.
    """
    thread_id = str(thread["id"])
    project_ids = await _thread_project_ids(thread_id)
    project_verdicts = await _classify_thread_project_ids(owner, project_ids)
    allowed_project_ids = [v.project_id for v in project_verdicts if not v.denied]

    datasource_verdicts, _revisions = await classify_datasource_selection(
        postgres_db,
        owner,
        str(owner["id"]),
        metadata.get("datasource_ids"),
        allowed_project_ids,
        None,
        allow_admin_explicit_override=True,
    )

    # Grants are enforced inside the session resolve; run it purely to harvest
    # the violations it would raise at attach.
    status: dict[str, Any] = {}
    try:
        await _resolve_session_config(thread, metadata, status=status)
    except GrantDenied:
        # Expected: this is exactly the signal we came here to harvest.
        pass
    except Exception as exc:
        # We could not determine whether grants drifted. Continuing with an
        # empty list would report "no grant drift" when the truth is "unknown",
        # letting the session resume on an unverified state — §7 requires
        # failing closed instead. `_is_experts_db_enabled` and
        # `_user_experts_enabled` run BEFORE _resolve_session_config's own
        # internal try, so a transient settings-read error lands here.
        logger.exception(
            "Thread %s: grant probe failed during drift collection; "
            "refusing to resume on an unknown state",
            thread_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Session configuration could not be verified",
        ) from exc
    grant_violations = status.get("grant_violations") or []

    deleted_ids = [
        v.datasource_id
        for v in datasource_verdicts
        if v.denied and v.reason == "deleted"
    ]
    tombstones = await postgres_db.get_datasource_tombstones(deleted_ids)

    return await collect_config_drift(
        postgres_db,
        thread,
        owner=owner,
        project_ids=project_verdicts,
        datasource_ids=datasource_verdicts,
        grant_violations=grant_violations,
        tombstones=tombstones,
    )
```

Add the imports at the top of `main.py`:

```python
from services.config_drift import DriftItem, collect_config_drift, drift_labels
from services.datasource_policy import classify_datasource_selection
```

- [ ] **Step 4: Wire the endpoint**

Change `resume_thread`'s signature to accept the optional body and replace the two revalidation calls:

```python
@app.post("/api/persistent/threads/{thread_id}/resume")
async def resume_thread(
    thread_id: str,
    request: Request,
    body: ThreadResumeRequest | None = None,
) -> dict[str, Any]:
```

Then, in place of lines 29907-29908 (`_revalidate_thread_project_ids` / `_revalidate_thread_datasource_ids`):

```python
    # Validation stays ahead of mutation: a thread that cannot resume must not
    # be left half-resumed.
    drift = await _thread_config_drift(thread, metadata, owner=user)
    acknowledged = set(body.acknowledge or []) if body else set()
    outstanding = {item.id for item in drift} - acknowledged
    if outstanding:
        # Subset, not equality: an item that RECOVERED between prompt and
        # confirm must not force a pointless re-prompt, while an item that
        # newly drifted is never silently acknowledged.
        raise HTTPException(
            status_code=428,
            detail={
                "code": "config_drift",
                "detail": (
                    "Parts of this session's configuration are no longer "
                    "available"
                ),
                "drift": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "reason": item.reason,
                        "label": item.label,
                    }
                    for item in drift
                ],
                "summary": drift_labels(drift),
            },
        )

    if drift:
        await postgres_db.record_thread_config_drift_ack(
            thread_id, {item.id: item.reason for item in drift}
        )
```

- [ ] **Step 5: Add the ack writer**

In `orchestrator/database/postgres.py`, near the other thread metadata writers:

```python
    async def record_thread_config_drift_ack(
        self, thread_id: str, ack: dict[str, str]
    ) -> None:
        """Merge acknowledged drift items into metadata.config_drift_ack.

        Merged, never replaced: an older acknowledgment stays valid so a
        previously accepted loss does not re-prompt.
        """
        if not ack:
            return
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{config_drift_ack}',
                        COALESCE(metadata->'config_drift_ack', '{}'::jsonb)
                            || $2::jsonb
                    )
                WHERE id = $1
                """,
                UUID(thread_id),
                json.dumps(ack),
            )
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_resume_config_drift.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the neighbouring resume suites for regressions**

Run: `python -m pytest tests/test_resume_endpoint_delegation.py tests/test_resume_missing_workspace.py tests/test_resume_stale_agent_requeue.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add orchestrator/main.py orchestrator/database/postgres.py \
        tests/test_resume_config_drift.py
git commit -m "feat(sessions): return 428 with the drift list and accept an acknowledgment"
```

---

### Task 7: Attach honors the acknowledgment

Without this, "resume without them" succeeds and the session then dies at attach.

**Files:**
- Modify: `orchestrator/main.py:27464-27530` (`_revalidate_thread_datasource_selection`), `orchestrator/main.py:4520-4535` (attach), `orchestrator/main.py:2529` (grant enforcement call site)
- Test: `tests/test_attach_honors_drift_ack.py` (create)

**Interfaces:**
- Consumes: `metadata.config_drift_ack` written by Task 6.
- Produces:
  ```python
  def acknowledged_drift_ids(metadata: Any) -> set[str]
  def strip_acknowledged(ids: list[str], ack: set[str], *, prefix: str) -> list[str]
  def acknowledged_grant_keys(metadata: Any) -> set[str]
  ```
  All three in `orchestrator/services/config_drift.py`, plus
  `_apply_acknowledged_grant_drift` in `orchestrator/main.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_attach_honors_drift_ack.py`:

```python
"""Acknowledged drift is skipped at attach; unacknowledged drift still denies."""

from __future__ import annotations

import pytest

from orchestrator.services.config_drift import (
    acknowledged_drift_ids,
    strip_acknowledged,
)


DS_GONE = "d7555d5d-ce46-49e2-b1fa-8235d720badc"
DS_OK = "2991589e-249d-4cca-98ce-780db69b2520"


def test_acknowledged_drift_ids_reads_metadata():
    metadata = {"config_drift_ack": {f"connector:{DS_GONE}": "deleted"}}
    assert acknowledged_drift_ids(metadata) == {f"connector:{DS_GONE}"}


def test_acknowledged_drift_ids_tolerates_a_json_string():
    """asyncpg hands back JSONB as a raw string; a guard that only accepts dict
    silently disables the feature."""
    metadata = '{"config_drift_ack": {"connector:x": "deleted"}}'
    assert acknowledged_drift_ids(metadata) == {"connector:x"}


def test_acknowledged_drift_ids_missing_key_is_empty():
    assert acknowledged_drift_ids({}) == set()


def test_strip_acknowledged_removes_only_acked_ids():
    result = strip_acknowledged(
        [DS_OK, DS_GONE], {f"connector:{DS_GONE}"}, prefix="connector"
    )
    assert result == [DS_OK]


def test_strip_acknowledged_leaves_unacked_ids_in_place():
    result = strip_acknowledged([DS_OK, DS_GONE], set(), prefix="connector")
    assert result == [DS_OK, DS_GONE]


def test_acknowledged_grant_keys_unprefixes_only_grants():
    metadata = {
        "config_drift_ack": {
            "grant:shell_tools": "revoked",
            f"connector:{DS_GONE}": "deleted",
        }
    }
    assert acknowledged_grant_keys(metadata) == {"shell_tools"}
```

And the partition rule, which is the security-relevant half — add to
`tests/test_attach_honors_drift_ack.py`:

```python
@pytest.mark.asyncio
async def test_unacknowledged_grant_violation_is_not_stripped():
    """Acknowledging ONE grant must never smuggle a different one through.
    With shell_tools acked but vm_workspace also violating, the fragment must
    come back untouched so the dispatch PEP still denies."""
    merged = {"tools": {"shell": True}, "workspace": {"backend": "vm"}}
    grants = {"shell_tools": False, "vm_workspace": False}

    with patch(
        "orchestrator.main._resolve_runner_grants",
        AsyncMock(return_value=grants),
    ):
        result = await _apply_acknowledged_grant_drift(
            merged,
            acknowledged={"shell_tools"},
            runner_user_id="u1",
            project_ids=[],
        )

    assert result == merged


@pytest.mark.asyncio
async def test_fully_acknowledged_grant_violations_are_stripped():
    merged = {"tools": {"shell": True}, "workspace": {"backend": "vm"}}
    grants = {"shell_tools": False, "vm_workspace": False}

    with patch(
        "orchestrator.main._resolve_runner_grants",
        AsyncMock(return_value=grants),
    ):
        result = await _apply_acknowledged_grant_drift(
            merged,
            acknowledged={"shell_tools", "vm_workspace"},
            runner_user_id="u1",
            project_ids=[],
        )

    assert "shell" not in result.get("tools", {})
    assert "backend" not in result.get("workspace", {})
    # The original must not be mutated in place — callers reuse it.
    assert merged["tools"]["shell"] is True


@pytest.mark.asyncio
async def test_admin_bypass_returns_the_fragment_untouched():
    merged = {"tools": {"shell": True}}

    with patch(
        "orchestrator.main._resolve_runner_grants",
        AsyncMock(return_value=None),
    ):
        result = await _apply_acknowledged_grant_drift(
            merged,
            acknowledged={"shell_tools"},
            runner_user_id="u1",
            project_ids=[],
        )

    assert result == merged
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_attach_honors_drift_ack.py -v`
Expected: FAIL with `ImportError: cannot import name 'acknowledged_drift_ids'`

- [ ] **Step 3: Add the helpers**

Append to `orchestrator/services/config_drift.py`:

```python
def acknowledged_drift_ids(metadata: Any) -> set[str]:
    """Drift ids the owner already accepted losing.

    Accepts a raw JSON string as well as a dict: asyncpg returns JSONB columns
    as strings, and an ``isinstance(x, dict)`` guard without a parse silently
    turns this feature off.
    """
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return set()
    if not isinstance(metadata, dict):
        return set()
    ack = metadata.get("config_drift_ack") or {}
    if not isinstance(ack, dict):
        return set()
    return {str(key) for key in ack}


def strip_acknowledged(ids: list[str], ack: set[str], *, prefix: str) -> list[str]:
    """Drop ids whose namespaced drift key was acknowledged."""
    return [value for value in ids if f"{prefix}:{value}" not in ack]


def acknowledged_grant_keys(metadata: Any) -> set[str]:
    """Acknowledged grant keys, unprefixed, ready to compare against the keys
    ``evaluate()`` puts in front of each violation string."""
    return {
        key[len("grant:") :]
        for key in acknowledged_drift_ids(metadata)
        if key.startswith("grant:")
    }
```

- [ ] **Step 4: Apply the ack at attach**

In `_revalidate_thread_datasource_selection` (`orchestrator/main.py:27484`), narrow the selection before authorization:

```python
    selected = list(dict.fromkeys(str(value) for value in datasource_ids or []))
    # Acknowledged losses are dropped rather than denied. Narrowing BEFORE
    # authorization keeps _require_exact_datasource_resolution's fail-closed
    # comparison intact — it compares resolution against this same list.
    ack = acknowledged_drift_ids(thread.get("metadata"))
    if ack:
        selected = strip_acknowledged(selected, ack, prefix="connector")
    if not selected:
        return [], {}
```

In the attach path (`orchestrator/main.py:~4527`), narrow the project list the same way after `_revalidate_thread_project_ids` — replace the `project_ids = await _revalidate_thread_project_ids(...)` line with:

```python
        ack = acknowledged_drift_ids(_meta)
        current_project_ids = strip_acknowledged(
            await _thread_project_ids(thread_id), ack, prefix="project"
        )
        project_ids = await _revalidate_thread_project_ids(_thread, current_project_ids)
```

For grants, **reuse the canonical stripper — do not write a path map.**
`src/core/capability_grants.py` already exports
`strip_to_grants(fragment, grants) -> tuple[dict, list[str]]`, whose docstring
states it is *"one-for-one with `evaluate`'s nine rules, and kept beside it
(rather than a second, route-side implementation) so the two cannot drift."* A
hand-written `{grant_key: config_path}` map in `main.py` would be exactly that
forbidden second implementation, and would silently miss `browser`,
`catalog_authoring`, `model_selection`, `autonomy_ceiling`, `permission_mode`,
and `delegation`'s second path (`tools.delegation` *and* `delegation.enabled`).
An unmapped key strips nothing, so the user acknowledges, resume succeeds, and
attach dies anyway — the precise failure this feature exists to prevent.

The one thing `strip_to_grants` must NOT do here is strip violations the user
never acknowledged. Partition first, then strip only when every live violation
is covered. Add near `_enforce_dispatch_grants`:

```python
async def _apply_acknowledged_grant_drift(
    merged: dict[str, Any],
    *,
    acknowledged: set[str],
    runner_user_id: str | None,
    project_ids: list[str],
) -> dict[str, Any]:
    """Strip acknowledged grant violations out of a merged config.

    Returns the fragment to enforce against. A violation the user did NOT
    acknowledge is left in place, so ``_enforce_dispatch_grants`` still denies
    exactly as it does today — acknowledging one grant must never smuggle a
    different one through.

    ``strip_to_grants`` is advisory by contract; the authoritative re-check is
    the ``_enforce_dispatch_grants`` call that immediately follows, which
    re-runs ``evaluate`` on whatever this returns.
    """
    if not acknowledged:
        return merged
    from src.core.capability_grants import evaluate, strip_to_grants

    grants = await _resolve_runner_grants(
        runner_user_id=runner_user_id, project_ids=project_ids
    )
    if grants is None:  # admin bypass — nothing to strip
        return merged
    violations = evaluate(merged, grants)
    if not violations:
        return merged
    flagged = {v.split(":", 1)[0] for v in violations}
    if not flagged <= acknowledged:
        # Something drifted that was never acknowledged. Leave the fragment
        # untouched and let the dispatch PEP fail closed on all of it.
        return merged
    stripped, _dropped = strip_to_grants(merged, grants)
    return stripped
```

**Where the strip must land — this is the whole difficulty.** It is NOT enough
to strip `_cap["merged_fragment"]`. That dict is a detached
`copy.deepcopy(data)` taken by `resolve_config`
(`orchestrator/services/config_resolver.py:184`) purely so the dispatch PDP has
something to evaluate; the blob actually delivered to the agent is built from
`data` *afterwards* via `load_agent_config_from_dict` +
`serialize_resolved_config`, and `inject_blob_credentials` never touches
`tools`/`workspace`. Stripping only the capture therefore **silences the grant
check while still shipping the capability** — a privilege escalation, and the
exact opposite of the "never deliver the unvetted override" comment sitting
above that call.

Nor can the blob be stripped after the fact: it is a serialized shape
(`blob["prompts"]`, `blob["skills"]`, …), not the merged fragment
`strip_to_grants` understands.

So the strip must be applied to `data` *before* the capture and before
serialization, so the capture and the delivered blob derive from the same
stripped config. Give `resolve_config` an optional hook — it is sync, so the
caller resolves grants first and passes a closure. Defaulting to `None` leaves
its other seven callers untouched:

```python
def resolve_config(
    *,
    ...,
    skills: Optional[dict] = None,
    grant_strip: Optional[Callable[[dict], dict]] = None,
) -> dict:
```

and, in the body, immediately before the existing capture write:

```python
    data = normalize_tool_policy(data)

    # Applied HERE so the capture the PDP evaluates and the blob the agent
    # hydrates are the same stripped config. Stripping only the capture would
    # silence the check while still delivering the capability.
    if grant_strip is not None:
        data = grant_strip(data)

    if capture is not None:
```

In `main.py`, resolve the grants and the acknowledgment BEFORE calling
`resolve_config`, then pass the closure:

```python
        _ack_grant_keys = acknowledged_grant_keys(metadata)
        _grants_for_strip = (
            await _resolve_runner_grants(
                runner_user_id=user_id,
                project_ids=[project_id] if project_id else [],
            )
            if _ack_grant_keys
            else None
        )
        resolved = resolve_config(
            ...,
            capture=_cap,
            skills=_skills_payload,
            grant_strip=(
                (lambda fragment: _strip_acknowledged_grants(
                    fragment, _grants_for_strip, _ack_grant_keys
                ))
                if _grants_for_strip is not None
                else None
            ),
        )
```

`_resolve_runner_grants` returns `None` for admins (bypass), which correctly
disables the hook — an admin has nothing to strip.

`_apply_acknowledged_grant_drift` above therefore becomes a **sync, pure**
helper, since the grants are already resolved by the caller:

```python
def _strip_acknowledged_grants(
    fragment: dict[str, Any], grants: dict[str, Any], acknowledged: set[str]
) -> dict[str, Any]:
    """Drop acknowledged grant violations from a merged config fragment.

    A violation the user did NOT acknowledge is left in place, so
    ``_enforce_dispatch_grants`` still denies on all of it — acknowledging one
    grant must never smuggle a different one through.

    ``strip_to_grants`` is advisory by contract; the authoritative re-check is
    the ``_enforce_dispatch_grants`` call that runs on the resulting capture.
    """
    from src.core.capability_grants import evaluate, strip_to_grants

    violations = evaluate(fragment, grants)
    if not violations:
        return fragment
    flagged = {v.split(":", 1)[0] for v in violations}
    if not flagged <= acknowledged:
        return fragment
    stripped, _dropped = strip_to_grants(fragment, grants)
    return stripped
```

Leave the `_enforce_dispatch_grants` call exactly where it is. It now runs on
the stripped capture and is the authoritative re-check.

**A test that would have caught this is mandatory.** Every grant test so far
calls the helper in isolation, which is precisely why the leak went unnoticed.
Add one that drives `_resolve_session_config` end to end and asserts on the
DELIVERED blob:

```python
@pytest.mark.asyncio
async def test_acknowledged_grant_is_stripped_from_the_DELIVERED_blob():
    """The capture the PDP sees and the blob the agent hydrates must be the
    same stripped config. Asserting only on the capture passes even when the
    capability is still shipped."""
    # thread metadata acknowledges shell_tools; grants deny shell_tools.
    # Drive _resolve_session_config and assert the returned blob does NOT
    # carry tools.shell, in addition to the call not raising GrantDenied.
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_attach_honors_drift_ack.py tests/test_config_drift.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/main.py orchestrator/services/config_drift.py \
        tests/test_attach_honors_drift_ack.py
git commit -m "feat(sessions): honor drift acknowledgments at attach"
```

---

### Task 8: Programmatic clients

**Files:**
- Modify: `src/shared/orch_surface/client.py:2780-2795`
- Modify: `orchestrator/mcp/` — the `resume_persistent_thread` tool definition (locate with `rg -n "resume_persistent_thread" orchestrator/mcp/`)
- Test: `tests/test_orch_surface_resume_ack.py` (create)

**Interfaces:**
- Produces: `resume_persistent_thread(thread_id, acknowledge: list[str] | None = None)`, raising `SessionConfigDriftError` on 428.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orch_surface_resume_ack.py`:

```python
"""The programmatic resume client surfaces drift instead of a bare HTTP error."""

from __future__ import annotations

import pytest

from shared.orch_surface.client import SessionConfigDriftError


@pytest.mark.asyncio
async def test_428_raises_a_drift_error_naming_the_items(orch_client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url__endswith="/resume",
        status_code=428,
        json={
            "detail": {
                "code": "config_drift",
                "detail": "Parts of this session's configuration are no longer available",
                "drift": [
                    {"id": "connector:abc", "kind": "connector",
                     "reason": "deleted", "label": "KurortEngine"}
                ],
            }
        },
    )

    with pytest.raises(SessionConfigDriftError) as excinfo:
        await orch_client.resume_persistent_thread("t1")

    assert "KurortEngine" in str(excinfo.value)
    assert excinfo.value.drift[0]["id"] == "connector:abc"


@pytest.mark.asyncio
async def test_acknowledge_is_sent_in_the_body(orch_client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url__endswith="/resume", status_code=200,
        json={"status": "created", "thread_id": "t1"},
    )

    await orch_client.resume_persistent_thread("t1", acknowledge=["connector:abc"])

    request = httpx_mock.get_requests()[-1]
    assert request.read() == b'{"acknowledge": ["connector:abc"]}'
```

Use whatever HTTP-mocking fixture the repo already uses for `orch_surface` tests — `rg -l "orch_surface" tests/ | head` to find the pattern.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_orch_surface_resume_ack.py -v`
Expected: FAIL — `SessionConfigDriftError` does not exist.

- [ ] **Step 3: Implement**

In `src/shared/orch_surface/client.py`, add near the other client exceptions:

```python
class SessionConfigDriftError(Exception):
    """Resume refused: parts of the session's stored config are unavailable.

    Carries the structured items so a caller can decide whether to re-resume
    with ``acknowledge`` set.
    """

    def __init__(self, drift: list[dict[str, Any]]):
        self.drift = drift
        labels = ", ".join(item.get("label", item.get("id", "?")) for item in drift)
        super().__init__(
            f"Session config is no longer fully available: {labels}. "
            f"Resume again with acknowledge=[...] to continue without them."
        )
```

Replace `resume_persistent_thread`:

```python
    async def resume_persistent_thread(
        self, thread_id: str, acknowledge: list[str] | None = None
    ) -> dict[str, Any]:
        """Resume an ended persistent thread.

        Raises ``SessionConfigDriftError`` when the session references
        connectors, projects or grants that are no longer available. Pass their
        ids back as ``acknowledge`` to resume without them.

        Returns:
            Dict with ``status`` and ``thread_id``.
        """
        payload: dict[str, Any] = {}
        if acknowledge is not None:
            payload["acknowledge"] = acknowledge
        resp = await self._mutation_request(
            "POST", f"/api/persistent/threads/{thread_id}/resume", json=payload
        )
        if resp.status_code == 428:
            detail = (resp.json() or {}).get("detail") or {}
            raise SessionConfigDriftError(detail.get("drift") or [])
        resp.raise_for_status()
        return resp.json()
```

Verify `_mutation_request` accepts a `json=` kwarg; if it does not, add it following the signature of the other mutation helpers in the same file.

- [ ] **Step 4: Update the MCP tool**

Add the optional `acknowledge` array parameter to the `resume_persistent_thread` MCP tool schema and pass it through. Extend its description with: *"If the session's config has drifted (deleted connector, revoked project, withdrawn grant), this returns the drifted items; call again with `acknowledge` set to their ids to resume without them."*

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_orch_surface_resume_ack.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/shared/orch_surface/client.py orchestrator/mcp/ \
        tests/test_orch_surface_resume_ack.py
git commit -m "feat(orch-surface): surface config drift and accept acknowledgments on resume"
```

---

### Task 9: Cockpit stops swallowing resume errors

Standalone value: this alone converts the dead button into a visible failure. Land it even if Task 10 slips.

**Files:**
- Create: `cockpit/src/app/core/services/resume-error.ts`
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts:2328-2337`
- Test: `cockpit/src/app/core/services/resume-error.spec.ts` (create)

**Interfaces:**
- Produces:
  ```ts
  export interface ConfigDriftItem {
      id: string; kind: 'connector' | 'project' | 'grant';
      reason: 'deleted' | 'revoked' | 'out_of_scope'; label: string;
  }
  export type ResumeOutcome =
      | {kind: 'ok'}
      | {kind: 'drift'; items: ConfigDriftItem[]}
      | {kind: 'benign'}
      | {kind: 'error'; status: number};

  export function classifyResumeError(err: unknown): ResumeOutcome;
  ```

A pure function, because `PersistentChatComponent` is unmountable in specs (NG0951).

- [ ] **Step 1: Write the failing test**

Create `cockpit/src/app/core/services/resume-error.spec.ts`:

```ts
import {describe, expect, it} from 'vitest';
import {classifyResumeError} from './resume-error';

describe('classifyResumeError', () => {
    it('treats 428 as drift and extracts the items', () => {
        const result = classifyResumeError({
            status: 428,
            error: {
                detail: {
                    code: 'config_drift',
                    drift: [{id: 'connector:abc', kind: 'connector',
                             reason: 'deleted', label: 'KurortEngine'}],
                },
            },
        });

        expect(result).toEqual({
            kind: 'drift',
            items: [{id: 'connector:abc', kind: 'connector',
                     reason: 'deleted', label: 'KurortEngine'}],
        });
    });

    it('treats 409 as benign so a double-click still falls through', () => {
        expect(classifyResumeError({status: 409})).toEqual({kind: 'benign'});
    });

    it('surfaces 403 as a real error instead of swallowing it', () => {
        expect(classifyResumeError({status: 403})).toEqual({kind: 'error', status: 403});
    });

    it('surfaces an unknown failure as an error', () => {
        expect(classifyResumeError(new Error('offline')))
            .toEqual({kind: 'error', status: 0});
    });

    it('falls back to an error when 428 carries no usable drift list', () => {
        expect(classifyResumeError({status: 428, error: {}}))
            .toEqual({kind: 'error', status: 428});
    });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/resume-error.spec.ts`
Expected: FAIL — cannot resolve `./resume-error`

- [ ] **Step 3: Write the classifier**

Create `cockpit/src/app/core/services/resume-error.ts`:

```ts
/** Outcome classification for POST /resume.
 *
 *  This exists as a pure function because PersistentChatComponent cannot be
 *  mounted in specs (NG0951), so the decision has to be testable on its own.
 */

export interface ConfigDriftItem {
    id: string;
    kind: 'connector' | 'project' | 'grant';
    reason: 'deleted' | 'revoked' | 'out_of_scope';
    label: string;
}

export type ResumeOutcome =
    | {kind: 'ok'}
    | {kind: 'drift'; items: ConfigDriftItem[]}
    | {kind: 'benign'}
    | {kind: 'error'; status: number};

/** Classify a failed resume.
 *
 *  409 stays benign: it means the thread was not actually 'ended' (a
 *  double-click), and connect()'s cold path is self-healing. Everything else
 *  used to be swallowed by the same catch, which is why a 403 rendered as a
 *  dead button with no message at all.
 */
export function classifyResumeError(err: unknown): ResumeOutcome {
    const status = (err as {status?: number})?.status ?? 0;
    if (status === 409) return {kind: 'benign'};
    if (status === 428) {
        const detail = (err as {error?: {detail?: {drift?: ConfigDriftItem[]}}})
            ?.error?.detail;
        const items = detail?.drift;
        if (Array.isArray(items) && items.length > 0) {
            return {kind: 'drift', items};
        }
        return {kind: 'error', status: 428};
    }
    return {kind: 'error', status};
}
```

- [ ] **Step 4: Run the test**

Run: `cd cockpit && npx vitest run src/app/core/services/resume-error.spec.ts`
Expected: PASS (5 tests)

- [ ] **Step 5: Use it in the service**

In `persistent-chat.service.ts`, add `pendingDrift = signal<ConfigDriftItem[] | null>(null);` to the service's signals, then replace the blind catch:

```ts
            try {
                await firstValueFrom(
                    this.http.post(
                        `${environment.apiUrl}/persistent/threads/${threadId}/resume`,
                        acknowledge ? {acknowledge} : {},
                    ),
                );
                this.pendingDrift.set(null);
            } catch (err) {
                // Currency FIRST. A resume POST can still be in flight when the
                // user navigates away, and `error`/`pendingDrift` are
                // current-thread-scoped singletons. Acting on a late failure
                // for an abandoned thread would surface thread A's error while
                // the user is looking at thread B — and, once the Task 10
                // dialog exists, would let an acknowledgment built from thread
                // A's drift POST against thread B's /resume.
                if (!this._isCurrentConnect(threadId, generation)) return;
                const outcome = classifyResumeError(err);
                if (outcome.kind === 'drift') {
                    // Surface the dialog and stop: connect() against a still-
                    // ended thread would achieve nothing.
                    this.pendingDrift.set(outcome.items);
                    return;
                }
                if (outcome.kind === 'error') {
                    this.error.set(this.errors.translate(err, 'errors.sessions.resumeFailed'));
                    return;
                }
                // benign (409): the thread was not actually ended. Fall through
                // to connect(), whose cold-start path is self-healing.
            }
```

Change the method signature to `async resumeSession(acknowledge?: string[]): Promise<void>` and import `classifyResumeError` and `ConfigDriftItem`.

`pendingDrift` is per-thread state, so clear it wherever the service already
resets per-thread state — `connect()`'s cold-path reset block (beside
`resumedFromEpoch` / `rewindPrefill` / `tasks`) and `disconnect()`. Nothing
clears it otherwise, and a drift list that survives a thread switch would let
Task 10's dialog acknowledge one session's items against another's.

- [ ] **Step 6: Typecheck and commit**

Run: `cd cockpit && npx tsc -p tsconfig.app.json --noEmit`
Expected: no errors.

```bash
git add cockpit/src/app/core/services/resume-error.ts \
        cockpit/src/app/core/services/resume-error.spec.ts \
        cockpit/src/app/core/services/persistent-chat.service.ts
git commit -m "fix(cockpit): stop swallowing resume errors behind the benign-409 catch"
```

---

### Task 10: The drift dialog

**Files:**
- Create: `cockpit/src/app/views/chat/config-drift-dialog.component.ts`
- Create: `cockpit/src/app/views/chat/config-drift-dialog.component.spec.ts`
- Modify: the chat view template that hosts the session (locate with `rg -ln "app-dialog" cockpit/src/app/views/chat/`)
- Modify: `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`

**Interfaces:**
- Consumes: `ConfigDriftItem`, `PersistentChatService.pendingDrift` (Task 9).
- Produces: `<app-config-drift-dialog [items]="…" (resumeAnyway)="…" (startNew)="…" />`

- [ ] **Step 1: Add the translation keys**

In `en.json` under `sessions`:

```json
"configDrift": {
  "title": "Some of this session's setup is gone",
  "intro": "Parts of this session's configuration aren't available anymore:",
  "resumeAnyway": "Resume without them",
  "startNew": "Start a new session",
  "connector": { "deleted": "Deleted connector — {{label}}",
                 "revoked": "Revoked connector — {{label}}",
                 "out_of_scope": "Revoked connector — {{label}}" },
  "project":   { "deleted": "Deleted project — {{label}}",
                 "revoked": "Revoked project — {{label}}",
                 "out_of_scope": "Revoked project — {{label}}" },
  "grant":     { "deleted": "Missing grant — {{label}}",
                 "revoked": "Missing grant — {{label}}",
                 "out_of_scope": "Missing grant — {{label}}" },
  "countSuffix": " (×{{count}})"
}
```

Add the German equivalents to `de-DE.json`.

- [ ] **Step 2: Write the failing spec**

Create `cockpit/src/app/views/chat/config-drift-dialog.component.spec.ts`:

```ts
import {describe, expect, it} from 'vitest';
import {groupDriftForDisplay} from './config-drift-dialog.component';

describe('groupDriftForDisplay', () => {
    it('collapses identical labels into one row with a count', () => {
        const rows = groupDriftForDisplay([
            {id: 'connector:a', kind: 'connector', reason: 'revoked',
             label: 'a connector you no longer have access to'},
            {id: 'connector:b', kind: 'connector', reason: 'revoked',
             label: 'a connector you no longer have access to'},
        ]);

        expect(rows).toEqual([{
            kind: 'connector', reason: 'revoked',
            label: 'a connector you no longer have access to', count: 2,
        }]);
    });

    it('keeps distinct labels separate and preserves order', () => {
        const rows = groupDriftForDisplay([
            {id: 'connector:a', kind: 'connector', reason: 'deleted',
             label: 'KurortEngine'},
            {id: 'grant:shell_tools', kind: 'grant', reason: 'revoked',
             label: 'shell tools'},
        ]);

        expect(rows.map(r => r.label)).toEqual(['KurortEngine', 'shell tools']);
        expect(rows.every(r => r.count === 1)).toBe(true);
    });

    it('returns nothing for an empty list', () => {
        expect(groupDriftForDisplay([])).toEqual([]);
    });
});
```

- [ ] **Step 3: Run the spec to verify it fails**

Run: `cd cockpit && npx vitest run src/app/views/chat/config-drift-dialog.component.spec.ts`
Expected: FAIL — cannot resolve the component module.

- [ ] **Step 4: Write the component**

Create `cockpit/src/app/views/chat/config-drift-dialog.component.ts`:

```ts
import {ChangeDetectionStrategy, Component, computed, input, output} from '@angular/core';
import {TranslocoModule} from '@jsverse/transloco';
import {DialogComponent} from '../../ui/dialog/dialog.component';
import type {ConfigDriftItem} from '../../core/services/resume-error';

export interface DriftRow {
    kind: string;
    reason: string;
    label: string;
    count: number;
}

/** Collapse items sharing a label into one row with a count — revoked items all
 *  render the same generic string, so two of them would otherwise produce two
 *  identical lines. Exported as a pure function so it is testable without
 *  mounting the component. */
export function groupDriftForDisplay(items: ConfigDriftItem[]): DriftRow[] {
    const rows: DriftRow[] = [];
    const seen = new Map<string, DriftRow>();
    for (const item of items) {
        const existing = seen.get(item.label);
        if (existing) {
            existing.count += 1;
            continue;
        }
        const row: DriftRow = {
            kind: item.kind, reason: item.reason, label: item.label, count: 1,
        };
        seen.set(item.label, row);
        rows.push(row);
    }
    return rows;
}

@Component({
    selector: 'app-config-drift-dialog',
    standalone: true,
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [DialogComponent, TranslocoModule],
    template: `
        <app-dialog
            [open]="true"
            [closable]="true"
            size="md"
            [title]="'sessions.configDrift.title' | transloco"
        >
            <p>{{ 'sessions.configDrift.intro' | transloco }}</p>
            <ul class="drift-list">
                @for (row of rows(); track row.label) {
                    <li>
                        {{ 'sessions.configDrift.' + row.kind + '.' + row.reason
                           | transloco: {label: row.label} }}
                        @if (row.count > 1) {
                            {{ 'sessions.configDrift.countSuffix'
                               | transloco: {count: row.count} }}
                        }
                    </li>
                }
            </ul>
            <footer>
                <button type="button" (click)="startNew.emit()">
                    {{ 'sessions.configDrift.startNew' | transloco }}
                </button>
                <button type="button" (click)="resumeAnyway.emit(ids())">
                    {{ 'sessions.configDrift.resumeAnyway' | transloco }}
                </button>
            </footer>
        </app-dialog>
    `,
})
export class ConfigDriftDialogComponent {
    readonly items = input.required<ConfigDriftItem[]>();
    readonly resumeAnyway = output<string[]>();
    readonly startNew = output<void>();

    readonly rows = computed(() => groupDriftForDisplay(this.items()));
    /** Every id, not the collapsed rows — the acknowledgment is per-item. */
    readonly ids = computed(() => this.items().map((item) => item.id));
}
```

- [ ] **Step 5: Host the dialog**

In the chat view template, render it when `chat.pendingDrift()` is non-null:

```html
@if (chat.pendingDrift(); as drift) {
    <app-config-drift-dialog
        [items]="drift"
        (resumeAnyway)="onResumeAnyway($event)"
        (startNew)="onStartNewSession()"
    />
}
```

And in the hosting component:

```ts
    async onResumeAnyway(ids: string[]): Promise<void> {
        this.chat.pendingDrift.set(null);
        await this.chat.resumeSession(ids);
    }

    onStartNewSession(): void {
        this.chat.pendingDrift.set(null);
        // Prefill from what still works so the user does not rebuild by hand.
        void this.router.navigate(['/sessions/new'], {
            queryParams: {from: this.chat.threadId()},
        });
    }
```

Confirm the session-create route already honors a `from` query parameter; if it does not, add the prefill read there, sourcing expert, model, project and surviving connectors from `GET /api/persistent/threads/{id}`.

- [ ] **Step 6: Run the spec and typecheck**

Run: `cd cockpit && npx vitest run src/app/views/chat/config-drift-dialog.component.spec.ts && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add cockpit/src/app/views/chat/config-drift-dialog.component.ts \
        cockpit/src/app/views/chat/config-drift-dialog.component.spec.ts \
        cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
git commit -m "feat(cockpit): offer resume-without-them when session config has drifted"
```

---

### Task 11: Live gate on dev

A mocked Kubernetes/Postgres client validates nothing about the real API. This flow is not done until it runs against the real cluster.

**Files:** none — verification only.

- [ ] **Step 1: Confirm the reproduction is still live**

```bash
kubectl --context main exec -n superhuman-remote-worker srw-postgres-0 -- \
  psql -U srw -d srw -At -c "
  WITH refs AS (
    SELECT jsonb_array_elements_text(metadata->'datasource_ids') AS ds_id
    FROM threads WHERE id='1930dec9-181d-4fd5-a030-90b3d0b363d6')
  SELECT r.ds_id, (d.id IS NOT NULL) AS exists
  FROM refs r LEFT JOIN datasources d ON d.id::text = r.ds_id;"
```

Expected: two rows, one `f`. If both are `t`, someone repaired the data — pick another blocked thread from the blast-radius query in the spec.

- [ ] **Step 2: Deploy to dev and confirm the migration ran**

After the image rolls out:

```bash
kubectl --context main exec -n superhuman-remote-worker srw-postgres-0 -- \
  psql -U srw -d srw -c "\dt datasource_tombstones"
```

Expected: the table exists.

- [ ] **Step 3: Verify the 428**

In the browser, open session `1930dec9` and click Resume.

Expected: the dialog appears listing one deleted connector (labelled with its bare uuid — it was deleted before tombstones existed, which is expected and documented in the spec). No silent failure, no dead button.

- [ ] **Step 4: Verify the acknowledgment**

Click "Resume without them".

Expected: the session resumes, attaches, and runs with KurortEngine only.

- [ ] **Step 5: Verify the ack persisted and does not re-prompt**

```bash
kubectl --context main exec -n superhuman-remote-worker srw-postgres-0 -- \
  psql -U srw -d srw -At -c "
  SELECT metadata->'config_drift_ack'
  FROM threads WHERE id='1930dec9-181d-4fd5-a030-90b3d0b363d6';"
```

Expected: `{"connector:d7555d5d-ce46-49e2-b1fa-8235d720badc": "deleted"}`.

End the session and resume it again — expected: no dialog, straight to 200.

- [ ] **Step 6: Verify delete-time cleanup end to end**

Create a throwaway connector, attach it to a new session, delete the connector, then confirm both the tombstone exists and the reference was scrubbed:

```bash
kubectl --context main exec -n superhuman-remote-worker srw-postgres-0 -- \
  psql -U srw -d srw -c "SELECT id, name FROM datasource_tombstones
                         ORDER BY deleted_at DESC LIMIT 3;"
```

Expected: the throwaway connector's name is present, and the new session resumes without any dialog because the reference is gone.

- [ ] **Step 7: Record the outcome**

Append a "Live gate" section to `docs/features/session_config_drift_resume.md` recording the date, what passed, and anything that did not. Commit.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
| --- | --- |
| §1 root cause / blind catch | 9 |
| §2 three drift families | 1, 2, 3, 4 |
| §3.1 one enumerator | 1, 2, 3, 4 |
| §3.2 acknowledge without mutating | 6, 7 |
| §3.3 connectors vs grants ack differently | 7 |
| §4.1 428 not 409 | 6, 9 |
| §4.2–4.4 request / response / subset rule | 6 |
| §4.5 order of operations | 6, 7 |
| §4.6 programmatic clients | 8 |
| §5 disclosure rules | 4 |
| §5.1 tombstones | 5 |
| §6 reference cleanup on delete | 5 |
| §7 failure modes | 4, 6 |
| §8 cockpit changes | 9, 10 |
| §9 testing | every task + 11 |
| §10 out of scope | not implemented, by design |

No spec section is unimplemented.

**Placeholder scan:** none. Three tasks (5, 6, 8) tell the implementer to locate an existing fixture/pattern rather than inventing one — each names the specific file to start from, and Task 5 states explicitly what to do if the fixture does not exist rather than leaving it open.

**Type consistency:** `ItemVerdict.datasource_id` / `ProjectVerdict.project_id` are distinct on purpose and are consumed as such in `collect_config_drift`. `DriftItem` fields are identical across Tasks 4, 6, 9, 10. `classifyResumeError` returns `ResumeOutcome`, consumed in Task 9 Step 5 and never elsewhere. `pendingDrift` is defined in Task 9 and consumed in Task 10. `strip_acknowledged(..., prefix=)` uses `"connector"` / `"project"`, matching the `DriftItem.id` namespaces built in Task 4.

**Known risk carried deliberately:** Task 6's `_thread_config_drift` runs a full `_resolve_session_config` purely to harvest grant violations, which also injects credentials when grants pass. That is wasted work on every resume. It is accepted because the alternative — extracting the merge — is a large refactor of a long function, and sharing the exact enforcement path is what guarantees the dialog cannot promise something attach will not honor. Revisit if resume latency regresses.
