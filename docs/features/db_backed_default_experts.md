---
tags:
  - feature
  - experts
  - defaults
  - config
  - capabilities
  - cockpit
related:
  - "[[global_expert_management]]"
  - "[[default_expert_roster]]"
  - "[[settings_design]]"
  - "[[instant_landing_session]]"
  - "[[application_tool_surface_baseline]]"
  - "[[session_db_experts_cannot_customize_interactive_prompt]]"
aliases:
  - default assistants
  - application default experts
  - personal default experts
  - worker base
  - session base
---

# DB-Backed Default Experts and Mode Base Configs

> **Status:** Implemented, 2026-07-22. Automated backend, Cockpit, i18n, and
> production-build verification is complete; the live rollout checks in the
> verification matrix remain deployment-operator steps.
> The product decisions in this document were confirmed in the default-expert
> discussion on 2026-07-22.
>
> **Amends:** [[global_expert_management]] Decision 1. Bundled experts remain
> disk-canonical in general, but the two application-default experts are a
> deliberate exception: disk files are their first-boot seed templates and DB
> rows are their runtime source of truth. This is not a general YAML-to-DB sync.

## Summary

SRW currently uses the word “default” for two different responsibilities:

1. `config/defaults.yaml` and `config/persistent_defaults.yaml` are framework
   inheritance bases and last-resort field fallbacks.
2. No expert selected in the job/session creation UI also means “use those base
   files as the user's starting configuration.”

Those responsibilities conflict. A framework base should be conservative,
role-neutral, and safe for any expert to inherit from. A useful new session or
job should instead start with an intentional persona, model policy, reasoning
policy, prompt, and tool surface which an administrator or user can customize.

This feature separates the concepts:

- Rename the framework bases to `worker_base.yaml` and `session_base.yaml`.
- Keep those YAML files internal, conservative, and role-neutral.
- Ship two seed templates: the existing **Assistant** (`session`) and a new
  **General Worker** (`worker`). Materialize each as a stable DB expert.
- Store one non-null application-default DB expert for each expert type.
- Let each user optionally point each type at an expert they own. A personal
  default overrides the application default.
- Add a restrict-only `personal_default_experts` capability grant, defaulting
  to `true`, which controls the personal-default workflow.
- Resolve omitted expert selection on the server for every creation path.
- Preselect the effective DB expert in the Cockpit and remove the normal
  “nothing selected” state.

The result is a useful, customizable product default backed by the database,
with stable framework fallbacks underneath it.

## Terminology

| Term | Meaning |
| --- | --- |
| **Mode base** | Internal YAML inheritance root: `worker_base.yaml` or `session_base.yaml`. It provides schema-complete, conservative fallbacks. |
| **Application default expert** | Admin-controlled, global DB expert selected for a type when no more specific default exists. There is exactly one worker and one session application default. |
| **Personal default expert** | Optional pointer from a user and expert type to a DB expert owned by that user. |
| **Project default expert** | Existing `project_experts.default_for` selection, scoped to one project and expert type. |
| **Explicit selection** | An expert deliberately chosen for this creation request. |
| **Seed template** | Bundled file content used only to create the initial DB Assistant and General Worker rows. It is not continuously synchronized into the DB. |

The Cockpit should stop calling a mode base “the default expert.” In ordinary
product UI, “default” means a DB expert selected by a default pointer.

## Current Problem

### One configuration is doing two jobs

The current bases contain both structural fallback values and product choices.
Examples include a concrete model, high reasoning, broad tool lists, worker
delegation, scholar/verification behavior, and default-on persistent
application-control groups. That makes a bare job/session useful, but it also
means every thin expert inherits decisions it never made.

This produces two bad choices:

- Keep the bases broad, and an expert that omits a field silently receives
  tools or orchestration behavior it may not need.
- Make the bases conservative, and a user who creates a job/session without an
  expert receives an unnecessarily weak or generic experience.

### “No expert” is a real user-facing state

Both creation forms currently allow the selected expert to be toggled back to
`null`. The request then falls through to `default`/`persistent_defaults`.
Other creation paths—instant landing, MCP, automations, and direct REST—can also
omit an expert. The UI cannot be the authority for this fallback.

### Administrators cannot customize the effective product default

An operator can edit image-owned YAML or configure per-field fallbacks, but
there is no DB object representing “the Assistant everyone starts with.” A
container upgrade should not be the mechanism for changing a deployment's
default persona, prompt, model, reasoning, or tools.

### User personalization and expert inheritance are conflated

Account settings such as default model/reasoning are intended as gap-fillers,
but some current paths materialize them into a high-precedence
`config_override`. That can override an explicitly selected expert. A user who
wants a consistently customized assistant needs a named, owned DB expert, not a
set of unrelated fields that unexpectedly override every specialist.

### Empty session tool groups are not currently sufficient

Persistent sessions auto-inject Fleet Management, Experts & Skills, and
Automations & Loops unless private disable markers are present. Those markers
are currently derived from request overrides, not from the fully resolved
config. Consequently, merely writing `tools.workflows: []` in the base YAML
does not reliably make workflows default-off. Base hardening therefore needs a
resolution/runtime change as well as YAML edits.

## Goals

1. Every ordinary new root job and session resolves to a compatible DB expert.
2. Admins can choose and customize the two application defaults without editing
   the container image.
3. Users can create isolated personal copies and select them as defaults when
   permitted.
4. Expert omission remains meaningful field-by-field: omitted fields fall
   through to stable account/system/base values.
5. Mode bases are conservative enough to be safe inheritance roots.
6. All UI, REST, MCP, instant-session, automation, and other headless creation
   paths use the same server-side selection service.
7. Existing explicit expert selections remain explicit; changing a default
   pointer does not rebind already-created work.
8. The rename is backward-compatible for persisted names and external configs.

## Non-goals

- Moving the entire bundled expert catalog into the database.
- Continuously synchronizing YAML expert changes into admin-edited DB rows.
- Making one expert usable as both `worker` and `session`; type remains
  structural and immutable.
- Allowing DB session experts to replace the trusted interactive system-prompt
  wrapper. The existing persona/instructions surface remains; a fenced
  `interactive_workflow` slot is a separate feature.
- Silently stripping capabilities from an incompatible expert. Existing
  capability enforcement remains fail-loud.
- Changing the lifecycle semantics of resolved configs: jobs freeze at
  dispatch; sessions re-resolve their selected expert on attach.

## Locked Decisions

### 1. Bases and defaults are separate concepts

`worker_base.yaml` and `session_base.yaml` are framework implementation assets.
They must be valid on their own for compatibility and recovery, but they are
not the normal user-facing starting profiles.

The application defaults are DB expert rows layered on those bases:

```text
worker_base.yaml  <-  DB General Worker  <-  project/user/request layers
session_base.yaml <-  DB Assistant       <-  project/user/request layers
```

### 2. There are two independent defaults at every scope

Worker and session experts have incompatible structural bases. Application,
user, and project defaults are therefore keyed by `expert_type` and cannot be
shared across types.

### 3. The normal effective default is always DB-backed

Under a healthy installation, resolving an omitted expert for an ordinary root
job/session returns an expert UUID. The Cockpit sends/receives UUID-based DB
selection, and the created job/thread stores that resolved `expert_id`.

Bundled names and direct base selection remain compatibility/advanced paths;
they are not presented as the ordinary default choice.

### 4. Ship Assistant and General Worker as insert-only seeds

- **Assistant** is the session seed.
- **General Worker** is a new worker seed.

Their bundled files are source artifacts used on first boot. Initialization
creates managed, global DB expert rows with stable keys. Re-running init does
not overwrite the DB rows, so admin changes survive restarts and image upgrades.

The managed seed rows cannot be deleted. An admin may point the application
default at another compatible global DB expert. Updating to a newer bundled
seed is an explicit preview/reset action, never an automatic reconciliation.

### 5. Default pointers select experts; experts own product behavior

The application/user default record stores only an expert UUID. Model,
reasoning, prompts, tools, workspace preferences, and other agent behavior stay
inside the selected expert (or lower fallback layers). We do not create a
second “default assistant settings” blob that can drift from expert CRUD.

### 6. Personal defaults are owned forks

A user's personal default must reference a DB expert owned by that same user
and of the matching type. A global or bundled expert can be used as the source
of a one-click **Customize my default** operation, but that operation first
forks it into an owned row and then sets the pointer in one transaction.

This makes the behavior intentional:

- Users without a personal fork follow later admin changes to the application
  default for future creations.
- Users with a personal fork are isolated from those changes.
- “Use application default” removes the personal pointer; it does not copy the
  current application expert.

### 7. A default pointer change affects new work only

At creation, the server resolves the applicable pointer and persists the actual
expert UUID. Later changes to application, project, or personal default
pointers do not rebind an existing job/thread.

Editing the selected expert itself follows existing semantics:

- A job uses the expert version resolved at dispatch and then freezes it.
- A session re-resolves its persisted expert UUID on attach, so an edit can
  affect that session on its next attach.

The Admin UI must explain this distinction. To roll out a new default without
changing existing sessions, duplicate the current expert, edit the duplicate,
then atomically switch the application pointer.

### 8. The server is the only default-selection authority

The same service resolves defaults for Cockpit, direct REST, MCP, instant
landing, automations, and other root creation flows. The UI preselection is a
preview of server behavior, not an independent fallback implementation.

Child/verification/delegation jobs do not unexpectedly pick the user's general
default: they continue to inherit or explicitly select their parent/specialist
config according to their existing lifecycle.

### 9. Normal creation has no deselected state

The expert picker behaves like a radio group. Clicking the selected card does
not clear it. The effective default is preselected and its provenance is shown
(`Project default`, `Your default`, or `Application default`).

An advanced/operator-only **Framework base** option may be retained for
diagnostics and compatibility, but it is visually separate and never the
implicit result of deselecting a card.

### 10. Personal-default permission is a capability grant

Add this catalog entry:

| Key | Type | Default | Restrict-only | Purpose |
| --- | --- | --- | --- | --- |
| `personal_default_experts` | bool | `true` | yes | Set or replace a personal default and use the atomic “fork/customize as my default” workflow. |

The grant is deliberately default-allow because personal customization is the
normal product experience. A global/project/user deny can restrict it, and a
more-specific scope cannot widen an inherited deny. Admins retain the standard
grant bypass.

This grant does **not** replace the existing `user_experts` kill switch or the
per-capability checks on tools/models/autonomy. It also does not prohibit using
an explicitly selected expert or ordinary expert authoring; it governs the
special persistent-default designation and its convenience fork flow. Clearing
an existing pointer to return to the application default is always allowed; it
narrows customization and must not be blocked by a revoked grant.

At set/fork time, the grant is resolved from the global and user scopes because
the preference itself is user-wide. At creation time it is resolved again with
the selected project scope. A project-level deny can therefore make the stored
personal default dormant inside that project without deleting the user's
preference elsewhere.

When the effective grant becomes false for a creation context, the stored
personal pointer is kept but ignored. The UI shows it as disabled by policy,
and restoring the grant restores the preference. Existing jobs/sessions are not
rebound.

### 11. Bases are conservative; experts explicitly opt into optional behavior

Mode bases contain required mechanics and reliable fallbacks, not the union of
all capabilities. At minimum:

- `session_base.yaml` explicitly disables Automations & Loops and other
  non-essential application-control groups.
- `worker_base.yaml` does not enable delegation, scholar pre-jobs,
  verification rounds, curator behavior, or loop controls by default.
- Datasource tool groups remain empty and are injected only for authorized,
  attached datasources.
- Empty list means explicitly disabled; omission means the layer has no
  opinion and may inherit.
- Capability grants always cap the result. An expert can request a capability,
  never grant it.

The exact tool lists are reviewed category-by-category during implementation.
Every bundled expert must explicitly declare the optional categories it needs
before they are removed from a base; this avoids silently weakening specialist
experts.

### 12. Seed experts are useful but baseline-compatible

The insert-only seed payloads should be broadly useful while fitting the
catalog's default grants:

| Profile | Initial policy |
| --- | --- |
| **Assistant** (`session`) | General collaborative persona; files/research/browser/citation/knowledge as applicable; normal session task tools; Automations & Loops off; no deny-by-default shell/delegation/VM requirement. |
| **General Worker** (`worker`) | General task-execution persona and worker lifecycle tools; files/research/browser/citation/knowledge as applicable; scholar/verification/delegation/loop behavior off; no deny-by-default shell/VM requirement. |

Admins can widen these DB rows for their deployment. A user with the relevant
grants can widen an owned fork. The universal seed itself must not make a newly
approved user unable to create work because it requires a deny-by-default
capability.

Neither seed needs to pin a deployment-specific model. An omitted model falls
through to the user's account default and then the administrator's system chat
default. An admin/user may pin model and reasoning in their DB expert when they
want the expert to be authoritative.

### 13. Prompt ownership stays layered

The base owns the generic operator wrapper, instruction hierarchy, safety
constraints, and a short role-neutral fallback persona. The expert owns its
persona and supported workflow/instruction content. Missing expert prompt
segments fall through to the base.

DB-authored content remains fenced and subordinate to the framework wrapper.
For sessions, v1 customization is the existing `persona` + `instructions`
surface; it does not permit replacing `systemprompt_interactive.txt`. If a
future `interactive_workflow` slot is added, it follows the safe design in
[[session_db_experts_cannot_customize_interactive_prompt]].

### 14. Missing defaults are an invariant failure, not a silent product choice

The two application pointer rows are non-null and protected by foreign keys.
If initialization or DB integrity leaves one unavailable, readiness reports a
degraded/error state and ordinary creation fails with an actionable 503. It
does not silently create work against a different personality.

Mode bases remain available for legacy explicit requests, internal recovery,
and field inheritance. They are not used to hide a broken application-default
invariant.

## Resolution Semantics

Default expert selection and config field merging are separate algorithms.

### Expert selection precedence

For a root creation request, highest precedence wins:

```text
1. Explicit compatible expert in the request
2. Compatible project default (project_experts.default_for)
3. User's personal default, when personal_default_experts is effectively true
4. Application default
5. Invariant error (ordinary creation) / explicit mode base (legacy or recovery only)
```

Rules:

- Type, visibility, ownership, project membership, and capability compatibility
  are validated server-side before persistence.
- A job uses its selected project's worker default when one is configured.
- A session uses a session project default only when it has one unambiguous
  primary project. A multi-project request without a primary project skips the
  project-default layer rather than choosing by list order.
- Existing `projects.default_config_name` is a legacy worker-only selector. It
  should migrate to `project_experts.default_for='worker'` when it names a
  compatible expert; unresolved legacy names continue through the compatibility
  resolver during the deprecation window.
- Project defaults are contextual conveniences, not capability grants. The
  runner's effective grants are still enforced against the resolved config.
- An incompatible default fails loudly. The resolver does not silently try the
  next default, because that would make policy errors look like personality
  changes.

### Field precedence inside the selected expert

For ordinary customizable fields, the intended order is:

```text
explicit request override
  > applicable operator DB config override
  > project_experts.config_override
  > selected expert's explicit value
  > user's account fallback
  > system/deployment fallback
  > mode base
  > hardcoded schema/emergency fallback
```

This preserves the existing `config_overrides` layer while moving account
defaults to their intended gap-filler position. Operator guardrails, credential
injection, capability ceilings, and immutable settings-matrix limits are policy
layers, not user preference layers; they keep their existing authoritative
position even where the simple preference ordering above does not describe
them.

Important consequences:

- If an expert pins `llm.model` or `llm.reasoning_level`, that value beats the
  account default.
- If the expert omits either field, the account/system default fills the gap.
- A model or reasoning choice made in this particular creation form is an
  explicit request override and wins.
- Saved account defaults must not be materialized as a high-precedence request
  override. The resolver applies them below the expert as gap-fillers.
- Arrays retain the existing replace-whole semantics. `[]` is an explicit
  disable, not “inherit.”
- Settings-matrix values are applied after the final model is known and fill
  only the model-family fields they own; explicit permitted fields retain their
  documented precedence.

Prompt precedence is deliberately different in altitude:

```text
non-overridable operator wrapper / safety constraints
  > fenced expert persona and workflow content
  > generic base persona/content
  > per-request user instructions as user content
```

“Higher” here means authority, not simple string replacement. User-authored
expert text never becomes an unfenced operator system prompt.

### Tool-group resolution

The final resolved tool policy, not only the request override, drives persistent
session auto-injection. For the four session-facing groups (`orchestrator`,
`agent_catalog`, `workflows`, and `canvas`):

- final `[]` means disabled and produces the matching runtime disable marker;
- final non-empty list means enabled with the allowed listed/default group
  semantics;
- absence follows documented compatibility behavior only for legacy configs;
  new bases always express an explicit policy.

This replaces the current behavior where the private markers are derived only
from `metadata.config_override`, which makes a base/expert-level `[]`
ineffective.

## Persistence Model

Use relational pointers rather than IDs hidden in `users.settings` or
`system_settings`. Foreign keys, deletion blockers, and type invariants are
important because a dangling default affects every creation path.

### Managed seed experts

Extend `experts` in the next app migration:

```sql
ALTER TABLE experts
  ADD COLUMN managed_key  TEXT UNIQUE,
  ADD COLUMN seed_version INTEGER;

-- Human-owned rows keep owner_id. Platform-managed seed rows have a stable
-- managed_key, no human owner, and are global. The migration adjusts owner_id
-- nullability and adds a CHECK enforcing exactly one ownership form.
```

Stable keys:

- `application-default-worker-seed`
- `application-default-session-seed`

The initial file bundles are `config/experts/general-worker/` and
`config/experts/assistant/`. They remain packaged for legacy name resolution,
but their managed DB counterparts are the application-default runtime objects.

`owner_id` must not point at the first admin: deleting or demoting a human must
not delete the platform's default expert. Platform-managed rows are admin-editable
and non-deletable. Existing human-owned rows retain their current ownership and
delete behavior.

The API should expose storage/management explicitly (for example
`storage_kind: bundled|db` and `managed_key`) rather than teaching clients that
`source === user|global` happens to imply a UUID. All DB experts, regardless of
provenance, are selected through `expert_id`.

### Application pointers

```sql
CREATE TABLE application_expert_defaults (
    expert_type TEXT PRIMARY KEY CHECK (expert_type IN ('worker', 'session')),
    expert_id   UUID NOT NULL REFERENCES experts(id) ON DELETE RESTRICT,
    updated_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

There must be exactly one row per type. The service validates that the target is
a matching-type, global DB expert. The type invariant should also be enforced
with a composite `(expert_id, expert_type)` foreign key or an equivalent DB
constraint, not only a Pydantic check.

### Personal pointers

```sql
CREATE TABLE user_expert_defaults (
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expert_type TEXT NOT NULL CHECK (expert_type IN ('worker', 'session')),
    expert_id   UUID NOT NULL REFERENCES experts(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, expert_type)
);
```

The write transaction validates that `experts.owner_id = user_id` and the type
matches. Normal expert deletion treats a personal-default reference as a
blocker and offers “Use application default and delete” as an explicit combined
operation. Account deletion still cascades cleanly.

Default-pointer changes should emit the existing platform audit event shape (or
a small dedicated audit row) with actor, scope, type, old expert ID, and new
expert ID. Application-default changes especially must be attributable.

### Reuse project defaults

Do not add another project setting. Activate the already-shipped
`project_experts.default_for` relation and its unique per-project/type index.
Its write API validates matching expert type and project/editor authority.

## Seed and Upgrade Behavior

Initialization is idempotent and insert-only:

1. Read the Assistant and General Worker bundled seed bundles.
2. Insert each managed expert only when its `managed_key` does not exist.
3. Insert missing application pointer rows targeting those seeds.
4. Never update an existing managed expert or existing pointer.

The DB expert stores a fragment relative to its type base plus supported prompt
fields, exactly like any other DB expert. It must not store the fully merged
base. This keeps future base fixes available to experts that did not override
the affected field.

The catalog de-duplicates the legacy bundled Assistant/General Worker entries
when their managed DB counterparts exist. The disk copies remain resolvable by
legacy `config_name` during the compatibility window, but normal pickers show
the DB records.

An admin-facing **Reset from bundled template** action, if implemented, must:

- preview the diff;
- name the bundled `seed_version`;
- pass the same validation and grant checks as expert update;
- require confirmation; and
- create an audit record.

It is never run implicitly at startup.

## Base Rename and Compatibility

Canonical names become:

| Old name | New canonical name |
| --- | --- |
| `config/defaults.yaml` / `defaults` / `default` | `config/worker_base.yaml` / `worker_base` |
| `config/persistent_defaults.yaml` / `persistent_defaults` / `persistent_default` | `config/session_base.yaml` / `session_base` |

Implementation requirements:

1. Rename both files and update all bundled `$extends` declarations.
2. Centralize logical-name canonicalization in `resolve_config_path`; do not
   spread string checks across routes and provisioners.
3. Keep old logical names as read aliases for at least one release. New writes,
   API responses, logs, and generated configs use canonical names.
4. Continue reading old values in `jobs.config_name`, thread metadata,
   automations, projects, imported expert bundles, CLI arguments, and external
   YAML `$extends` declarations.
5. Do not rewrite historical frozen job configs merely to change a label.
6. Update type inference everywhere: `worker_base` means worker and
   `session_base` means session. Bundled experts must be filtered by type in both
   backend and UI; type filtering cannot apply only to DB rows.
7. Replace direct `config_dir / "defaults.yaml"` reads with the shared resolver.
8. Keep `GET /api/experts/defaults?type=...` as a deprecated compatibility
   alias if needed; a clearer base-detail endpoint may replace it. Normal create
   UI fetches the effective default expert, not the base-detail endpoint.

Completed historical docs may retain old names when describing old behavior.
Active docs, examples, tests, and comments use the new terminology.

## API Design

Exact route naming may follow current router conventions, but the required
surface is:

| Method | Suggested path | Purpose |
| --- | --- | --- |
| `GET` | `/api/expert-defaults` | Return application, personal, project (when context supplied), and effective defaults for both types, including provenance and grant state. |
| `GET` | `/api/expert-defaults/resolve?type=&project_id=` | Preview the exact server-side selection used by creation. |
| `PUT` | `/api/users/me/expert-defaults/{type}` | Set a matching owned DB expert as the caller's personal default. Grant-gated. |
| `DELETE` | `/api/users/me/expert-defaults/{type}` | Clear the personal pointer and return to project/application resolution. Always allowed to the owning user. |
| `POST` | `/api/users/me/expert-defaults/{type}/fork` | Atomically duplicate a visible compatible source expert and set the owned copy as personal default. Grant-gated. |
| `GET` | `/api/admin/expert-defaults` | Admin view of both application pointers and seed/update state. |
| `PUT` | `/api/admin/expert-defaults/{type}` | Atomically select a compatible global DB expert as the application default. |

Creation endpoints keep accepting explicit `expert_id`. When it is omitted for
an ordinary root creation, they call the shared resolver and persist its result.
They return the selected expert ID and provenance so callers can explain what
happened.

Explicit bundled `config_name` remains supported during compatibility, but a
request must not provide conflicting `expert_id` and bundled expert selection.
Reject ambiguous bodies with 400 rather than relying on incidental precedence.

The preview response should include at least:

```json
{
  "expert_type": "session",
  "expert": {"id": "<uuid>", "display_name": "Assistant"},
  "source": "project|personal|application",
  "personal_default_allowed": true,
  "compatible_with_grants": true
}
```

## Cockpit UX

### New Job / New Session

- Load only experts compatible with the creation mode.
- Ask the default resolver for the current project/context and preselect its DB
  expert.
- Show a small provenance label on the selected card.
- Treat cards as a radio group; no click-to-deselect behavior.
- When project selection changes, re-resolve the default only if the user has
  not manually chosen another expert in this draft.
- If the user manually selects an expert, that becomes the explicit request
  selection.
- Keep **Framework base** under Advanced/operator diagnostics if retained.
- Submit UUID DB experts with `expert_id`; never infer DB storage from a display
  `source` string.

The server still resolves omissions so a stale, failed, or bypassed UI cannot
change semantics.

### User Settings

Add an **AI defaults** section with independent Worker and Session cards:

- effective expert and provenance;
- **Customize my default** (fork current effective expert and set it);
- **Choose one of my experts**;
- **Edit my default**;
- **Use application default**.

If `personal_default_experts` is false, the section shows the application
default and a concise “Managed by your administrator” explanation. A dormant
personal choice may be displayed but cannot be changed or applied; the user
may still clear/forget it.

Retire `users.settings.persistent_agent.config_name` as the session expert
selector. During migration, resolve a valid owned DB reference into the new
table when possible; otherwise leave the user on the application default and
surface a one-time warning rather than guessing between incompatible bundled
types.

### Admin Settings

Add an **Application default experts** panel:

- Worker default selector (global DB worker experts only).
- Session default selector (global DB session experts only).
- Edit current expert.
- Duplicate, edit, and switch (recommended no-existing-session-impact flow).
- Seed version/status and optional reset-from-bundle action.
- Compatibility preview against catalog/global grant floors.

Setting an application default is blocked if the target is the wrong type,
non-global, deleted, or invalid. If it requests capabilities denied by the
deployment's global floor, the UI warns and the API rejects unless the policy
is corrected; no silent tool removal occurs.

The existing Admin → Grants page automatically renders
`personal_default_experts` from the catalog.

## Capability and Security Rules

- Default selection never bypasses normal expert visibility or project access.
- An arbitrary UUID cannot be persisted without resolving it for the caller.
- Personal default writes require ownership, matching type, and the effective
  grant.
- Application default writes require admin, matching type, and a global DB row.
- The selected expert is evaluated against the **runner's** grants at the same
  create/dispatch/attach enforcement points used today.
- Default resolution never auto-elevates tools, model access, autonomy, or
  permission mode.
- Default incompatibility is reported with the existing actionable capability
  violations. It is not fixed by substituting another expert.
- DB prompts retain the existing persona/phase fences. Platform-managed does
  not mean untrusted content becomes an operator wrapper: admins may edit it,
  but the same structural prompt boundary remains simpler and safer.
- Application pointer changes and managed-expert updates are audited.

## Lifecycle and Edge Cases

### Deletion

- A managed seed expert cannot be deleted.
- An application-default target cannot be deleted until the pointer is switched
  (`ON DELETE RESTRICT`).
- If that target is a human-owned global expert, deleting its owner is likewise
  blocked until the application pointer is switched or the expert is rehomed.
- A personal-default target is an expert-delete blocker. The UI can combine
  “return to application default” and deletion in one explicit transaction.
- Existing live thread and pending-job blockers from
  [[global_expert_management]] remain.

### Grant revocation

Revoking `personal_default_experts` makes the project/application default
effective for future creations in the denied scope and leaves the stored
personal pointer dormant. The user can still clear it. Revocation does not
mutate existing jobs or sessions. Other capability revocations keep their
current enforcement timing and can make an expert fail at dispatch/attach.

### Admin default edits

- Switching the pointer affects new creations only.
- Editing the pointed-to expert can affect undispatched jobs and reattached
  sessions according to existing resolution timing.
- The UI recommends duplicate-edit-switch when the admin wants a clean rollout
  boundary.

### Automations and other headless creation

- An automation with an explicit expert remains pinned to it.
- An automation without one resolves the owner's effective worker default when
  it fires, including its project default, and stores the resolved UUID on the
  created job.
- Instant landing resolves the effective session default from its minimal
  create body.
- MCP and direct REST omission behave identically.
- System/internal root work with no user must supply an explicit expert or an
  explicitly authorized mode base; it must not borrow an arbitrary human's
  default.

### Existing work during rollout

- Finished/running jobs keep their frozen resolved config.
- Existing jobs/threads with `expert_id` keep it.
- Existing records with an explicit bundled `config_name` continue through the
  legacy-name aliases and are not rebound to an application default.
- Existing bare records are not bulk-pointed at whatever the admin happens to
  choose during migration. New creations use the new resolver.
- Because sessions re-resolve on attach, base hardening can affect legacy
  base-only sessions. The rollout must either preserve the old optional tool
  behavior with a legacy tool-surface version marker or explicitly communicate
  the least-privilege change; it must not happen accidentally as a side effect
  of the filename rename.

## Implementation Slices

### Slice 1 — terminology, aliases, and base rename

- Rename the two files and every bundled `$extends`.
- Add centralized legacy-name canonicalization.
- Replace direct base-file reads with the shared path resolver.
- Fix expert type inference/filtering for bundled and DB experts.
- Keep behavior otherwise equivalent in this slice.

### Slice 2 — DB schema and seeds

- Add managed seed metadata/ownership support to `experts`.
- Add application and personal default tables and deletion blockers.
- Add Assistant and General Worker insert-only seed bundles.
- Seed the two application pointers idempotently.
- De-duplicate seed templates from the visible merged catalog.

### Slice 3 — shared resolution and APIs

- Add one default-expert selection service.
- Wire root job/session creation, instant landing, MCP, REST, and automation
  firing through it.
- Persist the resolved expert UUID and provenance where useful.
- Activate `project_experts.default_for` in the selection chain.
- Correct account model/reasoning defaults to be gap-fillers below explicit
  expert values.

### Slice 4 — grant and Cockpit

- Add `personal_default_experts` to the capability catalog with default `true`.
- Add user/admin default APIs and audit events.
- Build Settings and Admin panels.
- Make job/session expert pickers type-safe, preselected, and non-null.
- Update English and German translations and run `npm run i18n:check`.

### Slice 5 — conservative base audit

- Inventory every base-provided tool/behavior and every bundled expert's true
  dependencies.
- Move optional product behavior from bases into the experts that need it.
- Make session auto-injected group enablement derive from the fully resolved
  tool policy.
- Harden Assistant and General Worker seed payloads to the approved initial
  policy.
- Verify legacy base-only session handling explicitly.

Keeping the behavioral hardening after selection/seeding makes regressions
attributable and gives every normal creation a named DB expert before the base
becomes minimal.

## Acceptance Criteria

1. A fresh install has exactly one application worker default and one
   application session default, both referencing DB UUIDs.
2. The seeded defaults are General Worker (`worker`) and Assistant (`session`).
3. Restarting or upgrading does not overwrite admin edits or pointer choices.
4. Creating a root job/session with no expert through Cockpit, REST, MCP,
   instant landing, or an unpinned automation persists the same server-resolved
   DB expert that the preview endpoint reports.
5. Job/session creation forms always show one selected compatible expert and
   cannot toggle it to no selection.
6. A project default wins over personal/application defaults; an explicit
   request wins over the project default.
7. A user with `personal_default_experts=true` can atomically fork the effective
   default, customize it, and set it for the matching type.
8. With the grant false, the same operation is rejected, the stored personal
   pointer is dormant, and new work uses project/application default.
9. A model/reasoning value explicitly set by the selected expert beats account
   fallbacks; if omitted, account then system values fill it.
10. Assistant/General Worker prompt overlays render inside the existing trust
    fences; the base prompt remains generic and role-neutral.
11. Automations & Loops are absent from a new default session unless the
    selected expert explicitly enables them, verified from the agent's actual
    tool list—not only the YAML or form state.
12. Default seed experts run for a newly approved, non-admin user with catalog
    default grants; they do not require shell, delegation, VM, or autonomous
    permission.
13. The application-default target cannot be deleted; a personal-default expert
    cannot be deleted without explicitly clearing/replacing the pointer.
14. Switching an application pointer does not change existing jobs/threads'
    persisted expert IDs.
15. Old `default`, `defaults`, `persistent_default`, and
    `persistent_defaults` names continue to resolve during the compatibility
    window, while all new writes use `worker_base`/`session_base`.
16. Job creation never offers session experts and session creation never offers
    worker experts, for both bundled and DB sources.
17. A missing/corrupt application pointer fails readiness and ordinary creation
    clearly; it does not silently create a base-only agent.

## Verification Matrix

Backend tests:

- selection precedence across explicit/project/personal/application;
- personal grant allow/deny/re-enable behavior;
- type, ownership, visibility, and global-target validation;
- concurrent fork-and-set and admin pointer updates;
- delete blockers and account cascade behavior;
- idempotent seed with admin-edit survival;
- model/reasoning gap-fill precedence;
- all legacy name aliases and bundled type inference;
- create paths for jobs, sessions, automations, MCP-forwarded requests, and
  instant landing;
- capability-incompatible default fails loud;
- resolved session `[]` creates runtime disable markers.

Cockpit tests:

- effective default is preselected;
- selected card cannot be deselected;
- project change re-resolves only before manual selection;
- DB UUID is submitted through `expert_id` regardless of provenance label;
- grant-disabled Settings state;
- fork/set/clear flows for both types;
- admin duplicate/edit/switch warning and validation;
- worker/session catalog filtering includes bundled type metadata.

Live verification:

1. Fresh k3d install seeds both DB defaults.
2. Customize the application Assistant's prompt/model/tool policy, restart the
   orchestrator, and confirm the DB version survives.
3. Create a no-options instant session and confirm it stores/uses the Assistant
   UUID with Automations & Loops absent.
4. Fork Assistant as a regular user's personal default, change its persona,
   and confirm only that user's new sessions receive it.
5. Globally deny `personal_default_experts`, confirm future sessions use the
   application default, restore the grant, and confirm the prior personal
   pointer becomes effective again.
6. Create a no-options job and confirm General Worker is selected and its
   resolved config remains frozen after dispatch.
7. Switch both application pointers and verify existing records retain their
   persisted expert IDs while new work uses the replacements.

## Decisions This Document Supersedes or Clarifies

- [[global_expert_management]] Decision 1 remains true for the general bundled
  catalog but gains the two managed default-seed exceptions described here.
- [[application_tool_surface_baseline]]'s “default-on when absent” rule remains
  a legacy compatibility rule, not the policy of new `session_base` configs.
- [[settings_design]] account model/reasoning values are gap-fillers below an
  expert's explicit values; paths that currently turn them into request-level
  overrides must be corrected.
- [[instant_landing_session]]'s deferred default-expert slot is implemented by
  this server-side resolver; the landing client remains minimal.
- [[default_expert_roster]]'s Assistant recommendation is adopted and extended
  with a general worker counterpart for the other structural expert type.
