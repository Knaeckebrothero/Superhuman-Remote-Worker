# Config Matrix DB Overrides — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the shipped prompt-overrides mechanism so `settings` (LLM params + `limits`) and `guardrails` are DB-overridable per model family — on the baked-file floor — completing the config-matrix→DB migration for stateless/HA orchestrators.

**Architecture:** Thin DB deltas on an immutable baked-file floor. The DB stores only overrides; `config/model_config_matrix.yaml` stays the permanent fallback. Resolution precedence everywhere is `DB-family > DB-global > file`. Prompts/instructions/guardrails resolve lazily (override consulted at resolve time); `settings` bake eagerly into the agent config, so they get a delta-application step at job start before LLM creation. Overrides are loaded once per job in the ephemeral agent process and frozen into `jobs.resolved_config`.

**Tech Stack:** Python 3.11/3.12, asyncpg, FastAPI/Pydantic v2, pytest. Migrations are numbered SQL under `orchestrator/database/migrations/app/`.

**Spec:** `docs/superpowers/specs/2026-05-31-config-matrix-db-overrides-design.md`

---

## Decision notes (refinements to spec §10/§12)

- **Extend in place — keep `prompt_overrides` / `/api/admin/prompts/*` / `PROMPT_DB_OVERRIDES_ENABLED`.** The rename to `config_*` is deferred to an optional follow-up: it would break the already-deployed dev Cockpit route contract and rewrite the working feature's tests for zero functional gain. Internals are generalized; identifiers stay.
- **Per-leaf settings rows** (`name` = `temperature`, `limits.context_threshold_tokens`, …), `value_json` holds the typed value. Guardrails = one row per `(family, 'guardrails')` whose `value_json` is the `{tool_examples, nudges}` doc.
- **Family-only** keying (no `expert` column) — per-expert is a separate feature.
- **UI is a separate plan** (`...-cockpit-ui.md`); this plan is backend + API only.

## File structure

| File | Change | Responsibility |
|---|---|---|
| `orchestrator/database/migrations/app/0022_config_overrides_value_json.sql` | **create** | Add `value_json`, relax `content`/`content_format`, widen `kind`, payload XOR |
| `src/core/loader.py` | modify | Generalize override map; settings/guardrails accessors; override-aware `resolve_model_settings`/`_apply_settings_matrix`/`resolve_guardrails`; `apply_settings_overrides(config)` |
| `src/database/postgres_db.py` | modify | `list_overrides_for_family` returns `value_json` |
| `src/agent.py` | modify | Load all override kinds + apply settings delta before LLM creation/freeze |
| `orchestrator/database/postgres.py` | modify | `upsert_prompt_override` accepts `value_json` + nullable `content` |
| `orchestrator/main.py` | modify | Pydantic + routes + bundled endpoint + validation for `settings`/`guardrails` |
| `config/prompts/catalog.yaml` | modify | Settings + guardrails catalog entries (with `type`/bounds) |
| `tests/test_prompt_overrides_loader.py` | modify | Settings/guardrails resolution + delta tests |
| `tests/test_admin_prompts_api.py` | modify | Pydantic/route/validation tests for new kinds |

---

### Task 1: Migration — generalize `prompt_overrides` storage

**Files:**
- Create: `orchestrator/database/migrations/app/0022_config_overrides_value_json.sql`

- [ ] **Step 1: Confirm the existing `kind` CHECK constraint name** (we drop it by name)

Run (against any env with the schema, e.g. the dev orchestrator's DB):
```bash
psql "$DATABASE_URL" -c "\d prompt_overrides" | grep -i check
```
Expected: a line naming the kind check, almost certainly `prompt_overrides_kind_check` (unnamed CHECKs are auto-named `<table>_<col>_check`). If it differs, use the real name in Step 2.

- [ ] **Step 2: Write the migration**

Create `orchestrator/database/migrations/app/0022_config_overrides_value_json.sql`:
```sql
-- migration:     0022_config_overrides_value_json.sql
-- description:   Generalize prompt_overrides to the whole config matrix. Adds a
--                JSONB value column for structured kinds (settings, guardrails),
--                widens `kind`, and relaxes content/content_format so text kinds
--                (prompts, instructions) keep using `content` while structured
--                kinds use `value_json`. Table keeps its name for back-compat
--                (the live admin API + agent read path reference it). Empty of
--                settings/guardrails rows == today's behavior.
-- depends-on:    0021_prompt_overrides.sql
-- expected:      < 100ms. Column add + constraint swaps; no data rewrite (every
--                existing row is a prompts/instructions row with content set).
-- locks:         Brief ACCESS EXCLUSIVE on prompt_overrides for the ALTERs.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Structured value for settings/guardrails (text kinds keep using `content`).
ALTER TABLE prompt_overrides ADD COLUMN IF NOT EXISTS value_json JSONB;

-- content / content_format only apply to text kinds now -> allow NULL.
ALTER TABLE prompt_overrides ALTER COLUMN content DROP NOT NULL;
ALTER TABLE prompt_overrides ALTER COLUMN content_format DROP NOT NULL;

-- Widen the kind domain (drop the 0021 CHECK by its real name from Step 1).
ALTER TABLE prompt_overrides DROP CONSTRAINT prompt_overrides_kind_check;
ALTER TABLE prompt_overrides ADD CONSTRAINT prompt_overrides_kind_check
    CHECK (kind IN ('prompts', 'instructions', 'settings', 'guardrails'));

-- Exactly one payload column populated, by kind.
ALTER TABLE prompt_overrides ADD CONSTRAINT prompt_overrides_payload_check CHECK (
    (kind IN ('prompts', 'instructions') AND content IS NOT NULL AND value_json IS NULL) OR
    (kind IN ('settings', 'guardrails')  AND value_json IS NOT NULL AND content IS NULL)
);

COMMENT ON COLUMN prompt_overrides.value_json IS
    'Structured override value for settings/guardrails kinds. Text kinds '
    '(prompts, instructions) use `content` instead. Design: '
    'docs/superpowers/specs/2026-05-31-config-matrix-db-overrides-design.md.';

COMMIT;
```

- [ ] **Step 3: Verify it applies against a scratch DB (dry-run, rolled back)**

Run:
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
\i orchestrator/database/migrations/app/0022_config_overrides_value_json.sql
-- the file COMMITs; re-open a txn to inspect then roll back the inspection
\d+ prompt_overrides
ROLLBACK;
SQL
```
Expected: `\d+` shows `value_json | jsonb`, `content` nullable, and both `prompt_overrides_kind_check` (4 kinds) + `prompt_overrides_payload_check`. (If applying to a shared DB, instead point `DATABASE_URL` at a scratch Postgres.)

- [ ] **Step 4: Commit**
```bash
git add orchestrator/database/migrations/app/0022_config_overrides_value_json.sql
git commit -m "feat(config-overrides): migration — value_json + widen kind on prompt_overrides"
```

---

### Task 2: Loader — generalize the override map + structured accessors

**Files:**
- Modify: `src/core/loader.py:34-80` (the override map + `set_prompt_overrides`/`clear_prompt_overrides`/`_db_lookup`)
- Test: `tests/test_prompt_overrides_loader.py`

- [ ] **Step 1: Write failing tests** (append to `tests/test_prompt_overrides_loader.py`)
```python
def test_settings_override_assembles_and_merges(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    loader.set_prompt_overrides([
        {"family": None, "kind": "settings", "name": "temperature", "value_json": 0.7},
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0},
        {"family": "gemma", "kind": "settings",
         "name": "limits.context_threshold_tokens", "value_json": 120000},
    ])
    # family wins over global; dotted names expand into nested dicts
    assert loader._settings_override_for("gemma") == {
        "temperature": 1.0,
        "limits": {"context_threshold_tokens": 120000},
    }
    assert loader._settings_override_for("other") == {"temperature": 0.7}  # global only


def test_guardrails_override_merges_global_and_family(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    loader.set_prompt_overrides([
        {"family": None, "kind": "guardrails", "name": "guardrails",
         "value_json": {"nudges": {"a": 1}}},
        {"family": "gemma", "kind": "guardrails", "name": "guardrails",
         "value_json": {"tool_examples": {"b": 2}}},
    ])
    assert loader._guardrails_override_for("gemma") == {
        "nudges": {"a": 1}, "tool_examples": {"b": 2},
    }


def test_structured_overrides_empty_when_flag_off(monkeypatch):
    _reset()
    monkeypatch.delenv("PROMPT_DB_OVERRIDES_ENABLED", raising=False)
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0},
    ])
    assert loader._settings_override_for("gemma") == {}
    assert loader._guardrails_override_for("gemma") == {}


def test_set_overrides_handles_jsonb_as_string(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    # asyncpg returns JSONB as a str unless a codec is set — loader must parse it
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": "1.0"},
    ])
    assert loader._settings_override_for("gemma") == {"temperature": 1.0}
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_prompt_overrides_loader.py -k "settings_override or guardrails_override or structured_overrides or jsonb_as_string" -v`
Expected: FAIL — `AttributeError: module 'src.core.loader' has no attribute '_settings_override_for'`.

- [ ] **Step 3: Implement** — replace `src/core/loader.py:34` (`_PROMPT_OVERRIDES = {}`) through `clear_prompt_overrides` (line 63) and add accessors after `_db_lookup`:
```python
_PROMPT_OVERRIDES: Dict[str, Dict[tuple, str]] = {}     # text kinds: (kind,name)->content
_VALUE_OVERRIDES: Dict[str, Dict[tuple, Any]] = {}      # structured kinds: (kind,name)->value


def set_prompt_overrides(rows: List[Dict[str, Any]]) -> None:
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
    global _PROMPT_OVERRIDES, _VALUE_OVERRIDES
    _PROMPT_OVERRIDES = text_map
    _VALUE_OVERRIDES = value_map


def clear_prompt_overrides() -> None:
    """Drop all process-local overrides (used between jobs and in tests)."""
    global _PROMPT_OVERRIDES, _VALUE_OVERRIDES
    _PROMPT_OVERRIDES = {}
    _VALUE_OVERRIDES = {}
```
Then add, immediately after `_db_lookup` (after line 80):
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
    if not _is_prompt_db_overrides_enabled():
        return {}

    def collect(fam: str) -> Dict[str, Any]:
        flat = {
            name: val
            for (kind, name), val in _VALUE_OVERRIDES.get(fam, {}).items()
            if kind == "settings"
        }
        return _expand_dotted(flat)

    return deep_merge(collect(""), collect(family))   # family wins


def _guardrails_override_for(family: str) -> Dict[str, Any]:
    """DB guardrails override ({tool_examples, nudges}) for <family> (global then
    family). {} when flag off or no rows."""
    if not _is_prompt_db_overrides_enabled():
        return {}

    def collect(fam: str) -> Dict[str, Any]:
        for (kind, name), val in _VALUE_OVERRIDES.get(fam, {}).items():
            if kind == "guardrails":
                return val if isinstance(val, dict) else {}
        return {}

    return deep_merge(collect(""), collect(family))
```
> Note: `deep_merge` is defined at `loader.py:88`; these accessors are defined after it at runtime (module-level functions resolve at call time, so definition order is fine).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: PASS (new tests + all existing prompt tests still green).

- [ ] **Step 5: Commit**
```bash
git add src/core/loader.py tests/test_prompt_overrides_loader.py
git commit -m "feat(config-overrides): loader override map + settings/guardrails accessors"
```

---

### Task 3: Loader — settings resolution consults the DB override

**Files:**
- Modify: `src/core/loader.py:414-436` (`resolve_model_settings`) and `:439-490` (`_apply_settings_matrix`)
- Test: `tests/test_prompt_overrides_loader.py`

- [ ] **Step 1: Write failing test**
```python
def test_resolve_model_settings_applies_override(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0},
    ])
    eff = loader.resolve_model_settings("google/gemma-4-31b")
    assert eff["temperature"] == 1.0
    base = loader.resolve_model_settings("google/gemma-4-31b", bundled_only=True)
    assert base["temperature"] != 1.0   # bundled bypasses the override
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py::test_resolve_model_settings_applies_override -v`
Expected: FAIL — `TypeError: resolve_model_settings() got an unexpected keyword argument 'bundled_only'`.

- [ ] **Step 3: Implement** — replace `resolve_model_settings` (lines 414-436):
```python
def resolve_model_settings(
    model: str, deployment_dir: str = None, *, bundled_only: bool = False
) -> Dict[str, Any]:
    """Resolve settings matrix values for a model (flat LLM keys, no 'limits').

    Applies the DB settings override (family > global) on top of the file
    matrix unless ``bundled_only`` is set (used by the admin "bundled default"
    view).
    """
    family = family_of(model)
    matrix = _load_settings_matrix(deployment_dir)
    default_settings = matrix.get("default", {})
    family_settings = matrix.get(family, {}) if family != "default" else {}
    settings = deep_merge(default_settings, family_settings)

    if not bundled_only:
        settings = deep_merge(settings, _settings_override_for(family))

    settings.pop("limits", None)
    return settings
```
Then in `_apply_settings_matrix`, after the file merge at line 465 (`settings = deep_merge(default_settings, family_settings)`), insert the override merge:
```python
    settings = deep_merge(default_settings, family_settings)
    settings = deep_merge(settings, _settings_override_for(family))   # DB delta (flag-gated, {} if off)
```
(The existing flat-key and `limits` application below now naturally carries override values, including dotted `limits.*` leaves expanded by `_settings_override_for`.)

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/core/loader.py tests/test_prompt_overrides_loader.py
git commit -m "feat(config-overrides): settings resolution honors DB overrides"
```

---

### Task 4: Loader — guardrails resolution consults the DB override

**Files:**
- Modify: `src/core/loader.py:391-411` (`resolve_guardrails`)
- Test: `tests/test_prompt_overrides_loader.py`

- [ ] **Step 1: Write failing test**
```python
def test_resolve_guardrails_applies_override(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "guardrails", "name": "guardrails",
         "value_json": {"nudges": {"extra": "be careful"}}},
    ])
    merged = loader.resolve_guardrails("google/gemma-4-31b")
    assert merged.get("nudges", {}).get("extra") == "be careful"
    base = loader.resolve_guardrails("google/gemma-4-31b", bundled_only=True)
    assert base.get("nudges", {}).get("extra") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py::test_resolve_guardrails_applies_override -v`
Expected: FAIL — unexpected kwarg `bundled_only`.

- [ ] **Step 3: Implement** — replace `resolve_guardrails` (lines 391-411):
```python
def resolve_guardrails(
    model: str, deployment_dir: Optional[str] = None, *, bundled_only: bool = False
) -> Dict[str, Any]:
    """Resolve the merged guardrails dict ({tool_examples, nudges}) for a model.

    Deep-merges family on default from the file matrix, then the DB guardrails
    override on top unless ``bundled_only`` is set.
    """
    family = family_of(model)
    matrix = _load_guardrails_matrix(deployment_dir)
    default_guardrails = matrix.get("default", {})
    family_guardrails = matrix.get(family, {}) if family != "default" else {}
    merged = deep_merge(default_guardrails, family_guardrails)
    if not bundled_only:
        merged = deep_merge(merged, _guardrails_override_for(family))
    return merged
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_prompt_overrides_loader.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/core/loader.py tests/test_prompt_overrides_loader.py
git commit -m "feat(config-overrides): guardrails resolution honors DB overrides"
```

---

### Task 5: Loader — `apply_settings_overrides(config)` delta for eager settings

Settings are baked into `config.llm`/`config.limits` at construction (before the agent loads overrides), so the agent needs a post-load delta step. This applies **only** the DB override keys, so it never clobbers non-overridden file/expert values.

**Files:**
- Modify: `src/core/loader.py` (add function near `_apply_settings_matrix`)
- Test: `tests/test_prompt_overrides_loader.py`

- [ ] **Step 1: Write failing test**
```python
def test_apply_settings_overrides_mutates_config(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import AgentConfig, LLMConfig, LimitsConfig, apply_settings_overrides

    cfg = AgentConfig(llm=LLMConfig(model="google/gemma-4-31b", temperature=0.0),
                      limits=LimitsConfig())
    loader.set_prompt_overrides([
        {"family": "gemma", "kind": "settings", "name": "temperature", "value_json": 1.0},
        {"family": "gemma", "kind": "settings",
         "name": "limits.context_threshold_tokens", "value_json": 123000},
    ])
    changed = apply_settings_overrides(cfg)
    assert changed is True
    assert cfg.llm.temperature == 1.0
    assert cfg.limits.context_threshold_tokens == 123000


def test_apply_settings_overrides_noop_without_rows(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import AgentConfig, LLMConfig, LimitsConfig, apply_settings_overrides

    cfg = AgentConfig(llm=LLMConfig(model="google/gemma-4-31b", temperature=0.3),
                      limits=LimitsConfig())
    assert apply_settings_overrides(cfg) is False
    assert cfg.llm.temperature == 0.3
```
> Verify `AgentConfig(llm=..., limits=...)` construction matches the dataclass (both are dataclasses with defaults at `loader.py:1209`/`:865`/`:1051`). If `AgentConfig` requires other fields, construct via the existing test helper or `AgentConfig()` then set `.llm`/`.limits`.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py -k apply_settings_overrides -v`
Expected: FAIL — `ImportError: cannot import name 'apply_settings_overrides'`.

- [ ] **Step 3: Implement** — add after `_apply_settings_matrix` (after line 490):
```python
def apply_settings_overrides(config: "AgentConfig") -> bool:
    """Apply ONLY the DB settings override on top of an already-resolved config,
    in place. File/expert settings are already baked in by load_agent_config; this
    writes just the DB delta, so it never clobbers non-overridden values.

    Call at job start after set_prompt_overrides(), before LLM (re)creation and
    the resolved_config freeze. Returns True if any value changed. No-op (False)
    when the override flag is off or no settings rows exist.

    `limits.*` leaves land on config.limits; all other keys on config.llm.
    """
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

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_prompt_overrides_loader.py -k apply_settings_overrides -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/core/loader.py tests/test_prompt_overrides_loader.py
git commit -m "feat(config-overrides): apply_settings_overrides delta for eager settings"
```

---

### Task 6: Agent — load all override kinds + apply settings delta before LLM creation/freeze

The current override-load sits at `src/agent.py:1052-1076`, **after** `_create_phase_llms()` at `:1049`. Settings affect LLM creation, so the load + settings delta must move **before** that block.

**Files:**
- Modify: `src/agent.py:1042-1089`

- [ ] **Step 1: Implement the reorder** — replace `src/agent.py:1042-1089` (from `config_dirty = bool(` through the freeze block) with:
```python
        config_dirty = bool(
            metadata.get("config_name")
            or metadata.get("config_upload_id")
            or metadata.get("config_override")
        )

        # Load DB config overrides (flag-gated; fail-open to bundled defaults).
        # Must run BEFORE _create_phase_llms() so settings overrides reach the
        # LLMs, and before the freeze so they're captured in resolved_config.
        # Prompts/instructions/guardrails resolve lazily from the process map;
        # settings are eager, so we apply them onto self.config here.
        if self.postgres_conn and not resume and not _config_from_db:
            from .core.loader import (
                set_prompt_overrides,
                apply_settings_overrides,
                _is_prompt_db_overrides_enabled,
            )

            if _is_prompt_db_overrides_enabled():
                try:
                    from .core.model_registry import family_of

                    _family = family_of(self.config.llm.model)
                    _rows = await self.postgres_conn.prompts.list_overrides_for_family(
                        _family
                    )
                    set_prompt_overrides(_rows)
                    if apply_settings_overrides(self.config):
                        config_dirty = True
                    logger.info(
                        f"Loaded {len(_rows)} config override(s) for family {_family}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to load config overrides (using bundled): {e}"
                    )

        if (not _config_from_db and config_dirty) or _config_from_db:
            logger.info("Config changed for this job — recreating LLMs")
            self._create_phase_llms()

        # Freeze resolved config on first run (not resume). Prompts/instructions
        # are re-resolved here through the override-aware resolvers; settings are
        # already applied to self.config above.
        if self.postgres_conn and not resume and not _config_from_db:
            try:
                import uuid as _uuid

                from .core.loader import serialize_resolved_config

                resolved = serialize_resolved_config(
                    self.config, model=self.config.llm.model
                )
                await self.postgres_conn.jobs.store_resolved_config(
                    _uuid.UUID(job_id), resolved
                )
                logger.info(f"Froze resolved config for job {job_id}")
            except Exception as e:
                logger.warning(f"Failed to freeze resolved config: {e}")
```

- [ ] **Step 2: Sanity-check the module imports/compiles**

Run: `python -c "import ast; ast.parse(open('src/agent.py').read())"`
Expected: no output (syntax OK). Full behavior is covered by the loader tests (Task 5) + manual verification in Task 11.

- [ ] **Step 3: Commit**
```bash
git add src/agent.py
git commit -m "feat(config-overrides): agent loads all kinds + applies settings before LLM creation"
```

---

### Task 7: Agent-side DB read — return `value_json`

**Files:**
- Modify: `src/database/postgres_db.py:799-806` (`list_overrides_for_family` SELECT)
- Test: `tests/test_prompt_overrides_loader.py:108-142` (the namespace test)

- [ ] **Step 1: Update the failing-on-assert test** — in `test_prompts_namespace_lists_family_and_global`, add `value_json` to the mocked row and the expected SELECT columns:
```python
    fake_db.fetch = AsyncMock(
        return_value=[
            {
                "family": "gemma", "kind": "prompts", "name": "persona",
                "content": "X", "content_format": "text", "value_json": None,
            },
        ]
    )
    ...
    sql = fake_db.fetch.call_args.args[0]
    assert "value_json" in sql
```
(Update the `assert rows == [...]` expected dict to include `"value_json": None`.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_prompt_overrides_loader.py::test_prompts_namespace_lists_family_and_global -v`
Expected: FAIL — `assert "value_json" in sql`.

- [ ] **Step 3: Implement** — update the SELECT in `list_overrides_for_family` (line 801):
```python
        rows = await self.db.fetch(
            """
            SELECT family, kind, name, content, content_format, value_json
            FROM prompt_overrides
            WHERE family = $1 OR family IS NULL
            """,
            family,
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_prompt_overrides_loader.py::test_prompts_namespace_lists_family_and_global -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/database/postgres_db.py tests/test_prompt_overrides_loader.py
git commit -m "feat(config-overrides): agent read path returns value_json"
```

---

### Task 8: Orchestrator DB — `upsert_prompt_override` handles `value_json` + nullable content

**Files:**
- Modify: `orchestrator/database/postgres.py:4994-5034` (`upsert_prompt_override`)
- Test: `tests/test_admin_prompts_api.py` (the mocked-pool test)

- [ ] **Step 1: Write failing test** (append to `tests/test_admin_prompts_api.py`)
```python
@pytest.mark.asyncio
async def test_upsert_settings_override_persists_value_json():
    from unittest.mock import AsyncMock
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(return_value={"id": "x", "kind": "settings",
                                          "name": "temperature", "value_json": 1.0})
    row = await db.upsert_prompt_override(
        family="gemma", kind="settings", name="temperature",
        content=None, content_format=None, value_json=1.0, notes=None, user_id=None,
    )
    sql = db.fetchrow.call_args.args[0]
    assert "value_json" in sql and "ON CONFLICT" in sql
    assert row["value_json"] == 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_admin_prompts_api.py::test_upsert_settings_override_persists_value_json -v`
Expected: FAIL — `TypeError: upsert_prompt_override() got an unexpected keyword argument 'value_json'`.

- [ ] **Step 3: Implement** — replace `upsert_prompt_override` signature/body (lines 4994-5034):
```python
    async def upsert_prompt_override(
        self,
        *,
        family: str | None,
        kind: str,
        name: str,
        content: str | None = None,
        content_format: str | None = "text",
        value_json: Any = None,
        notes: str | None = None,
        user_id: Any = None,
    ) -> Dict[str, Any]:
        """Create or replace the override for (family, kind, name).

        Text kinds (prompts, instructions) pass `content`; structured kinds
        (settings, guardrails) pass `value_json`. Conflict target is the
        `uq_prompt_override` expression index (COALESCE(family,''), kind, name).
        """
        import json as _json

        vj = _json.dumps(value_json) if value_json is not None else None
        row = await self.fetchrow(
            """
            INSERT INTO prompt_overrides
                (family, kind, name, content, content_format, value_json, notes,
                 created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $8)
            ON CONFLICT (COALESCE(family, ''), kind, name) DO UPDATE
            SET content = EXCLUDED.content,
                content_format = EXCLUDED.content_format,
                value_json = EXCLUDED.value_json,
                notes = EXCLUDED.notes,
                updated_by = EXCLUDED.updated_by,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *
            """,
            family, kind, name, content, content_format, vj, notes,
            UUID(str(user_id)) if user_id else None,
        )
        return dict(row)
```
> `value_json` is serialized with `json.dumps` and cast `$6::jsonb` so it works whether or not a JSONB codec is registered on the pool. `Any` is already imported in this module (used elsewhere in the signatures above).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_admin_prompts_api.py::test_upsert_settings_override_persists_value_json tests/test_admin_prompts_api.py::test_upsert_prompt_override_uses_on_conflict -v`
Expected: PASS (the original prompt upsert test still passes — `content` defaults preserved).

- [ ] **Step 5: Commit**
```bash
git add orchestrator/database/postgres.py tests/test_admin_prompts_api.py
git commit -m "feat(config-overrides): orchestrator upsert handles value_json"
```

---

### Task 9: API — Pydantic + routes + bundled + validation for structured kinds

**Files:**
- Modify: `orchestrator/main.py:2871-2891` (Pydantic), `:15471-15521` (bundled reader + create route)
- Test: `tests/test_admin_prompts_api.py`

- [ ] **Step 1: Write failing tests** (append to `tests/test_admin_prompts_api.py`)
```python
def test_settings_override_create_model_validates():
    m = _import_main()
    Create = m.PromptOverrideCreate
    ok = Create(family="gemma", kind="settings", name="temperature", value_json=1.0)
    assert ok.value_json == 1.0 and ok.content is None
    # text kind still requires content
    with pytest.raises(Exception):
        Create(family="gemma", kind="prompts", name="persona")  # no content
    # structured kind requires value_json
    with pytest.raises(Exception):
        Create(family="gemma", kind="settings", name="temperature")  # no value_json


def test_validate_override_value_checks_catalog():
    m = _import_main()
    # temperature is type=number, max 2.0 in the catalog (Task 10)
    with pytest.raises(Exception):
        m.validate_override_value("settings", "temperature", 9.0)   # out of bounds
    m.validate_override_value("settings", "temperature", 1.0)       # ok (no raise)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_admin_prompts_api.py -k "settings_override_create or validate_override_value" -v`
Expected: FAIL — `value_json` not a field / `validate_override_value` missing.

- [ ] **Step 3: Implement** — replace the Pydantic models (lines 2871-2891):
```python
class PromptOverrideCreate(BaseModel):
    """Create/replace a config override. Text kinds (prompts, instructions) use
    `content`; structured kinds (settings, guardrails) use `value_json`."""

    family: str | None = Field(None, max_length=64)
    kind: Literal["prompts", "instructions", "settings", "guardrails"]
    name: str = Field(..., min_length=1, max_length=128)
    content: str | None = Field(None, min_length=1)
    content_format: Literal["text", "markdown", "jinja", "yaml"] | None = "text"
    value_json: Any = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_payload(self):
        text = self.kind in ("prompts", "instructions")
        if text and self.content is None:
            raise ValueError(f"kind={self.kind} requires `content`")
        if not text and self.value_json is None:
            raise ValueError(f"kind={self.kind} requires `value_json`")
        return self


class PromptOverrideUpdate(BaseModel):
    """Update an existing override's payload; family/kind/name are immutable."""

    content: str | None = Field(None, min_length=1)
    content_format: Literal["text", "markdown", "jinja", "yaml"] | None = "text"
    value_json: Any = None
    notes: str | None = None
```
Ensure `model_validator` and `Any` are imported at the top of `main.py` (add to the existing `from pydantic import ...` and `from typing import ...` lines if missing).

Add the catalog validator near `_prompt_catalog_entry` (after line 15468):
```python
def validate_override_value(kind: str, name: str, value: Any) -> None:
    """Validate a structured override value against its catalog entry.

    Raises HTTPException(422) on type/bounds/unknown-key violation. Text kinds
    and uncatalogued keys are accepted (fail-open is for *reads*; writes of
    catalogued settings are checked)."""
    if kind not in ("settings", "guardrails"):
        return
    entry = _prompt_catalog_entry(kind, name)
    if entry is None:
        raise HTTPException(status_code=422, detail=f"unknown {kind} key: {name!r}")
    t = entry.get("type")
    if t in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"{name} must be a number")
        if t == "integer" and not float(value).is_integer():
            raise HTTPException(status_code=422, detail=f"{name} must be an integer")
        if "min" in entry and value < entry["min"]:
            raise HTTPException(status_code=422, detail=f"{name} < min {entry['min']}")
        if "max" in entry and value > entry["max"]:
            raise HTTPException(status_code=422, detail=f"{name} > max {entry['max']}")
    elif t == "boolean" and not isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{name} must be a boolean")
    elif t == "json" and not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{name} must be an object")
```
Update the create route (lines 15507-15521) and update route to pass `value_json` and validate:
```python
@app.post("/api/admin/prompts/overrides")
async def admin_create_prompt_override(
    request: Request, body: PromptOverrideCreate
) -> dict[str, Any]:
    """Create or replace the override for (family, kind, name)."""
    user = await _require_admin(request)
    validate_override_value(body.kind, body.name, body.value_json)
    return await postgres_db.upsert_prompt_override(
        family=body.family, kind=body.kind, name=body.name,
        content=body.content, content_format=body.content_format,
        value_json=body.value_json, notes=body.notes, user_id=user.get("id"),
    )
```
Apply the same `value_json=` + `validate_override_value(existing["kind"], existing["name"], body.value_json)` additions to `admin_update_prompt_override` (lines 15524-15541).

Finally extend `read_bundled_prompt` (lines 15471-15485) so the bundled endpoint serves settings/guardrails defaults too:
```python
def read_bundled_prompt(kind: str, family: str | None, name: str):
    """Bundled (file) default for (kind, family, name), bypassing overrides.
    Text kinds return a string; settings/guardrails return the file-resolved value."""
    from src.core.loader import (
        InstructionMatrixResolver, PromptMatrixResolver,
        resolve_model_settings, resolve_guardrails,
    )
    if kind in ("prompts", "instructions"):
        resolver_cls = {"prompts": PromptMatrixResolver,
                        "instructions": InstructionMatrixResolver}[kind]
        resolver = resolver_cls(None, family or "default")
        try:
            return resolver.load(name, bundled_only=True)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="no bundled default for that key")
    # structured kinds: resolve from the file matrix, bypassing DB overrides
    model = f"__family__/{family}" if family else ""   # family_of() maps unknown -> default; see note
    if kind == "settings":
        return resolve_model_settings(model, bundled_only=True).get(name)
    if kind == "guardrails":
        return resolve_guardrails(model, bundled_only=True)
    raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
```
> Note: the bundled endpoint keys settings/guardrails by *family*, but `resolve_*` take a *model*. Resolve the family directly instead of faking a model — replace the `model = ...` line with a small helper that calls `_load_settings_matrix`/`_load_guardrails_matrix` and picks `default ⊕ family`. (Implementer: prefer adding `resolve_*_for_family(family, bundled_only=True)` thin wrappers in `loader.py` to avoid the model round-trip; covered by reusing Task 3/4 internals.) Keep the response shape `{family, kind, name, content/value, catalog}`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_admin_prompts_api.py -v`
Expected: PASS (new + existing route/model tests).

- [ ] **Step 5: Commit**
```bash
git add orchestrator/main.py tests/test_admin_prompts_api.py
git commit -m "feat(config-overrides): admin API + validation for settings/guardrails kinds"
```

---

### Task 10: Catalog — settings + guardrails entries

**Files:**
- Modify: `config/prompts/catalog.yaml`
- Test: `tests/test_admin_prompts_api.py:113-122`

- [ ] **Step 1: Write failing test** (append)
```python
def test_catalog_has_settings_and_guardrails_keys():
    from pathlib import Path
    import yaml
    path = Path(__file__).parent.parent / "config" / "prompts" / "catalog.yaml"
    entries = yaml.safe_load(path.read_text())
    by_key = {(e["kind"], e["name"]): e for e in entries}
    assert ("settings", "temperature") in by_key
    assert by_key[("settings", "temperature")]["type"] == "number"
    assert ("guardrails", "guardrails") in by_key
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_admin_prompts_api.py::test_catalog_has_settings_and_guardrails_keys -v`
Expected: FAIL — keys absent.

- [ ] **Step 3: Implement** — append to `config/prompts/catalog.yaml` (the editable `settings` leaves map to `config/model_config_matrix.yaml` keys):
```yaml
# --- settings (LLM inference params + limits) -------------------------------
- kind: settings
  name: temperature
  type: number
  min: 0.0
  max: 2.0
  title: "Sampling temperature"
  description: "0 = deterministic. Reasoning models often want ~1.0."
- kind: settings
  name: top_p
  type: number
  min: 0.0
  max: 1.0
  title: "Top-p (nucleus sampling)"
  description: "Nucleus sampling cutoff. Leave unset for model default."
- kind: settings
  name: parallel_tool_calls
  type: boolean
  title: "Allow parallel tool calls"
  description: "Whether the model may emit multiple tool calls per turn."
- kind: settings
  name: model_max_context_tokens
  type: integer
  min: 1000
  title: "Model context window (tokens)"
  description: "Per-model context window the agent assumes."
- kind: settings
  name: limits.context_threshold_tokens
  type: integer
  min: 1000
  title: "Context threshold (tokens)"
  description: "When to trigger summarization/compaction."
- kind: settings
  name: limits.summarization_safe_limit
  type: integer
  min: 1000
  title: "Summarization safe limit (tokens)"
  description: "Upper bound kept safe during summarization."
# --- guardrails -------------------------------------------------------------
- kind: guardrails
  name: guardrails
  type: json
  title: "Guardrails (tool examples + nudges)"
  description: "Merged onto the family's bundled guardrails. Shape: {tool_examples, nudges}."
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_admin_prompts_api.py::test_catalog_has_settings_and_guardrails_keys tests/test_admin_prompts_api.py::test_prompt_catalog_yaml_has_core_keys -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add config/prompts/catalog.yaml tests/test_admin_prompts_api.py
git commit -m "feat(config-overrides): catalog entries for settings + guardrails"
```

---

### Task 11: Full suite + manual end-to-end verification

- [ ] **Step 1: Run the full affected suite**

Run: `pytest tests/test_prompt_overrides_loader.py tests/test_admin_prompts_api.py -v`
Expected: all PASS. (CI on Py3.12 is the gate; locally some `main` imports may skip per the file header.)

- [ ] **Step 2: Lint**

Run: `ruff check src/core/loader.py src/agent.py src/database/postgres_db.py orchestrator/database/postgres.py orchestrator/main.py`
Expected: no errors. (The push workflow also auto-runs ruff.)

- [ ] **Step 3: Manual end-to-end on dev** (flag is already ON in `deployment/values-experimental.yaml`)

After the migration applies and images roll out, insert a settings override and confirm a *new* job picks it up:
```bash
# via the admin API (srw-admin session) or directly:
psql "$DATABASE_URL" -c "INSERT INTO prompt_overrides (family, kind, name, value_json)
  VALUES ('gemma','settings','temperature','1.0'::jsonb)
  ON CONFLICT (COALESCE(family,''), kind, name) DO UPDATE SET value_json = EXCLUDED.value_json;"
```
Dispatch a gemma-family job and confirm `jobs.resolved_config -> llm.temperature == 1.0`:
```bash
psql "$DATABASE_URL" -c "SELECT resolved_config->'llm'->>'temperature' FROM jobs ORDER BY created_at DESC LIMIT 1;"
```
Expected: `1.0`. Delete the row → next job reverts to the file default (0.0).

- [ ] **Step 4: Final commit (if any lint/cleanup)**
```bash
git add -A && git commit -m "chore(config-overrides): lint + verification cleanup"
```

---

## Self-review

**Spec coverage:** §4 data model → Task 1. §5 resolution/freeze (prompts unchanged; settings via `_apply_settings_matrix`+delta; guardrails via `resolve_guardrails`; load-once via `set_prompt_overrides`) → Tasks 2-7. §6 coherence (read fresh per job, file cache kept) → Tasks 6-7 (no long-lived DB cache added). §7 API → Tasks 8-9. §8 catalog + validation → Tasks 9-10. §10 back-compat (table/route/flag kept) → honored throughout. Reproducibility freeze → Task 6. **Gap accepted:** guardrails are not added to `serialize_resolved_config()` (resolved lazily from the once-per-job process map — effectively frozen per process); noted in spec §5.

**Placeholders:** Task 9's bundled-endpoint family→model note is the one soft spot — it gives a working fallback plus the preferred `resolve_*_for_family` wrapper to add. All other steps carry concrete code/commands.

**Type consistency:** `set_prompt_overrides`/`clear_prompt_overrides`/`_settings_override_for`/`_guardrails_override_for`/`apply_settings_overrides`/`validate_override_value`/`value_json` are used consistently across loader, agent, DB, and API tasks. `resolve_model_settings`/`resolve_guardrails` gain the same `bundled_only` kwarg in Tasks 3/4 and are called that way in Task 9.

## Out of scope (separate plans)
- **Cockpit UI** (`2026-05-31-config-matrix-db-overrides-cockpit-ui.md`): typed editors for settings, JSON editor for guardrails, on the existing `/admin/prompts` page.
- **Rename `prompt_* → config_*`** (optional mechanical refactor).
- **Per-expert overrides** (own feature).
