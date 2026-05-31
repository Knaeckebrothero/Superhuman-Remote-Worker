# DB-Backed Config Matrix Overrides — Design

**Status:** v1 designed, not yet implemented.
**Date:** 2026-05-31
**Builds on:** [`docs/features/prompt_editing_page.md`](../../features/prompt_editing_page.md) and [`docs/superpowers/plans/2026-05-26-prompt-overrides-v1.md`](../plans/2026-05-26-prompt-overrides-v1.md) (the shipped prompt-overrides feature this generalizes).

> **TL;DR** — The prompt-overrides feature already lets admins override prompt/instruction *content* in Postgres, with the bundled file as an immutable floor. This extends the same "thin DB delta on a baked-file floor" model to the **rest of the model-config matrix** — `settings` (LLM inference params + `limits`) and `guardrails` — so every config value an operator might tune lives in the DB, not in the image. The motivation is **horizontal scalability / HA**: N stateless orchestrator replicas converge on edited config from shared Postgres with no redeploy and no writable volume. We do **not** move the whole matrix into the DB and we do **not** add a per-expert dimension (separate future feature).

---

## 1. Motivation & goals

Today a config change (a temperature, a context-window limit, a guardrail) requires editing `config/model_config_matrix.yaml`, rebuilding the image, and redeploying. That is fine for one orchestrator; it does not scale to "20+ orchestrators managing lots of users." The prompt-overrides feature already proved the pattern that fixes this for prompts. This work finishes the job for the remaining matrix subsections.

**Goals**

1. `settings` and `guardrails` become DB-overridable per model family, exactly the way `prompts`/`instructions` already are.
2. The orchestrator/agent need **no writable volume** for config and **no redeploy** to apply a config change; all replicas read the same edited values from Postgres.
3. The bundled YAML remains the **permanent immutable floor and fallback** — if the DB has no row (or is unreachable, or someone deletes a value), resolution still completes from the file.
4. Reuse the existing flag, resolution hook, per-job freeze, admin API, and Cockpit page rather than inventing parallel machinery.

**Non-goals (explicitly deferred)**

- **Per-expert overrides.** The matrix has expert overlays (`config/experts/{critic,developer,scholar,designer}/model_config_matrix.yaml`); overrides stay **family-only**, as `prompt_overrides` is today. Per-expert editing is a separate feature.
- **Moving the whole matrix into the DB** / making the DB the structural source of truth (add new families/keys, prompt reuse by ID). See §4 for why this is rejected.
- **Reconcile-on-redeploy** (the `source`/`helm_value_hash` machinery designed in `docs/features/helm_managed_settings.md`). The override-on-floor model does not need it (§4).
- **Live in-flight updates** to running jobs. Edits apply to the *next* job, matching prompt-overrides semantics.

---

## 2. Background: how the matrix resolves today

`config/model_config_matrix.yaml` is a per-family map with four subsections, resolved by `src/core/loader.py`. Two of them work by *pointer-to-file*, two by *inline value*:

| Subsection | Matrix holds | Real value lives in | Resolver | DB override today? |
|---|---|---|---|---|
| `prompts` | filename pointer | `config/prompts/*.txt` | `PromptMatrixResolver.load()` | **Yes** (content) |
| `instructions` | filename pointer | `config/templates/*` | `InstructionMatrixResolver.load()` | **Yes** (content; not surfaced in UI) |
| `settings` | inline dict | the matrix itself | `_apply_settings_matrix()` / `resolve_model_settings()` | **No** |
| `guardrails` | `{file: x.yaml}` pointer | `config/guardrails/*.yaml` → `{tool_examples, nudges}` | `resolve_guardrails()` / `_load_guardrails_matrix()` | **No** |

- **Prompts/instructions** get DB overrides via `_db_lookup(subsection, family, name)` inside `MatrixResolver.load()` (`src/core/loader.py:744`): a DB row short-circuits the file read. Override content lives in the `prompt_overrides` table (migration `0021_prompt_overrides.sql`), keyed `(family, kind, name)` with `NULL` family = global. Per-family rows are loaded once at job start in the agent process (`PromptsNamespace.list_overrides_for_family()` → `set_prompt_overrides()` in `src/agent.py`), then everything is frozen into `jobs.resolved_config` by `serialize_resolved_config()`.
- **Settings** are deep-merged (`default` ⊕ family) and written into `config.llm` / `config.limits` by `_apply_settings_matrix()` during config load. **No DB consultation.**
- **Guardrails** dereference the file pointer and deep-merge (`default` ⊕ family) into `{tool_examples, nudges}` via `resolve_guardrails()`, consumed by `src/services/guardrails.py`. **No DB consultation.**

Reproducibility is already solved for settings (frozen as concrete `llm`/`limits` values in `resolved_config`) and prompts/instructions (frozen as full text). The flag `PROMPT_DB_OVERRIDES_ENABLED` gates the agent's resolver only; the admin API/UI store overrides regardless.

---

## 3. Architecture: thin DB delta on a baked-file floor

Extend the *existing* override model to all four subsections. The DB stores **only deltas**; the baked YAML is the always-present base.

**Resolution precedence (per leaf), for every subsection:**

```
DB override (family-specific)  >  DB override (global / NULL family)  >  bundled file
```

This is already what `_db_lookup` does for prompts. We add the equivalent merge for `settings` and `guardrails`.

**Why this shape (and not the full-matrix-in-DB sketch we discussed):**

| Requirement | Override-on-floor (chosen) | Whole-matrix-in-DB (rejected) |
|---|---|---|
| Stateless / no config volume | ✓ file image-baked, DB shared | ✓ |
| Edits in DB, all replicas converge | ✓ read fresh per job | ✓ |
| File is fallback if a row is missing/deleted | ✓ **inherent** — no row → file default | ⚠ must keep a *full duplicate* matrix in the file |
| New shipped default reaches un-edited families | ✓ **automatic** (file is always the base) | ✗ needs unbuilt reconcile, else silently ignored |
| Survives DB-level mishaps (bad migration, accidental delete, restore) | ✓ floor is in the image — a **different failure domain** | ✗ floor shares the DB's failure domain |
| "Just like prompts" / reuse flag+freeze+API+UI | ✓ same mechanism | ✗ migrates the working table |
| **No seed step required** | ✓ empty table == today's behavior | ✗ must seed DB from file on first boot |

The decisive reason is **failure-domain isolation**: a fallback is only worth anything if it survives whatever broke the primary, so the floor must not live in the same database as the edits. A bonus is that override-on-floor needs **no seeding job and no reconcile machinery** — the empty-override state is exactly today's bundled behavior, and a newly shipped file default automatically reaches every family that has not been explicitly overridden. The only capability we give up — adding brand-new families/keys or reusing one prompt across many from the UI — is out of scope (and new *models* are already handled by dynamic provider discovery for the model catalog).

---

## 4. Data model

Generalize the existing table from "prompt overrides" to "config overrides." It already carries everything we need except a place for structured (non-text) values.

**Recommended: rename + extend `prompt_overrides` → `config_overrides`** (next migration, e.g. `0022_config_overrides.sql`):

```sql
ALTER TABLE prompt_overrides RENAME TO config_overrides;

-- structured values for settings/guardrails (text stays in `content` for prompts/instructions)
ALTER TABLE config_overrides ADD COLUMN value_json JSONB;

-- content_format is meaningless for structured kinds → allow NULL
ALTER TABLE config_overrides ALTER COLUMN content_format DROP NOT NULL;

-- widen the kind domain: drop the CHECK created in 0021 (an unnamed CHECK is
-- auto-named `prompt_overrides_kind_check` and is NOT renamed by RENAME TABLE;
-- verify the actual name), then re-add the wider one.
ALTER TABLE config_overrides DROP CONSTRAINT prompt_overrides_kind_check;
ALTER TABLE config_overrides ADD CONSTRAINT config_overrides_kind_check
  CHECK (kind IN ('prompts', 'instructions', 'settings', 'guardrails'));

-- exactly one of content / value_json is populated, depending on kind
ALTER TABLE config_overrides ADD CONSTRAINT config_overrides_payload_check CHECK (
  (kind IN ('prompts','instructions') AND content IS NOT NULL AND value_json IS NULL) OR
  (kind IN ('settings','guardrails')  AND value_json IS NOT NULL AND content IS NULL)
);
```

Unchanged: `id`, `family` (`NULL` = global), `name`, `content_format`, `notes`, `created_by`/`updated_by`, `created_at`/`updated_at`, and the unique index on `(COALESCE(family,''), kind, name)`. **No `expert` column** — the future per-expert feature adds it and widens the unique index then; nothing here paints us into a corner.

**Keys per kind:**

- `settings`: `name` identifies the leaf — e.g. `temperature`, `parallel_tool_calls`, `model_max_context_tokens`, or a dotted path into `limits` (`limits.context_threshold_tokens`). `value_json` holds the typed value. *(Alternative: one row per `(family, 'settings')` whose `value_json` is the whole settings sub-dict. Per-leaf is recommended — finer-grained edits, smaller diffs, simpler validation, mirrors prompts' per-name granularity.)*
- `guardrails`: one row per `(family, 'guardrails')`; `value_json` is the `{tool_examples, nudges}` document (deep-merged onto the file default).

(If renaming the live dev table is judged not worth the churn, the fallback is to keep the name `prompt_overrides` and add `value_json` + widen `kind` in place. See §10 decision.)

---

## 5. Resolution & freeze changes

**Generalize the load-once machinery.** `list_overrides_for_family(family)` already returns *all* rows for the family + globals in one query; today only `prompts`/`instructions` are routed. Rename `set_prompt_overrides()` → `set_config_overrides()` (keep a thin alias) and bucket rows by kind into the process-local map. Still one indexed `SELECT` at job start.

**Per subsection:**

- **prompts / instructions** — unchanged (`_db_lookup` in `MatrixResolver.load()`).
- **settings** — `set_config_overrides()` first assembles the family's per-leaf `settings` rows into a nested dict (dotted `name`s such as `limits.context_threshold_tokens` expand into `{limits: {context_threshold_tokens: …}}`), producing `db_global` / `db_family` dicts. Then in `_apply_settings_matrix()` and `resolve_model_settings()`, after building the file-resolved `settings = deep_merge(default, family)`, deep-merge the DB dicts on top: `deep_merge(file_resolved, db_global)` then `deep_merge(·, db_family)`. This yields the §3 precedence and supports both flat keys and dotted `limits.*` leaves.
- **guardrails** — in `resolve_guardrails()`, after the file-resolved `{tool_examples, nudges}`, deep-merge the DB `guardrails` override (global then family).

**Freeze.** `serialize_resolved_config()` already freezes settings (concrete `llm`/`limits`) and prompt/instruction text. Confirm guardrails are loaded once at job start (via the same `set_config_overrides` set) so an in-flight guardrails edit cannot change behavior mid-job — consistent with the frozen prompts/settings. Resumed jobs read the frozen copy; no change to resume.

**Fail-open everywhere.** Any DB error, missing row, or malformed override → fall through to the file (log a warning). This is the existing prompt behavior and the whole point of the floor.

---

## 6. Coherence / HA

- **Read the DB layer fresh, per job.** Overrides are fetched at job start in the **ephemeral per-job agent process** (one `SELECT`), so every job sees the latest edit and all N replicas are coherent with **zero invalidation machinery**. There is no long-lived cache of the DB layer to go stale.
- **Keep caching the file layer.** `_model_config_matrix_cache` / guardrails-file cache parse the baked YAML once per process; the file only changes on image rebuild → pod restart → fresh process. Leave as-is.
- **Orchestrator admin reads are uncached**, following the model-catalog rule (`/api/models` queries Postgres fresh; `reload_model_catalog()` is a deliberate no-op). Do **not** introduce a process-wide cache of the DB override layer in any long-lived service. *(If a future hot path needs it — e.g. the orchestrator resolving `settings` for its own auxiliary LLM calls — the escape hatch is a short TTL or a NATS "config-changed" broadcast; not needed for v1.)*
- **No seeding job.** Empty `config_overrides` == today's bundled behavior; the file is the live base, not a one-time seed source.

---

## 7. Admin API

Extend the existing admin surface (rename `/api/admin/prompts/*` → `/api/admin/config/*`, see §10) to cover all kinds. All routes stay `srw-admin`-gated.

- `GET    /api/admin/config/overrides` — list all override rows.
- `GET    /api/admin/config/overrides/{id}` — fetch one.
- `POST   /api/admin/config/overrides` — upsert (ON CONFLICT on `(COALESCE(family,''), kind, name)`).
- `PUT    /api/admin/config/overrides/{id}` — update payload/notes (family/kind/name immutable).
- `DELETE /api/admin/config/overrides/{id}` — delete row (reverts that leaf to the file default).
- `GET    /api/admin/config/catalog` — editable keys + metadata.
- `GET    /api/admin/config/bundled/{family}/{kind}/{name}` — the file-resolved default for side-by-side display (`bundled_only=True` for prompts; file-resolved `settings`/`guardrails` value for the structured kinds).

The create/update model gains `value_json` and the widened `kind`. **Validation** (see §8) runs server-side on POST/PUT.

---

## 8. Catalog & validation

The catalog (today `config/prompts/catalog.yaml`, 5 prompt entries) grows to describe the new editable keys so the UI can render correct editors and the API can validate. Settings entries carry a **type** (and optional bounds/enum):

```yaml
- kind: settings
  name: temperature
  type: number          # number | integer | boolean | enum
  min: 0.0
  max: 2.0
  title: "Sampling temperature"
  description: "0 = deterministic. Reasoning models often want ~1.0."
- kind: settings
  name: limits.context_threshold_tokens
  type: integer
  title: "Context threshold (tokens)"
  description: "When to trigger summarization/compaction."
- kind: guardrails
  name: guardrails
  type: json
  title: "Guardrails (tool examples + nudges)"
  description: "Merged onto the bundled default for the family."
```

**Validation rules (server-side, fail-closed on write / fail-open on read):**

- `settings` values must match the catalog `type` and bounds; unknown `name`s are rejected.
- `guardrails`/`json` values must parse and (optionally) match a shape (`tool_examples`/`nudges` are dicts).
- A write is rejected with a clear error; a *stored-but-somehow-invalid* value at read time is ignored in favor of the file (defensive — bad config must never break an agent).

---

## 9. Cockpit UI

Extend the existing admin page (`cockpit/src/app/views/admin/prompts/` + `admin-prompts.service.ts`), renamed to `admin/config`:

- Family picker + a kind/key selector spanning all four kinds (grouped: Prompts, Instructions, Settings, Guardrails).
- **Prompts/instructions:** existing two-column editor (read-only bundled default | override textarea).
- **Settings:** catalog-driven **typed inputs** (number/stepper, toggle, enum dropdown) with the bundled default shown alongside; bounds enforced client-side, re-validated server-side.
- **Guardrails:** JSON/YAML editor with parse validation; bundled default shown read-only.
- "Reset to default" = DELETE the row. "Override active" badges as today.

UI depth is intentionally minimal for v1, matching the prompt editor's altitude; richer UX is deferrable.

---

## 10. Migration, flag & back-compat

- **Full rename now (locked).** The feature is **dev-only** (flag `false` in `helm/values.yaml`, `true` only in `deployment/values-experimental.yaml`) with low/no production data, so we rename in one push: `prompt_overrides` → `config_overrides`, `/api/admin/prompts/*` → `/api/admin/config/*`, `PROMPT_DB_OVERRIDES_ENABLED` → `CONFIG_DB_OVERRIDES_ENABLED` (helm value `configDbOverridesEnabled`), and the Cockpit `/admin/config` page. No deprecated aliases. **The table rename happens in a new migration `0022` (`ALTER TABLE … RENAME`)** — the already-applied `0021` file is never edited (the migration runner checksums it).
- **Back-compat:** existing prompt/instruction override rows are untouched by the rename + column add. Resume path unchanged. Prod stays off.
- **Data path correctness:** depends on the orchestrator's `get_project_root()` marker fix already in `docker/Dockerfile.orchestrator:69-70` (the bundled/file reads rely on it).

---

## 11. Testing

- **Loader unit tests** (extend `tests/test_prompt_overrides_loader.py`): settings DB-merge precedence (DB-family > DB-global > file), incl. dotted `limits.*`; guardrails DB-merge precedence; flag-off → file only; malformed override → fail-open to file; `set_config_overrides` buckets all four kinds from one row set.
- **API tests** (extend `tests/test_admin_prompts_api.py`): upsert/CRUD for `settings`/`guardrails`, `value_json` round-trip, catalog-type validation (reject bad type/out-of-bounds/unknown name), route registration under the new path, `srw-admin` gating.
- **Freeze test:** a `settings` and a `guardrails` override are captured in `resolved_config` at job start and survive resume.
- **Migration test:** `prompt_overrides` data survives the rename + column add; constraints enforce the content/value_json XOR.

---

## 12. Decisions (locked with the user, 2026-05-31)

1. **Full rename now** (`prompt_* → config_*`): table, `/api/admin/config/*`, `CONFIG_DB_OVERRIDES_ENABLED`, loader/DB/Pydantic identifiers, and the Cockpit page (`/admin/config`). No deprecated aliases — one push, verified on the k3d-local cluster.
2. **Per-leaf settings rows** (`name` = `temperature`, `limits.context_threshold_tokens`, …); `value_json` holds the typed value.
3. **Basic UI now**: rename the existing admin page to `/admin/config` and add minimal `value_json` editing for settings/guardrails (the picker is already catalog-driven).
4. Implementation plan: `docs/superpowers/plans/2026-05-31-config-matrix-db-overrides.md` (Phases A rename → B backend → C UI → D k3d).

## 13. Deferred / roadmap

Per-expert overrides (own feature) · structural/whole-matrix-in-DB editing · reconcile-on-redeploy (`helm_managed_settings.md`) · live in-flight updates · richer Cockpit UX · edit-history audit log beyond `created_by`/`updated_by`.
