# DB-backed Prompt Overrides + Cockpit Editor

Move every model-facing prompt that today lives in `config/` into a layered
resolution chain: **bundled files (immutable, ship-with-image defaults)** at
the bottom, **DB-stored overrides** on top, edited (eventually) from a Cockpit
page. Bundled defaults are refreshed on every redeploy and never clobber
operator edits — they are the floor, not the canonical copy.

This is the natural follow-up to the guardrails-matrix migration: every
hardcoded model-facing string is now keyed and resolvable per family from a
YAML file, which means the same key→content lookup can be re-pointed at a
database with no changes to the call sites.

> **This document leads with the scoped v1 we are building now, then records
> the broader design and everything intentionally deferred.** The v1 section
> is the spec for implementation; the Deferred / Roadmap section preserves the
> fuller design (per-expert, compiler, versioning, per-project, audit log,
> drift/merge, full UI) so none of it is lost.

**Status:** v1 designed, not yet implemented.

---

## Goal

Let admins **see and customize the framework / model-family prompts** from the
database, overriding the bundled `config/` defaults **without a redeploy**.
Resolution falls through DB → bundled, so an empty override table behaves
exactly like today.

Three things are explicitly **out of v1** (see [Deferred / Roadmap](#deferred--roadmap-beyond-v1)):
per-**expert** content (personas, expert guides — these belong to the future
expert-creation feature), the live **prompt compiler**, and content-addressed
**hash versioning**.

---

## Problem

The guardrails work proved the resolution chain is sound, but the storage
layer is wrong for an operations-facing product:

- **Editing requires a redeploy.** Any prompt tweak — fixing a typo in a
  persona, adjusting a Gemma-specific nudge, adding a budget reminder — means a
  git commit, image build, and Fleet sync. The cycle is 10–20 minutes for a
  one-line change.
- **No live preview.** The only way to see what an agent actually receives for
  a given `(family, …)` is to spin up an agent and sniff the LLM request.
- **Templates and content are mixed.** `config/` holds both Jinja templates
  (`has_tool()` / conditional blocks) and pure text (personas, system
  prompts). A UI needs to render them differently.

The fix: keep `config/` as the **bundled default** and add a DB layer on top
that the Cockpit can edit.

---

## v1 — Building Now

### Scope

**In:**

- A single table storing prompt overrides keyed by `(family, kind, name)`.
- A loader extension that consults the DB before the bundled file, behind a
  feature flag. Same `resolve_*` API; the behavior change is internal.
- Admin REST endpoints for CRUD on overrides + reading the bundled default,
  gated by the `srw-admin` Keycloak role.
- A static **description catalog** mapping each `(kind, name)` to human-readable
  explanatory text ("what this prompt is, where it's used") — the text shown
  above the editor.
- Reproducibility via the **existing per-job config freeze** (no new
  versioning — see below).

**Out (deferred):** per-expert overrides, the prompt compiler, hash
versioning, per-project/per-user scope, an edit-history audit log,
bundled-drift detection / three-way merge, live updates to in-flight jobs, and
the full three-column cockpit UI. A *minimal* one-prompt editor page is the
last v1 slice; richer UI is deferred.

### Reproducibility is already solved — no versioning in v1

The instinct to "version the prompts so we know what an agent ran" is already
satisfied by code that exists today:

- **`serialize_resolved_config()`** (`src/core/loader.py:3344`) resolves *every*
  prompt to full text (`systemprompt`, `persona`, `strategic`, `tactical`,
  `summarization`, instructions) plus settings and writes them into the job's
  `resolved_config` JSONB. Its docstring: *"Captures everything needed to
  reproduce a job's config without disk access."*
- **`store_resolved_config()`** (`src/database/postgres_db.py:654`) writes
  **only if `resolved_config IS NULL`** — first run only
  (`UPDATE … WHERE id = $2 AND resolved_config IS NULL`). After that it is
  immutable; resume reads the frozen copy via `load_config_from_resolved()`,
  which is how the system already "prevents config drift on resume."

Because the DB override lookup sits **below** the matrix resolver that
`serialize_resolved_config()` already calls, overrides are captured in the
per-job snapshot **automatically**. Therefore:

- **"Future agents use the edit"** → automatic. A new job freezes the
  overridden text at first run; jobs already running keep their snapshot.
- **"Know what an agent ran back then"** → already there. `jobs.resolved_config`
  *is* the per-agent version record.
- **No** prompt-version counter, **no** cache-invalidation machinery, **no**
  agent-versioning in v1.

> Caveat: worker jobs are confirmed frozen-at-first-run. Persistent /
> interactive sessions are long-lived (not one-shot), so *when* they pick up an
> edit is a planning detail — resolve at session/thread start, or on next
> session. (`serialize_resolved_config` already captures
> `systemprompt_interactive`, so the content path exists.)

### Semantics: edits apply to future agents

v1 deliberately uses **frozen-per-job** semantics: an override affects agents
resolved *after* the edit; running agents are untouched. We are **not** doing
live in-flight updates in v1 — that was the most complex part of the original
design (version counter + cache invalidation at phase boundaries), and it
conflicts with the freeze. It is recorded under Deferred.

### Storage (single table)

```sql
-- 00NN_prompt_overrides.sql
-- Use the next free number in orchestrator/database/migrations/app/.
-- (The original draft's 0042–0044 numbering was illustrative; v1 needs one
-- migration, not three.)
CREATE TABLE prompt_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    family   VARCHAR(64),          -- NULL = global default (applies to all families)
    kind     VARCHAR(32) NOT NULL
              CHECK (kind IN ('prompts', 'instructions')),  -- = MatrixResolver.MATRIX_SUBSECTION
    name     VARCHAR(128) NOT NULL,  -- resolver entry_type, e.g. "persona", "systemprompt"

    content        TEXT NOT NULL,
    content_format VARCHAR(16) NOT NULL DEFAULT 'text'
              CHECK (content_format IN ('text', 'markdown', 'jinja', 'yaml')),
    notes          TEXT,            -- operator-facing change comment

    created_by   UUID REFERENCES users(id),
    updated_by   UUID REFERENCES users(id),
    created_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- (family, kind, name) is unique. COALESCE handles NULL family (global),
-- which PostgreSQL otherwise treats as distinct.
CREATE UNIQUE INDEX uq_prompt_override ON prompt_overrides
    (COALESCE(family, ''), kind, name);
CREATE INDEX idx_prompt_override_lookup ON prompt_overrides (family, kind, name);
```

No `expert`, no `project_id`, no `scope` enum, no `bundled_hash` — those belong
to deferred features. `kind` is the resolver **subsection** (`MatrixResolver.MATRIX_SUBSECTION`:
`prompts` / `instructions`) — exactly the value the single hook point has in
hand, so no name-mapping is needed. Finer grouping (auxiliary vs. core prompt)
lives in the static catalog, not the DB key. `updated_by` / `updated_at` give a
minimal "who last touched this" without a full audit log.

### Resolution

Each `resolve_*` in `src/core/loader.py` gains a `_db_lookup(kind, family, name)`
consulted before the bundled file, behind the feature flag
`PROMPT_DB_OVERRIDES_ENABLED` (default `false`):

```python
# src/core/loader.py (sketch)
def resolve_prompt(name, family, *, deployment_dir=None):
    if (override := _db_lookup("prompt", family, name)) is not None:
        return override
    return _bundled_prompt(name, family, deployment_dir=deployment_dir)
```

Lookup order for any single key:

1. `(family=<f>, kind=<k>, name=<n>)` — family-specific
2. `(family=NULL, kind=<k>, name=<n>)` — global default
3. Bundled file resolution (existing behavior)

Because resolution is frozen once per job, the agent does **not** need version
polling or an LRU cache (both existed in the original design only to support
live invalidation, which v1 drops). The agent **reads the relevant override
rows directly from Postgres at first run** — it already holds a DB connection
there to write the freeze — loads them into a process-local map, resolves, and
freezes the result into `resolved_config`. The orchestrator never resolves
prompts — it only stores them.

**Fail-open:** if the DB / internal endpoint is unreachable, `_db_lookup`
returns `None`, the loader falls through to the bundled file, and the agent
behaves identically to today (logged at `WARN`). Same posture as MongoDB and
Neo4j elsewhere in the stack.

### Description catalog

The "text description above the prompt" is metadata about the prompt *key*, not
the override. It lives in a small static catalog shipped with the app
(e.g. `config/prompts/catalog.yaml` or a module), keyed `(kind, name)`:

```yaml
- kind: prompt
  name: persona
  title: "Expert persona / identity"
  description: "Injected into the system prompt every call as {expert_identity}. Defines who the agent is and how it thinks."
  where_used: "get_phase_system_prompt() — rebuilt every LLM call, survives compaction."
```

The bundled-default endpoint returns the catalog entry alongside the content,
and it drives the (eventual) UI's explanatory text and tree labels.

### API (v1)

All admin routes under `/api/admin/prompts/*`, `srw-admin`-gated, same shape as
`/api/admin/providers/*` from `db_backed_llm_config.md`.

```
GET    /api/admin/prompts/catalog                       → editable (kind,name) keys + descriptions
GET    /api/admin/prompts/bundled/{family}/{kind}/{name} → bundled default content + catalog entry
GET    /api/admin/prompts/overrides                      → list overrides
GET    /api/admin/prompts/overrides/{id}                 → read one
POST   /api/admin/prompts/overrides                      → create  {family,kind,name,content,content_format,notes}
PUT    /api/admin/prompts/overrides/{id}                 → update
DELETE /api/admin/prompts/overrides/{id}                 → reset to bundled (delete the row)
```

`family` accepts the literal `_` as a stand-in for `NULL` (global), matching
`/api/admin/prompts/bundled/_/prompt/persona`.

The agent does **not** use an HTTP endpoint — it reads override rows directly
via its existing Postgres connection at first run
(`PromptsNamespace.list_overrides_for_family`). There is intentionally **no**
`/version` endpoint in v1 (no version counter).

### Build slices

**Slice 1 — storage + read path.**
Migration (single table) + `_db_lookup` wired into the resolver behind
`PROMPT_DB_OVERRIDES_ENABLED` + the internal fetch endpoint + the agent fetching
overrides at first run and freezing them.
*Acceptance:* with the flag on and one manually-inserted row, a **new** job
resolves the override and freezes it into `jobs.resolved_config`; an
already-running job is unchanged; flag off ⇒ byte-identical to today. Unit test
hits `_db_lookup` with a mocked DB.

**Slice 2 — admin API + catalog.**
All `/api/admin/prompts/*` endpoints + the description catalog, `srw-admin`
enforced.
*Acceptance:* full CRUD on overrides via curl; bundled-default endpoint returns
content + catalog entry.

**Slice 3 — minimal cockpit page** *(UI last, per "tech below the UI first").*
A single page: pick a `(family, kind, name)`, show description + bundled default
+ editable override, Save / Reset.
*Acceptance:* an operator edits a prompt in the UI and the next job picks it up.

### Open questions (v1)

1. **Persistent/interactive session refresh timing** — when does a long-lived
   session pick up an edit (session start vs. per-message)? Worker jobs are
   settled (frozen at first run).
2. **Editable surface** — wire/expose only `kind=prompt`
   (`systemprompt`/`persona`/`strategic`/`tactical`/`summarization`) first, or
   all kinds from day one? The table supports all; this is about how much to
   wire and surface.
3. **Catalog format/location** — `config/` YAML vs. a Python module.
4. **Jinja override validation** — parse-check `kind in (template, …)` content
   before save? Recommend hard-block on parse error; a broken template breaks
   the agent.

---

## Deferred / Roadmap (beyond v1)

Everything below is intentionally **not** in v1. The detailed designs are
preserved here as the eventual target.

### Per-expert overrides → expert-creation feature

v1 keys overrides by `(family, kind, name)` only. Per-expert content (personas,
expert-specific strategic/tactical; note the guides like `research-guide` have since
migrated to **skills** — [[agent_skills]] — edited via the skills editor, not here) is
the bulk of the prompt surface and belongs to the larger **expert-creation** feature, which
extends this one by letting operators *create* experts, not just edit prompts.
Adding it back means restoring the `expert` column and a second resolution
layer:

```
1. (family=<f>, expert=<e>, kind, name)  — most specific
2. (family=<f>, expert=NULL, kind, name) — family-wide
3. (family=NULL, expert=<e>, kind, name) — expert-wide
4. (family=NULL, expert=NULL, kind, name)— global
5. bundled
```

(`ORDER BY (family IS NULL), (expert IS NULL)` so the most-specific row wins.)

### Prompt compiler → its own spec

The single most-requested capability and the most complex. Goal: show the
operator the **fully assembled final string the model actually receives** for a
given situation (not just one resolved key). Two framings discussed:

- **Hand-fed situation dictionary** — the caller supplies a dict describing the
  situation (agent kind: persistent / worker / auxiliary; phase:
  strategic / tactical; special triggers, e.g. the file tool enforcing a read
  before a write; project membership) and the compiler renders the assembled
  prompt. Simple inputs, but the dictionary drifts from reality.
- **Drive it from the real graph / process** *(truer, harder)* — re-run the
  actual assembly path (`get_phase_system_prompt()` + the auxiliary prompt
  builders + per-tool guardrails injection at bind time + active/passive
  instruction-file injections) **headless**, on synthetic inputs, outside a live
  job. Faithful but a feature in its own right — it requires the assembly code
  to run without a job context.

Scope when picked up (from the earlier discussion): "everything sent to the
model" — system prompts (strategic/tactical), auxiliary calls
(summarization / memory extraction / curation), the kickoff message, per-tool
guardrails/`Examples:` blocks, and instruction-file injections — per family.
Deferred because it is too large to bundle with the editor.

### Content-addressed hash versioning → fast-follow

Hash the resolved prompts+settings, let the hash be the version, tag jobs with
it; look up the hash to recover what an agent ran. (The Git/Nix/Docker model.)
Reproducibility is **already** free via the per-job inline freeze, so this is an
optimization, not a prerequisite. What it adds: **dedup** (today every job row
carries a full fat copy; most are identical), a clean short **version ID** for
grouping "which jobs ran identical prompts" (useful for A/B-ing edits), and free
**change-detection** (an override that doesn't move the hash was a no-op). It
composes cleanly — once overrides flow through the resolver, the hash reflects
them automatically. Adopt when prompt-set variety + job volume make dedup /
grouping worth it; retrofitting costs the same as doing it now.

### Per-project / per-user overrides

v1 is system-wide. A `project_id` (and `scope`) column lets a single deployment
host two teams with different prompts, or A/B a stricter prompt for a subset of
jobs. Eventual schema:

```sql
    scope        VARCHAR(16) NOT NULL DEFAULT 'system'
                  CHECK (scope IN ('system', 'project')),
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    CONSTRAINT scope_project_consistency CHECK (
        (scope = 'system'  AND project_id IS NULL) OR
        (scope = 'project' AND project_id IS NOT NULL)
    )
```

### Edit-history audit log

Distinct from per-job reproducibility (already free). This answers "who changed
this prompt, when, and what was the before/after." Eventual design: every write
to `prompt_overrides` emits an `audit_event` row
(`event_type: prompt_override_changed`, actor, timestamp, before/after content +
hash, notes), with a `revert?to_event=<id>` endpoint and an
`/admin/prompts/audit` page.

### Bundled-default drift detection + three-way merge

When a redeploy ships a new bundled version of a prompt an operator has
overridden, surface a "bundled default has changed" badge. Eventual design: a
`prompt_bundled_snapshot` table (hash of every bundled file at startup) plus a
`bundled_hash` column on the override (the hash at edit time); a read-time hash
compare yields a drift flag, and the UI offers a three-way view
(bundled-at-edit / current bundled / current override). The redeploy never edits
operator rows; the bundled file is replaced, the override stays.

### Live updates to in-flight jobs (explicitly dropped from v1)

The original design pushed edits to running jobs at the next phase boundary via
a `prompt_config_version` counter consulted at three sites:

| Site | When checked | Action on bump |
|---|---|---|
| Tool bind (`apply_guardrails_to_tools`) | per `bind_tools()` call | re-inject `Examples:` block |
| Phase transition (`get_phase_system_prompt`) | every phase boundary | rebuild systemprompt + persona + phase prompt |
| Persistent session handler | before each user send | same |

v1 drops this entirely in favor of frozen-per-job semantics. If live in-flight
updates are ever wanted, this is the mechanism; note it re-introduces the
version counter and an LRU cache keyed on `(version, kind, family, name)`.

### Full cockpit UI

The eventual `/admin/prompts` page is a three-column layout: a tree picker
(`family → kind → name`, with per-expert subtrees once experts land), a
center Monaco-style editor (breadcrumb, format badge, Save / Discard / Reset,
notes field), and tabbed reference panes (bundled default with live diff,
resolved view, audit log, drift three-way). Jinja templates render against a
sample-context pane; guardrails entries collapse to a single line and show the
tool's upstream docstring. A separate `/admin/prompts/audit` page lists all
changes, filterable. v1 ships only the minimal single-prompt page (Slice 3).

---

## Operational concerns

**Encryption at rest.** Prompt content is not secret (no keys, no PII). Stored
in plaintext, like git history.

**Backups.** `prompt_overrides` goes in the existing PostgreSQL backup set.

**Redeploy behavior.** Migrations run (no-op if applied). The bundled files in
the image change; **override rows are never edited or deleted** by a redeploy —
the bundled file is replaced, the override stays where it is. (Drift *detection*
across redeploys is deferred; see above.)

**Performance.** A job resolves its prompts once, at first run, then freezes
them. That is a single small fetch of the override rows for the job's family —
zero steady-state DB reads thereafter. Persistent sessions consult overrides at
session start (planning detail).

**Failure mode.** DB unreachable ⇒ fail-open to bundled (see Resolution).

---

## References

- Bundled defaults today: `config/prompts/`, `config/experts/<role>/`,
  `config/templates/`, `config/guardrails/`
- Resolution: `src/core/loader.py` (`resolve_prompt`, `resolve_instruction`,
  `resolve_template`, `resolve_guardrails`; `PromptMatrixResolver`,
  `InstructionMatrixResolver`)
- Per-job config freeze (the reproducibility mechanism v1 relies on):
  `serialize_resolved_config()` / `load_config_from_resolved()`
  (`src/core/loader.py:3344`), `store_resolved_config()` /
  `get_resolved_config()` (`src/database/postgres_db.py:654`), call sites in
  `src/agent.py:1054` (store) and `src/agent.py:866` (load on resume)
- Injection: `src/services/guardrails.py`
  (`apply_guardrails_to_tools`, `format_nudge`)
- Prior art: `docs/features/db_backed_llm_config.md`
  (helm-as-seed pattern, encryption posture, admin role gating)
- Prior art: `docs/design/guardrails_matrix.md` (the matrix shape this builds on)
- Architecture: `docs/features/prompting.md` (the four-layer prompt system and
  the content-type taxonomy this resolves against)
