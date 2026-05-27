# DB-backed Prompt Overrides — v1 Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins override the bundled `config/` prompts from the database (no redeploy), with overrides resolved beneath the existing matrix resolver so they freeze into each job's `resolved_config` automatically.

**Architecture:** A single `prompt_overrides` table keyed `(family, kind, name)`. The agent reads the relevant rows **directly via its existing Postgres connection** at job first-run and loads them into a process-local map in `loader.py`; the resolver's `MatrixResolver.load()` consults that map (sync) before reading the bundled file, behind `PROMPT_DB_OVERRIDES_ENABLED`. Because `serialize_resolved_config` calls the same resolver, overrides are captured in the per-job freeze with no extra work — so reproducibility needs no versioning. The orchestrator exposes admin CRUD (`/api/admin/prompts/*`, gated by `_require_admin`) over its own `PostgresDB`.

**Tech Stack:** Python, asyncpg (`PostgresDB` in both `orchestrator/database/postgres.py` and `src/database/postgres_db.py`), FastAPI (`orchestrator/main.py`), pytest. SQL migrations under `orchestrator/database/migrations/app/` applied by `orchestrator/database/migrate.py`.

> **Commit policy:** This plan includes `git commit` steps for the TDD rhythm, but per the user's standing instruction *commits happen only on explicit approval*. During execution, stage at each commit point and ask before committing (or batch). Current branch is `develop` (not the default `main`), so no new branch is required.

> **Local test caveat:** the pure `loader.py` override tests (Task 3) run cleanly locally. Tests that import `orchestrator/main.py` (Task 7) may hit the known local env gaps (Py3.14 / missing optional deps); CI (Py3.12) is the gate for those.

> **Scope deviations from the spec** (`docs/features/prompt_editing_page.md`), made for code-truth — reconcile the spec text to match:
> 1. `kind` uses the resolver's `MATRIX_SUBSECTION` values — **`'prompts'` / `'instructions'`** — not the doc's finer 6-value enum. Finer categorization (auxiliary vs. prompt) lives in the static catalog, not the DB key.
> 2. The agent reads overrides **directly from Postgres** (it already holds a connection for the freeze) — **no `/api/internal/prompts/*` endpoint**.

---

## File Structure

**Slice 1 — storage + agent read path (overrides freeze into new jobs):**
- Create: `orchestrator/database/migrations/app/0021_prompt_overrides.sql` — the table.
- Modify: `src/core/loader.py` — flag helper, process-local override map, `_db_lookup`, and the `MatrixResolver.load()` hook (loader.py:669).
- Modify: `src/database/postgres_db.py` — `PromptsNamespace` + wire `self.prompts` in `PostgresDB.__init__` (line 107).
- Modify: `src/agent.py:1051` — load overrides into the map before `serialize_resolved_config`.
- Test: `tests/test_prompt_overrides_loader.py`.

**Slice 2 — admin CRUD API + catalog:**
- Modify: `orchestrator/database/postgres.py` — `list/get/upsert/delete_prompt_override` methods on `PostgresDB`.
- Modify: `orchestrator/main.py` — `PromptOverrideCreate`/`PromptOverrideUpdate` models, `/api/admin/prompts/*` routes, bundled + catalog helpers.
- Create: `config/prompts/catalog.yaml` — `(kind, name) → {title, description}` for the editable prompt keys.
- Test: `tests/test_admin_prompts_api.py`.

---

## Slice 1 — Storage + Agent Read Path

### Task 1: Migration — `prompt_overrides` table

**Files:**
- Create: `orchestrator/database/migrations/app/0021_prompt_overrides.sql`

- [ ] **Step 1: Write the migration**

```sql
-- migration:     0021_prompt_overrides.sql
-- description:   DB-backed prompt overrides. Bundled config/ files remain the
--                immutable floor; a row here overrides resolution for one
--                (family, kind, name). NULL family = global default.
-- depends-on:    0020_thread_messages_window_index.notx.sql
-- expected:      < 100ms
-- locks:         none (new table)
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS prompt_overrides (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    family         VARCHAR(64),            -- NULL = global default (all families)
    kind           VARCHAR(32) NOT NULL
                     CHECK (kind IN ('prompts', 'instructions')),  -- MatrixResolver.MATRIX_SUBSECTION
    name           VARCHAR(128) NOT NULL,  -- resolver entry_type, e.g. 'persona', 'systemprompt'

    content        TEXT NOT NULL,
    content_format VARCHAR(16) NOT NULL DEFAULT 'text'
                     CHECK (content_format IN ('text', 'markdown', 'jinja', 'yaml')),
    notes          TEXT,

    created_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- (family, kind, name) unique; COALESCE folds NULL family into one global slot
-- (Postgres treats NULLs as distinct otherwise). Also the ON CONFLICT target.
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_override
    ON prompt_overrides (COALESCE(family, ''), kind, name);
CREATE INDEX IF NOT EXISTS idx_prompt_override_lookup
    ON prompt_overrides (family, kind, name);

COMMIT;
```

- [ ] **Step 2: Apply and verify**

Run (against the dev/test app DB the orchestrator uses):
```bash
python -m orchestrator.database.migrate
```
Expected: `0021_prompt_overrides.sql` is discovered and recorded in `schema_migrations`. Verify:
```bash
psql "$DATABASE_URL" -c "\d prompt_overrides"
```
Expected: table exists with the columns above and both indexes.

- [ ] **Step 3: Commit** (on approval)

```bash
git add orchestrator/database/migrations/app/0021_prompt_overrides.sql
git commit -m "feat(prompts): add prompt_overrides table (migration 0021)"
```

---

### Task 2: Loader — flag, override map, `_db_lookup`

**Files:**
- Modify: `src/core/loader.py` (add a block near the other module-level helpers; ensure `os` and `Optional` are imported — they are used elsewhere in this file)
- Test: `tests/test_prompt_overrides_loader.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompt_overrides_loader.py
import importlib
import src.core.loader as loader


def _reset():
    loader.clear_prompt_overrides()


def test_db_lookup_returns_none_when_flag_off(monkeypatch):
    _reset()
    monkeypatch.delenv("PROMPT_DB_OVERRIDES_ENABLED", raising=False)
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "prompts", "name": "persona", "content": "X"},
    ])
    assert loader._db_lookup("prompts", "gemma", "persona") is None


def test_db_lookup_family_specific_hit(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "prompts", "name": "persona", "content": "GEMMA"},
    ])
    assert loader._db_lookup("prompts", "gemma", "persona") == "GEMMA"
    assert loader._db_lookup("prompts", "gpt_5", "persona") is None  # other family: miss


def test_db_lookup_global_fallback_and_precedence(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "1")
    loader.set_prompt_overrides([
        {"family": None, "kind": "prompts", "name": "persona", "content": "GLOBAL"},
        {"family": "gemma", "kind": "prompts", "name": "persona", "content": "GEMMA"},
    ])
    assert loader._db_lookup("prompts", "gpt_5", "persona") == "GLOBAL"   # falls back to global
    assert loader._db_lookup("prompts", "gemma", "persona") == "GEMMA"    # family beats global


def test_clear_overrides(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "prompts", "name": "persona", "content": "X"},
    ])
    loader.clear_prompt_overrides()
    assert loader._db_lookup("prompts", "gemma", "persona") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: FAIL — `AttributeError: module 'src.core.loader' has no attribute 'set_prompt_overrides'`.

- [ ] **Step 3: Implement in `src/core/loader.py`**

Add near the top-level helpers (after imports):

```python
# --- DB-backed prompt overrides (PROMPT_DB_OVERRIDES_ENABLED) ----------------
# Populated once per job by the agent at first run (before serialize_resolved_config),
# then read synchronously by the resolver. Map: family -> {(kind, name): content};
# global (NULL-family) overrides live under the "" key. One job per agent process
# at a time, so a plain module-level map is safe.
_PROMPT_OVERRIDES: dict[str, dict[tuple[str, str], str]] = {}


def _is_prompt_db_overrides_enabled() -> bool:
    return os.getenv("PROMPT_DB_OVERRIDES_ENABLED", "").lower().strip() in ("true", "1", "yes")


def set_prompt_overrides(rows: list[dict]) -> None:
    """Load override rows into the process map (replaces any previous set).

    Each row needs keys: family (str|None), kind, name, content.
    """
    mapping: dict[str, dict[tuple[str, str], str]] = {}
    for row in rows:
        fam = row.get("family") or ""
        mapping.setdefault(fam, {})[(row["kind"], row["name"])] = row["content"]
    global _PROMPT_OVERRIDES
    _PROMPT_OVERRIDES = mapping


def clear_prompt_overrides() -> None:
    global _PROMPT_OVERRIDES
    _PROMPT_OVERRIDES = {}


def _db_lookup(kind: str, family: str, name: str) -> Optional[str]:
    """Override for (kind, family, name): family-specific then global. None if absent/flag off."""
    if not _is_prompt_db_overrides_enabled():
        return None
    fam_map = _PROMPT_OVERRIDES.get(family)
    if fam_map is not None and (kind, name) in fam_map:
        return fam_map[(kind, name)]
    global_map = _PROMPT_OVERRIDES.get("")
    if global_map is not None and (kind, name) in global_map:
        return global_map[(kind, name)]
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Commit** (on approval)

```bash
git add src/core/loader.py tests/test_prompt_overrides_loader.py
git commit -m "feat(prompts): add flag-gated prompt-override map + _db_lookup to loader"
```

---

### Task 3: Loader — hook `_db_lookup` into `MatrixResolver.load()`

**Files:**
- Modify: `src/core/loader.py:669` (`MatrixResolver.load`)
- Test: `tests/test_prompt_overrides_loader.py` (add a hook test)

- [ ] **Step 1: Write the failing test**

```python
def test_matrix_load_prefers_override(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")

    # A resolver whose bundled file would say "BUNDLED"; override says "OVERRIDE".
    from src.core.loader import PromptMatrixResolver
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    (tmp_path / "config" / "prompts" / "persona.txt").write_text("BUNDLED")
    resolver = PromptMatrixResolver(None, "gemma")

    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "prompts", "name": "persona", "content": "OVERRIDE"},
    ])
    assert resolver.load("persona") == "OVERRIDE"           # override wins
    assert resolver.load("persona", bundled_only=True) != "OVERRIDE"  # bundled path bypasses overrides
```

> If constructing `PromptMatrixResolver(None, "gemma")` needs a real deployment dir to find files, point it at the framework `config/` the same way `serialize_resolved_config` does (`config._deployment_dir`); the assertion that matters is `load("persona") == "OVERRIDE"`, which short-circuits before any file read.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py::test_matrix_load_prefers_override -v`
Expected: FAIL — `load()` ignores overrides and/or rejects the `bundled_only` kwarg.

- [ ] **Step 3: Implement the hook**

Current (`src/core/loader.py:669`):
```python
def load(self, entry_type: str) -> str:
    filename = self.resolve_filename(entry_type)
    return self._file_resolver.load(filename)
```

Replace with:
```python
def load(self, entry_type: str, *, bundled_only: bool = False) -> str:
    if not bundled_only:
        override = _db_lookup(self.MATRIX_SUBSECTION, self.model_family, entry_type)
        if override is not None:
            return override
    filename = self.resolve_filename(entry_type)
    return self._file_resolver.load(filename)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: PASS. Also run the existing resolver tests to confirm no regression:
`pytest tests/test_prompt_matrix.py tests/test_instruction_matrix.py -v` → PASS.

- [ ] **Step 5: Commit** (on approval)

```bash
git add src/core/loader.py tests/test_prompt_overrides_loader.py
git commit -m "feat(prompts): consult overrides in MatrixResolver.load (flag-gated)"
```

---

### Task 4: Agent reads overrides into the map before the freeze

**Files:**
- Modify: `src/database/postgres_db.py` — add `PromptsNamespace`, wire `self.prompts` at line 107
- Modify: `src/agent.py:1051` — load overrides before `serialize_resolved_config`
- Test: `tests/test_prompt_overrides_loader.py` (namespace query shape)

- [ ] **Step 1: Write the failing test (namespace query)**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_prompts_namespace_lists_family_and_global():
    from src.database.postgres_db import PromptsNamespace
    fake_db = MagicMock()
    fake_db.fetch = AsyncMock(return_value=[{"family": "gemma", "kind": "prompts",
                                             "name": "persona", "content": "X",
                                             "content_format": "text"}])
    fake_db._row_to_dict = lambda r: dict(r)

    ns = PromptsNamespace(fake_db)
    rows = await ns.list_overrides_for_family("gemma")

    assert rows == [{"family": "gemma", "kind": "prompts", "name": "persona",
                     "content": "X", "content_format": "text"}]
    sql = fake_db.fetch.call_args.args[0]
    assert "FROM prompt_overrides" in sql
    assert "family = $1 OR family IS NULL" in sql
    assert fake_db.fetch.call_args.args[1] == "gemma"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py::test_prompts_namespace_lists_family_and_global -v`
Expected: FAIL — `ImportError: cannot import name 'PromptsNamespace'`.

- [ ] **Step 3: Implement `PromptsNamespace` and wire it**

Add a namespace class in `src/database/postgres_db.py` (mirror `JobsNamespace`/`CitationsNamespace`):
```python
class PromptsNamespace:
    """Prompt-override reads for the agent's resolution path."""

    def __init__(self, db: "PostgresDB"):
        self.db = db

    async def list_overrides_for_family(self, family: str) -> List[Dict[str, Any]]:
        """Override rows for <family> plus global (NULL-family) rows."""
        rows = await self.db.fetch(
            """
            SELECT family, kind, name, content, content_format
            FROM prompt_overrides
            WHERE family = $1 OR family IS NULL
            """,
            family,
        )
        return [self.db._row_to_dict(row) for row in rows]
```

In `PostgresDB.__init__`, after `self.jobs = JobsNamespace(self)` (line 107):
```python
        self.prompts = PromptsNamespace(self)
```

- [ ] **Step 4: Wire the agent freeze path**

In `src/agent.py`, inside the `if self.postgres_conn and not resume and not _config_from_db:` block (line ~1051), immediately **before** `resolved = serialize_resolved_config(...)`:
```python
                # Load DB prompt overrides so they're captured in the freeze
                # (flag-gated, fail-open to bundled defaults).
                from .core.loader import set_prompt_overrides, _is_prompt_db_overrides_enabled
                from .core.model_registry import family_of
                if _is_prompt_db_overrides_enabled():
                    try:
                        _family = family_of(self.config.llm.model)
                        _rows = await self.postgres_conn.prompts.list_overrides_for_family(_family)
                        set_prompt_overrides(_rows)
                        logger.info(f"Loaded {len(_rows)} prompt override(s) for family {_family}")
                    except Exception as e:
                        logger.warning(f"Failed to load prompt overrides (using bundled): {e}")
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: PASS.

- [ ] **Step 6: End-to-end manual acceptance (Slice 1 done)**

With `PROMPT_DB_OVERRIDES_ENABLED=true`, insert a row and run a job:
```bash
psql "$DATABASE_URL" -c "INSERT INTO prompt_overrides (family, kind, name, content) VALUES ('gemma','prompts','persona','TEST OVERRIDE PERSONA');"
```
Dispatch a gemma-family job, then:
```bash
psql "$DATABASE_URL" -c "SELECT resolved_config->'prompts'->>'persona' FROM jobs ORDER BY created_at DESC LIMIT 1;"
```
Expected: `TEST OVERRIDE PERSONA`. A job already running keeps its old snapshot; with the flag off, the snapshot shows the bundled persona.

- [ ] **Step 7: Commit** (on approval)

```bash
git add src/database/postgres_db.py src/agent.py tests/test_prompt_overrides_loader.py
git commit -m "feat(prompts): agent loads DB overrides into the resolved-config freeze"
```

---

## Slice 2 — Admin CRUD API + Catalog

### Task 5: Orchestrator `PostgresDB` — override CRUD

**Files:**
- Modify: `orchestrator/database/postgres.py` (add methods to `PostgresDB`, mirroring `list_system_api_keys` / `create_system_llm_endpoint`)
- Test: `tests/test_admin_prompts_api.py`

- [ ] **Step 1: Write the failing test (upsert SQL shape, mocked pool)**

```python
# tests/test_admin_prompts_api.py
import os
os.environ.setdefault("VECTOR_DB_URL", "postgresql://localhost/test")  # mirror test_admin_providers_api.py

import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_upsert_prompt_override_uses_on_conflict():
    from orchestrator.database.postgres import PostgresDB
    db = PostgresDB.__new__(PostgresDB)              # bypass real pool init
    db.fetchrow = AsyncMock(return_value={"id": "abc", "family": "gemma",
                                          "kind": "prompts", "name": "persona",
                                          "content": "C"})
    row = await db.upsert_prompt_override(family="gemma", kind="prompts", name="persona",
                                          content="C", content_format="text",
                                          notes=None, user_id=None)
    sql = db.fetchrow.call_args.args[0]
    assert "INSERT INTO prompt_overrides" in sql
    assert "ON CONFLICT" in sql and "DO UPDATE" in sql
    assert row["content"] == "C"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_admin_prompts_api.py::test_upsert_prompt_override_uses_on_conflict -v`
Expected: FAIL — `AttributeError: 'PostgresDB' object has no attribute 'upsert_prompt_override'`.

- [ ] **Step 3: Implement the CRUD methods**

In `orchestrator/database/postgres.py`, on `PostgresDB`:
```python
async def list_prompt_overrides(self) -> list[dict]:
    rows = await self.fetch(
        "SELECT * FROM prompt_overrides ORDER BY family NULLS FIRST, kind, name"
    )
    return [dict(r) for r in rows]

async def get_prompt_override(self, override_id: str) -> Optional[dict]:
    row = await self.fetchrow(
        "SELECT * FROM prompt_overrides WHERE id = $1", UUID(str(override_id))
    )
    return dict(row) if row else None

async def upsert_prompt_override(self, *, family, kind, name, content,
                                 content_format, notes, user_id) -> dict:
    row = await self.fetchrow(
        """
        INSERT INTO prompt_overrides
            (family, kind, name, content, content_format, notes, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
        ON CONFLICT (COALESCE(family, ''), kind, name) DO UPDATE SET
            content        = EXCLUDED.content,
            content_format = EXCLUDED.content_format,
            notes          = EXCLUDED.notes,
            updated_by     = EXCLUDED.updated_by,
            updated_at     = CURRENT_TIMESTAMP
        RETURNING *
        """,
        family, kind, name, content, content_format, notes,
        UUID(str(user_id)) if user_id else None,
    )
    return dict(row)

async def delete_prompt_override(self, override_id: str) -> bool:
    result = await self.execute(
        "DELETE FROM prompt_overrides WHERE id = $1", UUID(str(override_id))
    )
    return result == "DELETE 1"
```
(`UUID` and `Optional` are already imported in this module.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_admin_prompts_api.py::test_upsert_prompt_override_uses_on_conflict -v`
Expected: PASS.

- [ ] **Step 5: Commit** (on approval)

```bash
git add orchestrator/database/postgres.py tests/test_admin_prompts_api.py
git commit -m "feat(prompts): prompt_overrides CRUD on orchestrator PostgresDB"
```

---

### Task 6: Admin routes + Pydantic models

**Files:**
- Modify: `orchestrator/main.py` — models near the other `BaseModel`s (~line 2763); routes near the admin/providers routes (~line 15087)
- Test: `tests/test_admin_prompts_api.py` (route registration + model validation)

- [ ] **Step 1: Write the failing tests**

```python
def _registered_routes() -> set:
    from orchestrator.main import app
    out = set()
    for route in app.routes:
        for m in (getattr(route, "methods", None) or set()):
            out.add((m, getattr(route, "path", "")))
    return out


PROMPT_ROUTES = {
    ("GET", "/api/admin/prompts/overrides"),
    ("POST", "/api/admin/prompts/overrides"),
    ("GET", "/api/admin/prompts/overrides/{override_id}"),
    ("PUT", "/api/admin/prompts/overrides/{override_id}"),
    ("DELETE", "/api/admin/prompts/overrides/{override_id}"),
    ("GET", "/api/admin/prompts/catalog"),
    ("GET", "/api/admin/prompts/bundled/{family}/{kind}/{name}"),
}


def test_prompt_admin_routes_registered():
    missing = [r for r in PROMPT_ROUTES if r not in _registered_routes()]
    assert not missing, f"missing routes: {missing}"


def test_prompt_override_create_model_validates_kind():
    import pytest
    from orchestrator.main import PromptOverrideCreate
    ok = PromptOverrideCreate(family="gemma", kind="prompts", name="persona", content="x")
    assert ok.content_format == "text"
    with pytest.raises(Exception):
        PromptOverrideCreate(family=None, kind="bogus", name="persona", content="x")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_admin_prompts_api.py -k "routes_registered or create_model" -v`
Expected: FAIL — routes missing / `PromptOverrideCreate` undefined.

- [ ] **Step 3: Implement models**

In `orchestrator/main.py` near the other request models:
```python
class PromptOverrideCreate(BaseModel):
    family: str | None = Field(None, max_length=64)
    kind: str = Field(..., pattern="^(prompts|instructions)$")
    name: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1)
    content_format: str = Field("text", pattern="^(text|markdown|jinja|yaml)$")
    notes: str | None = None


class PromptOverrideUpdate(BaseModel):
    content: str = Field(..., min_length=1)
    content_format: str = Field("text", pattern="^(text|markdown|jinja|yaml)$")
    notes: str | None = None
```

- [ ] **Step 4: Implement routes**

In `orchestrator/main.py` near the admin/providers routes:
```python
@app.get("/api/admin/prompts/overrides")
async def admin_list_prompt_overrides(request: Request) -> list[dict]:
    await _require_admin(request)
    return await postgres_db.list_prompt_overrides()


@app.get("/api/admin/prompts/overrides/{override_id}")
async def admin_get_prompt_override(request: Request, override_id: str) -> dict:
    await _require_admin(request)
    row = await postgres_db.get_prompt_override(override_id)
    if not row:
        raise HTTPException(status_code=404, detail="override not found")
    return row


@app.post("/api/admin/prompts/overrides")
async def admin_create_prompt_override(request: Request, body: PromptOverrideCreate) -> dict:
    user = await _require_admin(request)
    return await postgres_db.upsert_prompt_override(
        family=body.family, kind=body.kind, name=body.name,
        content=body.content, content_format=body.content_format,
        notes=body.notes, user_id=user.get("id"),
    )


@app.put("/api/admin/prompts/overrides/{override_id}")
async def admin_update_prompt_override(request: Request, override_id: str,
                                       body: PromptOverrideUpdate) -> dict:
    user = await _require_admin(request)
    existing = await postgres_db.get_prompt_override(override_id)
    if not existing:
        raise HTTPException(status_code=404, detail="override not found")
    return await postgres_db.upsert_prompt_override(
        family=existing["family"], kind=existing["kind"], name=existing["name"],
        content=body.content, content_format=body.content_format,
        notes=body.notes, user_id=user.get("id"),
    )


@app.delete("/api/admin/prompts/overrides/{override_id}")
async def admin_delete_prompt_override(request: Request, override_id: str) -> dict:
    await _require_admin(request)
    if not await postgres_db.delete_prompt_override(override_id):
        raise HTTPException(status_code=404, detail="override not found")
    return {"deleted": True}
```
(The `catalog` and `bundled` routes are added in Task 7.)

- [ ] **Step 5: Run to verify**

Run: `pytest tests/test_admin_prompts_api.py -k "routes_registered or create_model" -v`
Expected: the override routes pass; the two Task-7 routes (`catalog`, `bundled`) still show as missing — that's expected until Task 7. (Temporarily trim `PROMPT_ROUTES` to the five override routes, or mark the two as `xfail` with reason "added in Task 7"; restore in Task 7.)

- [ ] **Step 6: Commit** (on approval)

```bash
git add orchestrator/main.py tests/test_admin_prompts_api.py
git commit -m "feat(prompts): admin CRUD routes for prompt overrides (_require_admin)"
```

---

### Task 7: Catalog + bundled-default endpoints

**Files:**
- Create: `config/prompts/catalog.yaml`
- Modify: `orchestrator/main.py` — `load_prompt_catalog`, `catalog_entry`, `read_bundled_prompt` helpers + two routes
- Test: `tests/test_admin_prompts_api.py`

- [ ] **Step 1: Write the catalog**

```yaml
# config/prompts/catalog.yaml — human descriptions for editable prompt keys.
- kind: prompts
  name: systemprompt
  title: "System prompt (framework scaffold)"
  description: "Framework system prompt; {expert_identity} is injected into it. Rebuilt every LLM call."
- kind: prompts
  name: persona
  title: "Expert persona / identity"
  description: "Injected as {expert_identity} every call. Defines who the agent is and how it thinks."
- kind: prompts
  name: strategic
  title: "Strategic-phase directive"
  description: "Appended during strategic (planning / reflection) phases."
- kind: prompts
  name: tactical
  title: "Tactical-phase directive"
  description: "Appended during tactical (execution) phases."
- kind: prompts
  name: summarization
  title: "Summarization (compaction) prompt"
  description: "Used when the conversation is compacted."
```

- [ ] **Step 2: Write the failing test**

```python
def test_catalog_has_core_prompt_keys():
    from orchestrator.main import load_prompt_catalog
    names = {(e["kind"], e["name"]) for e in load_prompt_catalog()}
    assert {("prompts", "persona"), ("prompts", "systemprompt")}.issubset(names)
```

Run: `pytest tests/test_admin_prompts_api.py::test_catalog_has_core_prompt_keys -v` → FAIL (`load_prompt_catalog` undefined).

- [ ] **Step 3: Implement helpers + routes** in `orchestrator/main.py`

```python
def load_prompt_catalog() -> list[dict]:
    import yaml
    from src.core.loader import get_project_root
    path = get_project_root() / "config" / "prompts" / "catalog.yaml"
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text()) or []


def catalog_entry(kind: str, name: str) -> dict | None:
    for e in load_prompt_catalog():
        if e.get("kind") == kind and e.get("name") == name:
            return e
    return None


def read_bundled_prompt(kind: str, family: str | None, name: str) -> str:
    from src.core.loader import PromptMatrixResolver, InstructionMatrixResolver
    resolver_cls = {"prompts": PromptMatrixResolver,
                    "instructions": InstructionMatrixResolver}.get(kind)
    if resolver_cls is None:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
    resolver = resolver_cls(None, family or "default")  # deployment_dir=None → framework config/
    try:
        return resolver.load(name, bundled_only=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no bundled default for that key")


@app.get("/api/admin/prompts/catalog")
async def admin_prompt_catalog(request: Request) -> list[dict]:
    await _require_admin(request)
    return load_prompt_catalog()


@app.get("/api/admin/prompts/bundled/{family}/{kind}/{name}")
async def admin_get_bundled_prompt(request: Request, family: str, kind: str, name: str) -> dict:
    await _require_admin(request)
    fam = None if family == "_" else family
    return {"family": fam, "kind": kind, "name": name,
            "content": read_bundled_prompt(kind, fam, name),
            "catalog": catalog_entry(kind, name)}
```

> Verify `PromptMatrixResolver(None, "default")` resolves framework files; if a deployment dir is required, pass `get_project_root()` instead of `None`.

- [ ] **Step 4: Run to verify it passes**

Restore the full `PROMPT_ROUTES` set (undo the Task-6 trim/xfail).
Run: `pytest tests/test_admin_prompts_api.py -v`
Expected: PASS (all routes registered, catalog + models validate).

- [ ] **Step 5: Commit** (on approval)

```bash
git add config/prompts/catalog.yaml orchestrator/main.py tests/test_admin_prompts_api.py
git commit -m "feat(prompts): prompt catalog + bundled-default admin endpoints"
```

---

### Task 8: Full-suite check + flag default

**Files:**
- (No new code) — confirm `PROMPT_DB_OVERRIDES_ENABLED` defaults to off everywhere; the helm value flip is a deploy step, not in this plan.

- [ ] **Step 1: Run the relevant suites**

Run:
```bash
pytest tests/test_prompt_overrides_loader.py tests/test_admin_prompts_api.py \
       tests/test_prompt_matrix.py tests/test_instruction_matrix.py tests/test_autonomy.py -v
```
Expected: PASS. (CI on Py3.12 is the gate for the `main.py`-importing tests.)

- [ ] **Step 2: Confirm fail-safe defaults**

- With `PROMPT_DB_OVERRIDES_ENABLED` unset, `_db_lookup` returns `None` → resolution and the freeze are byte-identical to today.
- DB unreachable at the agent freeze → the `try/except` logs WARN and proceeds with bundled defaults.

- [ ] **Step 3: Commit** (on approval) — only if any cleanup was needed.

---

## Self-Review

**Spec coverage (v1 section of `prompt_editing_page.md`):**
- Single `(family, kind, name)` table → Task 1. ✓
- `_db_lookup` in the resolver behind `PROMPT_DB_OVERRIDES_ENABLED`, empty table = today → Tasks 2–3. ✓
- Overrides freeze into `resolved_config` automatically (reproducibility, no versioning) → Task 4 (hook is under `serialize_resolved_config`). ✓
- Admin CRUD gated by admin role; bundled default + description catalog → Tasks 5–7. ✓
- Fail-open on DB error → Task 4 / Task 8. ✓
- UI (Slice 3) → out of scope (follow-up plan). ✓

**Type/name consistency:** override key tuple `(kind, name)` and the `kind` values `'prompts'`/`'instructions'` are used identically in `set_prompt_overrides`, `_db_lookup`, the migration CHECK, `PromptsNamespace`, the API models (`pattern="^(prompts|instructions)$"`), and `read_bundled_prompt`'s class map. `load(..., bundled_only=...)` signature matches all call sites. `upsert_prompt_override` keyword args match both call sites in the routes.

**Placeholders:** none — every step has concrete code or an exact command. The two "verify constructor" notes are real runtime checks, not gaps.

**Decisions deferred to the doc, not the plan:** finer `kind` taxonomy, per-expert/per-project, hash versioning, audit log, drift, live in-flight updates, UI.

---

## Execution Handoff

Two options:
1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Remember: per the commit policy, pause for approval at each commit step.
