# Expert CRUD + Create UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, batched with checkpoints) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Commits:** The user owns ALL commits. Execution leaves changes **uncommitted** unless the user explicitly says otherwise. The `git commit` steps below are logical checkpoints, not actions to run autonomously.

**Goal:** Restore the clobbered DB-backed expert write-CRUD on the orchestrator and ship a Cockpit "Experts" page where any authenticated user can create, edit, duplicate, delete, and import/export expert configs.

**Architecture:** The orchestrator owns ALL config resolution (`orchestrator/services/config_resolver.py::resolve_config`); the agent is a pure executor. This work touches ONLY the DB-management surface — create/update endpoints persist a raw *fragment* (`config` + `prompts`); resolution stays orchestrator-side and is unchanged. Credential safety is enforced server-side at save time (`hard_deny_scan` → 422) and again at dispatch (existing merged-config scan). No per-user grants (Slice 2) — creation is open to all approved users.

**Tech Stack:** FastAPI + asyncpg (orchestrator), Angular 21 standalone/signals + Transloco (Cockpit), pytest (Py3.12 CI is the gate), vitest+jsdom (Cockpit).

**Spec of record:** `docs/features/global_expert_management.md` — Slice 1 (write-CRUD, "no grants yet, hard-deny only") + Slice 3 (Cockpit page + type-aware editor). This plan is the restore-of-Slice-1-write-half + the create/edit subset of Slice 3.

---

## Implementation status — ✅ COMPLETE (2026-06-17)

All 10 tasks implemented, tested, and verified live on dev k3d (including a browser walkthrough).
**Uncommitted on `develop`** — owner commits. Backend **46** pytest + Cockpit **579** vitest green;
`ruff` + `tsc` + `ng build` (AOT template check) clean.

**Verified live:**
- **Backend** (k3d, MCP-header auth): create → 409 dup-name → 422 credential-deny →
  update/version-bump → export(no creds) → duplicate → delete; `source=user`; self-cleaned.
- **Cockpit** (Playwright, as dev `test` user): `/experts/new` renders, **auto-slug** works
  (`Research Helper 2026` → `research-helper-2026`), 0 console errors; tilt redeployed the cockpit.

**As-built deviations from the task bodies below (intentional — believe these over the steps where they conflict):**
1. **Auth style** — handlers are `async def f(request: Request, …)` + `await require_approved_user(request, postgres_db)`
   (returns the user dict), **not** `Depends(...)`. Delete returns `{"deleted": true}`. Ported verbatim from
   `8334fb3c` with the single rename `_is_uuid` → `_looks_like_uuid`.
2. **Editor config surface** — shipped an **editable raw `config` JSON textarea** (single source of truth,
   server-validated by the hard-deny gate) instead of wiring the `app-tools-group` widget. Structured
   tool toggles + a model picker are the documented fast-follow.
3. **Edit-prefill** — uses `GET /api/experts/{id}/export` (the raw fragment) so re-saving never bakes the
   merged result into the stored fragment.
4. **Test infra** — added `tests/conftest.py` `os.environ.setdefault("VECTOR_DB_URL", …)` so
   `orchestrator.main`-importing tests run locally (never overrides CI).
5. **Debug grid** — also registered the experts view in `ComponentRegistryService`
   (`app.ts:registerComponents()` + `'experts-list'` added to the `ComponentType` union in
   `debug/layout.model.ts`). Not in the original task list — the debug grid is a separate hand-maintained
   registry, not route-derived.
6. **Theme tokens** — editor/list styles use real theme tokens (`--panel-bg`, `--text-primary`,
   `--text-muted`, `--border-color`, `--danger-tint`/`--success-tint`). An earlier hardcoded
   `var(--surface-color, #161616)` fallback caused a dark-on-dark contrast bug (found in the browser
   walkthrough), fixed.
7. **Routes are eager** (`component:`), consistent with the rest of `app.routes.ts` (not lazy `loadComponent`).

**Files changed:** `orchestrator/main.py`, `src/api/app.py`, `src/database/postgres_db.py`,
`tests/test_expert_crud.py` (new), `tests/conftest.py`; cockpit `core/models/api.model.ts`,
`core/services/api.service.ts` (+ spec), `views/experts/*` (page/list/editor + 3 specs),
`app.ts`, `app.routes.ts`, `shell/sidebar/sidebar.component.ts`, `debug/layout.model.ts`,
`assets/i18n/en.json` + `de-DE.json`.

**Still deferred (unchanged):** Slice 2 grants + `/api/users/me/capabilities` + control-greying,
project-link/`default_for` UI, test-drive, version/stats panels, the structured tool-toggle widget,
de-DE page translations.

---

## Scope

**In scope (this plan):**
- Backend: `POST/PUT/DELETE /api/experts`, `POST /api/experts/{id}/duplicate`, `GET /api/experts/{id}/export`, `POST /api/experts/import` + the `ExpertCreate`/`ExpertUpdate` models + the save-time hard-deny gate.
- Cockpit: an Experts page (list with filters/badges/row-actions) + a type-aware create/edit editor (identity, persona/instructions, model, tool toggles, type-specific fields) + duplicate/export/import wiring.
- Cleanup: delete the dead agent-side `_apply_db_expert` call + orphaned `ExpertsNamespace` (keeps "agent is a pure executor" true in code).

**Out of scope (deferred — DO NOT build):**
- Slice 2 grants/enforcement, `/api/users/me/capabilities`, control-greying, grant-fed model narrowing.
- Project-link / `default_for` UI (the `project_experts` API).
- Test-drive button, version/stats panels (Slice 4).
- Worker verification/scholar/curator sub-expert toggles (decision 11) — defer; the type switch covers base + tool mode + session-only fields only.
- Monaco; an editable raw-fragment flap (v1 shows the assembled fragment **read-only** for transparency); icon/color *picker* widgets (use text input + live preview).
- Admin "make global" (`is_global` stays non-writable, matching `8334fb3c`).

## Design decisions (baked in — flag at review if you disagree)

1. **Editor surface = dedicated routes**, not the datasources inline-form. `/experts` (list), `/experts/new` and `/experts/:id/edit` (one `ExpertEditorComponent`). Rationale: the type-aware editor is far richer than a datasource form and the list component must stay under the 32 kB `anyComponentStyle` budget.
2. **Open to all approved users.** Routes guard with `authGuard` only (no `adminGuard`); nav item in the always-visible group. Server enforces credential safety via `hard_deny_scan`, not per-user grants.
3. **Reuse `app-tools-group`** (verified API) for tool toggles; hand-roll identity/persona/model/type with UI primitives. Deeper `app-agent-settings` reuse (model-group/execution-group) is a later enhancement — avoids coupling to unverified sub-group internals in v1.
4. **Delete uses an `app-dialog` confirm**; a 409 "in use" response lists the blocking jobs.
5. **`name` (slug) is create-only** (immutable; `ExpertUpdate` has no `name`). The create form auto-suggests a slug from `display_name` but lets the user edit it.
6. **Cleanup is a separable task (T9)** — flagged so it can be dropped if you want CRUD-only.

## File structure

**Backend (orchestrator):**
- Modify: `orchestrator/main.py` — one new block after `get_expert` (~line 16173): `ExpertCreate`, `ExpertUpdate`, `_require_experts_db`, `_validate_expert_fragment`, `_bundled_expert_bundle`, `_db_expert_to_bundle_src`, `_create_forked_expert`, and the 6 endpoints. (postgres.py store methods + `expert_resolution.py` helpers already exist — DO NOT touch.)
- Modify (T9 cleanup): `src/api/app.py` (remove dead `_apply_db_expert` call ~108-117), `src/database/postgres_db.py` (remove orphaned `ExpertsNamespace` ~994 + registration ~136).
- Test: `tests/test_expert_crud.py` (new).

**Frontend (cockpit):**
- Modify: `cockpit/src/app/core/models/api.model.ts` (add `ExpertCreateRequest`, `ExpertUpdateRequest`; add `name?`, `owner_id?`, `version?` to `ExpertDetail`).
- Modify: `cockpit/src/app/core/services/api.service.ts` (add `createExpert`/`updateExpert`/`deleteExpert`/`duplicateExpert`/`exportExpert`/`importExpert`).
- Create: `cockpit/src/app/views/experts/experts-page.component.ts` (thin wrapper, copy `datasources-page.component.ts`).
- Create: `cockpit/src/app/views/experts/experts-list.component.ts` (list + filters + badges + row actions).
- Create: `cockpit/src/app/views/experts/expert-editor.component.ts` (create/edit editor).
- Create specs: `experts-list.component.spec.ts`, `expert-editor.component.spec.ts`.
- Modify: `cockpit/src/app/app.routes.ts` (3 routes), `cockpit/src/app/shell/sidebar/sidebar.component.ts` (nav item), `cockpit/src/assets/i18n/en.json` + `de-DE.json` (`nav.experts` + `experts` block).

---

## Task 1: Backend — models, save-time gate, and create endpoint

**Files:**
- Modify: `orchestrator/main.py` (insert after `get_expert`, ~line 16173, before the `# Project Expert Endpoints` banner)
- Test: `tests/test_expert_crud.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_expert_crud.py
import pytest
from orchestrator.main import ExpertCreate, ExpertUpdate, _validate_expert_fragment
from fastapi import HTTPException


def test_expert_create_rejects_bad_slug():
    with pytest.raises(Exception):
        ExpertCreate(name="Bad Name", display_name="X", expert_type="worker")


def test_expert_create_rejects_bad_type():
    with pytest.raises(Exception):
        ExpertCreate(name="ok", display_name="X", expert_type="orchestrator")


def test_expert_create_minimal_ok():
    e = ExpertCreate(name="my-helper", display_name="My Helper", expert_type="session")
    assert e.config == {} and e.prompts == {} and e.color == "#6B7280"


def test_validate_fragment_blocks_credentials():
    with pytest.raises(HTTPException) as ei:
        _validate_expert_fragment({"llm": {"api_key": "secret"}})
    assert ei.value.status_code == 422


def test_validate_fragment_allows_clean_config():
    _validate_expert_fragment({"llm": {"model": "gemma-4-moe"}, "tools": {"shell": True}})


def test_expert_update_excludes_immutable_name():
    assert "name" not in ExpertUpdate.model_fields
    assert "expert_type" not in ExpertUpdate.model_fields
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_expert_crud.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExpertCreate'`.
(Local env may be noisy per repo norms; the import error is the signal. CI Py3.12 is authoritative.)

- [ ] **Step 3: Add the models + save-time gate**

In `orchestrator/main.py`, immediately after `get_expert`'s return (~line 16173), open the block:

```python
# ── User-Defined Experts: DB-backed CRUD + import/export (Slice 1) ────


class ExpertCreate(BaseModel):
    """Create a DB-backed expert (Slice 1: hard-deny validated; grants in S2)."""

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_-]*$", max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    expert_type: Literal["worker", "session"]
    description: str | None = None
    icon: str = "smart_toy"
    color: str = Field("#6B7280", pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] = []
    config: dict[str, Any] = {}
    prompts: dict[str, Any] = {}


class ExpertUpdate(BaseModel):
    """Patch a DB expert; name + expert_type are immutable so they are absent."""

    display_name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    icon: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] | None = None
    config: dict[str, Any] | None = None
    prompts: dict[str, Any] | None = None


def _require_experts_db() -> None:
    """The DB-experts feature is fully behind EXPERTS_DB_ENABLED."""
    if not _is_experts_db_enabled():
        raise HTTPException(status_code=404, detail="DB-backed experts are not enabled")


def _validate_expert_fragment(config: dict[str, Any]) -> None:
    """Reject credential sections in a user fragment (decision 10, hard-deny)."""
    from src.core.expert_resolution import hard_deny_scan

    offending = hard_deny_scan(config)
    if offending:
        raise HTTPException(
            status_code=422,
            detail="config may not set credential sections: "
            + ", ".join(sorted(offending)),
        )
```

> Verify `_is_experts_db_enabled` (main.py ~944) and `Literal`/`Any`/`BaseModel`/`Field` imports exist (they do per research). Do NOT re-declare them.

- [ ] **Step 4: Add the create endpoint**

Match the auth style of the existing `list_experts`/`get_expert` handlers (they use `Depends(require_approved_user)` — confirm and mirror exactly):

```python
@app.post("/api/experts")
async def create_expert(
    body: ExpertCreate, user: dict = Depends(require_approved_user)
):
    _require_experts_db()
    _validate_expert_fragment(body.config)
    try:
        row = await postgres_db.create_expert(
            name=body.name,
            display_name=body.display_name,
            expert_type=body.expert_type,
            owner_id=user["id"],
            description=body.description,
            icon=body.icon,
            color=body.color,
            tags=body.tags,
            config=body.config,
            prompts=body.prompts,
        )
    except Exception as e:  # noqa: BLE001
        if "uq_experts_name_owner" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"You already have an expert named '{body.name}'",
            )
        raise
    return row
```

> Cross-check signature/body against `git show 8334fb3c -- orchestrator/main.py` (function `create_expert`) and apply any `_is_uuid`→`_looks_like_uuid` rename if present. The store method `postgres_db.create_expert` already exists (postgres.py:5248) with this exact kwarg shape.

- [ ] **Step 5: Run unit tests to verify they pass**

Run: `python -m pytest tests/test_expert_crud.py -v`
Expected: PASS (6 tests). If `import orchestrator.main` is too heavy locally, note it and rely on CI; the model/helper logic is hermetic.

- [ ] **Step 6: Lint**

Run: `ruff check orchestrator/main.py tests/test_expert_crud.py`
Expected: clean (push workflow also auto-runs ruff).

- [ ] **Step 7: Commit (logical checkpoint — do not run unless user authorizes)**

```bash
git add orchestrator/main.py tests/test_expert_crud.py
git commit -m "feat(experts): restore create endpoint + save-time hard-deny gate"
```

---

## Task 2: Backend — update + delete endpoints

**Files:**
- Modify: `orchestrator/main.py` (same block, after `create_expert`)
- Test: `tests/test_expert_crud.py`

- [ ] **Step 1: Add update/delete contract tests**

Append:

```python
def test_update_payload_drops_unset_fields():
    body = ExpertUpdate(display_name="New Name")
    dumped = body.model_dump(exclude_unset=True)
    assert dumped == {"display_name": "New Name"}


def test_update_validates_config_when_present():
    with pytest.raises(HTTPException) as ei:
        _validate_expert_fragment(
            ExpertUpdate(config={"connections": {"db": "x"}}).config
        )
    assert ei.value.status_code == 422
```

Run: `python -m pytest tests/test_expert_crud.py -k "update" -v` → both pass (these exercise already-present code; they guard the contract the endpoints below rely on).

- [ ] **Step 2: Add the update endpoint**

```python
@app.put("/api/experts/{expert_id}")
async def update_expert(
    expert_id: str, body: ExpertUpdate, user: dict = Depends(require_approved_user)
):
    _require_experts_db()
    if not _looks_like_uuid(expert_id):
        raise HTTPException(
            status_code=403, detail="Bundled experts are read-only; duplicate to customize"
        )
    row = await postgres_db.get_expert_by_id(expert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Expert not found")
    if str(row["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Not your expert")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("config") is not None:
        _validate_expert_fragment(fields["config"])
    updated = await postgres_db.update_expert(
        expert_id, updated_by=user["id"], **fields
    )
    return updated
```

- [ ] **Step 3: Add the delete endpoint (with 409 in-use block)**

```python
@app.delete("/api/experts/{expert_id}")
async def delete_expert(expert_id: str, user: dict = Depends(require_approved_user)):
    _require_experts_db()
    if not _looks_like_uuid(expert_id):
        raise HTTPException(status_code=403, detail="Bundled experts cannot be deleted")
    row = await postgres_db.get_expert_by_id(expert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Expert not found")
    if str(row["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Not your expert")
    blockers = await postgres_db.expert_delete_blockers(expert_id)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={"message": "Expert is referenced by active work", "blockers": blockers},
        )
    await postgres_db.delete_expert(expert_id)
    return {"status": "deleted"}
```

> Store methods `get_expert_by_id` (5287), `update_expert` (5325; whitelists columns, bumps version, sets `updated_at`), `expert_delete_blockers` (5363; keep its current `('created','waiting')` filter), `delete_expert` (5394) all exist. Cross-check the handler bodies against `git show 8334fb3c`.

- [ ] **Step 4: Run + lint**

Run: `python -m pytest tests/test_expert_crud.py -v` → PASS. `ruff check orchestrator/main.py` → clean.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add orchestrator/main.py tests/test_expert_crud.py
git commit -m "feat(experts): restore update + delete endpoints"
```

---

## Task 3: Backend — duplicate, export, import + bundle helpers

**Files:**
- Modify: `orchestrator/main.py` (same block; helpers go above the endpoints)
- Test: `tests/test_expert_crud.py`

- [ ] **Step 1: Extract the three bundle helpers from history**

Run: `git show 8334fb3c -- orchestrator/main.py | grep -nA40 "_bundled_expert_bundle\|_db_expert_to_bundle_src\|_create_forked_expert"`

Port them **verbatim** into the block (just above the endpoints), applying any `_is_uuid`→`_looks_like_uuid` rename. Their behavior (per research):
- `_bundled_expert_bundle(expert_id)` → reads `config/experts/{id}/{config.yaml,persona.txt,instructions.md}`, infers `expert_type` from `$extends`; returns the bundle dict or `None`. Uses `_get_config_dir()` (15840), `_scan_experts()` (15856), `_experts_cache` (15905) — all present.
- `_db_expert_to_bundle_src(row)` → normalizes a DB row (JSONB-string-tolerant) to the bundle shape.
- `_create_forked_expert(src, owner_id, suffix="copy")` → `await postgres_db.create_expert(...)`, retry-suffix on `uq_experts_name_owner` collision.

- [ ] **Step 2: Add a fork-suffix unit test**

If `_create_forked_expert` derives the forked name with a pure helper, test it; otherwise add a light test that `_db_expert_to_bundle_src` maps a row dict to `{name, display_name, expert_type, config, prompts, ...}`:

```python
def test_db_row_to_bundle_src_shape():
    from orchestrator.main import _db_expert_to_bundle_src
    row = {"name": "scholar", "display_name": "Scholar", "expert_type": "worker",
           "description": None, "icon": "school", "color": "#111111",
           "tags": ["research"], "config": {"llm": {"model": "x"}}, "prompts": {"persona": "p"}}
    src = _db_expert_to_bundle_src(row)
    assert src["name"] == "scholar" and src["expert_type"] == "worker"
    assert src["config"] == {"llm": {"model": "x"}}
```

Run: `python -m pytest tests/test_expert_crud.py -k "bundle" -v` → PASS.

- [ ] **Step 3: Add duplicate / export / import endpoints**

```python
@app.post("/api/experts/{expert_id}/duplicate")
async def duplicate_expert(expert_id: str, user: dict = Depends(require_approved_user)):
    _require_experts_db()
    if _looks_like_uuid(expert_id):
        row = await postgres_db.get_expert_by_id(expert_id)
        if not row:
            raise HTTPException(status_code=404, detail="Expert not found")
        src = _db_expert_to_bundle_src(row)
    else:
        src = _bundled_expert_bundle(expert_id)
        if not src:
            raise HTTPException(status_code=404, detail="Expert not found")
    return await _create_forked_expert(src, user["id"])


@app.get("/api/experts/{expert_id}/export")
async def export_expert(expert_id: str, user: dict = Depends(require_approved_user)):
    _require_experts_db()
    if _looks_like_uuid(expert_id):
        row = await postgres_db.get_expert_by_id(expert_id)
        if not row:
            raise HTTPException(status_code=404, detail="Expert not found")
        src = _db_expert_to_bundle_src(row)
    else:
        src = _bundled_expert_bundle(expert_id)
        if not src:
            raise HTTPException(status_code=404, detail="Expert not found")
    from src.core.expert_resolution import to_export_bundle

    return to_export_bundle(src)


@app.post("/api/experts/import")
async def import_expert(body: ExpertCreate, user: dict = Depends(require_approved_user)):
    _require_experts_db()
    _validate_expert_fragment(body.config)
    src = {
        "name": body.name,
        "display_name": body.display_name,
        "expert_type": body.expert_type,
        "description": body.description,
        "icon": body.icon,
        "color": body.color,
        "tags": body.tags,
        "config": body.config,
        "prompts": body.prompts,
    }
    return await _create_forked_expert(src, user["id"], suffix="import")
```

> Cross-check against `git show 8334fb3c` and reconcile any naming nuance (`to_export_bundle` exists at `src/core/expert_resolution.py`).

- [ ] **Step 4: Run + lint**

Run: `python -m pytest tests/test_expert_crud.py -v` → PASS. `ruff check orchestrator/main.py` → clean.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add orchestrator/main.py tests/test_expert_crud.py
git commit -m "feat(experts): restore duplicate/export/import endpoints"
```

> **CHECKPOINT A** — backend write-CRUD complete. Verify on k3d before frontend (see Task 10 procedure, run the `create→duplicate→export→delete` curl subset now if convenient).

---

## Task 4: Frontend — DTOs + ApiService write methods

**Files:**
- Modify: `cockpit/src/app/core/models/api.model.ts`
- Modify: `cockpit/src/app/core/services/api.service.ts`
- Test: extend an existing api-service spec or add `cockpit/src/app/core/services/api.service.experts.spec.ts`

- [ ] **Step 1: Add request DTOs to `api.model.ts`** (after the `Expert`/`ExpertDetail` block, ~line 74)

```ts
export interface ExpertCreateRequest {
  name: string;
  display_name: string;
  expert_type: 'worker' | 'session';
  description?: string | null;
  icon?: string;
  color?: string;
  tags?: string[];
  config?: Record<string, unknown>;
  prompts?: Record<string, unknown>;
}

export type ExpertUpdateRequest = Partial<Omit<ExpertCreateRequest, 'name' | 'expert_type'>>;
```

Also extend `ExpertDetail` with the fields the editor reads back: `name?: string; owner_id?: string; version?: number;` (server returns them on create/get).

- [ ] **Step 2: Write the failing service test**

```ts
// api.service.experts.spec.ts
import {describe, expect, it, vi} from 'vitest';
import {ApiService} from './api.service';

describe('ApiService expert write methods', () => {
  it('exposes create/update/delete/duplicate/export/import', () => {
    const svc = Object.create(ApiService.prototype) as ApiService;
    for (const m of ['createExpert', 'updateExpert', 'deleteExpert',
                     'duplicateExpert', 'exportExpert', 'importExpert']) {
      expect(typeof (svc as any)[m]).toBe('function');
    }
  });
});
```

Run: `cd cockpit && npx vitest run src/app/core/services/api.service.experts.spec.ts` → FAIL (methods undefined).

- [ ] **Step 3: Add methods to `api.service.ts`** (next to `getExperts`/`getExpertDetail`, ~line 470, mirroring `createDatasource`/`updateDatasource` at 588-607)

```ts
createExpert(body: ExpertCreateRequest): Observable<ExpertDetail> {
  return this.http.post<ExpertDetail>(`${this.baseUrl}/experts`, body);
}
updateExpert(id: string, body: ExpertUpdateRequest): Observable<ExpertDetail> {
  return this.http.put<ExpertDetail>(`${this.baseUrl}/experts/${id}`, body);
}
deleteExpert(id: string): Observable<{status: string}> {
  return this.http.delete<{status: string}>(`${this.baseUrl}/experts/${id}`);
}
duplicateExpert(id: string): Observable<ExpertDetail> {
  return this.http.post<ExpertDetail>(`${this.baseUrl}/experts/${id}/duplicate`, {});
}
exportExpert(id: string): Observable<Record<string, unknown>> {
  return this.http.get<Record<string, unknown>>(`${this.baseUrl}/experts/${id}/export`);
}
importExpert(body: ExpertCreateRequest): Observable<ExpertDetail> {
  return this.http.post<ExpertDetail>(`${this.baseUrl}/experts/import`, body);
}
```

Import `ExpertCreateRequest`, `ExpertUpdateRequest`, `ExpertDetail` from `../models/api.model`. Confirm `this.baseUrl`/`this.http` names match the existing methods in the file.

- [ ] **Step 4: Run test + tsc**

Run: `cd cockpit && npx vitest run src/app/core/services/api.service.experts.spec.ts` → PASS.
Run: `cd cockpit && npx tsc --noEmit` → clean.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add cockpit/src/app/core/models/api.model.ts cockpit/src/app/core/services/api.service.ts cockpit/src/app/core/services/api.service.experts.spec.ts
git commit -m "feat(cockpit): expert write API methods + DTOs"
```

---

## Task 5: Frontend — Experts page scaffolding (route, nav, i18n, wrapper, list)

**Files:**
- Create: `cockpit/src/app/views/experts/experts-page.component.ts`
- Create: `cockpit/src/app/views/experts/experts-list.component.ts`
- Modify: `cockpit/src/app/app.routes.ts`, `cockpit/src/app/shell/sidebar/sidebar.component.ts`, `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`

- [ ] **Step 1: Add routes** (`app.routes.ts`, beside the datasources route ~line 37)

```ts
import {ExpertsPageComponent} from './views/experts/experts-page.component';
// ...
{path: 'experts', component: ExpertsPageComponent, canActivate: [authGuard]},
{path: 'experts/new', loadComponent: () =>
  import('./views/experts/expert-editor.component').then(m => m.ExpertEditorComponent),
  canActivate: [authGuard]},
{path: 'experts/:id/edit', loadComponent: () =>
  import('./views/experts/expert-editor.component').then(m => m.ExpertEditorComponent),
  canActivate: [authGuard]},
```

- [ ] **Step 2: Add nav item** (`sidebar.component.ts`, copy the datasources `<a>` block ~80-87, into the always-visible `.sidebar-nav` group — NOT the admin `@if` block)

```html
<a class="nav-link" routerLink="/experts" routerLinkActive="active">
  <app-icon size="md" class="nav-icon">psychology</app-icon>
  <span>{{ 'nav.experts' | transloco }}</span>
</a>
```

- [ ] **Step 3: Add i18n keys** (`en.json`: add `"experts": "Experts"` under `nav`; add a top-level `"experts": { ... }` block mirroring `datasources`. Repeat in `de-DE.json` with German strings.) Minimum keys: `title`, `new`, `empty`, `filter_all/worker/session`, `col_name/type/source/actions`, `edit/duplicate/export/delete/import`, `bundled_readonly`, `confirm_delete_title/body`, `deleted`, `in_use`, `created`, `updated`, `save`, `cancel`.

- [ ] **Step 4: Create the page wrapper** (copy `datasources-page.component.ts` verbatim, rename)

```ts
import {Component} from '@angular/core';
import {SidebarToggleComponent} from '../../shell/sidebar/sidebar-toggle.component';
import {ExpertsListComponent} from './experts-list.component';

@Component({
  selector: 'app-experts-page',
  standalone: true,
  imports: [SidebarToggleComponent, ExpertsListComponent],
  template: `<app-sidebar-toggle/><main class="content"><app-experts-list/></main>`,
})
export class ExpertsPageComponent {}
```
Confirm the `SidebarToggleComponent` import path against `datasources-page.component.ts`.

- [ ] **Step 5: Write the failing list-logic test**

```ts
// experts-list.component.spec.ts
import {describe, expect, it} from 'vitest';
import {filterExperts} from './experts-list.component';
import type {Expert} from '../../core/models/api.model';

const rows: Expert[] = [
  {id: '1', display_name: 'A', description: '', icon: '', color: '', tags: [], source: 'user', expert_type: 'worker'},
  {id: 'scholar', display_name: 'Scholar', description: '', icon: '', color: '', tags: [], source: 'bundled', expert_type: 'worker'},
  {id: '2', display_name: 'B', description: '', icon: '', color: '', tags: [], source: 'global', expert_type: 'session'},
];

describe('filterExperts', () => {
  it('all returns everything', () => expect(filterExperts(rows, 'all').length).toBe(3));
  it('worker filter', () => expect(filterExperts(rows, 'worker').map(r => r.id)).toEqual(['1', 'scholar']));
  it('session filter', () => expect(filterExperts(rows, 'session').map(r => r.id)).toEqual(['2']));
});
```

Run: `cd cockpit && npx vitest run src/app/views/experts/experts-list.component.spec.ts` → FAIL.

- [ ] **Step 6: Create the list component** (table + chip filters + badges + row actions; export the pure `filterExperts` helper for the test)

```ts
import {Component, computed, inject, OnInit, signal} from '@angular/core';
import {Router} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import type {Expert} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppBadgeComponent} from '../../ui/badge';
import {AppChipComponent} from '../../ui/chip';
import {AppIconComponent} from '../../ui/icon';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppDialogComponent} from '../../ui/dialog';

export type ExpertTypeFilter = 'all' | 'worker' | 'session';

export function filterExperts(rows: Expert[], f: ExpertTypeFilter): Expert[] {
  return f === 'all' ? rows : rows.filter(r => r.expert_type === f);
}
export function isBundled(e: Expert): boolean {
  return (e.source ?? 'bundled') === 'bundled';
}

@Component({
  selector: 'app-experts-list',
  standalone: true,
  imports: [TranslocoPipe, AppButtonComponent, AppIconButtonComponent, AppBadgeComponent,
            AppChipComponent, AppIconComponent, AppSpinnerComponent, AppDialogComponent],
  template: `
    <header class="head">
      <h1>{{ 'experts.title' | transloco }}</h1>
      <app-button variant="primary" (clicked)="newExpert()">{{ 'experts.new' | transloco }}</app-button>
    </header>
    <div class="filters">
      @for (f of filters; track f) {
        <app-chip [selected]="typeFilter() === f" [selectable]="true" (clicked)="typeFilter.set(f)">
          {{ ('experts.filter_' + f) | transloco }}
        </app-chip>
      }
    </div>
    @if (loading()) { <app-spinner/> }
    @else if (rows().length === 0) { <p class="empty">{{ 'experts.empty' | transloco }}</p> }
    @else {
      <table class="grid">
        <thead><tr>
          <th>{{ 'experts.col_name' | transloco }}</th>
          <th>{{ 'experts.col_type' | transloco }}</th>
          <th>{{ 'experts.col_source' | transloco }}</th>
          <th class="actions-col">{{ 'experts.col_actions' | transloco }}</th>
        </tr></thead>
        <tbody>
          @for (e of filtered(); track e.id) {
            <tr>
              <td><app-icon [style.color]="e.color">{{ e.icon || 'smart_toy' }}</app-icon> {{ e.display_name }}</td>
              <td>{{ e.expert_type || '—' }}</td>
              <td>
                <app-badge [tone]="bundled(e) ? 'neutral' : 'info'">{{ e.source || 'bundled' }}</app-badge>
                @if (bundled(e)) { <app-badge tone="neutral">{{ 'experts.bundled_readonly' | transloco }}</app-badge> }
              </td>
              <td class="actions-col">
                @if (!bundled(e)) {
                  <app-icon-button ariaLabel="edit" variant="ghost" (clicked)="edit(e)">edit</app-icon-button>
                }
                <app-icon-button ariaLabel="duplicate" variant="ghost" (clicked)="duplicate(e)">content_copy</app-icon-button>
                <app-icon-button ariaLabel="export" variant="ghost" (clicked)="exportExpert(e)">download</app-icon-button>
                @if (!bundled(e)) {
                  <app-icon-button ariaLabel="delete" variant="danger" (clicked)="askDelete(e)">delete</app-icon-button>
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
    }
    @if (successMessage()) { <div class="banner ok">{{ successMessage() }}</div> }
    @if (errorMessage()) { <div class="banner err">{{ errorMessage() }}</div> }

    <app-dialog [open]="confirmOpen()" [title]="'experts.confirm_delete_title' | transloco" (closed)="confirmOpen.set(false)">
      <p>{{ 'experts.confirm_delete_body' | transloco }} <strong>{{ pendingDelete()?.display_name }}</strong></p>
      <div appDialogActions>
        <app-button variant="ghost" (clicked)="confirmOpen.set(false)">{{ 'experts.cancel' | transloco }}</app-button>
        <app-button variant="danger" (clicked)="confirmDelete()">{{ 'experts.delete' | transloco }}</app-button>
      </div>
    </app-dialog>
  `,
  styles: [`
    .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
    .filters{display:flex;gap:.5rem;margin-bottom:1rem}
    .grid{width:100%;border-collapse:collapse}
    .grid th,.grid td{text-align:left;padding:.5rem;border-bottom:1px solid var(--border,#2a2a2a)}
    .actions-col{text-align:right;white-space:nowrap}
    .empty{opacity:.7}
    .banner{margin-top:1rem;padding:.5rem .75rem;border-radius:6px}
    .banner.ok{background:#16331f;color:#7ee2a8}
    .banner.err{background:#3a1d1d;color:#f3a6a6}
  `],
})
export class ExpertsListComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);

  filters: ExpertTypeFilter[] = ['all', 'worker', 'session'];
  typeFilter = signal<ExpertTypeFilter>('all');
  rows = signal<Expert[]>([]);
  loading = signal(true);
  successMessage = signal('');
  errorMessage = signal('');
  confirmOpen = signal(false);
  pendingDelete = signal<Expert | null>(null);

  filtered = computed(() => filterExperts(this.rows(), this.typeFilter()));
  bundled = isBundled;

  ngOnInit(): void { this.refresh(); }

  refresh(): void {
    this.loading.set(true);
    this.api.getExperts().subscribe(rows => { this.rows.set(rows); this.loading.set(false); });
  }
  newExpert(): void { this.router.navigate(['/experts/new']); }
  edit(e: Expert): void { this.router.navigate(['/experts', e.id, 'edit']); }

  duplicate(e: Expert): void {
    this.api.duplicateExpert(e.id).subscribe({
      next: () => { this.successMessage.set('Duplicated'); this.refresh(); },
      error: err => this.errorMessage.set(err?.error?.detail || 'Duplicate failed'),
    });
  }
  exportExpert(e: Expert): void {
    this.api.exportExpert(e.id).subscribe(bundle => {
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `${e.display_name || e.id}.expert.json`; a.click();
      URL.revokeObjectURL(url);
    });
  }
  askDelete(e: Expert): void { this.pendingDelete.set(e); this.confirmOpen.set(true); }
  confirmDelete(): void {
    const e = this.pendingDelete(); if (!e) return;
    this.api.deleteExpert(e.id).subscribe({
      next: () => { this.confirmOpen.set(false); this.successMessage.set('Deleted'); this.refresh(); },
      error: err => {
        this.confirmOpen.set(false);
        const d = err?.error?.detail;
        this.errorMessage.set(typeof d === 'object' && d?.blockers
          ? `In use by ${d.blockers.length} job(s)` : (d || 'Delete failed'));
      },
    });
  }
}
```

> Confirm every `../../ui/*` barrel path + each primitive's input/output names against an existing consumer (e.g. `datasource-list.component.ts`). `app-badge` `tone` values and `app-chip` `selectable`/`clicked` are per research; adjust if the real API differs.

- [ ] **Step 7: Run tests + tsc + style budget**

Run: `cd cockpit && npx vitest run src/app/views/experts/experts-list.component.spec.ts` → PASS.
Run: `cd cockpit && npx tsc --noEmit` → clean.
Confirm the inline `styles` block is well under 32 kB.

- [ ] **Step 8: Commit (checkpoint)**

```bash
git add cockpit/src/app/views/experts/ cockpit/src/app/app.routes.ts cockpit/src/app/shell/sidebar/sidebar.component.ts cockpit/src/assets/i18n/
git commit -m "feat(cockpit): experts list page (route, nav, filters, row actions)"
```

> **CHECKPOINT B** — list + duplicate/export/delete work against live backend. The "New"/edit buttons route to a not-yet-existing editor (Task 6).

---

## Task 6: Frontend — the type-aware create/edit editor

**Files:**
- Create: `cockpit/src/app/views/experts/expert-editor.component.ts`
- Create: `cockpit/src/app/views/experts/expert-editor.component.spec.ts`

- [ ] **Step 1: Write the failing assembly test** (pure helpers for slug + config assembly)

```ts
// expert-editor.component.spec.ts
import {describe, expect, it} from 'vitest';
import {slugify, assembleConfig} from './expert-editor.component';

describe('expert editor helpers', () => {
  it('slugify', () => {
    expect(slugify('My Cool Expert!')).toBe('my-cool-expert');
    expect(slugify('  Über Helper ')).toMatch(/^[a-z][a-z0-9_-]*$/);
  });
  it('assembleConfig merges model + tool overrides, drops empties', () => {
    expect(assembleConfig('gemma-4-moe', {tools: {shell: false}})).toEqual({
      llm: {model: 'gemma-4-moe'}, tools: {shell: false},
    });
    expect(assembleConfig('', {})).toEqual({});
  });
});
```

Run: `cd cockpit && npx vitest run src/app/views/experts/expert-editor.component.spec.ts` → FAIL.

- [ ] **Step 2: Create the editor component**

Key design: a plain mutable `form` object (codebase convention — NOT reactive forms), signals for flags, `app-tools-group` for tool toggles (read via `@ViewChild`), `assembleConfig`/`slugify` exported as pure helpers. Persona/instructions → `prompts`; model + tool overrides → `config`. On edit, prefill from `getExpertDetail`.

```ts
import {Component, computed, inject, OnInit, signal, viewChild} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import type {ExpertCreateRequest, ExpertUpdateRequest} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppSelectComponent} from '../../ui/select';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppIconComponent} from '../../ui/icon';
import {ToolsGroupComponent} from '../agent-settings/tools-group.component';

export function slugify(s: string): string {
  const base = s.normalize('NFKD').replace(/[^\x00-\x7F]/g, '')
    .toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return /^[a-z]/.test(base) ? base : `e-${base}` || 'expert';
}
export function assembleConfig(model: string, toolOverrides: Record<string, unknown>): Record<string, unknown> {
  const cfg: Record<string, unknown> = {};
  if (model) cfg['llm'] = {model};
  if (toolOverrides && Object.keys(toolOverrides).length) Object.assign(cfg, toolOverrides);
  return cfg;
}

interface EditorForm {
  name: string; display_name: string; description: string;
  icon: string; color: string; tags: string;
  expert_type: 'worker' | 'session'; model: string;
  persona: string; instructions: string;
}

@Component({
  selector: 'app-expert-editor',
  standalone: true,
  imports: [TranslocoPipe, AppButtonComponent, AppInputComponent, AppTextareaComponent,
            AppSelectComponent, AppFormFieldComponent, AppIconComponent, ToolsGroupComponent],
  template: `
    <header class="head"><h1>{{ (isEdit() ? 'experts.edit' : 'experts.new') | transloco }}</h1></header>

    <section class="card">
      <app-form-field [label]="'Display name'" [required]="true">
        <app-input [value]="form.display_name" (valueChange)="onName($event)"/>
      </app-form-field>
      <app-form-field [label]="'Name (slug)'" [required]="true" [hint]="'lowercase, immutable after create'">
        <app-input [value]="form.name" [disabled]="isEdit()" (valueChange)="form.name = $event"/>
      </app-form-field>
      <app-form-field [label]="'Type'" [required]="true">
        <app-select [value]="form.expert_type" [disabled]="isEdit()" (valueChange)="form.expert_type = $event">
          <option value="worker">worker</option>
          <option value="session">session</option>
        </app-select>
      </app-form-field>
      <app-form-field [label]="'Description'">
        <app-input [value]="form.description" (valueChange)="form.description = $event"/>
      </app-form-field>
      <div class="row">
        <app-form-field [label]="'Icon'">
          <app-input [value]="form.icon" (valueChange)="form.icon = $event"/>
        </app-form-field>
        <app-icon [style.color]="form.color">{{ form.icon || 'smart_toy' }}</app-icon>
        <app-form-field [label]="'Color'">
          <app-input type="color" [value]="form.color" (valueChange)="form.color = $event"/>
        </app-form-field>
      </div>
      <app-form-field [label]="'Tags (comma separated)'">
        <app-input [value]="form.tags" (valueChange)="form.tags = $event"/>
      </app-form-field>
    </section>

    <section class="card">
      <h2>{{ 'Persona' }}</h2>
      <app-textarea [value]="form.persona" [rows]="8" (valueChange)="form.persona = $event"/>
      <h2>{{ 'Instructions (optional)' }}</h2>
      <app-textarea [value]="form.instructions" [rows]="5" (valueChange)="form.instructions = $event"/>
    </section>

    <section class="card">
      <h2>{{ 'Model' }}</h2>
      <app-input [value]="form.model" (valueChange)="form.model = $event" [placeholder]="'e.g. gemma-4-moe'"/>
      <h2>{{ 'Tools' }}</h2>
      <app-tools-group [config]="toolsConfig()" [mode]="toolMode()"/>
    </section>

    @if (errorMessage()) { <div class="banner err">{{ errorMessage() }}</div> }
    <footer class="actions">
      <app-button variant="ghost" (clicked)="cancel()">{{ 'experts.cancel' | transloco }}</app-button>
      <app-button variant="primary" [loading]="saving()" (clicked)="save()">{{ 'experts.save' | transloco }}</app-button>
    </footer>
  `,
  styles: [`
    .head{margin-bottom:1rem}
    .card{background:var(--surface,#161616);padding:1rem;border-radius:8px;margin-bottom:1rem;display:flex;flex-direction:column;gap:.75rem}
    .row{display:flex;gap:1rem;align-items:center}
    .actions{display:flex;justify-content:flex-end;gap:.5rem}
    .banner.err{background:#3a1d1d;color:#f3a6a6;padding:.5rem .75rem;border-radius:6px;margin-bottom:1rem}
  `],
})
export class ExpertEditorComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private toolsGroup = viewChild(ToolsGroupComponent);

  editingId = signal<string | null>(null);
  saving = signal(false);
  errorMessage = signal('');
  private prefillConfig = signal<Record<string, unknown>>({});

  form: EditorForm = {
    name: '', display_name: '', description: '', icon: 'smart_toy',
    color: '#6B7280', tags: '', expert_type: 'worker', model: '',
    persona: '', instructions: '',
  };
  private slugTouched = false;

  isEdit = computed(() => this.editingId() !== null);
  toolMode = computed<'job' | 'session'>(() => this.form.expert_type === 'session' ? 'session' : 'job');
  toolsConfig = computed(() => this.prefillConfig());

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (id) {
      this.editingId.set(id);
      this.api.getExpertDetail(id).subscribe(d => {
        if (!d) return;
        this.form.name = d.name ?? '';
        this.form.display_name = d.display_name;
        this.form.description = d.description ?? '';
        this.form.icon = d.icon; this.form.color = d.color;
        this.form.tags = (d.tags ?? []).join(', ');
        this.form.expert_type = (d.expert_type as 'worker' | 'session') ?? 'worker';
        this.form.model = (((d.config?.['llm'] as any)?.model) as string) ?? '';
        this.form.persona = (((d as any).prompts?.persona) as string) ?? d.instructions ?? '';
        this.form.instructions = (((d as any).prompts?.instructions) as string) ?? '';
        this.prefillConfig.set(d.config ?? {});
      });
    }
  }

  onName(v: string): void {
    this.form.display_name = v;
    if (!this.slugTouched && !this.isEdit()) this.form.name = slugify(v);
  }

  private buildPayload(): ExpertCreateRequest {
    const tools = this.toolsGroup()?.getOverrides?.() ?? {};
    return {
      name: this.form.name,
      display_name: this.form.display_name,
      expert_type: this.form.expert_type,
      description: this.form.description || null,
      icon: this.form.icon, color: this.form.color,
      tags: this.form.tags.split(',').map(t => t.trim()).filter(Boolean),
      config: assembleConfig(this.form.model, tools),
      prompts: {persona: this.form.persona, instructions: this.form.instructions},
    };
  }

  save(): void {
    this.saving.set(true); this.errorMessage.set('');
    const payload = this.buildPayload();
    const id = this.editingId();
    const obs = id
      ? this.api.updateExpert(id, payload as ExpertUpdateRequest)
      : this.api.createExpert(payload);
    obs.subscribe({
      next: () => this.router.navigate(['/experts']),
      error: err => { this.saving.set(false); this.errorMessage.set(err?.error?.detail || 'Save failed'); },
    });
  }
  cancel(): void { this.router.navigate(['/experts']); }
}
```

> **Verify before running:** (a) `ToolsGroupComponent` selector/inputs/`getOverrides()` against `cockpit/src/app/views/agent-settings/tools-group.component.ts`; (b) each `../../ui/*` primitive's input names (`value`/`valueChange`, `rows`, `disabled`, `loading`); (c) whether `getExpertDetail` returns `prompts` (the backend `get_expert` detail shape) — if persona lives under `instructions`/a different key, adjust the prefill. If `viewChild`/`getOverrides` timing is awkward, fall back to subscribing to the tools-group `change` output into a local signal.

- [ ] **Step 3: Run tests + tsc**

Run: `cd cockpit && npx vitest run src/app/views/experts/expert-editor.component.spec.ts` → PASS.
Run: `cd cockpit && npx tsc --noEmit` → clean. Confirm inline styles < 32 kB.

- [ ] **Step 4: Commit (checkpoint)**

```bash
git add cockpit/src/app/views/experts/expert-editor.component.ts cockpit/src/app/views/experts/expert-editor.component.spec.ts
git commit -m "feat(cockpit): type-aware expert create/edit editor"
```

> **CHECKPOINT C** — full create/edit/list/duplicate/export/delete loop works in the UI.

---

## Task 7: Frontend — import button

**Files:**
- Modify: `cockpit/src/app/views/experts/experts-list.component.ts`

- [ ] **Step 1: Add an Import control** to the list header (file input → parse JSON → `importExpert`)

```ts
// add to template head, next to the New button:
//   <app-button variant="secondary" (clicked)="fileInput.click()">{{ 'experts.import' | transloco }}</app-button>
//   <input #fileInput type="file" accept="application/json,.json,.yaml,.yml" hidden (change)="onImport($event)"/>
onImport(ev: Event): void {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0]; if (!file) return;
  file.text().then(text => {
    let body: any;
    try { body = JSON.parse(text); } catch { this.errorMessage.set('Invalid JSON'); return; }
    this.api.importExpert(body).subscribe({
      next: () => { this.successMessage.set('Imported'); this.refresh(); },
      error: err => this.errorMessage.set(err?.error?.detail || 'Import failed'),
    });
    input.value = '';
  });
}
```

- [ ] **Step 2: tsc + a small parse test** (optional) → `npx tsc --noEmit` clean.

- [ ] **Step 3: Commit (checkpoint)**

```bash
git add cockpit/src/app/views/experts/experts-list.component.ts
git commit -m "feat(cockpit): import expert from bundle file"
```

---

## Task 8: Cleanup — remove dead agent-side expert resolution (SEPARABLE)

> Skip this task if you want CRUD-only; it is hygiene, not function. It removes a latent `AttributeError` landmine and keeps "agent is a pure executor" true in code.

**Files:**
- Modify: `src/api/app.py` (~108-117), `src/database/postgres_db.py` (~136 registration, ~994 `ExpertsNamespace`)

- [ ] **Step 1: Confirm the call is dead**

Run: `rg -n "AGENT_EXPERT_ID|_apply_db_expert|ExpertsNamespace|self\.experts" src/`
Expected: the only producers are the `src/api/app.py` block + `src/database/postgres_db.py`; nothing sets `AGENT_EXPERT_ID` (the orchestrator strips all `expert_id=` from provisioner calls), and `src/agent.py` has no `_apply_db_expert`. So the `if _expert_id:` guard is never true.

- [ ] **Step 2: Remove the dead block in `src/api/app.py`** (the `_expert_id = os.environ.get("AGENT_EXPERT_ID")` … `await _agent._apply_db_expert(_expert_id)` block). Leave surrounding setup intact.

- [ ] **Step 3: Remove `ExpertsNamespace`** (class ~994) and its `self.experts = ExpertsNamespace(self)` registration (~136) in `src/database/postgres_db.py`.

- [ ] **Step 4: Verify nothing references them**

Run: `rg -n "_apply_db_expert|ExpertsNamespace|\.experts\b" src/ tests/`
Expected: no live references (test references, if any, removed/updated).
Run: `python -m pytest tests/test_resolved_config_hydrate.py tests/test_config_resolver.py -q` → still green (agent executor path unaffected).
Run: `ruff check src/api/app.py src/database/postgres_db.py` → clean.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add src/api/app.py src/database/postgres_db.py
git commit -m "chore(experts): remove dead agent-side _apply_db_expert + ExpertsNamespace"
```

---

## Task 9: Full test sweep + lint

- [ ] **Step 1: Backend**

Run: `python -m pytest tests/test_expert_crud.py tests/test_config_resolver.py tests/test_resolved_config_hydrate.py -v` (env may be noisy locally; CI Py3.12 is the gate).
Run: `ruff check orchestrator/ src/ tests/test_expert_crud.py` → "All checks passed!".

- [ ] **Step 2: Frontend**

Run: `cd cockpit && npx vitest run` → all green (~353 + new).
Run: `cd cockpit && npx tsc --noEmit` → clean.

---

## Task 10: Live verification on k3d (dev cluster, `EXPERTS_DB_ENABLED=on`)

> Agent code is unaffected here, but the orchestrator changed → it hot-reloads (no agent image rebuild needed). Drive the API with the internal key per [[local_k3d_testing_via_orchestrator_api]].

- [ ] **Step 1: Backend e2e (curl via the orchestrator, in-pod `urllib` or internal key)**
  - `POST /api/experts` `{name:"sentinel-helper", display_name:"Sentinel", expert_type:"session", prompts:{persona:"SENTINEL-PERSONA-XYZ"}, config:{llm:{model:"gemma-4-moe"}}}` → 200 with a UUID + `version:1`.
  - `POST /api/experts` same name again → **409**.
  - `POST /api/experts` `{... config:{llm:{api_key:"x"}}}` → **422** naming the credential key.
  - `PUT /api/experts/{id}` `{display_name:"Sentinel 2"}` → 200, `version:2`.
  - `GET /api/experts/{id}/export` → bundle with `persona` = `SENTINEL-PERSONA-XYZ`, no credentials.
  - `POST /api/experts/{id}/duplicate` → new UUID, name suffixed.
  - `GET /api/experts?type=session` → includes the row, `source:"user"`.
  - `DELETE /api/experts/{id}` → 200 `{status:"deleted"}`.

- [ ] **Step 2: Resolution still works** — create a session/job bound to the new expert (via `expert_id`) and confirm via `GET /api/agents/threads/{id}/workspace` that `resolved_config` carries the persona fenced + `_persona_source:"db"` + model `gemma-4-moe` (this proves the orchestrator-resolver consumes a UI-created expert end-to-end).

- [ ] **Step 3: Cockpit smoke (Playwright/manual on dev)** — Experts nav → create an expert through the editor → it appears in the list with the `user` badge → edit → duplicate → export downloads JSON → delete (confirm dialog). Job-create and session-create grids show the new expert and send `expert_id` (not `config_name`).

- [ ] **Step 4: Update docs + memory**
  - `docs/features/global_expert_management.md`: flip the stale status line — Slice 1 write-CRUD **restored**; Slice 3 create/edit/list/duplicate/export/import **shipped** (grants/project-link/test-drive still deferred).
  - Update memory `orchestrator-resolved-config-progress` (or a new `expert-crud-ui` memory): write-CRUD gap CLOSED; UI shipped; what's still deferred.

---

## Self-review notes (author)

- **Spec coverage:** Slice-1 write-CRUD (all 6 endpoints + save gate) ✓; Slice-3 list/create/edit/fork/delete/import/export ✓; deferrals (grants, project-link, test-drive, verification toggles, editable raw flap, pickers) explicitly listed ✓.
- **Type consistency:** `ExpertCreate`/`ExpertUpdate` (backend) ↔ `ExpertCreateRequest`/`ExpertUpdateRequest` (frontend) field names aligned; `name`/`expert_type` immutable on update on both sides; `source` server-assigned so the existing job/session `expert_id`-vs-`config_name` branch keeps working.
- **Known verify-points (flagged inline, not placeholders):** exact `../../ui/*` primitive input names; `ToolsGroupComponent.getOverrides()` shape; `get_expert` detail `prompts` shape; `require_approved_user` dependency style + user-dict `id`/`is_admin` keys. Each step says to cross-check against the named existing file before running.
- **Risk:** the editor's structured config covers model + tools only in v1 (raw flap read-only); richer fields land via deeper `app-agent-settings` reuse later. Acceptable for "create an expert" MVP.
