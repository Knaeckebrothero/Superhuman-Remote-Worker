# Public Datasources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grant-gated org-wide datasource publishing: a `public_datasources` capability gates `is_global` on the datasource API (closing today's ungated hole), a declarative `read_only` column (default TRUE on publish, forced for `kb`), and a Cockpit Visibility section with two-tier confirmation (warning dialog for read-only publish, type-the-name dialog for read-write).

**Architecture:** Backend adds one grant-catalog key, one nullable column (migration 0055), a `user_can_publish_datasource` helper mirroring `user_can_use_vm`, and gate checks in the create/update endpoints. Frontend adds a capability-gated form section, a reusable confirm-name dialog on top of `AppDialogComponent`, and RO/RW badges. Read-only is declarative everywhere except `kb` (architecturally read-only).

**Tech Stack:** FastAPI + asyncpg (orchestrator), Angular 19 standalone components with signals (cockpit), pytest, vitest.

**Spec:** `docs/features/public_datasources.md` (approved 2026-07-11).

## Global Constraints

- Work directly on `develop`. Commit after each task with a targeted `git add <files>` — **never `git add -A`** (the tree may hold unrelated WIP) and **never push**.
- CI is the test gate (Python 3.12, `pytest tests/ -x -q --tb=short`); local Python may be noisy — run the targeted test files listed per task.
- Frontend checks run from `cockpit/`: `npx vitest run <file>`, `npm run i18n:check`, `npm run build`.
- The 403 detail string is fixed by the spec: `"Publishing public datasources requires the 'public_datasources' capability"`.
- The 400 detail string for kb: `"Knowledge-base datasources are always read-only"`.
- Grant key name is exactly `public_datasources`; migration is exactly `0055_datasources_read_only.sql`.
- UI display string for the `is_global` scope badge changes "Global" → "Public" (en) / "Global" → "Öffentlich" (de); i18n **keys** stay `scopeGlobal`.
- en.json and de-DE.json must stay key-parallel (`npm run i18n:check` enforces).
- Do NOT add `public_datasources` to `_ADMIN_GRANT_SEED` (postgres.py ~9479): admins short-circuit in the helper; a demoted admin must not silently keep publish rights.
- Do NOT touch `project_datasources.read_only` / `project_read_only` semantics (per-link connector mode — out of scope per spec).
- `testFromForm()` in the datasource modal keeps building its payload **without** `is_global`/`read_only` — connection tests must stay private and never hit the publish gate.

---

### Task 1: Grant catalog key `public_datasources`

**Files:**
- Modify: `src/core/capability_grants.py` (CATALOG dict, lines 18-44)
- Test: `tests/test_capability_grants.py` (hardcoded key set at lines 24-43)

**Interfaces:**
- Produces: `CATALOG["public_datasources"] == {"type": "bool", "default": False, "restrict_only": True}`. Tasks 3, 5, 6 rely on `resolve_grants(...)["public_datasources"]` resolving to `False` by default. The `/admin/grants` UI and `GET /api/users/me/capabilities` pick the key up automatically (catalog-driven) — no other code change.

- [ ] **Step 1: Extend the failing test**

In `tests/test_capability_grants.py`, `test_catalog_keys_and_defaults` currently asserts the exact 8-key set. Add the new key to the set literal and a default assertion:

```python
def test_catalog_keys_and_defaults():
    assert set(CATALOG) == {
        "vm_workspace",
        "shell_tools",
        "delegation",
        "datasource_tools",
        "browser",
        "model_selection",
        "autonomy_ceiling",
        "permission_mode",
        "public_datasources",
    }
    assert all(spec["restrict_only"] for spec in CATALOG.values())
    assert CATALOG["vm_workspace"]["default"] is False
    assert CATALOG["shell_tools"]["default"] is False  # deny-by-default
    assert CATALOG["delegation"]["default"] is False
    assert CATALOG["public_datasources"]["default"] is False  # deny-by-default
    assert CATALOG["browser"]["default"] is True  # spec-deferred allow
    assert CATALOG["datasource_tools"]["default"] is True
    assert CATALOG["model_selection"]["default"] is None
    assert CATALOG["autonomy_ceiling"]["default"] == "review"
    assert CATALOG["permission_mode"]["default"] == "auto_accept"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capability_grants.py::test_catalog_keys_and_defaults -q`
Expected: FAIL (set mismatch — `public_datasources` not in CATALOG).

- [ ] **Step 3: Add the catalog entry**

In `src/core/capability_grants.py`, after the `"delegation"` line (line 21):

```python
    "delegation": {"type": "bool", "default": False, "restrict_only": True},
    # Publish datasources org-wide (is_global). Deny-by-default: publishing
    # hands the publisher's stored credentials to every user's agents.
    # Spec: docs/features/public_datasources.md
    "public_datasources": {"type": "bool", "default": False, "restrict_only": True},
```

- [ ] **Step 4: Run the whole grants test file**

Run: `pytest tests/test_capability_grants.py -q`
Expected: all PASS (resolution/PDP tests iterate the catalog generically; a new bool key must not break them).

- [ ] **Step 5: Commit**

```bash
git add src/core/capability_grants.py tests/test_capability_grants.py
git commit -m "feat(grants): public_datasources capability key (deny-by-default)"
```

---

### Task 2: Migration 0055 — `datasources.read_only`

**Files:**
- Create: `orchestrator/database/migrations/app/0055_datasources_read_only.sql`
- Regenerate: `orchestrator/database/schema_current.sql` (+ sibling `*_current.sql` if the script rewrites them)

**Interfaces:**
- Produces: nullable `datasources.read_only BOOLEAN` column. NULL = not applicable (private/job-scoped). Tasks 4-6 read/write it.

- [ ] **Step 1: Write the migration**

Create `orchestrator/database/migrations/app/0055_datasources_read_only.sql` (header format copied from 0054; note this deliberately re-adds a column that `0001_initial.sql` dropped — the old one was advisory-and-unused, this one is the declarative publish flag per the spec):

```sql
-- migration:     0055_datasources_read_only.sql
-- description:   Declarative read-only flag for published (is_global) data-
--                sources. NULL = not applicable (private/job-scoped rows).
--                Publishing defaults it to TRUE; type='kb' is always TRUE.
--                Declarative only — credentials remain the enforcement
--                boundary (docs/features/public_datasources.md).
-- depends-on:    0054_datasource_config.sql
-- expected:      < 1s; brief table lock for ADD COLUMN (nullable, no default
--                value rewrite).
-- locks:         Brief ACCESS EXCLUSIVE on datasources.
-- transactional: yes
-- ============================================================================

ALTER TABLE datasources
    ADD COLUMN IF NOT EXISTS read_only BOOLEAN;

COMMENT ON COLUMN datasources.read_only IS
    'Declared read-only flag for public (is_global) datasources. NULL = not applicable. Declarative: credentials are the enforcement boundary; kb datasources are read-only by architecture.';
```

- [ ] **Step 2: Regenerate the schema artifacts (CI fails without this)**

Run: `scripts/schema-snapshot.sh` (podman fallback is built in).
Expected: rewrites `orchestrator/database/schema_current.sql` containing `read_only boolean` in the `datasources` table. Verify:

Run: `grep -n "read_only" orchestrator/database/schema_current.sql | head -5`
Expected: a new `read_only boolean` line under `CREATE TABLE public.datasources` (plus the existing `project_datasources.read_only`).

- [ ] **Step 3: Commit (migration + regenerated artifacts together — CI artifact job enforces same-commit)**

```bash
git add orchestrator/database/migrations/app/0055_datasources_read_only.sql orchestrator/database/*_current.sql
git commit -m "feat(db): 0055 datasources.read_only — declarative publish flag"
```

---

### Task 3: `user_can_publish_datasource` helper

**Files:**
- Modify: `orchestrator/database/postgres.py` (add method next to `user_can_use_vm`, ~line 9443)
- Test: `tests/test_public_datasources.py` (new file)

**Interfaces:**
- Consumes: `CATALOG["public_datasources"]` (Task 1), `self.list_grants_for_scopes` (existing).
- Produces: `async def user_can_publish_datasource(self, user: dict) -> bool` on `PostgresDB`. Tasks 5 and 6 call `postgres_db.user_can_publish_datasource(user)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_public_datasources.py`:

```python
"""Public datasources — grant-gated is_global publishing.

Spec: docs/features/public_datasources.md. Covers the PostgresDB helper here;
endpoint gates are covered in the classes added by later tasks.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from database.postgres import PostgresDB


def _patch_caller_and_db(user: dict, db):
    """Patch the caller (require_approved_user) and DB on the main module.

    Mirrors tests/test_datasource_access.py — kept local so this file stays
    self-contained.
    """
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch(
            "security.access.require_approved_user",
            AsyncMock(return_value=user),
        )
    )
    stack.enter_context(patch("main.postgres_db", db))
    return stack


EMPTY_SCOPES = {"user": [], "project": [], "global": []}


def _db_with_grant_rows(scoped):
    """PostgresDB with no pool — only list_grants_for_scopes is exercised."""
    db = PostgresDB.__new__(PostgresDB)
    db.list_grants_for_scopes = AsyncMock(return_value=scoped)
    return db


class TestUserCanPublishDatasource:
    async def test_admin_short_circuits_without_grant_read(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": True}
        ) is True
        db.list_grants_for_scopes.assert_not_awaited()

    async def test_no_rows_denies_by_default(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": False}
        ) is False

    async def test_user_scope_grant_allows(self):
        db = _db_with_grant_rows(
            {
                "user": [{"key": "public_datasources", "value_json": True}],
                "project": [],
                "global": [],
            }
        )
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": False}
        ) is True

    async def test_grant_read_failure_fails_closed(self):
        db = PostgresDB.__new__(PostgresDB)
        db.list_grants_for_scopes = AsyncMock(side_effect=RuntimeError("db down"))
        assert await db.user_can_publish_datasource(
            {"id": "u1", "is_admin": False}
        ) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_public_datasources.py -q`
Expected: FAIL with `AttributeError: 'PostgresDB' object has no attribute 'user_can_publish_datasource'`.

- [ ] **Step 3: Implement the helper**

In `orchestrator/database/postgres.py`, directly after the `user_can_use_vm` method body (~line 9471):

```python
    async def user_can_publish_datasource(self, user: dict) -> bool:
        """Effective public_datasources grant (publish is_global datasources).

        Admins short-circuit to True. Unlike user_can_use_vm there is no
        legacy-column fallback (new capability, deny-by-default) and the
        fail mode is CLOSED: publishing hands the publisher's credentials
        to every user's agents, so a grant-read failure must deny.
        Spec: docs/features/public_datasources.md.
        """
        if user.get("is_admin"):
            return True
        try:
            scoped = await self.list_grants_for_scopes(
                user_id=str(user["id"]), project_ids=[]
            )
            from src.core.capability_grants import resolve_grants

            g = resolve_grants(
                user_rows=scoped["user"],
                project_rows=scoped["project"],
                global_rows=scoped["global"],
            )
            return bool(g.get("public_datasources"))
        except Exception:
            logger.exception(
                "public_datasources grant read failed; denying publish"
            )
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_public_datasources.py -q`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/database/postgres.py tests/test_public_datasources.py
git commit -m "feat(grants): user_can_publish_datasource helper (admin short-circuit, fail-closed)"
```

---

### Task 4: `read_only` column plumbing through postgres.py

**Files:**
- Modify: `orchestrator/database/postgres.py` — six sites (line numbers pre-Task-3; the Task-3 insert is at ~9471 so all sites below are unshifted):
  1. `list_datasources` SELECT (~4284)
  2. `get_datasource` SELECT (~4314)
  3. `create_datasource` signature (~4324), INSERT+RETURNING (~4368-4388)
  4. `update_datasource` signature + dynamic SET builder (~4392-4473)
  5. `resolve_datasources_for_job` — both the main and the legacy-fallback SELECT (~4530-4560)
  6. `list_eligible_datasources` `select_cols` (~4716)
  7. `upsert_default_datasource` (~5106-5133) — seeded system defaults become `read_only=TRUE`

**Interfaces:**
- Consumes: column from Task 2.
- Produces: `create_datasource(..., is_global: bool = False, read_only: bool | None = None, ...)` and `update_datasource(..., is_global: bool | None = None, read_only: bool | None = None)`. Every datasource dict returned by get/list/eligible/create now carries `read_only`; dicts from `resolve_datasources_for_job` carry `read_only` (alongside the distinct, unchanged `project_read_only`). `_datasource_row_to_dict` passes new columns through untouched — no change needed there.

- [ ] **Step 1: SELECT lists — add the column**

In `list_datasources` (~4284) and `get_datasource` (~4314), change:

```
config, cli_hint, default_branch, job_id, created_by, is_global,
```
to
```
config, cli_hint, default_branch, job_id, created_by, is_global, read_only,
```

In `list_eligible_datasources` `select_cols` (~4716), change:

```
d.default_branch, d.created_by, d.is_global,
```
to
```
d.default_branch, d.created_by, d.is_global, d.read_only,
```

In `resolve_datasources_for_job`, in **both** queries (main ~4536 and legacy fallback ~4556), change:

```
d.cli_hint, d.default_branch,
d.created_at, d.updated_at,
```
to
```
d.cli_hint, d.default_branch, d.read_only,
d.created_at, d.updated_at,
```

- [ ] **Step 2: `create_datasource` — persist and return it**

Add the parameter after `is_global` in the signature:

```python
        is_global: bool = False,
        read_only: bool | None = None,
```

Extend the INSERT (~4368-4388): column list `..., created_by, is_global)` → `..., created_by, is_global, read_only)`; VALUES `..., $10, $11)` → `..., $10, $11, $12)`; RETURNING `..., created_by, is_global, created_at, updated_at` → `..., created_by, is_global, read_only, created_at, updated_at`; and append `read_only,` after the `is_global,` argument (~4387).

- [ ] **Step 3: `update_datasource` — dynamic SET entries**

Add to the signature after `config`:

```python
        config: Dict[str, Any] | None = None,
        is_global: bool | None = None,
        read_only: bool | None = None,
```

After the existing `config` block in the SET builder (~4455):

```python
        if is_global is not None:
            param_count += 1
            updates.append(f"is_global = ${param_count}")
            values.append(is_global)
        if read_only is not None:
            param_count += 1
            updates.append(f"read_only = ${param_count}")
            values.append(read_only)
```

(`is not None` keeps explicit `False` working — unpublish and RW-declare both pass `False`.)

- [ ] **Step 4: `upsert_default_datasource` — seeded defaults are declared read-only**

System-seeded datasources are already `is_global=TRUE`; keep the spec invariant (`is_global ⇒ read_only NOT NULL`) by forcing TRUE (~5124-5133): INSERT column list `..., created_by, is_global)` → `..., created_by, is_global, read_only)`, VALUES `..., NULL, TRUE)` → `..., NULL, TRUE, TRUE)`, add `read_only = TRUE` to the `DO UPDATE SET` list, and add `read_only` to the RETURNING list.

- [ ] **Step 5: Verify no explicit datasource column list was missed**

Run: `grep -n "FROM datasources\|INTO datasources" orchestrator/database/postgres.py`
Check each hit: any query with an explicit datasource column list that feeds `_datasource_row_to_dict` or an API response should now include `read_only`. (Queries selecting only ids/counts or the legacy pre-0026 clone path that selects specific non-visibility columns are fine as-is.)

- [ ] **Step 6: Run the existing datasource test files (regression)**

Run: `pytest tests/test_datasource_access.py tests/test_datasource_repo_clone.py tests/test_kb_datasource_api.py tests/test_public_datasources.py -q`
Expected: PASS (mock-driven — the new kwargs default to None and change no behavior yet).

- [ ] **Step 7: Commit**

```bash
git add orchestrator/database/postgres.py
git commit -m "feat(db): thread datasources.read_only through create/update/get/list/eligible/resolve"
```

---

### Task 5: Create-endpoint publish gate

**Files:**
- Modify: `orchestrator/main.py` — `DatasourceCreate` model (~5114-5143) and `create_datasource` endpoint (~15014-15093)
- Test: `tests/test_public_datasources.py` (extend)

**Interfaces:**
- Consumes: `postgres_db.user_can_publish_datasource(user)` (Task 3), `create_datasource(..., read_only=...)` (Task 4).
- Produces: `POST /api/datasources` returns 403 for ungranted `is_global=true`; `DatasourceCreate.read_only: bool | None`. The exact detail strings from Global Constraints.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_public_datasources.py` (uses conftest fixtures `user_a`, `user_admin`, `fake_db`, `fake_request`):

```python
def _created_row(**overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Org Wiki",
        "description": None,
        "type": "repository",
        "connection_url": "https://github.com/org/wiki",
        "credentials": {"auth_method": "token", "token": "secret"},
        "config": {},
        "job_id": None,
        "cli_hint": None,
        "default_branch": None,
        "created_by": "user-a",
        "is_global": True,
        "read_only": True,
        "created_at": "2026-07-11T00:00:00Z",
        "updated_at": "2026-07-11T00:00:00Z",
    }
    row.update(overrides)
    return row


class TestCreatePublishGate:
    async def test_publish_without_grant_403(self, user_a, fake_db, fake_request):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        fake_db.create_datasource = AsyncMock()
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await create_datasource(
                    DatasourceCreate(
                        name="Org Wiki",
                        type="repository",
                        connection_url="https://github.com/org/wiki",
                        is_global=True,
                    ),
                    fake_request,
                )
        assert exc.value.status_code == 403
        assert "public_datasources" in exc.value.detail
        fake_db.create_datasource.assert_not_awaited()

    async def test_publish_with_grant_defaults_read_only_true(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        fake_db.create_datasource = AsyncMock(return_value=_created_row())
        with _patch_caller_and_db(user_a, fake_db):
            result = await create_datasource(
                DatasourceCreate(
                    name="Org Wiki",
                    type="repository",
                    connection_url="https://github.com/org/wiki",
                    is_global=True,
                ),
                fake_request,
            )
        kwargs = fake_db.create_datasource.await_args.kwargs
        assert kwargs["is_global"] is True
        assert kwargs["read_only"] is True  # defaulted on publish
        assert "token" not in str(result.get("credentials"))  # still redacted

    async def test_publish_read_write_with_grant_keeps_false(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        fake_db.create_datasource = AsyncMock(
            return_value=_created_row(read_only=False)
        )
        with _patch_caller_and_db(user_a, fake_db):
            await create_datasource(
                DatasourceCreate(
                    name="Org Wiki",
                    type="repository",
                    connection_url="https://github.com/org/wiki",
                    is_global=True,
                    read_only=False,
                ),
                fake_request,
            )
        assert fake_db.create_datasource.await_args.kwargs["read_only"] is False

    async def test_private_create_never_calls_gate(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        fake_db.create_datasource = AsyncMock(
            return_value=_created_row(is_global=False, read_only=None)
        )
        with _patch_caller_and_db(user_a, fake_db):
            await create_datasource(
                DatasourceCreate(
                    name="Mine",
                    type="repository",
                    connection_url="https://github.com/me/mine",
                ),
                fake_request,
            )
        fake_db.user_can_publish_datasource.assert_not_awaited()
        assert fake_db.create_datasource.await_args.kwargs["read_only"] is None

    async def test_kb_read_write_flag_400(self, user_a, fake_db, fake_request):
        from main import DatasourceCreate, create_datasource

        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        fake_db.create_datasource = AsyncMock()
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await create_datasource(
                    DatasourceCreate(
                        name="Org KB",
                        type="kb",
                        connection_url="https://github.com/org/kb",
                        is_global=True,
                        read_only=False,
                    ),
                    fake_request,
                )
        assert exc.value.status_code == 400
        assert "read-only" in exc.value.detail
        fake_db.create_datasource.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_public_datasources.py -q`
Expected: the new `TestCreatePublishGate` tests FAIL (`DatasourceCreate` has no `read_only` field / gate absent); the Task 3 helper tests still PASS.

- [ ] **Step 3: Implement model + gate**

In `orchestrator/main.py`, `DatasourceCreate` — after the `is_global` field (~5141-5143):

```python
    is_global: bool = Field(
        False, description="Whether this datasource is visible to all users"
    )
    read_only: bool | None = Field(
        None,
        description=(
            "Declared read-only flag for public datasources (defaults to true "
            "on publish; kb is always read-only). Declarative — credentials "
            "are the enforcement boundary."
        ),
    )
```

In the `create_datasource` endpoint, directly after `user_id = str(user["id"])` (~15044):

```python
    # Publish gate — is_global hands the publisher's stored credentials to
    # every user's agents (docs/features/public_datasources.md).
    if body.is_global and not await postgres_db.user_can_publish_datasource(user):
        raise HTTPException(
            status_code=403,
            detail=(
                "Publishing public datasources requires the "
                "'public_datasources' capability"
            ),
        )
    read_only = body.read_only
    if body.type == "kb":
        if read_only is False:
            raise HTTPException(
                status_code=400,
                detail="Knowledge-base datasources are always read-only",
            )
        if body.is_global:
            read_only = True
    elif body.is_global and read_only is None:
        read_only = True  # invariant: public ⇒ read_only set (RO default)
```

And add `read_only=read_only,` to the `postgres_db.create_datasource(...)` call (after `is_global=body.is_global,` ~15078).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_public_datasources.py tests/test_datasource_access.py tests/test_kb_datasource_api.py -q`
Expected: PASS (including untouched kb/create regressions).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_public_datasources.py
git commit -m "feat(api): grant-gate is_global on datasource create; read_only defaulting + kb rule"
```

---

### Task 6: Update-endpoint publish/unpublish gate

**Files:**
- Modify: `orchestrator/main.py` — `DatasourceUpdate` model (~5146-5158) and `update_datasource` endpoint (~15096-15185)
- Test: `tests/test_public_datasources.py` (extend)

**Interfaces:**
- Consumes: Tasks 3-5.
- Produces: `DatasourceUpdate.is_global: bool | None`, `DatasourceUpdate.read_only: bool | None`. Publish (false→true) requires grant; unpublish requires only the existing creator/admin gate; RO→RW flip needs no grant re-check (client-side friction per spec); kb rejects `read_only=false`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_public_datasources.py`:

```python
def _existing_private(owner_id="user-a"):
    return _created_row(is_global=False, read_only=None, created_by=owner_id)


def _existing_public(owner_id="user-a", read_only=True):
    return _created_row(is_global=True, read_only=read_only, created_by=owner_id)


def _wire_owner_update(fake_db, user, existing):
    """require_datasource_owner resolves via get_datasource + creator check."""
    existing = {**existing, "created_by": str(user["id"])}
    fake_db.get_datasource = AsyncMock(return_value=existing)
    fake_db.update_datasource = AsyncMock(return_value=True)
    fake_db.list_datasource_projects = AsyncMock(return_value=[])
    return existing


class TestUpdatePublishGate:
    async def test_publish_flip_without_grant_403(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_private())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await update_datasource(
                    fake_request, existing["id"], DatasourceUpdate(is_global=True)
                )
        assert exc.value.status_code == 403
        fake_db.update_datasource.assert_not_awaited()

    async def test_publish_flip_with_grant_defaults_read_only(
        self, user_a, fake_db, fake_request
    ):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_private())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=True)
        with _patch_caller_and_db(user_a, fake_db):
            result = await update_datasource(
                fake_request, existing["id"], DatasourceUpdate(is_global=True)
            )
        assert result == {"status": "updated"}
        kwargs = fake_db.update_datasource.await_args.kwargs
        assert kwargs["is_global"] is True
        assert kwargs["read_only"] is True

    async def test_unpublish_needs_no_grant(self, user_a, fake_db, fake_request):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_public())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        with _patch_caller_and_db(user_a, fake_db):
            result = await update_datasource(
                fake_request, existing["id"], DatasourceUpdate(is_global=False)
            )
        assert result == {"status": "updated"}
        fake_db.user_can_publish_datasource.assert_not_awaited()
        assert fake_db.update_datasource.await_args.kwargs["is_global"] is False

    async def test_ro_to_rw_flip_needs_no_grant(
        self, user_a, fake_db, fake_request
    ):
        # Spec: friction for RO→RW is the client-side typed confirmation;
        # the server gate is only on the publish transition.
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(fake_db, user_a, _existing_public())
        fake_db.user_can_publish_datasource = AsyncMock(return_value=False)
        with _patch_caller_and_db(user_a, fake_db):
            result = await update_datasource(
                fake_request, existing["id"], DatasourceUpdate(read_only=False)
            )
        assert result == {"status": "updated"}
        fake_db.user_can_publish_datasource.assert_not_awaited()
        assert fake_db.update_datasource.await_args.kwargs["read_only"] is False

    async def test_kb_read_write_flag_400(self, user_a, fake_db, fake_request):
        from main import DatasourceUpdate, update_datasource

        existing = _wire_owner_update(
            fake_db, user_a, _created_row(type="kb", is_global=True)
        )
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await update_datasource(
                    fake_request, existing["id"], DatasourceUpdate(read_only=False)
                )
        assert exc.value.status_code == 400
        fake_db.update_datasource.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_public_datasources.py -q`
Expected: new `TestUpdatePublishGate` tests FAIL (`DatasourceUpdate` has no `is_global` field).

- [ ] **Step 3: Implement model + gate**

`DatasourceUpdate` — append after `config` (~5153-5158):

```python
    is_global: bool | None = Field(
        None,
        description=(
            "Publish (true) or unpublish (false). Publishing requires the "
            "'public_datasources' capability; unpublishing needs only "
            "creator/admin."
        ),
    )
    read_only: bool | None = Field(
        None,
        description="Declared read-only flag (kb: always true; declarative only)",
    )
```

In the `update_datasource` endpoint: change the first line `_, existing_ds = await require_datasource_owner(...)` to `user, existing_ds = await require_datasource_owner(...)`, then insert directly after it:

```python
    # Publish gate (spec: docs/features/public_datasources.md). Only the
    # false→true transition needs the capability; unpublishing must always
    # work for creator/admin (a revoked grant must not trap a public row).
    if body.is_global is True and not existing_ds.get("is_global"):
        if not await postgres_db.user_can_publish_datasource(user):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Publishing public datasources requires the "
                    "'public_datasources' capability"
                ),
            )
    read_only = body.read_only
    if existing_ds.get("type") == "kb" and read_only is False:
        raise HTTPException(
            status_code=400,
            detail="Knowledge-base datasources are always read-only",
        )
    effective_global = (
        body.is_global
        if body.is_global is not None
        else bool(existing_ds.get("is_global"))
    )
    if (
        effective_global
        and read_only is None
        and existing_ds.get("read_only") is None
    ):
        read_only = True  # invariant: public ⇒ read_only set
```

And extend the `postgres_db.update_datasource(...)` call (~15153-15163) with:

```python
            config=datasource_config,
            is_global=body.is_global,
            read_only=read_only,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_public_datasources.py tests/test_datasource_access.py -q`
Expected: PASS. Note `tests/test_datasource_access.py::TestUpdateDatasource` pins the pre-existing update behavior — it must stay green (the new kwargs are None-defaulted).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_public_datasources.py
git commit -m "feat(api): publish/unpublish gate on datasource update; kb RW rejected"
```

---

### Task 7: Agent-facing index note

**Files:**
- Modify: `src/core/datasource_setup.py` — `inject_datasource_index` (~826-950)
- Test: `tests/test_datasource_repo_clone.py` (has the `make_workspace_manager` harness)

**Interfaces:**
- Consumes: `read_only` on resolved dicts (Task 4, `resolve_datasources_for_job`).
- Produces: index entries for declared-RO datasources carry a `declared read-only` note. `project_read_only` handling is untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_datasource_repo_clone.py` (reusing `make_workspace_manager` and `token_ds` defined at the top of the file):

```python
class TestDeclaredReadOnlyIndexNote:
    """Public datasources declared read-only get an advisory index note
    (docs/features/public_datasources.md — declarative, not enforced)."""

    def test_declared_ro_repo_notes_in_index(self):
        ws = make_workspace_manager()
        written = {}
        ws.write_file.side_effect = lambda path, content: written.update(
            {path: content}
        )
        ds = token_ds(name="Org Wiki")
        ds["read_only"] = True

        inject_datasource_index([ds], ws)

        assert "declared read-only" in written["datasources.md"]

    def test_private_repo_has_no_ro_note(self):
        ws = make_workspace_manager()
        written = {}
        ws.write_file.side_effect = lambda path, content: written.update(
            {path: content}
        )

        inject_datasource_index([token_ds(name="Mine")], ws)

        assert "declared read-only" not in written["datasources.md"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_datasource_repo_clone.py -q -k DeclaredReadOnly`
Expected: FAIL (`assert "declared read-only" in ...`).

- [ ] **Step 3: Implement the note**

In `src/core/datasource_setup.py`, add a module-level helper above `inject_datasource_index`:

```python
def _declared_ro_note(ds: Dict[str, Any]) -> str:
    """Advisory suffix for publisher-declared read-only datasources.

    Declarative only (docs/features/public_datasources.md): credentials are
    the enforcement boundary; this just tells the agent the intent.
    """
    return " (declared read-only — treat as no-write)" if ds.get("read_only") else ""
```

Then append `{_declared_ro_note(ds)}` to the per-entry line in each category branch of `inject_datasource_index` where write access is otherwise implied: the **Repositories** entry line (~861-870), the **Databases** read-write CLI branch (the `else: lines.append(_format_rw_cli_entry(name, ds_type))` arm, ~884-895), the **webdav read-write** arm (~922-937), and the **generic/Other** entries. Do NOT touch the kb branch (already labeled read-only) or the `project_read_only` query-tools branches (already labeled).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_datasource_repo_clone.py tests/test_datasource_redesign.py -q`
Expected: PASS (including the existing index-format pins).

- [ ] **Step 5: Commit**

```bash
git add src/core/datasource_setup.py tests/test_datasource_repo_clone.py
git commit -m "feat(agent): declared read-only note in the datasource index"
```

---

### Task 8: Frontend models + `canPublishDatasources` capability

**Files:**
- Modify: `cockpit/src/app/core/models/api.model.ts` (`Datasource` ~267-290, `DatasourceCreateRequest` ~295-305, `DatasourceUpdateRequest` ~310-318)
- Modify: `cockpit/src/app/core/services/capabilities.service.ts`
- Test: `cockpit/src/app/core/services/capabilities.service.spec.ts` (new file)

**Interfaces:**
- Produces: `Datasource.read_only?: boolean | null`; `is_global?: boolean; read_only?: boolean` on both request interfaces; `CapabilitiesService.canPublishDatasources: Signal<boolean>` (computed — `true` for admins (`grants === null`) and granted users; `false` while loading (`undefined`) and when ungranted). Tasks 10-11 consume all of these.

- [ ] **Step 1: Write the failing service test**

Create `cockpit/src/app/core/services/capabilities.service.spec.ts` (matching the repo's `Injector.create` + `runInInjectionContext` harness):

```ts
import {Injector, runInInjectionContext} from '@angular/core';
import {NEVER, of} from 'rxjs';
import {describe, expect, it} from 'vitest';

import {ApiService} from './api.service';
import {CapabilitiesService} from './capabilities.service';

function createService(response$: unknown) {
  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: {getMyCapabilities: () => response$}},
    ],
  });
  return runInInjectionContext(injector, () => new CapabilitiesService());
}

describe('CapabilitiesService.canPublishDatasources', () => {
  it('is true for admins (grants === null)', () => {
    const svc = createService(of({is_admin: true, grants: null, catalog: {}}));
    expect(svc.canPublishDatasources()).toBe(true);
  });

  it('is true when the grant resolves true', () => {
    const svc = createService(
      of({is_admin: false, grants: {public_datasources: true}, catalog: {}}),
    );
    expect(svc.canPublishDatasources()).toBe(true);
  });

  it('is false when the grant is absent (deny-by-default)', () => {
    const svc = createService(of({is_admin: false, grants: {}, catalog: {}}));
    expect(svc.canPublishDatasources()).toBe(false);
  });

  it('fails closed while loading', () => {
    const svc = createService(NEVER);
    expect(svc.canPublishDatasources()).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `cockpit/`): `npx vitest run src/app/core/services/capabilities.service.spec.ts`
Expected: FAIL (`canPublishDatasources` is not a function).

Note: if the `CapabilitiesService` constructor signature differs from what `createService` assumes (e.g. it needs another provider), add that provider to the test injector rather than changing the service.

- [ ] **Step 3: Implement**

`capabilities.service.ts` — first check the `load()` **error** handler: the service's design note says it "fails open on error". If the error path sets `grants` to `null` (indistinguishable from admin), the publish section would fail OPEN on a fetch error — spec §6 requires this section to fail CLOSED. In that case leave the error path setting `grants` to `undefined` for this computed's purposes (or add a dedicated `loadFailed` signal and include it in the computed). Then add a computed next to the existing ones:

```ts
  /** Whether the Visibility (publish) section may render. Unlike the
   * permission-mode helpers this FAILS CLOSED while loading — the section
   * is hidden until capabilities arrive (the server is the real gate). */
  readonly canPublishDatasources = computed(() => {
    const g = this.grants();
    if (g === null) return true; // admin/unrestricted
    if (g === undefined) return false; // loading
    return g['public_datasources'] === true;
  });
```

`api.model.ts` — `Datasource`: after `is_global?: boolean;` add:

```ts
  /** Declared read-only flag for public datasources (null = not applicable).
   *  Declarative — credentials are the enforcement boundary. */
  read_only?: boolean | null;
```

`DatasourceCreateRequest` and `DatasourceUpdateRequest`: add to both:

```ts
  is_global?: boolean;
  read_only?: boolean;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx vitest run src/app/core/services/capabilities.service.spec.ts`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/core/models/api.model.ts cockpit/src/app/core/services/capabilities.service.ts cockpit/src/app/core/services/capabilities.service.spec.ts
git commit -m "feat(cockpit): datasource visibility fields + canPublishDatasources capability"
```

---

### Task 9: Reusable confirm-name dialog component

**Files:**
- Create: `cockpit/src/app/ui/confirm-name-dialog/confirm-name-dialog.component.ts`
- Create: `cockpit/src/app/ui/confirm-name-dialog/index.ts`

**Interfaces:**
- Consumes: `AppDialogComponent` (`../dialog`), `AppButtonComponent` (`../button`), `AppInputComponent` (`../input`) — check the exact barrel paths of button/input by looking at the imports at the top of `datasource-list.component.ts` and mirror them.
- Produces: `<app-confirm-name-dialog [open]="..." [title]="..." [message]="..." [requiredName]="..." [confirmLabel]="..." [cancelLabel]="..." [namePrompt]="..." (confirmed)="..." (dismissed)="...">`. When `requiredName` is null it is a plain warning-confirm; when set, the confirm button stays disabled until the typed value matches exactly (case-sensitive). Task 10 consumes it. Coverage: template typecheck via `npm run build` + behavior via Task 10's tier tests (the repo has no fixture-based component test harness — do not introduce one).

- [ ] **Step 1: Implement the component**

`confirm-name-dialog.component.ts`:

```ts
import {ChangeDetectionStrategy, Component, computed, input, model, output, signal} from '@angular/core';

import {AppButtonComponent} from '../button';
import {AppDialogComponent} from '../dialog';
import {AppInputComponent} from '../input';

/** Warning-confirm dialog with an optional type-the-name friction gate.
 *  requiredName == null → plain confirm; requiredName set → the confirm
 *  button enables only on an exact (case-sensitive) match. Built for the
 *  datasource publish flow (docs/features/public_datasources.md) but
 *  deliberately generic (reusable for e.g. delete confirmation later). */
@Component({
  selector: 'app-confirm-name-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AppButtonComponent, AppDialogComponent, AppInputComponent],
  template: `
    <app-dialog
      [open]="open()"
      size="sm"
      [title]="title()"
      (closed)="dismiss()"
    >
      <p class="confirm-message">{{ message() }}</p>
      @if (requiredName(); as name) {
        <p class="confirm-name-prompt">{{ namePrompt() }}</p>
        <app-input
          size="sm"
          class="mono"
          [value]="typed()"
          (valueChange)="typed.set($event)"
          [placeholder]="name"
        />
      }
      <ng-container appDialogActions>
        <app-button variant="secondary" size="sm" (clicked)="dismiss()">
          {{ cancelLabel() }}
        </app-button>
        <app-button
          variant="primary"
          size="sm"
          [disabled]="!canConfirm()"
          (clicked)="confirm()"
        >
          {{ confirmLabel() }}
        </app-button>
      </ng-container>
    </app-dialog>
  `,
  styles: `
    .confirm-message {
      margin: 0 0 0.75rem;
      line-height: 1.5;
    }
    .confirm-name-prompt {
      margin: 0 0 0.5rem;
      font-size: 0.85em;
      opacity: 0.8;
    }
  `,
})
export class AppConfirmNameDialogComponent {
  readonly open = model<boolean>(false);
  readonly title = input<string>('');
  readonly message = input<string>('');
  /** Exact string the user must type; null disables the input gate. */
  readonly requiredName = input<string | null>(null);
  readonly namePrompt = input<string>('');
  readonly confirmLabel = input<string>('');
  readonly cancelLabel = input<string>('');
  readonly confirmed = output<void>();
  readonly dismissed = output<void>();

  readonly typed = signal('');
  readonly canConfirm = computed(() => {
    const required = this.requiredName();
    return required === null || this.typed() === required;
  });

  confirm(): void {
    if (!this.canConfirm()) return;
    this.open.set(false);
    this.typed.set('');
    this.confirmed.emit();
  }

  dismiss(): void {
    this.open.set(false);
    this.typed.set('');
    this.dismissed.emit();
  }
}
```

If `AppInputComponent`/`AppButtonComponent` barrel names differ (check `datasource-list.component.ts` imports), adjust the two import lines — do not guess new components.

`index.ts`:

```ts
export {AppConfirmNameDialogComponent} from './confirm-name-dialog.component';
```

- [ ] **Step 2: Verify it compiles**

Run (from `cockpit/`): `npm run build`
Expected: build succeeds (component is not referenced yet — this validates the template typechecks).

- [ ] **Step 3: Commit**

```bash
git add cockpit/src/app/ui/confirm-name-dialog/
git commit -m "feat(cockpit): reusable confirm-name dialog (typed-name friction gate)"
```

---

### Task 10: Modal Visibility section + confirmation tiers

**Files:**
- Modify: `cockpit/src/app/views/datasources/datasource-list.component.ts`
- Test: `cockpit/src/app/views/datasources/datasource-list.component.spec.ts`

**Interfaces:**
- Consumes: `canPublishDatasources` (Task 8), `AppConfirmNameDialogComponent` (Task 9), request-interface fields (Task 8).
- Produces: `publishConfirmTier(): 'name' | 'warn' | null` (unit-tested); create/update payloads carrying `is_global` + `read_only`; `editingOriginal: Datasource | null`.

- [ ] **Step 1: Write the failing tests**

Extend `datasource-list.component.spec.ts`. First, the existing `createComponent()` harness must provide the two newly injected services — add to its `providers` array:

```ts
      {provide: CapabilitiesService, useValue: {canPublishDatasources: () => true}},
```

(import `CapabilitiesService` from `'../../core/services/capabilities.service'`). Then append:

```ts
describe('DatasourceListComponent publish confirmation tiers', () => {
  it('needs no confirmation for a private save', () => {
    const {component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Mine';
    expect(component.publishConfirmTier()).toBeNull();
  });

  it('warns on a read-only publish (create)', () => {
    const {component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.is_global = true;
    expect(component.publishConfirmTier()).toBe('warn');
  });

  it('requires the typed name on a read-write publish (create)', () => {
    const {component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.is_global = true;
    component.formData.read_only = false;
    expect(component.publishConfirmTier()).toBe('name');
  });

  it('requires the typed name on a public RO→RW flip (edit)', () => {
    const {component, ds} = createComponent();
    component.openEditForm({...ds, is_global: true, read_only: true});
    component.formData.read_only = false;
    expect(component.publishConfirmTier()).toBe('name');
  });

  it('needs no confirmation for unpublish or RW→RO', () => {
    const {component, ds} = createComponent();
    component.openEditForm({...ds, is_global: true, read_only: false});
    component.formData.is_global = false;
    expect(component.publishConfirmTier()).toBeNull();

    component.openEditForm({...ds, is_global: true, read_only: false});
    component.formData.read_only = true;
    expect(component.publishConfirmTier()).toBeNull();
  });

  it('stays silent when an already-public RW datasource is edited unchanged', () => {
    const {component, ds} = createComponent();
    component.openEditForm({...ds, is_global: true, read_only: false});
    expect(component.publishConfirmTier()).toBeNull();
  });

  it('sends is_global and read_only in the create payload', () => {
    const {api, component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.type = 'repository';
    component.formData.is_global = true;
    component.doSave();
    const payload = api.createDatasource.mock.calls[0][0];
    expect(payload.is_global).toBe(true);
    expect(payload.read_only).toBe(true);
  });

  it('forces read_only=true for kb in the payload', () => {
    const {api, component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org KB';
    component.formData.type = 'kb';
    component.formData.is_global = true;
    component.formData.read_only = false; // UI forbids this; belt-and-braces
    component.doSave();
    expect(api.createDatasource.mock.calls[0][0].read_only).toBe(true);
  });

  it('openEditForm seeds visibility from the datasource', () => {
    const {component, ds} = createComponent();
    component.openEditForm({...ds, is_global: true, read_only: false});
    expect(component.formData.is_global).toBe(true);
    expect(component.formData.read_only).toBe(false);
  });
});
```

Check what the existing harness's `api` mock provides: `createDatasource` must be `vi.fn().mockReturnValue(of({...}))` — add it (and `updateDatasource` returning `of(true)`) to the mock if missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx vitest run src/app/views/datasources/datasource-list.component.spec.ts`
Expected: new tests FAIL (`publishConfirmTier`/`doSave` not defined, `formData.is_global` missing); pre-existing tests still PASS after the provider addition.

- [ ] **Step 3: Implement component state + logic**

In `datasource-list.component.ts`:

a) Inject the capability service and import the dialog (top of class, alongside existing `inject` calls at ~1433):

```ts
  protected readonly capabilities = inject(CapabilitiesService);
```

Add `AppConfirmNameDialogComponent` to the `imports` array and both import statements:

```ts
import {AppConfirmNameDialogComponent} from '../../ui/confirm-name-dialog';
import {CapabilitiesService} from '../../core/services/capabilities.service';
```

b) `formData` type + initializer (~1552-1568): add `is_global: boolean;` / `read_only: boolean;` to the type and `is_global: false, read_only: true,` to the initializer. Mirror in `resetFormData()` (~2188).

c) Edit-mode original + dialog state (near `showForm`/`editingId`, ~1442):

```ts
  /** Original datasource in edit mode — publishConfirmTier compares against it. */
  private editingOriginal: Datasource | null = null;
  readonly showPublishConfirm = signal(false);
  readonly publishConfirmName = signal<string | null>(null);
```

d) `openEditForm(ds)`: add `is_global: ds.is_global ?? false, read_only: ds.read_only ?? true,` to the seeded object and `this.editingOriginal = ds;` before `this.editingId.set(ds.id)`. `openCreateForm()` and `closeForm()`: add `this.editingOriginal = null;` and `this.showPublishConfirm.set(false);`.

e) Type-change coupling: `kb` must stay RO. In the existing type-change handler (wherever `formData.type` is set from the type select), append `if (this.formData.type === 'kb') this.formData.read_only = true;` — locate the `(valueChange)` binding of the type select in the template and its handler.

f) Tier logic + save split. Rename the current `saveForm()` body (everything after the early-return guards) into a new `doSave(): void`, then:

```ts
  /** Which confirmation the pending save needs.
   *  'name' — read-write publish or RO→RW flip (typed-name gate)
   *  'warn' — newly public, read-only (plain warning)
   *  null   — private saves, unpublish, RW→RO (exposure-reducing) */
  publishConfirmTier(): 'name' | 'warn' | null {
    if (!this.formData.is_global) return null;
    const prev = this.editingId() ? this.editingOriginal : null;
    const wasPublic = prev?.is_global === true;
    const wasRw = wasPublic && prev?.read_only === false;
    const isRw = !this.formData.read_only && this.formData.type !== 'kb';
    if (isRw && !wasRw) return 'name';
    if (!wasPublic) return 'warn';
    return null;
  }

  saveForm(): void {
    if (!this.formData.name) return;
    const tier = this.publishConfirmTier();
    if (tier) {
      this.publishConfirmName.set(tier === 'name' ? this.formData.name : null);
      this.showPublishConfirm.set(true);
      return;
    }
    this.doSave();
  }

  onPublishConfirmed(): void {
    this.showPublishConfirm.set(false);
    this.doSave();
  }
```

Keep the existing repo-without-credentials `confirm()` guard at the top of `doSave()` (it was part of the original body).

g) Error surfacing: the update path's error handler (~1850-1853) currently shows only the generic `updateError` message. Mirror the create path so the publish-denied 403 detail reaches the user:

```ts
        error: (err) => {
          this.isSaving.set(false);
          const detail = err?.error?.detail;
          this.errorMessage.set(
            detail || this.transloco.translate('datasources.messages.updateError'),
          );
        },
```

h) Payloads: in `doSave()`, extend the update payload (~1827-1837) and the create payload (~1856-1867) — but NOT `testFromForm()` — with:

```ts
        is_global: this.formData.is_global,
        read_only: this.formData.is_global
          ? (this.formData.type === 'kb' ? true : this.formData.read_only)
          : undefined,
```

i) Template — Visibility section between the last type-specific field block (after ~line 590) and the footer `form-row form-footer`:

```html
            <!-- Visibility (publish) — rendered only with the public_datasources
                 capability; the server enforces regardless. -->
            @if (capabilities.canPublishDatasources()) {
              <div class="form-row">
                <app-form-field
                  [label]="'datasources.form.visibilityLabel' | transloco"
                  [hint]="formData.is_global
                    ? ((formData.type === 'kb'
                        ? 'datasources.form.visibilityKbHint'
                        : 'datasources.form.visibilityCredentialHint') | transloco)
                    : ''"
                >
                  <div class="visibility-controls">
                    <label class="visibility-toggle">
                      <input
                        type="checkbox"
                        [checked]="formData.is_global"
                        (change)="formData.is_global = $any($event.target).checked"
                        [disabled]="isSaving()"
                      >
                      {{ 'datasources.form.visibilityPublic' | transloco }}
                    </label>
                    @if (formData.is_global) {
                      <div class="access-radio">
                        <label>
                          <input type="radio" name="ds-access"
                            [checked]="formData.read_only"
                            (change)="formData.read_only = true"
                            [disabled]="isSaving()">
                          {{ 'datasources.form.accessReadOnly' | transloco }}
                        </label>
                        <label>
                          <input type="radio" name="ds-access"
                            [checked]="!formData.read_only"
                            (change)="formData.read_only = false"
                            [disabled]="isSaving() || formData.type === 'kb'">
                          {{ 'datasources.form.accessReadWrite' | transloco }}
                        </label>
                      </div>
                    }
                  </div>
                </app-form-field>
              </div>
            }
```

Add minimal styles beside the existing form styles:

```scss
      .visibility-controls { display: flex; flex-direction: column; gap: 0.5rem; }
      .visibility-toggle, .access-radio label {
        display: inline-flex; align-items: center; gap: 0.5rem; cursor: pointer;
      }
      .access-radio { display: flex; gap: 1.25rem; }
```

j) Template — the dialog, next to the existing SSH public-key `app-dialog` (~line 835):

```html
    <app-confirm-name-dialog
      [open]="showPublishConfirm()"
      [title]="'datasources.publishDialog.title' | transloco"
      [message]="(publishConfirmName() !== null
        ? 'datasources.publishDialog.rwMessage'
        : 'datasources.publishDialog.message') | transloco"
      [requiredName]="publishConfirmName()"
      [namePrompt]="'datasources.publishDialog.namePrompt' | transloco"
      [confirmLabel]="'datasources.publishDialog.confirm' | transloco"
      [cancelLabel]="'datasources.publishDialog.cancel' | transloco"
      (confirmed)="onPublishConfirmed()"
      (dismissed)="showPublishConfirm.set(false)"
    />
```

- [ ] **Step 4: Run tests + build**

Run: `npx vitest run src/app/views/datasources/datasource-list.component.spec.ts && npm run build`
Expected: all PASS; build succeeds. (i18n keys are added in Task 11 — the transloco mock returns keys, so specs don't depend on them; the build doesn't either.)

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/views/datasources/datasource-list.component.ts cockpit/src/app/views/datasources/datasource-list.component.spec.ts
git commit -m "feat(cockpit): visibility section + two-tier publish confirmation in datasource modal"
```

---

### Task 11: Badges, picker chips, i18n

**Files:**
- Modify: `cockpit/src/app/views/datasources/datasource-list.component.ts` (scope cell, ~715-726)
- Modify: `cockpit/src/app/views/agent-settings/datasources-group.component.ts` (picker rows, ~83-108)
- Modify: `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`

**Interfaces:**
- Consumes: `Datasource.read_only` (Task 8).
- Produces: "Public" scope label; warning-tone RW chip in list + picker; subtle RO chip in picker.

- [ ] **Step 1: List badges**

In `datasource-list.component.ts`, add after the scope badge in BOTH the mobile inline block (~715-719) and the desktop scope cell (~722-726):

```html
                    @if (ds.is_global && ds.read_only === false) {
                      <app-badge tone="warning" size="xs">
                        {{ 'datasources.table.badgeRw' | transloco }}
                      </app-badge>
                    }
```

- [ ] **Step 2: Picker chips**

In `datasources-group.component.ts`, after the existing `ds-type-badge` span (~104-106) add (hand-rolled span, matching the file's local-badge style):

```html
              @if (ds.is_global) {
                <span class="ds-type-badge" [class.ds-rw-badge]="ds.read_only === false">
                  {{ (ds.read_only === false
                    ? 'datasources.table.badgeRw'
                    : 'datasources.table.badgeRo') | transloco }}
                </span>
              }
```

And in its styles near the existing `.ds-type-badge` rules (~220-230):

```scss
    .ds-rw-badge {
      color: var(--color-warning, #e0a030);
      border-color: currentColor;
    }
```

(Check how `.ds-type-badge` gets its colors and mirror the mechanism; use the repo's warning color token — grep `--color-warning` or the `app-badge` warning tone variable and reuse it.)

- [ ] **Step 3: i18n — en.json**

In the `datasources.table` block (~1490): change `"scopeGlobal": "Global"` → `"scopeGlobal": "Public"`, and add:

```json
      "badgeRo": "RO",
      "badgeRw": "RW",
```

In the `datasources.form` block (before its closing brace, ~1480):

```json
      "visibilityLabel": "Visibility",
      "visibilityPublic": "Public — visible to all users",
      "accessReadOnly": "Read-only",
      "accessReadWrite": "Read-write",
      "visibilityCredentialHint": "Read-only is declared, not enforced — use read-only credentials (deploy token / restricted DB account).",
      "visibilityKbHint": "Knowledge-base datasources are always read-only; credentials never reach agents.",
```

New top-level sibling block `datasources.publishDialog` (next to `sshKeyDialog`):

```json
    "publishDialog": {
      "title": "Publish datasource?",
      "message": "This datasource becomes visible to all users. Their agents will run with its stored credentials.",
      "rwMessage": "This datasource becomes visible to all users WITH WRITE ACCESS — their agents can modify the underlying system using its stored credentials.",
      "namePrompt": "Type the datasource name to confirm:",
      "confirm": "Publish",
      "cancel": "Cancel"
    },
```

- [ ] **Step 4: i18n — de-DE.json (same keys, same positions)**

`"scopeGlobal": "Global"` → `"scopeGlobal": "Öffentlich"`, plus:

```json
      "badgeRo": "RO",
      "badgeRw": "RW",
```
```json
      "visibilityLabel": "Sichtbarkeit",
      "visibilityPublic": "Öffentlich — für alle Benutzer sichtbar",
      "accessReadOnly": "Nur Lesen",
      "accessReadWrite": "Lesen & Schreiben",
      "visibilityCredentialHint": "Nur-Lesen ist deklariert, nicht erzwungen — verwenden Sie Nur-Lese-Zugangsdaten (Deploy-Token / eingeschränktes DB-Konto).",
      "visibilityKbHint": "Wissensdatenbank-Datenquellen sind immer schreibgeschützt; Zugangsdaten erreichen die Agenten nie.",
```
```json
    "publishDialog": {
      "title": "Datenquelle veröffentlichen?",
      "message": "Diese Datenquelle wird für alle Benutzer sichtbar. Deren Agenten arbeiten mit den hinterlegten Zugangsdaten.",
      "rwMessage": "Diese Datenquelle wird für alle Benutzer MIT SCHREIBZUGRIFF sichtbar — deren Agenten können das dahinterliegende System mit den hinterlegten Zugangsdaten verändern.",
      "namePrompt": "Geben Sie zur Bestätigung den Namen der Datenquelle ein:",
      "confirm": "Veröffentlichen",
      "cancel": "Abbrechen"
    },
```

- [ ] **Step 5: Verify**

Run (from `cockpit/`): `npm run i18n:check && npx vitest run && npm run build`
Expected: parity clean, all specs PASS, build succeeds.

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/views/datasources/datasource-list.component.ts cockpit/src/app/views/agent-settings/datasources-group.component.ts cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
git commit -m "feat(cockpit): Public scope label + RO/RW badges in list and picker"
```

---

### Task 12: Full verification + spec status flip

**Files:**
- Modify: `docs/features/public_datasources.md` (status line)

- [ ] **Step 1: Backend suite (targeted — local env may lack optional deps; CI runs the full suite)**

Run: `pytest tests/test_capability_grants.py tests/test_public_datasources.py tests/test_datasource_access.py tests/test_datasource_repo_clone.py tests/test_datasource_redesign.py tests/test_kb_datasource_api.py -q`
Expected: all PASS.

- [ ] **Step 2: Frontend suite + build + parity**

Run (from `cockpit/`): `npx vitest run && npm run build && npm run i18n:check`
Expected: all PASS, build clean, parity clean.

- [ ] **Step 3: Flip the spec status**

In `docs/features/public_datasources.md` change the status line to:

```markdown
Status: **IMPLEMENTED on develop 2026-07-11 — pending dev-deploy verification**
```

- [ ] **Step 4: Final commit**

```bash
git add docs/features/public_datasources.md
git commit -m "docs(datasources): public-datasources spec → implemented on develop"
```

- [ ] **Step 5: Report**

Summarize: what shipped, test counts, and the two follow-ups that need a deployed environment — (a) e2e smoke on dev (grant a test user `public_datasources` via /admin/grants, publish a datasource, verify it appears in another user's picker with the right chips), (b) verify migration 0055 applies cleanly on dev deploy. Do NOT push — the user pushes.
