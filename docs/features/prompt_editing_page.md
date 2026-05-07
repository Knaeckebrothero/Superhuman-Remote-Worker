# DB-backed Prompt Overrides + Cockpit Editor

Move every model-facing prompt that today lives in `config/` into a layered
resolution chain: **bundled files (immutable, ship-with-image defaults)** at
the bottom, **DB-stored overrides** on top, edited from a new Cockpit page.
Operators can author custom prompts per **family** (default, gemma, gpt-5, …)
or per **expert** (scholar, critic, developer, curator, designer,
interactive). Bundled defaults are refreshed on every redeploy and never
clobber user edits — they are the floor, not the canonical copy.

This is the natural follow-up to the just-completed guardrails-matrix
migration: every hardcoded model-facing string is now keyed and resolvable per
family from a YAML file, which means the same key→content lookup can be
re-pointed at a database with no code changes to the call sites.

---

## Problem

The guardrails work proved that the resolution chain is sound, but the
storage layer is wrong for an operations-facing product:

- **Editing requires a redeploy.** Any prompt tweak — fixing a typo in the
  scholar persona, adjusting a Gemma-specific nudge, adding a budget
  reminder — means a git commit, image build, and Fleet sync. The cycle is
  10–20 minutes for a one-line change.
- **No tenancy.** All prompts are global to the cluster. There is no way for
  a single deployment to host two teams with different scholar personas, or
  to A/B a stricter critic prompt for a subset of jobs.
- **No audit.** Git history captures what changed, but not who triggered the
  change in production, or which jobs ran under which version of the prompt.
  When a behavior regression appears, "what was the prompt at the time?" is
  hard to answer from git alone because the resolution chain merges multiple
  files.
- **No live preview.** The only way to see what an agent actually receives
  for `(family=gemma, expert=critic, kind=tactical)` is to spin up an agent
  and sniff the LLM request. Operators can't reason about the resolved
  prompt without running the system.
- **Templates and content are mixed.** `config/` holds both Jinja templates
  with `has_tool()`/conditional blocks (phase transition todos, todo guides)
  and pure text (personas, system prompts). The current resolution treats
  them identically; a UI needs to render them differently.

The fix is to keep `config/` as the **bundled default snapshot** and add a
DB layer on top that the Cockpit can edit. Resolution falls through DB →
bundled, so an empty DB behaves identically to today.

---

## Scope

**In scope (v1)**

- Schema for storing prompt content, templates, and guardrail snippets keyed
  by `(scope, family, expert, kind, name)`.
- A loader extension that consults the DB before falling back to the bundled
  file. Same `resolve_*` API; behavior change is internal.
- A bundled-default registry: at orchestrator startup, the contents of
  `config/prompts/`, `config/experts/<role>/`, `config/templates/`, and
  `config/guardrails/` are hashed and recorded so the UI can show
  "ships-with-image" content side-by-side with the override. **Bundled
  content is never written to the override table.**
- REST endpoints under `/api/admin/prompts/*` for listing, reading, writing,
  and previewing resolved prompts. Gated by a Keycloak role (`srw-admin`).
- Cockpit page at `/admin/prompts` with a tree picker on the left
  (family → expert → kind → name) and a split editor on the right
  (resolved view, override editor, bundled-default reference).
- Cache invalidation: a `prompts_version` counter bumped on every write,
  consulted by agent bind sites and persistent sessions to know when to
  rebuild prompt strings.
- Audit trail: every override write produces an `audit_event` row with
  user, timestamp, before/after content, and the resolved hash.

**Out of scope (v1)**

- Per-project and per-user overrides. Scope stays "system-wide" for v1.
  The schema reserves a nullable `project_id` column so a future per-project
  layer slots in without a migration.
- A diff-and-merge UI for upgrading bundled defaults. When `config/` ships a
  new version of a prompt, the UI surfaces a "bundled default has changed"
  badge but the operator manually decides what to do with their override.
- Per-job overrides at dispatch time. The orchestrator already supports
  per-job `config_override` for settings; extending that to prompts can come
  later but is not needed for the editing UX.
- Localization. Prompts are English-only today; multi-language is a
  separate feature.
- An import/export workflow (YAML round-trip). Operators who want
  GitOps-style management edit `config/` files in the bundle, not the DB.

---

## Architecture

### Resolution chain

Today (after the guardrails migration):

```
caller (graph node, tool bind site, formatter)
  └─ loader.resolve_*(family, expert)
       └─ deep-merge(family.yaml, default.yaml)
            └─ read content from config/prompts|experts|templates|guardrails/
```

After this feature:

```
caller (unchanged)
  └─ loader.resolve_*(family, expert)
       └─ DB: SELECT FROM prompt_overrides WHERE …  (most-specific first)
       └─ bundled YAML/MD/TXT (the existing config/ files)
            └─ deep-merge identical to today
```

The DB layer sits **above** the family→default deep-merge. Lookup order for
any single key:

1. `(scope=system, family=<f>, expert=<e>, kind=<k>, name=<n>)` — most specific
2. `(scope=system, family=<f>, expert=null,  kind=<k>, name=<n>)` — family-wide
3. `(scope=system, family=null, expert=<e>, kind=<k>, name=<n>)` — expert-wide
4. `(scope=system, family=null, expert=null,  kind=<k>, name=<n>)` — global
5. Bundled file resolution (existing behavior)

Layers 1–4 are all queried in a single SQL statement using
`ORDER BY (family IS NULL), (expert IS NULL)` so the most-specific row wins.
If no row exists at any specificity, the bundled file is read.

### Storage

Three tables. All migrations land as new files in
`orchestrator/database/migrations/app/`.

```sql
-- 0042_prompt_overrides.sql
CREATE TABLE prompt_overrides (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    scope        VARCHAR(16) NOT NULL DEFAULT 'system'
                  CHECK (scope IN ('system', 'project')),
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    family       VARCHAR(64),     -- NULL = applies to all families
    expert       VARCHAR(64),     -- NULL = applies to all experts
    kind         VARCHAR(32) NOT NULL
                  CHECK (kind IN (
                    'prompt',        -- systemprompt, persona, strategic, tactical, …
                    'instruction',   -- instructions.md, todo_guide.md, research_guide.md
                    'template',      -- strategic_todos_*, workspace_template
                    'auxiliary',     -- summarization, memory_extraction, curation, …
                    'nudge',         -- runtime injection strings (16 keys)
                    'tool_example'   -- per-tool guardrails snippets (28 keys)
                  )),
    name         VARCHAR(128) NOT NULL,    -- e.g. "persona", "todo_action", "git_log"

    content      TEXT NOT NULL,
    content_format VARCHAR(16) NOT NULL DEFAULT 'text'
                  CHECK (content_format IN ('text', 'markdown', 'jinja', 'yaml')),

    bundled_hash VARCHAR(64),     -- SHA-256 of the bundled default at the time of edit
    notes        TEXT,            -- operator-facing change comment

    created_by   UUID REFERENCES users(id),
    updated_by   UUID REFERENCES users(id),
    created_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    -- Project scope requires project_id; system scope forbids it.
    CONSTRAINT scope_project_consistency CHECK (
        (scope = 'system'  AND project_id IS NULL) OR
        (scope = 'project' AND project_id IS NOT NULL)
    )
);

-- A given (scope, project, family, expert, kind, name) tuple is unique.
-- Two partial unique indexes because PostgreSQL treats NULLs as distinct.
CREATE UNIQUE INDEX uq_prompt_override_full ON prompt_overrides
    (scope, COALESCE(project_id::text, ''), COALESCE(family, ''), COALESCE(expert, ''), kind, name);

CREATE INDEX idx_prompt_override_lookup ON prompt_overrides
    (scope, family, expert, kind, name);
```

```sql
-- 0043_prompt_bundled_snapshot.sql
-- Records the hash of every bundled prompt at orchestrator startup.
-- Lets the UI render "your override is based on bundled v1, current bundled is v2".
CREATE TABLE prompt_bundled_snapshot (
    family       VARCHAR(64),
    expert       VARCHAR(64),
    kind         VARCHAR(32) NOT NULL,
    name         VARCHAR(128) NOT NULL,
    content      TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    image_tag    VARCHAR(64),       -- e.g. "sha-a7d2ea5", informational
    captured_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (COALESCE(family,''), COALESCE(expert,''), kind, name)
);
```

```sql
-- 0044_prompt_versioning.sql
-- Single-row counter bumped on every override write. Agents and persistent
-- sessions read this to know when to invalidate cached prompt strings.
CREATE TABLE prompt_config_version (
    id          INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version     BIGINT NOT NULL DEFAULT 0,
    last_changed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_changed_by UUID REFERENCES users(id)
);
INSERT INTO prompt_config_version (id, version) VALUES (1, 0);
```

### Loader changes

`src/core/loader.py` already exposes `resolve_model_settings`,
`resolve_guardrails`, `resolve_prompt`, `resolve_instruction`, and
`resolve_template`. Each currently goes file→file. The change is: each
`resolve_*` calls a new `_db_lookup(kind, family, expert, name)` first and
returns early if the DB has a hit.

```python
# src/core/loader.py (sketch)
def resolve_prompt(name, family, expert=None, *, deployment_dir=None):
    if (override := _db_lookup("prompt", family, expert, name)) is not None:
        return override
    return _bundled_prompt(name, family, expert, deployment_dir=deployment_dir)
```

`_db_lookup` is a thin synchronous read helper that:
- Uses an in-process LRU cache keyed on `(prompts_version, kind, family, expert, name)`.
- On startup and at every cache miss, reads `prompt_config_version.version`.
- Returns `None` when no matching row is found.

The agent process and persistent-session process both call `_db_lookup`.
The orchestrator does not — it never resolves prompts; it only stores them.

### Cache invalidation

The version counter is consulted at three levels:

| Site | When checked | Action on bump |
|---|---|---|
| Tool bind (`apply_guardrails_to_tools`) | Once per `bind_tools()` call | Re-strip and re-inject `Examples:` block |
| Phase transition (`get_phase_system_prompt`) | At every phase boundary | Rebuild systemprompt + persona + phase prompt |
| Persistent session message handler | Before each user-initiated send | Same as above |

This means: an operator editing the persona at 14:00 sees the new persona
applied to in-flight jobs at their next phase boundary (typically 30s–5min
later), and to interactive sessions at the user's next message. No
restarts, no pod evictions, no kill-and-redispatch.

A side effect of the version counter: the cockpit's prompt page shows a
"applied to N agents within N seconds" status line after every save, by
correlating `prompt_config_version.last_changed_at` with the next
`prompts_version` value seen in agent heartbeats.

### Bundled snapshot ingestion

On orchestrator startup, after migrations have run, a one-shot job walks
`config/{prompts,experts,templates,guardrails}/`, computes SHA-256 of each
file, and upserts into `prompt_bundled_snapshot`. The upsert is conditional
on `content_hash` differing — a redeploy of the same image is a no-op.

This serves three purposes:
1. The UI can show bundled content without the file system being readable
   from the cockpit pod.
2. The "bundled has changed since you wrote your override" badge becomes a
   simple hash compare.
3. Auditing: rolling back to "what shipped in image sha-a7d2ea5" is one
   query.

---

## API surface

All routes live under `/api/admin/prompts/*` and require the `srw-admin`
Keycloak role. They follow the same shape as
`/api/admin/providers/*` from `db_backed_llm_config.md`.

### List bundled defaults

```
GET /api/admin/prompts/bundled
  → [{family, expert, kind, name, content, content_hash, image_tag}, …]
GET /api/admin/prompts/bundled/{family}/{expert}/{kind}/{name}
  → {family, expert, kind, name, content, content_hash, image_tag}
```

`family` and `expert` accept the literal `_` as a stand-in for `NULL`,
matching the URL pattern `/api/admin/prompts/bundled/_/scholar/prompt/persona`.

### List and edit overrides

```
GET    /api/admin/prompts/overrides
GET    /api/admin/prompts/overrides/{id}
POST   /api/admin/prompts/overrides
PUT    /api/admin/prompts/overrides/{id}
DELETE /api/admin/prompts/overrides/{id}
```

`POST` body:
```json
{
  "scope": "system",
  "family": "gemma",
  "expert": "scholar",
  "kind": "prompt",
  "name": "persona",
  "content": "...",
  "content_format": "markdown",
  "notes": "shorter persona block; cuts ~400 tokens/call"
}
```

The server records `bundled_hash` automatically from
`prompt_bundled_snapshot` at write time — the operator does not supply it.

### Preview resolved prompt

```
POST /api/admin/prompts/preview
  body: {family, expert, kind, name, with_overrides?: bool}
  → {content, content_format, source: "override" | "bundled" | "deep_merge",
     resolved_specificity: "family+expert" | "family" | "expert" | "global"}
```

This drives the editor's "what does the agent actually see" pane.

### Diff bundled vs override

```
GET /api/admin/prompts/overrides/{id}/diff
  → {bundled_content, override_content, bundled_hash_at_edit, bundled_hash_now,
     drift: bool}
```

`drift: true` means the bundled content has changed since the override was
written — the UI shows a yellow badge and an "open three-way merge" button
(deferred to v2; v1 just shows the diff).

### Internal: agent reads (no admin role required)

```
GET /api/internal/prompts/version  → {version: 142, changed_at: "..."}
GET /api/internal/prompts/lookup
  ?family=gemma&expert=scholar&kind=prompt&name=persona
  → {content, source, content_format}
```

These are called from the agent's `_db_lookup` and are authenticated with
the same internal token the agent already uses for heartbeats and job
completion.

---

## Cockpit UI

### Page: `/admin/prompts`

Three-column layout (desktop) collapsing to a tab-bar on mobile.

**Left: tree picker.** Hierarchy:
```
default (family)
├── prompts
│   ├── systemprompt
│   ├── persona
│   ├── strategic
│   ├── tactical
│   └── …
├── auxiliary
│   ├── summarization_prompt
│   ├── memory_extraction_prompt
│   └── …
├── templates
│   ├── strategic_todos_initial
│   └── strategic_todos_transition
├── guardrails
│   ├── nudges (16 entries)
│   └── tool_examples (28 entries)
└── experts
    ├── scholar
    │   ├── persona
    │   ├── strategic
    │   ├── tactical
    │   ├── instructions
    │   └── todo_guide
    ├── critic
    └── …
gemma (family)
   └── … (same shape; only entries with bundled-default or override show)
gpt-5
codex
gpt-oss
minimax
```

Each leaf shows one of three icons:
- **○** bundled default only (no override)
- **●** override exists, in sync with bundled
- **⚠** override exists, bundled has drifted (hash mismatch)

**Center: editor.** Monaco-style text area sized to the content. Above it:
breadcrumb `gemma › scholar › prompt › persona` and a format badge
(`text` / `markdown` / `jinja` / `yaml`). Below it:
- `[Save override]` / `[Discard]` buttons
- A `notes` textarea ("change comment, shown in the audit log")
- A `[Reset to bundled]` button that deletes the override row

**Right: reference panes.** Tabbed:
- **Bundled default** — read-only view of the file as it ships with the
  current image. Diff-highlighted against the editor content live.
- **Resolved view** — what the agent will actually receive for the chosen
  `(family, expert)` after deep-merge. Updates as the user types.
- **Audit log** — last 20 changes to this row, oldest at bottom. Each
  entry: timestamp, user, change comment, expand-to-see-diff.
- **Drift** — only shown if `bundled_hash_at_edit ≠ bundled_hash_now`.
  Three-way view: bundled-at-edit, current bundled, current override.

### Behavior details

- **Jinja templates** render with a sample context (provided in the editor
  via a small "preview context" JSON pane) so the operator can see what
  `{% if has_tool("kb_write") %}` actually expands to. Template parse
  errors block save.
- **Guardrails entries** are short — the editor for them collapses to a
  single textarea height and shows the tool's full upstream docstring
  (with the `Examples:` block highlighted) above the editor.
- **Family inheritance preview**: when editing a `default` row, a
  hint-line under the editor says *"applies to: gemma (overridden), gpt-5,
  codex, codex-spark, gpt-oss (overridden), minimax"* — listing which
  families inherit and which override.
- **Expert inheritance preview**: when editing an expert-specific row, a
  similar hint shows which experts inherit from this default.
- **Save UX**: optimistic write with toast `applied to 3 agents within 8s`
  driven by the version counter + agent heartbeats.

### Page: `/admin/prompts/audit`

Flat list of every change across all overrides, paginated. Filter by
user, family, expert, kind, date range. Each row links back to the
relevant editor page with the diff opened.

### Permissions

`/admin/prompts/*` requires the `srw-admin` Keycloak role. The page
short-circuits to a "you don't have access" notice for unprivileged users.
Read-only access for non-admins is **not** in v1 — partly for blast-radius
reasons, partly because there's no use case yet.

---

## Audit and rollback

Every write to `prompt_overrides` produces an `audit_event` row
(table created earlier for the audit-store roadmap):

```json
{
  "event_type": "prompt_override_changed",
  "actor_user_id": "...",
  "occurred_at": "2026-05-07T14:32:11Z",
  "subject": "prompt_overrides:42da…",
  "metadata": {
    "scope": "system",
    "family": "gemma",
    "expert": "scholar",
    "kind": "prompt",
    "name": "persona",
    "before_content_hash": "abc123…",
    "after_content_hash":  "def456…",
    "before_content": "...",
    "after_content":  "...",
    "notes": "shorter persona; cuts ~400 tokens/call"
  }
}
```

Rollback is `POST /api/admin/prompts/overrides/{id}/revert?to_event=<id>`
which writes a new override row with the historical content and a notes
field of `revert of {event_id}`.

The audit table is the source of truth for "what was the prompt at the
time job X ran". Each LLM request (logged via the existing `llm_requests`
audit) records its `prompts_version`; a job postmortem can reconstruct the
exact resolved prompts by joining version → audit events.

---

## Migration plan

The feature lands in three slices, mirroring the structure of the
guardrails migration. Each slice ships independently and is reversible by
dropping the relevant tables.

### Slice 1: storage + read path

- Migrations 0042–0044 land.
- Bundled-snapshot ingestion runs at orchestrator startup.
- `_db_lookup` added to `loader.py` but **off by default** behind a feature
  flag `PROMPT_DB_OVERRIDES_ENABLED` (default `false`).
- Internal endpoints for version + lookup are exposed.
- No UI yet. Verified by manually inserting an override row and watching
  an agent pick it up after toggling the flag.

Acceptance: with the flag on and one row in `prompt_overrides`, an agent
running a job uses the override content and falls back to bundled for
every other key. Test added that hits `_db_lookup` with a mocked DB.

### Slice 2: REST surface + audit

- All `/api/admin/prompts/*` and `/api/internal/prompts/*` endpoints
  shipped.
- Audit events emitted on every write.
- Authorization enforced with the `srw-admin` role.
- Feature flag still gates whether agents *consult* the DB; the API is
  always live.

Acceptance: full CRUD on overrides via curl, audit rows produced, preview
endpoint returns identical content to what an agent would resolve.

### Slice 3: Cockpit page

- New module `cockpit/src/app/admin/prompts/`.
- Tree picker, editor, reference panes, audit page.
- Feature flag flipped to `true` in helm values for staging, then
  production.

Acceptance: an operator can change the `gemma › scholar › persona` from
the UI and see a running scholar job pick up the new persona at the next
phase boundary.

---

## Operational concerns

**Encryption at rest.** Prompt content is not secret data — no API keys,
no PII. Stored in plaintext, like git history. Audit content is the same.

**Backups.** `prompt_overrides` and `prompt_config_version` go in the
existing PostgreSQL backup set. `prompt_bundled_snapshot` is regenerable
at startup; backing it up is optional but cheap.

**Redeploy behavior.** When a new image lands:
1. Migrations run (no-op if already applied).
2. Bundled-snapshot ingestion compares hashes file-by-file.
3. For files whose hash changed: snapshot row is updated, and any
   `prompt_overrides` row whose `bundled_hash` matches the *previous*
   snapshot value gets a "drift" flag (computed at read time, not stored).
4. Operators see ⚠ badges on affected entries in the UI and decide
   whether to update their override or accept the drift.

The redeploy never edits or deletes user override rows. The bundled file
is *replaced*, the override stays where it is.

**Rolling deploys.** During a rolling deploy, two pods may briefly hold
different bundled snapshots. The version counter doesn't move during a
deploy (only on operator edits), so agents continue using the cached
override content. The bundled-snapshot rows are last-writer-wins — the
final pod to start sets the row state.

**Performance.** A typical phase boundary resolves ~6 prompt strings
(systemprompt, persona, strategic/tactical, instructions, todo_guide).
With the LRU cache keyed on `(version, kind, family, expert, name)`, each
boundary costs zero DB reads in the steady state and ~6 reads after a
version bump. Persistent sessions consult the cache before every user
message — an extra `SELECT version FROM prompt_config_version` per
message, well within budget.

**Failure mode.** If the DB is unreachable, `_db_lookup` returns `None`
(non-fatal), the loader falls through to the bundled file, and the agent
behaves identically to today. Logged at `WARN` so an operator can see the
degradation. Same fail-open posture as MongoDB and Neo4j elsewhere in the
stack.

---

## Open questions

1. **Per-project overrides as v1 or v2?** The schema reserves `project_id`
   already. Including project-scope CRUD in v1 doubles the UI work but
   answers the multi-tenant question that motivated this feature in the
   first place. Default to v2 unless a concrete v1 user appears.

2. **Should template parse errors hard-block save, or save with a
   warning?** Recommend hard-block — a broken Jinja template breaks the
   agent, and there's no recovery path other than another save. Validation
   runs the template against a known-good context derived from the
   bundled default.

3. **Do we expose the "preview resolved prompt with merge" view to
   non-admins?** Useful for users debugging their own jobs, but exposes
   the prompt internals more than we currently do. Default to admin-only;
   revisit when there's a concrete need.

4. **Versioning bundled snapshots across image tags.** Today the bundled
   snapshot is "last image's files." A change at image build time
   overwrites the snapshot. Keeping a history of bundled snapshots
   (one row per image tag) would let operators see "what shipped in
   sha-a7d2ea5 vs sha-d0945cc." Cheap to add but bloats the table over
   long-running deployments. Default: keep only the current snapshot,
   rely on git history for older images.

5. **Editor format hints for guardrails.** A `tool_example` for Gemma is a
   one-line `<|tool_call>call:fn{…}<tool_call|>` snippet, while a
   `nudge` may be 30 lines of markdown. The editor should resize and
   pre-fill differently for each. Probably means a small JSON map of
   `(kind, name) → editor_config` shipped with the cockpit, derived from
   the bundled default's shape.

---

## References

- Bundled defaults today: `config/prompts/`, `config/experts/<role>/`,
  `config/templates/`, `config/guardrails/`
- Resolution: `src/core/loader.py` (`resolve_prompt`,
  `resolve_instruction`, `resolve_template`, `resolve_guardrails`)
- Injection: `src/services/guardrails.py`
  (`apply_guardrails_to_tools`, `format_nudge`)
- Prior art: `docs/features/db_backed_llm_config.md`
  (helm-as-seed pattern, encryption posture, admin role gating)
- Prior art: `docs/design/guardrails_matrix.md`
  (the matrix shape this builds on)
- Prior art: `docs/features/prompting.md`
  (content-type taxonomy: identity, methodology, task, phase guidance,
  reference, output formats)
