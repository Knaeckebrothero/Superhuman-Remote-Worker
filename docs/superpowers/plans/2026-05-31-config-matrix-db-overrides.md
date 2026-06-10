# Config Matrix DB Overrides — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the whole model-config matrix (`settings` + `guardrails`, joining the existing `prompts`/`instructions`) DB-overridable per model family on a baked-file floor, and rename the feature `prompt_* → config_*` end-to-end (DB, API, flag, Angular UI) — for stateless/HA orchestrators.

**Architecture:** Thin DB deltas on an immutable baked-file floor; precedence `DB-family > DB-global > file`. Prompts/instructions/guardrails resolve lazily; `settings` bake eagerly into config so they get a delta-application step at job start before LLM creation. Overrides load once per job in the ephemeral agent process and freeze into `jobs.resolved_config`.

**Tech Stack:** Python 3.11/3.12, asyncpg, FastAPI/Pydantic v2, pytest; Angular 21 (Cockpit); k3d + Tilt local cluster.

**Spec:** `docs/superpowers/specs/2026-05-31-config-matrix-db-overrides-design.md`

---

## Current state (2026-06-07)

**✅ Phases A–D complete + k3d-verified (2026-06-01).** Migration `0022` applied on the real DB; the full UI→API→DB round-trip and the agent read→apply→freeze chain were verified in-pod, and a gemma-family job froze `resolved_config->'agent'->'llm'->>'temperature' = 1` from a DB override. Backend tests + `ruff` clean; committed on `develop`. Flag stays `false` in prod, `true` only in `values-experimental.yaml` and local `values-local.yaml`.

**The Cockpit UI then grew past this plan's "basic structured editor" scope** in three follow-on rounds — all live-verified on k3d. **No orchestrator/agent resolution code changed** (the resolver already handled every kind); round 1 also edited `config/prompts/catalog.yaml` (catalog *data*), rounds 2–3 are cockpit-only:

1. **Grouped picker + exposed keys (2026-06-03).** Picker keys bucket into `<optgroup>`s via a new `group:` field in `config/prompts/catalog.yaml`. Exposed two kinds the plumbing always supported but the catalog never listed: the persistent `systemprompt_interactive` prompt and all `instructions`. `workspace_template` deliberately left out (vestigial).
2. **Typed settings form (2026-06-06).** `settings` pulled out of the key selector into a dedicated, family-scoped typed form (number inputs + a toggle) with diff-against-bundled Save (changed→upsert / reset→delete / unchanged→no-op), per-field reset, and override badges. Fixed a `value_json`-as-JSON-string decode bug (asyncpg returns JSONB as text) once at the service boundary (`coerceOverrideValue`), which also corrected the guardrails editor.
3. **Layout + single-field editor (2026-06-07).** Reorganized into family-scope-on-top → settings → overrides; the key dropdown moved *inside* the editor card and default-selects the first key. The two-column *bundled \| override* pair collapsed into one editable field (seeded to the effective value) with a codeblock-style copy button, drag-and-drop / file-upload, a collapsible read-only "shipped default", and diff-aware Save.

The spec's §9 "As-built" mirrors this. The per-step `[ ]` checkboxes below are the **original** implementation recipe (left as the historical record); Task C2's "single textarea / two-column" describes the minimal target, not the as-built UI.

---

## Decisions (locked with the user)

- **Full rename now** (`prompt_* → config_*`) across DB table, loader, agent, orchestrator DB, API routes, env flag, and the Cockpit page. No deprecated aliases — it's one push, tested on k3d-local.
- **Per-leaf settings rows**; `value_json` holds the typed value. Guardrails = one row per `(family,'guardrails')`, `value_json` = `{tool_examples, nudges}`.
- **Family-only** keying (no `expert` column) — per-expert is a separate feature.
- **Basic UI**: rename the existing `/admin/prompts` page to `/admin/config` and add minimal value_json editing for settings/guardrails (the picker is already catalog-driven, so this is small).
- **Verify on k3d-local** via Tilt.

## Phasing & gates

- **Phase A — Rename + storage** (mechanical rename + new migration). Gate: existing prompt-override behavior preserved under new names; backend tests green.
- **Phase B — Backend feature** (settings + guardrails resolution, agent application, API validation).
- **Phase C — Cockpit UI** (rename page + basic structured editing).
- **Phase D — k3d end-to-end verification.**

## Rename map (authoritative)

| Old | New |
|---|---|
| table `prompt_overrides` | `config_overrides` (via `ALTER TABLE RENAME` in 0022 — do **not** edit 0021) |
| env `PROMPT_DB_OVERRIDES_ENABLED` | `CONFIG_DB_OVERRIDES_ENABLED` |
| helm value `agent.promptDbOverridesEnabled` | `agent.configDbOverridesEnabled` |
| `_PROMPT_OVERRIDES` | `_CONFIG_OVERRIDES` (text kinds) + new `_VALUE_OVERRIDES` (structured) |
| `_is_prompt_db_overrides_enabled` | `_is_config_db_overrides_enabled` |
| `set_prompt_overrides` / `clear_prompt_overrides` | `set_config_overrides` / `clear_config_overrides` |
| `list/get/upsert/delete_prompt_override(s)` (orchestrator DB) | `list/get/upsert/delete_config_override(s)` |
| `PromptsNamespace` / `self.prompts` (agent DB) | `ConfigOverridesNamespace` / `self.config_overrides` |
| `list_overrides_for_family` | **keep** (already generic) |
| `PromptOverrideCreate` / `PromptOverrideUpdate` | `ConfigOverrideCreate` / `ConfigOverrideUpdate` |
| routes `/api/admin/prompts/*` | `/api/admin/config/*` |
| handlers `admin_*_prompt_override`, `admin_prompt_catalog`, `admin_get_bundled_prompt` | `admin_*_config_override`, `admin_config_catalog`, `admin_get_bundled_config` |
| `load_prompt_catalog` / `_prompt_catalog_entry` / `read_bundled_prompt` | `load_config_catalog` / `_config_catalog_entry` / `read_bundled_config` |
| Cockpit route `admin/prompts`, dir `views/admin/prompts/`, `AdminPromptsComponent`, `admin-prompts.service.ts`/`AdminPromptsService`, `PromptOverride[]` interfaces, i18n `admin.prompts.*` | `admin/config`, `views/admin/config/`, `AdminConfigComponent`, `admin-config.service.ts`/`AdminConfigService`, `ConfigOverride*`, `admin.config.*` |
| tests `test_prompt_overrides_loader.py`, `test_admin_prompts_api.py` | `test_config_overrides_loader.py`, `test_admin_config_api.py` |

**Do NOT rename:** `PromptMatrixResolver`/`InstructionMatrixResolver` (matrix infra, not the override feature), the `config/prompts/` directory or its `.txt`/`.md` content files, and `config/prompts/catalog.yaml`'s *path* (keep the file location; only the loader function name changes). `_db_lookup` keeps its name (already generic).

---

## PHASE A — Rename + storage migration ✅

### Task A1: Migration 0022 — rename table + generalize storage

**Files:**
- Create: `orchestrator/database/migrations/app/0022_config_overrides.sql`

- [ ] **Step 1: Confirm current constraint/index names**

Run (any env with the schema):
```bash
psql "$DATABASE_URL" -c "\d prompt_overrides"
```
Expected: PK `prompt_overrides_pkey`, CHECK `prompt_overrides_kind_check`, unique index `uq_prompt_override`, index `idx_prompt_override_lookup`, FKs `prompt_overrides_created_by_fkey`/`..._updated_by_fkey`. If any differ, adjust Step 2.

- [ ] **Step 2: Write the migration** (`orchestrator/database/migrations/app/0022_config_overrides.sql`):
```sql
-- migration:     0022_config_overrides.sql
-- description:   Rename prompt_overrides -> config_overrides and generalize it
--                to the whole config matrix. Adds value_json for structured
--                kinds (settings, guardrails); relaxes content/content_format
--                (text kinds only); widens `kind`. RENAME preserves all data.
--                Do NOT edit 0021 (already applied + checksummed).
-- depends-on:    0021_prompt_overrides.sql
-- expected:      < 100ms. Rename + column add + constraint swaps; no data rewrite.
-- locks:         Brief ACCESS EXCLUSIVE on the table for the rename/ALTERs.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE prompt_overrides RENAME TO config_overrides;

-- Tidy the carried-over object names (RENAME TABLE leaves these unchanged).
ALTER INDEX uq_prompt_override RENAME TO uq_config_override;
ALTER INDEX idx_prompt_override_lookup RENAME TO idx_config_override_lookup;
ALTER TABLE config_overrides RENAME CONSTRAINT prompt_overrides_kind_check
    TO config_overrides_kind_check;

-- Structured value for settings/guardrails (text kinds keep using `content`).
ALTER TABLE config_overrides ADD COLUMN IF NOT EXISTS value_json JSONB;

-- content / content_format only apply to text kinds now -> allow NULL.
ALTER TABLE config_overrides ALTER COLUMN content DROP NOT NULL;
ALTER TABLE config_overrides ALTER COLUMN content_format DROP NOT NULL;

-- Widen the kind domain.
ALTER TABLE config_overrides DROP CONSTRAINT config_overrides_kind_check;
ALTER TABLE config_overrides ADD CONSTRAINT config_overrides_kind_check
    CHECK (kind IN ('prompts', 'instructions', 'settings', 'guardrails'));

-- Exactly one payload column populated, by kind.
ALTER TABLE config_overrides ADD CONSTRAINT config_overrides_payload_check CHECK (
    (kind IN ('prompts', 'instructions') AND content IS NOT NULL AND value_json IS NULL) OR
    (kind IN ('settings', 'guardrails')  AND value_json IS NOT NULL AND content IS NULL)
);

COMMENT ON TABLE config_overrides IS
    'DB-backed overrides for the bundled config matrix (prompts, instructions, '
    'settings, guardrails). One row overrides one (family, kind, name); NULL '
    'family = global. File matrix is the immutable floor. Design: '
    'docs/superpowers/specs/2026-05-31-config-matrix-db-overrides-design.md.';

COMMIT;
```

- [ ] **Step 3: Verify it applies (dry-run, rolled back)**
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
\i orchestrator/database/migrations/app/0022_config_overrides.sql
\d+ config_overrides
ROLLBACK;
SQL
```
Expected: `\d+` shows table `config_overrides`, `value_json | jsonb`, `content` nullable, kind CHECK with 4 kinds, `config_overrides_payload_check`.

- [ ] **Step 4: Commit**
```bash
git add orchestrator/database/migrations/app/0022_config_overrides.sql
git commit -m "feat(config-overrides): migration 0022 — rename table + value_json + widen kind"
```

### Task A2: Mechanical rename of the existing feature (behavior-preserving)

Rename every identifier per the Rename map across backend + tests. This task changes **names only** — no new behavior. Work file-by-file, then verify with grep + tests.

**Files (edit all):**
- `src/core/loader.py` — `_PROMPT_OVERRIDES`→`_CONFIG_OVERRIDES`, `_is_prompt_db_overrides_enabled`→`_is_config_db_overrides_enabled`, `set_prompt_overrides`→`set_config_overrides`, `clear_prompt_overrides`→`clear_config_overrides`; env string `PROMPT_DB_OVERRIDES_ENABLED`→`CONFIG_DB_OVERRIDES_ENABLED`; comment at line 25.
- `src/database/postgres_db.py` — class `PromptsNamespace`→`ConfigOverridesNamespace`; `self.prompts = PromptsNamespace(self)` (line 109) → `self.config_overrides = ConfigOverridesNamespace(self)`; SQL `FROM prompt_overrides`→`FROM config_overrides` (line 802).
- `src/agent.py` — imports (lines 1055-1056) + call (line 1066 `self.postgres_conn.prompts`→`self.postgres_conn.config_overrides`) + line 1069.
- `orchestrator/database/postgres.py` — method names `*_prompt_override(s)`→`*_config_override(s)`; SQL table refs (lines 4983, 4990, 5014, 5039) `prompt_overrides`→`config_overrides`.
- `orchestrator/main.py` — Pydantic class names; route decorators `/api/admin/prompts/*`→`/api/admin/config/*`; handler fn names; `load_prompt_catalog`/`_prompt_catalog_entry`/`read_bundled_prompt`→`load_config_catalog`/`_config_catalog_entry`/`read_bundled_config`; the `postgres_db.*_prompt_override(s)` call sites.
- `helm/templates/configmap.yaml:53` — `PROMPT_DB_OVERRIDES_ENABLED`/`promptDbOverridesEnabled`→`CONFIG_DB_OVERRIDES_ENABLED`/`configDbOverridesEnabled`.
- `helm/values.yaml:129,133` and `deployment/values-experimental.yaml:134` — `promptDbOverridesEnabled`→`configDbOverridesEnabled`.
- Rename test files: `git mv tests/test_prompt_overrides_loader.py tests/test_config_overrides_loader.py` and `git mv tests/test_admin_prompts_api.py tests/test_admin_config_api.py`; update every identifier/route/table/flag string inside them to the new names (keep assertions behavior-equivalent).

- [ ] **Step 1: Apply all renames** above (mechanical; the Rename map is authoritative).

- [ ] **Step 2: Verify no stale backend references remain**

Run:
```bash
rg -n "prompt_overrides|PROMPT_DB_OVERRIDES|promptDbOverridesEnabled|set_prompt_overrides|clear_prompt_overrides|_is_prompt_db_overrides_enabled|PromptsNamespace|_prompt_override|PromptOverrideCreate|PromptOverrideUpdate|load_prompt_catalog|_prompt_catalog_entry|read_bundled_prompt|/api/admin/prompts" \
  src/ orchestrator/ helm/ deployment/ tests/
```
Expected: **no matches** (except inside `0021_prompt_overrides.sql`, which is intentionally frozen). If 0021 shows up, that's fine; anything else must be fixed.

- [ ] **Step 3: Run the renamed backend tests (still behavior-preserving)**

Run: `pytest tests/test_config_overrides_loader.py tests/test_admin_config_api.py -v`
Expected: PASS (same assertions, new names/routes/table).

- [ ] **Step 4: Lint + commit**
```bash
ruff check src/ orchestrator/
git add -A
git commit -m "refactor(config-overrides): rename prompt_* -> config_* (table/loader/api/flag/tests)"
```

> **Gate:** Phase A complete — the existing prompt/instruction override feature now runs under `config_*` names with no behavior change.

---

## PHASE B — Backend feature (settings + guardrails) ✅

### Task B1: Loader — generalize the override map + structured accessors

**Files:**
- Modify: `src/core/loader.py` (the override map + `set_config_overrides`/`clear_config_overrides`/`_db_lookup`, ~lines 34-80)
- Test: `tests/test_config_overrides_loader.py`

- [ ] **Step 1: Write failing tests** (append):
```python
def test_settings_override_assembles_and_merges(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides([
        {"family": None, "kind": "settings", "name": "temperature", "value_json": 0.7},
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0},
        {"family": "gemma", "kind": "settings",
         "name": "limits.context_threshold_tokens", "value_json": 120000},
    ])
    assert loader._settings_override_for("gemma") == {
        "temperature": 1.0, "limits": {"context_threshold_tokens": 120000}}
    assert loader._settings_override_for("other") == {"temperature": 0.7}


def test_guardrails_override_merges(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides([
        {"family": None, "kind": "guardrails", "name": "guardrails",
         "value_json": {"nudges": {"a": 1}}},
        {"family": "gemma", "kind": "guardrails", "name": "guardrails",
         "value_json": {"tool_examples": {"b": 2}}},
    ])
    assert loader._guardrails_override_for("gemma") == {
        "nudges": {"a": 1}, "tool_examples": {"b": 2}}


def test_structured_overrides_empty_when_flag_off(monkeypatch):
    _reset()
    monkeypatch.delenv("CONFIG_DB_OVERRIDES_ENABLED", raising=False)
    loader.set_config_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0}])
    assert loader._settings_override_for("gemma") == {}
    assert loader._guardrails_override_for("gemma") == {}


def test_set_overrides_parses_jsonb_string(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": "1.0"}])
    assert loader._settings_override_for("gemma") == {"temperature": 1.0}
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_config_overrides_loader.py -k "settings_override or guardrails_override or structured_overrides or jsonb_string" -v`
Expected: FAIL — `_settings_override_for` missing.

- [ ] **Step 3: Implement** — replace the map decl + `set_config_overrides`/`clear_config_overrides` (post-Phase-A, ~lines 34-63) and add accessors after `_db_lookup`:
```python
_CONFIG_OVERRIDES: Dict[str, Dict[tuple, str]] = {}   # text kinds: (kind,name)->content
_VALUE_OVERRIDES: Dict[str, Dict[tuple, Any]] = {}    # structured kinds: (kind,name)->value


def set_config_overrides(rows: List[Dict[str, Any]]) -> None:
    """Load override rows into the process maps (replaces any previous set).

    Text kinds (prompts, instructions) carry `content`; structured kinds
    (settings, guardrails) carry `value_json`. NULL/empty family -> "" bucket.
    """
    import json as _json

    text_map: Dict[str, Dict[tuple, str]] = {}
    value_map: Dict[str, Dict[tuple, Any]] = {}
    for row in rows:
        fam = row.get("family") or ""
        kind = row["kind"]
        if kind in ("prompts", "instructions"):
            if row.get("content") is not None:
                text_map.setdefault(fam, {})[(kind, row["name"])] = row["content"]
        elif kind in ("settings", "guardrails"):
            val = row.get("value_json")
            if isinstance(val, str):           # asyncpg JSONB w/o codec -> str
                val = _json.loads(val)
            value_map.setdefault(fam, {})[(kind, row["name"])] = val
    global _CONFIG_OVERRIDES, _VALUE_OVERRIDES
    _CONFIG_OVERRIDES = text_map
    _VALUE_OVERRIDES = value_map


def clear_config_overrides() -> None:
    """Drop all process-local overrides (used between jobs and in tests)."""
    global _CONFIG_OVERRIDES, _VALUE_OVERRIDES
    _CONFIG_OVERRIDES = {}
    _VALUE_OVERRIDES = {}
```
Update `_db_lookup` to read `_CONFIG_OVERRIDES` (was `_PROMPT_OVERRIDES`). Then add after `_db_lookup`:
```python
def _expand_dotted(flat: Dict[str, Any]) -> Dict[str, Any]:
    """Expand dotted keys ('limits.x') into nested dicts ({'limits': {'x': ...}})."""
    out: Dict[str, Any] = {}
    for key, val in flat.items():
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = val
    return out


def _settings_override_for(family: str) -> Dict[str, Any]:
    """DB settings override for <family> (global then family) as a nested dict
    ready to deep_merge onto file settings. {} when flag off or no rows."""
    if not _is_config_db_overrides_enabled():
        return {}

    def collect(fam: str) -> Dict[str, Any]:
        flat = {name: val for (kind, name), val in _VALUE_OVERRIDES.get(fam, {}).items()
                if kind == "settings"}
        return _expand_dotted(flat)

    return deep_merge(collect(""), collect(family))


def _guardrails_override_for(family: str) -> Dict[str, Any]:
    """DB guardrails override ({tool_examples, nudges}) for <family>. {} when off."""
    if not _is_config_db_overrides_enabled():
        return {}

    def collect(fam: str) -> Dict[str, Any]:
        for (kind, name), val in _VALUE_OVERRIDES.get(fam, {}).items():
            if kind == "guardrails":
                return val if isinstance(val, dict) else {}
        return {}

    return deep_merge(collect(""), collect(family))
```
> `_reset()` in the test file calls `clear_config_overrides()` (renamed in Phase A).

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_config_overrides_loader.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): loader override map + settings/guardrails accessors"`

### Task B2: Loader — settings resolution honors DB override

**Files:** Modify `src/core/loader.py` `resolve_model_settings` (~414) and `_apply_settings_matrix` (~439). Test: `tests/test_config_overrides_loader.py`.

- [ ] **Step 1: Failing test**
```python
def test_resolve_model_settings_applies_override(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0}])
    assert loader.resolve_model_settings("google/gemma-4-31b")["temperature"] == 1.0
    assert loader.resolve_model_settings(
        "google/gemma-4-31b", bundled_only=True)["temperature"] != 1.0
```
- [ ] **Step 2: Run → FAIL** (`unexpected keyword 'bundled_only'`).
- [ ] **Step 3: Implement** — add `*, bundled_only: bool = False` to `resolve_model_settings`; after the file merge insert `if not bundled_only: settings = deep_merge(settings, _settings_override_for(family))` (before `settings.pop("limits", None)`). In `_apply_settings_matrix`, after `settings = deep_merge(default_settings, family_settings)` add `settings = deep_merge(settings, _settings_override_for(family))`.
- [ ] **Step 4: Run → PASS** (`pytest tests/test_config_overrides_loader.py -v`).
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): settings resolution honors DB overrides"`

### Task B3: Loader — guardrails resolution honors DB override

**Files:** Modify `src/core/loader.py` `resolve_guardrails` (~391). Test: `tests/test_config_overrides_loader.py`.

- [ ] **Step 1: Failing test**
```python
def test_resolve_guardrails_applies_override(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides([
        {"family": "gemma", "kind": "guardrails", "name": "guardrails",
         "value_json": {"nudges": {"extra": "be careful"}}}])
    assert loader.resolve_guardrails(
        "google/gemma-4-31b")["nudges"]["extra"] == "be careful"
    assert loader.resolve_guardrails(
        "google/gemma-4-31b", bundled_only=True).get("nudges", {}).get("extra") is None
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — add `*, bundled_only: bool = False`; build `merged = deep_merge(default_guardrails, family_guardrails)` then `if not bundled_only: merged = deep_merge(merged, _guardrails_override_for(family))`; return `merged`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): guardrails resolution honors DB overrides"`

### Task B4: Loader — `apply_settings_overrides(config)` delta

**Files:** Modify `src/core/loader.py` (add after `_apply_settings_matrix`, ~490). Test: `tests/test_config_overrides_loader.py`.

- [ ] **Step 1: Failing test**
```python
def test_apply_settings_overrides_mutates_config(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import AgentConfig, LLMConfig, LimitsConfig, apply_settings_overrides
    cfg = AgentConfig(llm=LLMConfig(model="google/gemma-4-31b", temperature=0.0),
                      limits=LimitsConfig())
    loader.set_config_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0},
        {"family": "gemma", "kind": "settings",
         "name": "limits.context_threshold_tokens", "value_json": 123000}])
    assert apply_settings_overrides(cfg) is True
    assert cfg.llm.temperature == 1.0
    assert cfg.limits.context_threshold_tokens == 123000


def test_apply_settings_overrides_noop_without_rows(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import AgentConfig, LLMConfig, LimitsConfig, apply_settings_overrides
    cfg = AgentConfig(llm=LLMConfig(model="google/gemma-4-31b", temperature=0.3),
                      limits=LimitsConfig())
    assert apply_settings_overrides(cfg) is False
    assert cfg.llm.temperature == 0.3
```
> Verify `AgentConfig(llm=..., limits=...)` is constructible (dataclass at `loader.py:1209`). If it needs more required fields, build `AgentConfig()` then set `.llm`/`.limits`.

- [ ] **Step 2: Run → FAIL** (`cannot import name 'apply_settings_overrides'`).
- [ ] **Step 3: Implement**:
```python
def apply_settings_overrides(config: "AgentConfig") -> bool:
    """Apply ONLY the DB settings override on top of an already-resolved config,
    in place. File/expert settings are already baked in by load_agent_config; this
    writes just the DB delta, so it never clobbers non-overridden values. Call at
    job start after set_config_overrides(), before LLM (re)creation and the freeze.
    Returns True if anything changed. No-op when flag off or no settings rows."""
    override = _settings_override_for(family_of(config.llm.model))
    if not override:
        return False
    changed = False
    for key, val in override.items():
        if key == "limits" and isinstance(val, dict):
            for lk, lv in val.items():
                if hasattr(config.limits, lk) and getattr(config.limits, lk) != lv:
                    setattr(config.limits, lk, lv)
                    changed = True
        elif hasattr(config.llm, key) and getattr(config.llm, key) != val:
            setattr(config.llm, key, val)
            changed = True
    return changed
```
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): apply_settings_overrides delta for eager settings"`

### Task B5: Agent-side DB read — return `value_json`

**Files:** Modify `src/database/postgres_db.py` `list_overrides_for_family` (SELECT ~801). Test: `tests/test_config_overrides_loader.py` (namespace test).

- [ ] **Step 1:** In the namespace test, add `value_json` to the mocked row + expected dict, and assert `"value_json" in sql`.
- [ ] **Step 2: Run → FAIL** (`value_json` not in SQL).
- [ ] **Step 3: Implement** — SELECT becomes `SELECT family, kind, name, content, content_format, value_json FROM config_overrides WHERE family = $1 OR family IS NULL`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): agent read path returns value_json"`

### Task B6: Orchestrator DB — `upsert_config_override` handles value_json

**Files:** Modify `orchestrator/database/postgres.py` `upsert_config_override` (~4994, renamed in A2). Test: `tests/test_admin_config_api.py`.

- [ ] **Step 1: Failing test**
```python
@pytest.mark.asyncio
async def test_upsert_settings_override_persists_value_json():
    from unittest.mock import AsyncMock
    from orchestrator.database.postgres import PostgresDB
    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(return_value={"id": "x", "kind": "settings",
                                          "name": "temperature", "value_json": 1.0})
    row = await db.upsert_config_override(
        family="gemma", kind="settings", name="temperature",
        content=None, content_format=None, value_json=1.0, notes=None, user_id=None)
    sql = db.fetchrow.call_args.args[0]
    assert "value_json" in sql and "ON CONFLICT" in sql
    assert row["value_json"] == 1.0
```
- [ ] **Step 2: Run → FAIL** (`unexpected keyword 'value_json'`).
- [ ] **Step 3: Implement** — add `content: str | None = None`, `content_format: str | None = "text"`, `value_json: Any = None` to the signature; serialize `vj = json.dumps(value_json) if value_json is not None else None`; INSERT columns add `value_json`, VALUES add `$6::jsonb` (shift params), `ON CONFLICT ... DO UPDATE SET ... value_json = EXCLUDED.value_json, ...`. (Full SQL mirrors the existing upsert with the extra column.)
- [ ] **Step 4: Run → PASS** (this + the original on-conflict test).
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): orchestrator upsert handles value_json"`

### Task B7: Agent — load all kinds + apply settings before LLM creation/freeze

**Files:** Modify `src/agent.py:1042-1089` (post-A2 names).

- [ ] **Step 1: Implement the reorder** — move the override-load above `_create_phase_llms()` and add the settings delta:
```python
        config_dirty = bool(
            metadata.get("config_name")
            or metadata.get("config_upload_id")
            or metadata.get("config_override")
        )

        # Load DB config overrides (flag-gated; fail-open). MUST precede
        # _create_phase_llms() so settings overrides reach the LLMs, and precede
        # the freeze so they're captured. Prompts/instructions/guardrails resolve
        # lazily from the process map; settings are eager -> apply onto self.config.
        if self.postgres_conn and not resume and not _config_from_db:
            from .core.loader import (
                set_config_overrides,
                apply_settings_overrides,
                _is_config_db_overrides_enabled,
            )

            if _is_config_db_overrides_enabled():
                try:
                    from .core.model_registry import family_of

                    _family = family_of(self.config.llm.model)
                    _rows = await self.postgres_conn.config_overrides.list_overrides_for_family(
                        _family
                    )
                    set_config_overrides(_rows)
                    if apply_settings_overrides(self.config):
                        config_dirty = True
                    logger.info(
                        f"Loaded {len(_rows)} config override(s) for family {_family}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to load config overrides (using bundled): {e}")

        if (not _config_from_db and config_dirty) or _config_from_db:
            logger.info("Config changed for this job — recreating LLMs")
            self._create_phase_llms()

        if self.postgres_conn and not resume and not _config_from_db:
            try:
                import uuid as _uuid
                from .core.loader import serialize_resolved_config

                resolved = serialize_resolved_config(self.config, model=self.config.llm.model)
                await self.postgres_conn.jobs.store_resolved_config(
                    _uuid.UUID(job_id), resolved)
                logger.info(f"Froze resolved config for job {job_id}")
            except Exception as e:
                logger.warning(f"Failed to freeze resolved config: {e}")
```
- [ ] **Step 2: Syntax check** — `python -c "import ast; ast.parse(open('src/agent.py').read())"` → no output. (Behavior verified by B4 loader test + Phase D e2e.)
- [ ] **Step 3: Commit** — `git commit -am "feat(config-overrides): agent loads all kinds + applies settings before LLM creation"`

### Task B8: API — structured kinds + catalog validation

**Files:** Modify `orchestrator/main.py` Pydantic (~2871, post-A2), create/update routes, `read_bundled_config`, and add `validate_override_value`. Test: `tests/test_admin_config_api.py`.

- [ ] **Step 1: Failing tests**
```python
def test_settings_override_create_model_validates():
    Create = _import_main().ConfigOverrideCreate
    ok = Create(family="gemma", kind="settings", name="temperature", value_json=1.0)
    assert ok.value_json == 1.0 and ok.content is None
    with pytest.raises(Exception):
        Create(family="gemma", kind="prompts", name="persona")        # text needs content
    with pytest.raises(Exception):
        Create(family="gemma", kind="settings", name="temperature")   # structured needs value_json


def test_validate_override_value_checks_catalog():
    m = _import_main()
    with pytest.raises(Exception):
        m.validate_override_value("settings", "temperature", 9.0)     # > max
    m.validate_override_value("settings", "temperature", 1.0)         # ok
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `ConfigOverrideCreate`: widen `kind` Literal to all four, make `content` optional, add `value_json: Any = None`, add `@model_validator(mode="after")` enforcing content-for-text / value_json-for-structured. `ConfigOverrideUpdate`: add `value_json`, make `content` optional. Add `validate_override_value(kind, name, value)` (after `_config_catalog_entry`) raising `HTTPException(422)` on unknown key / wrong type / out-of-bounds for settings (number/integer/boolean) and non-dict for guardrails (`type: json`). Wire `validate_override_value(...)` + `value_json=body.value_json` into the create + update routes. Extend `read_bundled_config` to return file-resolved values for settings (`resolve_model_settings(model, bundled_only=True).get(name)`) and guardrails (`resolve_guardrails(model, bundled_only=True)`), keying by family via the matrix (`_load_settings_matrix`/`_load_guardrails_matrix` `default ⊕ family`) — add thin `loader` helpers `bundled_settings_for_family(family, name)` / `bundled_guardrails_for_family(family)` to avoid faking a model string. Ensure `model_validator` + `Any` are imported in `main.py`.
- [ ] **Step 4: Run → PASS** (`pytest tests/test_admin_config_api.py -v`).
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): admin API + validation for settings/guardrails kinds"`

### Task B9: Catalog — settings + guardrails entries

**Files:** Modify `config/prompts/catalog.yaml`. Test: `tests/test_admin_config_api.py`.

- [ ] **Step 1: Failing test**
```python
def test_catalog_has_settings_and_guardrails_keys():
    from pathlib import Path
    import yaml
    entries = yaml.safe_load(
        (Path(__file__).parent.parent / "config/prompts/catalog.yaml").read_text())
    by_key = {(e["kind"], e["name"]): e for e in entries}
    assert by_key[("settings", "temperature")]["type"] == "number"
    assert ("guardrails", "guardrails") in by_key
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — append `settings` entries (`temperature` number 0–2; `top_p` number 0–1; `parallel_tool_calls` boolean; `model_max_context_tokens` integer; `limits.context_threshold_tokens` integer; `limits.summarization_safe_limit` integer) and one `guardrails` entry (`name: guardrails`, `type: json`), each with `title` + `description`. (Names must match `config/model_config_matrix.yaml` keys + `LimitsConfig` fields.)
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(config-overrides): catalog entries for settings + guardrails"`

---

## PHASE C — Cockpit UI (rename + basic structured editing) ✅

### Task C1: Rename the admin page `prompts → config`

**Files (rename + edit):**
- `git mv cockpit/src/app/views/admin/prompts cockpit/src/app/views/admin/config`; rename `admin-prompts.component.ts` → `admin-config.component.ts`, class `AdminPromptsComponent` → `AdminConfigComponent`.
- `git mv cockpit/src/app/core/services/admin-prompts.service.ts cockpit/src/app/core/services/admin-config.service.ts` (+ `.spec.ts`); class `AdminPromptsService` → `AdminConfigService`; interfaces `PromptOverride*` → `ConfigOverride*`; API path strings `/admin/prompts/*` → `/admin/config/*`.
- `cockpit/src/app/app.routes.ts:20,45` — import + `path: 'admin/prompts'` → `'admin/config'`.
- `cockpit/src/app/shell/sidebar/sidebar.component.ts:133` — `routerLink="/admin/prompts"` → `/admin/config`; label "Admin · Prompts" → "Admin · Config".
- `cockpit/src/assets/i18n/en.json` — move `admin.prompts.*` keys to `admin.config.*`; update `transloco.translate()` keys in the component.

- [ ] **Step 1: Apply renames.**
- [ ] **Step 2: Verify no stale refs** — `rg -n "admin/prompts|AdminPrompts|admin-prompts|PromptOverride|admin\.prompts" cockpit/src` → no matches.
- [ ] **Step 3: Run cockpit unit tests** — `cd cockpit && npx vitest run src/app/core/services/admin-config.service.spec.ts` → PASS (per cockpit_build_test_env: vitest is reliable locally).
- [ ] **Step 4: Commit** — `git commit -am "refactor(cockpit): rename admin prompts page -> admin config"`

### Task C2: Basic structured editing for settings/guardrails

The picker is already catalog-driven, so new kinds appear automatically once Task B9 ships them. This task makes the editor send/receive `value_json` for structured kinds.

**Files:** Modify `cockpit/src/app/core/services/admin-config.service.ts` (`ConfigOverride`/`ConfigOverrideCreate` gain `value_json?: unknown`, widen `kind` to 4) and `cockpit/src/app/views/admin/config/admin-config.component.ts`.

- [ ] **Step 1:** In the service, add `value_json?: unknown` to the interfaces and widen `PromptKind`→`ConfigKind = 'prompts'|'instructions'|'settings'|'guardrails'`.
- [ ] **Step 2:** In the component, when `selectedEntry().kind` is `settings`/`guardrails`: show a single textarea bound to a JSON string; on `save()`, `JSON.parse` it and send `{family, kind, name, value_json}` (no `content`); on load, render `JSON.stringify(existingOverride.value_json ?? bundledValue, null, 2)`. For text kinds keep the current `content` path. Catch parse errors and toast (reuse the i18n message pattern). Display the bundled default (from `getBundled`, now returning `content` for text and a value for structured) read-only beside it.
- [ ] **Step 3:** Build check — `cd cockpit && npm install --no-save @monaco-editor/loader >/dev/null 2>&1; npx ng build 2>&1 | tail -5` → succeeds (per cockpit_build_test_env). If a unit spec exists for the component, run it via vitest.
- [ ] **Step 4: Commit** — `git commit -am "feat(cockpit): basic settings/guardrails editing on admin config page"`

---

## PHASE D — k3d end-to-end verification ✅

### Task D1: Bring up k3d-local with the flag on, and verify

- [ ] **Step 1: Enable the flag locally** — add to `deployment/values-local.yaml` (gitignored; create from `values-local.example.yaml` if absent):
```yaml
agent:
  configDbOverridesEnabled: "true"
```
- [ ] **Step 2: Bring up the cluster + Tilt**
```bash
./scripts/local-dev-up.sh          # one-time: creates k3d cluster + cert-manager + secrets
./scripts/local-dev-tilt-up.sh     # builds images, deploys helm chart, runs migrations at orchestrator start
```
Watch the Tilt UI (`https://localhost:10350`) until orchestrator + cockpit are green. Migration `0022` applies automatically at orchestrator startup (`orchestrator/main.py` lifespan → `apply_migrations()`).

- [ ] **Step 2b: Confirm the migration applied**
```bash
kubectl -n srw exec deploy/srw-orchestrator -- \
  psql "$DATABASE_URL" -c "\d config_overrides" | grep -E "value_json|config_overrides_payload_check"
```
Expected: both present. (If `$DATABASE_URL` isn't in the pod env, use the in-cluster Postgres service creds.)

- [ ] **Step 3: Create a settings override + dispatch a job**

Log into Cockpit (`https://localhost`, `test`/`test`), open **Admin · Config**, pick family `gemma`, kind `settings`, key `temperature`, set `1.0`, Save. (Or `curl -b cookies https://localhost/api/admin/config/overrides -d '{"family":"gemma","kind":"settings","name":"temperature","value_json":1.0}'`.)

Dispatch a **gemma-family** job (Create → New Job). Then confirm the freeze captured the override:
```bash
# NOTE: resolved_config nests the agent config under "agent" (serialize_resolved_config
# returns {"agent": asdict(config), "prompts": ..., ...}), so the path is
# agent->llm->temperature — NOT llm->temperature. Verified 2026-06-01 in-pod.
kubectl -n srw exec deploy/srw-orchestrator -- \
  psql "$DATABASE_URL" -c "SELECT resolved_config->'agent'->'llm'->>'temperature' FROM jobs ORDER BY created_at DESC LIMIT 1;"
```
Expected: `1`. Delete the override → next gemma job reverts to the file default (`0.3`).

- [ ] **Step 4: Verify the family-key canonicalization** (pre-existing risk)

Confirm the family the UI/`FAMILIES` uses matches `family_of(model)` for the gemma model — i.e. the override actually matched (Step 3 returned `1.0`). If a hyphen/underscore mismatch prevents matching for `gpt_5`/`gpt-5` etc., file a follow-up; do **not** fix it in this plan. Log the finding either way.

- [ ] **Step 5: Run the full backend suite once more + lint**
```bash
pytest tests/test_config_overrides_loader.py tests/test_admin_config_api.py -v
ruff check src/ orchestrator/
```
Expected: PASS / clean. CI (Py3.12) is the gate.

- [ ] **Step 6: Final commit** — `git commit -am "chore(config-overrides): k3d verification notes + cleanup"` (if any).

---

## Self-review

**Spec coverage:** §4 data model → A1. Rename (user-locked) → A2 + C1. §5 resolution/freeze → B1-B7. §6 coherence (read fresh per job, file cache kept, no long-lived DB cache) → B5/B7. §7 API → B6/B8. §8 catalog + validation → B8/B9. UI → C1/C2. Verification → D1. **Accepted gap:** guardrails not added to `serialize_resolved_config()` (resolved lazily from the once-per-job process map → effectively frozen per process); noted in spec §5.

**Placeholders:** B8's `read_bundled_config` family→value resolution names two thin `loader` helpers to add (`bundled_settings_for_family`/`bundled_guardrails_for_family`) rather than leaving it vague. A2/C1 renames are bounded by `rg`-zero-match gates. No "TODO/TBD" steps.

**Type consistency:** `set_config_overrides`/`clear_config_overrides`/`_is_config_db_overrides_enabled`/`_settings_override_for`/`_guardrails_override_for`/`apply_settings_overrides`/`validate_override_value`/`ConfigOverrideCreate`/`value_json`/`config_overrides`/`CONFIG_DB_OVERRIDES_ENABLED`/`self.config_overrides` are used consistently across all phases. `resolve_model_settings`/`resolve_guardrails` gain `bundled_only` in B2/B3 and are called that way in B8.

**Migration safety:** 0021 is never edited; the rename + generalization happen in new 0022; RENAME preserves data; ON CONFLICT infers by expression so the unique-index rename is safe.

## Out of scope (separate work)
- Per-expert overrides (own feature). · Reconcile-on-redeploy (`helm_managed_settings.md`). · Live in-flight updates. · ~~Richer UI than the basic structured editor~~ — originally out of scope, since **built** (see "Current state"). · ~~Fixing the `FAMILIES` hyphen/underscore mismatch (flagged in D1)~~ — since corrected (underscore forms dropped, `minimax-m3` added; see `admin-config.component.ts` `FAMILIES`).
