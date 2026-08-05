# Project-scoped connector availability and auto-attach defaults

Status: **IMPLEMENTED 2026-08-05; LIVE VALIDATION PARTIAL** — policy UI
advertisement and generic REST omission-as-default remain behind separate
default-off rollout gates described in section 12.2.

A k3d/Playwright acceptance run on 2026-08-05 exercised all three rollout
profiles (R0/R1/R2) against the `aec2e5da` build and found **no product
failures in 35 assertions**. Confirmed live: the R0 disabled posture, the owner
eligibility/default matrix (0 deviations across 30 cells), the member matrix
(no cross-owner default expansion in either eligibility or R2 omission), job and
thread materialization corroborated against `job_datasources` /
`jobs.context.datasource_selection` / `threads.metadata`, the full raw
validation contract (9 probes → 422), stale-revision 409, the native project KB
lock (rescope/relink/delete all 409), existing-work immutability, final-unlink
non-widening, and the complete R2 omission semantics.

**Not yet live-accepted.** Automations, project loops, retained-only links,
workspace-tier filtering, public/shared policy, delegation inheritance, MCP
transport, and the dispatch-revocation boundary are still unproven on a
deployment — several are blocked on missing prerequisites (an authenticated MCP
client, a `public_datasources` grant, documented dispatch/batch fault hooks).
Per-row results, evidence and three follow-up findings:
[`docs/done/project_scoped_connector_auto_attach_k3d_playwright_validation.md`](../done/project_scoped_connector_auto_attach_k3d_playwright_validation.md).
Date: 2026-08-05
Scope: orchestrator datasource persistence and authorization, job/session creation,
system-created jobs, MCP clients, and the Cockpit connector catalog/create forms and
job/session pickers.

Related: [[datasources]], [[multi_datasource_support]], [[projects]],
[[public_datasources]], [[knowledge_base_repo_separation]],
[[2026-07-16-live-session-settings]].

## 1. Summary

Connectors gain two independent policies:

1. **Availability scope** — either available throughout the creator's work, or
   restricted to an explicit set of projects.
2. **Auto-attach** — the connector owner's preference for whether their available
   connector is selected by default for their newly created work.

The rule is deliberately small:

```text
target_project_grant =
    work has at least one project
    AND every work project is linked to the connector
    AND effective user is currently a member of every work project

execution_authorized =
    connector owner
    OR public connector
    OR target_project_grant
    OR administrator making an explicit selection

scope_matches =
    scope_mode == "all"
    OR (
        scope_mode == "projects"
        AND work has at least one project
        AND every work project is linked to the connector
    )

available =
    execution_authorized AND scope_matches

default_selected =
    available
    AND auto_attach
    AND connector.created_by == effective_work_owner_id
```

Project-granted access is bound to the target context. Membership through Project A
must not make a private all-scope connector portable into Project B or projectless
work. The administrator branch applies only to explicit selection; management or
override access never creates an ambient default.

The owner condition is deliberate. Project-linked and public connectors owned by
someone else remain available for explicit selection, but their publisher cannot
change another user's job defaults. A future project-wide automatic binding belongs
to a project-admin-controlled policy, not this creator preference. The native project
knowledge resource remains a separately managed exception because it belongs to the
project rather than to an individual connector publisher.

Auto-attach remains a **creation-time default**, not a dispatch-time force-attach
rule. The selected IDs are materialized into the existing explicit attachment
stores:

- jobs: `job_datasources`;
- persistent sessions: `threads.metadata.datasource_ids`.

After creation, the ordinary explicit-only resolver remains authoritative. Changing
`auto_attach` never mutates an existing job or session.

## 2. Problem

The current behavior has two mismatched halves:

- `resolve_datasources_for_job` and `resolve_datasources_for_thread` are correctly
  explicit-only. Public and project-linked connectors do not widen the result set.
- The Cockpit's create-job and create-session picker treats **every eligible
  connector as selected by default**. Eligibility is currently the union of every
  connector the caller owns, every public connector, and every connector linked to
  the selected project(s).
- Non-UI root creation paths do the opposite. Automations attach nothing, external
  MCP calls attach only explicitly supplied IDs, while project loops currently
  special-case the issue by attaching every connector linked to the project.

This produces surprising outcomes in both directions. A connector for Application A
is preselected when its owner creates unrelated work in Application B, while an
automation for Application A receives no connector unless that creation path has its
own bespoke attachment logic.

The intended example is:

> A user creates an application-database connector, limits it to the Application
> project, and enables auto-attach. That user's new jobs, sessions, loops,
> automations, and omitted-selection MCP jobs for that project receive it by
> default. It is absent
> from picker and system-default resolution for every other project, the user's
> default project, and projectless work.

If the connector is shared with another project member, that member may select it
when it is in scope, but it is unchecked by default unless a later project-owned
default policy explicitly says otherwise.

## 3. Goals

- Let the connector creator restrict every connector type to one or more projects.
- Keep all owned connectors visible and manageable in the creator's connector
  catalog regardless of execution scope.
- Replace “all eligible is checked” with an explicit connector-level
  owner-specific `auto_attach` preference.
- Apply the same defaulting semantics to UI and non-UI root creation paths.
- Preserve explicit-only runtime resolution and durable, inspectable selections.
- Preserve the distinction between an omitted selection and an explicit empty
  selection.
- Keep administrator management powers from becoming ambient default execution
  access.
- Enforce project scope on the server so a guessed connector UUID cannot bypass it.
- Reuse `project_datasources`; do not store project UUID arrays in a datasource
  JSONB field.
- Degrade safely on lite workspace tiers and after project deletion.

## 4. Non-goals

- Force-attaching a connector at every dispatch.
- Changing an existing job/session when `auto_attach` changes.
- Per-project auto-attach differences for one connector. In v1 a connector is
  auto-attached in its owner's work for every project in which it is available or in
  none of them.
- Per-user defaults for someone else's public connector.
- Project-wide defaults chosen by a connector publisher. A later project-level
  binding must be controlled by a project owner/admin.
- Organization-wide automatic connectors. That deserves a separate admin-owned
  policy rather than piggybacking on a publisher's connector preference.
- Replacing the explicit `job_datasources` or thread-metadata attachment stores.
- Turning project cloud mounts into datasource connectors. Main-cloud mounts remain
  project resources with their own lifecycle.

## 5. Locked semantics

### 5.1 Management visibility and execution availability are different

The creator always sees every connector they own in the main Connectors catalog and
can edit, test, filter, or delete it. Project scope only controls whether the
connector is available to a job/session picker or a system-created job in a given
project context.

Keep four decisions separate:

| Decision | Question | Relevant policy |
|---|---|---|
| Management visibility | May this caller list/inspect the row? | creator, project membership, administrator management access |
| Execution authorization | May this effective user explicitly use its stored credentials? | owner, public execution access, or membership through a project link |
| Scope match | May it be used in these work projects? | `scope_mode` plus linked project IDs |
| Default selection | Should it be selected without an explicit choice? | available, `auto_attach=true`, and effective work owner equals connector owner |

An administrator's broad catalog access is not an auto-attach entitlement. Default
resolution must never scan every connector merely because the effective user is an
administrator. Administrators may retain existing explicit-use powers, but ambient
defaults are limited to their own connector rows (plus separately defined
project/system-managed resources).

The current code intentionally has different public behavior on its two reads:
`GET /api/datasources/eligible` includes `is_global=true` rows, while
`user_can_access_datasource`/`GET /api/datasources` do not expose an unlinked public
row to a non-creator merely because it is public. Older design notes describe public
rows as catalog-visible. This feature must not accidentally choose between those
contracts. Treat `is_global` here as an **execution/picker authorization** input and
preserve the current management-list behavior; align public catalog visibility in a
separate issue if desired.

The new scope check only narrows an otherwise-authorized connector. It never grants
access by itself.

### 5.2 Scope has an explicit mode

`scope_mode` has two v1 values:

| Value | Meaning |
|---|---|
| `all` | Does not further restrict an execution-authorized user by work context; it includes projectless work. Existing owner/sharing/public rules still decide who is authorized. |
| `projects` | Available only when work has projects and **every** work project has a matching `project_datasources` row. This applies to the creator too. |

The mode must be stored explicitly. It must **not** be inferred from whether the
connector currently has project links. Otherwise deleting the final linked project
would cascade-delete the final junction row and silently broaden a production
connector from “only this project” to “all projects.”

Creation and ordinary edits reject `scope_mode="projects"` with an empty project
selection. A later project deletion or explicit project unlink may legitimately
leave such a connector with zero links; it then remains unavailable everywhere
until its creator edits it. Neither event silently changes `scope_mode`.

### 5.3 One auto-attach flag

`auto_attach` belongs to the connector, not to `project_datasources` in v1:

- an `all` connector with `auto_attach=true` is selected by default in its owner's
  new work wherever it is otherwise available;
- a `projects` connector with `auto_attach=true` is selected by default in its
  owner's new work only when every work project is linked;
- `auto_attach=false` leaves an available connector visible but unchecked.
- shared/public connectors owned by someone else remain unchecked even when their
  own creator enabled `auto_attach`.

This keeps resolution to “available first, ownership second, auto-attach third.” If
teams later need “automatic for everyone in Project A, optional in Project B,” add a
project-owner-controlled `auto_attach` policy to `project_datasources`; do not let a
connector publisher set other users' defaults indirectly.

### 5.4 Explicit selection wins

The request shape retains three distinct states:

| Request state | Effective selection |
|---|---|
| `datasource_ids=[...]` | Exactly the authorized, in-scope IDs supplied |
| `datasource_ids=[]` | No connectors |
| field omitted on a parented job | Inherit the parent thread first, otherwise the parent job |
| field omitted on a root job/session | Available connectors with `auto_attach=true` |
| `datasource_ids=null` | Reject with 422; null must not silently become omission |

Defaults are a replacement seed, not an additive merge. If an AI supplies one
connector explicitly, the system does not silently add three automatic connectors
beside it.

Parent inheritance remains above root defaults because a child should preserve the
concrete data context of the task that spawned it. Scholar/critic/curator propagation
continues copying the parent's materialized selection.

Pydantic currently maps an omitted optional field and an explicit JSON `null` to the
same Python value. Creation models must inspect `model_fields_set` (or use an
equivalent request-layer sentinel) to reject explicit null while preserving omission.
Every client must likewise use presence checks, not list truthiness.

### 5.5 Selections are materialized once

Creation computes defaults, authorizes them, filters them for the workspace tier,
and persists their IDs. Dispatch and resume read the persisted selection; they do
not rerun `auto_attach`. Therefore:

- enabling auto-attach affects future work only;
- disabling it does not detach existing work;
- a newly created connector does not appear in an already-running session;
- child jobs inherit a stable parent selection even if defaults change later.

Restricting scope or removing a project link is different from changing a default:
it revokes future eligibility. It does not attempt to remove credentials from an
already-running agent mid-turn, but the selection is revalidated at the next
authorization boundary (session attach/resume, job dispatch/resume, or child
creation) before credentials are delivered.

A materialized connector is a required part of that work's data contract. If any
stored ID is missing, no longer authorized, or out of scope at a later boundary, the
server rejects the **whole** attachment set; it never runs the agent with a silently
reduced set of data. A queued/resumed job fails closed with a stable
`connector_unavailable` reason before dispatch. A session attach/resume returns a
conflict and remains detached until the owner removes or replaces the unavailable
connector. User-facing detail may identify a connector the caller is still allowed
to manage; cross-user/API errors remain non-enumerating.

Scope is not an emergency credential-revocation mechanism. A process that already
received credentials may retain them until its current turn/process ends. Scope
narrowing must show this limitation; immediate revocation requires rotating the
external credential and, for a persistent workspace, reprovisioning/restarting it.

### 5.6 Project matching

- A normal job has at most one project; a project-scoped connector must match it.
- Projectless work cannot use a project-scoped connector.
- The default project is an ordinary project and matches only when explicitly linked.
- Ordinary root job creation resolves the user's default project before resolving
  connector defaults. “Projectless” therefore applies only to APIs/system/session
  contexts that genuinely remain without a project, not to a transient pre-default
  state.
- Persistent sessions may span several projects. V1 uses **all-match** semantics:
  every selected session project must be linked. A connector linked only to Project A
  is not available in an A + B session. This preserves the promise that restricting a
  connector to A does not introduce it into a context that also carries B data.
- After that all-match authorization succeeds, multi-project resolution returns one
  deterministic row per selected connector. Project-level `read_only` overrides are
  combined conservatively with `BOOL_OR`: if any matched project link is read-only,
  the connector is read-only for the combined session. Differing A/B overrides must
  never duplicate the connector or its policy revision in the delivery payload.
- All project IDs used for matching must already have passed the existing membership
  authorization check.

All-match is intentionally conservative. If sessions later gain one explicit “active
project” for connector resolution, that active project can replace the set comparison
without weakening the stored connector scope.

### 5.7 Public connectors

`is_global` remains the public execution-authorization flag; `scope_mode` controls
the work contexts in which an authorized connector is eligible. Management-list
visibility remains the separate current contract described in 5.1.

- Public + `scope_mode="projects"` is allowed. It is eligible only to members of a
  linked project.
- Public + `scope_mode="all"` is broadly available for explicit selection.
- Either public form may have `auto_attach=true`, but it defaults only into work whose
  effective owner is the connector creator. Other users always see it unchecked.
- A future organization-wide default should be a separate, administrator-controlled
  binding with its own audit and confirmation flow.

### 5.8 Lite workspace tiers

The existing repository boundary stays intact:

- an explicitly selected clone-based `repository` on `virtual`/`none` is rejected;
- an implicitly defaulted repository is filtered out rather than making an otherwise
  valid system-created lite job fail;
- centrally indexed `kb` connectors remain available on every tier.

## 6. User-visible behavior

### 6.1 Connector create/edit form

Every connector type gets the same shared “Availability” section after its
type-specific connection fields:

```text
Availability

( ) Everywhere in my work
    Available in all projects and in standalone work, subject to sharing and
    public-access rules.

( ) Selected projects
    Available only when every project carried by the work is selected here.
    [ Search and select projects...                         v ]

[ ] Attach by default to my new work
    Preselected in create forms and attached to my loops, automations, and
    API/MCP-created work when no connector list is supplied. Existing work is
    unchanged, and other users do not inherit this preference.
```

Rules:

- A newly rendered Cockpit form requires a deliberate scope choice. When opened
  from Project A, prefill `Selected projects: A`; when opened from the global
  catalog, neither radio is preselected. The REST compatibility default remains
  `scope_mode="all"`.
- “Selected projects” requires at least one selection on create/update.
- Removing the final project does not implicitly switch to “Everywhere in my work.”
  The user must choose that radio explicitly.
- The multiselect lists only projects the caller is authorized to manage for
  connector linking. It supports server-side search/pagination, selected-count
  copy, and removable chips. Existing links the creator could not add today remain
  visible as retained-only choices: they may be kept or revoked, but cannot be
  re-added without current target-project authority.
- The same section is reused by generic, repository, KB, managed database, WebDAV,
  email, MCP, and credential-file connector forms.
- Enabling auto-attach shows an impact summary such as “Automatic in my work for
  3 selected projects.” Shared users are explicitly described as unaffected.
- Scope state must finish loading before edit controls or Save become available;
  a failed project/link load offers Retry and never falls back to `all`.

The current Cockpit `Project` model omits the `user_role` already returned by the
database, and the generic project list is capped. Add a dedicated paginated
`GET /api/projects/linkable-datasource-targets?datasource_id=...&q=...&cursor=...`
read. It returns typed role plus `addable`/`retained_only` state, includes current
links on edit, and returns only authorized additions. Omit `datasource_id` for create.
Authorization remains server-enforced on write.

The response separates the searchable page (`items`, `next_cursor`) from an
unpaginated `selected_items` array. On edit, `selected_items` contains every
currently linked project visible within the caller's token scope regardless of
the search term or cursor, so a paginated form cannot silently remove an off-page
or retained-only link. It is empty when `datasource_id` is omitted.

Suggested user-facing terminology is **connector**. `datasource`, `auto_attach`,
`scope_mode`, and `project_datasources` remain internal compatibility names.

### 6.2 Connector catalog

The creator's catalog remains complete. Add optional filters suitable for users with
many connectors:

- free-text name/description search;
- connector type;
- project;
- availability (`Everywhere` / `Project scoped` / `Unavailable`);
- auto-attach (`Automatic` / `Manual`);
- visibility (`Private` / `Public`);
- existing readiness/index state where relevant.

Rows show compact `All projects`, `N projects`, and `Auto` badges. Filtering does not
change authorization.

These filters must be server-side from the first release. Today the database query
limits the newest 100 raw datasource rows **before** the REST layer applies caller
visibility, and Cockpit sends no pagination arguments. Client-side filters would
therefore hide older owned connectors and violate the complete-catalog requirement.
Refactor the catalog query so authorization and filters run in SQL before a stable
`(created_at, id)` cursor/limit. Cockpit provides Load more/infinite paging and a
compact filter menu rather than adding another row of chips. Include `Mine / Shared`.

Keep the existing `GET /api/datasources` array contract for compatibility and add a
Cockpit-focused `GET /api/datasources/catalog` endpoint:

```json
{"items": [], "next_cursor": null}
```

It supports `q`, `type`, `project_id`, `scope_mode`, `auto_attach`, visibility,
ownership, `limit`, and an opaque `(created_at,id)` cursor. Filtering/authorization
precedes the cursor limit. Project-ID enrichment and project filtering must not reveal
inaccessible project associations. Register/test the static `/catalog` route so the
dynamic `/{datasource_id}` route cannot consume it.

The current table's “Scope” badge actually represents `is_global` visibility
(`Public`/`Private`/legacy job). Rename that concept to **Visibility** and add a
separate **Availability** badge (`Everywhere`, `N projects`, or `Unavailable`) plus
the owner-only `Auto` badge. For non-owners whose project-link list is redacted, use
`Project scoped` without a count.

### 6.3 Job/session picker

`GET /api/datasources/eligible` returns only connectors available in the selected
project context. The picker:

- checks rows whose server-computed `default_selected` is true;
- leaves other eligible rows unchecked;
- never renders out-of-scope connectors;
- recomputes eligibility/defaults after the selected project set changes;
- drops stale out-of-scope IDs from its submission;
- keeps the current attached selection as the default in live-session edit mode,
  rather than reapplying connector defaults.

Eligibility is part of the submitted context, not an optional decoration. Project
changes must cancel/ignore older HTTP responses (for example with `switchMap` or a
monotonic request serial plus a project-context key), and Create stays disabled while
the current request is loading or failed. A failure renders Retry; it must not be
converted to an empty eligible list, because unconditional `[]` submission would
silently opt out and a stale list could attach the wrong project's connector.

Preserve deliberate choices across a context refresh:

```text
first successful context load:
    select rows where default_selected is true

later context load:
    keep touched selected/unselected choices for IDs still eligible
    drop IDs no longer eligible
    initialize newly eligible IDs from default_selected

Reset:
    discard touched state and restore the current server defaults
```

Key the state by execution context as well as connector IDs. The current
`datasourceSetKey` fallback loses manual choices whenever the set changes, and the job
form reset changes its project without refetching connectors; both behaviors must be
fixed.

The settings summary must compare the selection with the current
`default_selected` set (symmetric difference) rather than counting every defaulted
connector as a user modification.

Both create forms must always submit `datasource_ids`, including an empty array. Once
omission means “apply defaults,” their current `if (dsIds.length > 0)` behavior would
turn “Deselect all” into “attach the defaults anyway.”

Cockpit also has creation paths without the full picker. Officer/conference sessions
are system-created and intentionally use defaults. The instant draft/landing session
is user-created: before the first send it must await a stable default-project lookup
and expose a compact “Default connectors (N)” control that can opt out. Do not attach
credentials before the user has an opportunity to review that preselection. Remove
or align the legacy inline session dialog rather than leaving a second omission
contract.

API services must let eligibility and policy-write errors propagate to these explicit
states. Do not use the current `catchError(() => of([]))`/`update -> null` behavior for
security-sensitive scope/default operations.

## 7. Data model

Use the next available app migration. Authored as `0082` at design time, this
shipped as `0083_datasource_scope_auto_attach.sql` — `0082` was claimed by
`0082_usage_cloud_rate_cards.sql` from a parallel branch, and per
docs/db_migration.md §Conflict resolution the second to merge renumbers. The
CHECK constraints ship `NOT VALID` here and are validated by
`0084_datasource_scope_validate_constraints.sql`; the `datasources` partial
index is built `CONCURRENTLY` by
`0085_datasources_auto_attach_owner_idx.notx.sql`:

```sql
ALTER TABLE datasources
    ADD COLUMN scope_mode TEXT NOT NULL DEFAULT 'all',
    ADD COLUMN auto_attach BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN policy_revision BIGINT NOT NULL DEFAULT 1;

ALTER TABLE datasources
    ADD CONSTRAINT datasources_scope_mode_check
    CHECK (scope_mode IN ('all', 'projects')) NOT VALID;

-- 0084, its own transaction: validating in the same one as the ADD would hold
-- that statement's ACCESS EXCLUSIVE lock across the scan.
ALTER TABLE datasources
    VALIDATE CONSTRAINT datasources_scope_mode_check;

-- 0085, outside any transaction (.notx.sql).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_datasources_auto_attach_owner
    ON datasources (created_by)
    WHERE job_id IS NULL AND auto_attach = TRUE;
```

Project IDs continue to live in the existing table:

```sql
project_datasources (
    project_id,
    datasource_id,
    linked_at,
    read_only,
    description
)
```

The same migration adds a small durable
`datasource_project_reconcile_queue(project_id, datasource_id, policy_revision,
attempts, next_attempt_at, last_error, updated_at)` with a composite primary key.
Deliberately avoid foreign keys to project/datasource rows: deletion is one of the
states the worker must carry to the external knowledge stores. `last_error` is
sanitized and never contains connector configuration.

Do not add `datasources.project_ids JSONB/UUID[]`. The junction provides foreign keys,
project-deletion cascading, existing membership/link APIs, and future per-project
settings without duplicated state.

Cross-table validity (`scope_mode='projects'` normally has at least one junction row)
cannot be expressed as a simple row CHECK. Enforce it transactionally in the write
service. Zero rows remain valid after a linked project's deletion and mean “available
nowhere,” never “all projects.”

`policy_revision` is an optimistic-concurrency token. Increment it, together with
`updated_at`, on every scope/auto change and every `project_datasources` link add,
update, or removal. A full-set connector edit supplies the revision it loaded; a
stale revision returns 409 rather than erasing a project binding/settings change made
concurrently from Project Details.

Use a database trigger for junction-row INSERT/UPDATE/DELETE so foreign-key cascades
from project deletion also bump the surviving datasource policy and enqueue that
project/connector reconciliation pair; application-only bookkeeping would miss that
path. Datasource-row policy updates increment the same revision in their guarded
`UPDATE ... WHERE policy_revision = $expected` statement.

After adding the migration, regenerate `orchestrator/database/schema_current.sql` with
the repository's schema snapshot workflow. The historical `schema.sql` and
`vector_schema.sql` reference snapshots are not edited directly.

## 8. Write API and transaction boundary

### 8.1 Request/response fields

`DatasourceCreate` gains:

```python
scope_mode: Literal["all", "projects"] = "all"
project_ids: list[str] | None = None
auto_attach: bool = False
```

On create, explicit `project_ids=null` is rejected; projects mode requires a nonempty
array, while all mode accepts omission or `[]` and creates no links.

`DatasourceUpdate` gains optional forms of the same fields plus the required
`policy_revision: int` for a policy/link edit. Lock the wire states:

| Update field | Meaning |
|---|---|
| `project_ids` omitted | Preserve every existing link and per-link setting |
| `project_ids=[]` | Explicitly remove every caller-manageable link; valid only with resulting `scope_mode="all"` |
| `project_ids=[...]` | Desired full manageable project set; diff against current links |
| `project_ids=null` | Reject with 422 |

For `scope_mode`, `auto_attach`, and `policy_revision`, omission preserves the current
value and explicit JSON null is rejected. Validate field presence using
`model_fields_set`, not only the decoded `None` value.

`scope_mode="projects"` requires a nonempty resulting set. Switching
`projects -> all` with `project_ids` omitted **preserves** links: those rows may still
grant project sharing and carry `read_only`/description overrides even though they no
longer restrict the owner's contexts. Clearing them is a separate explicit action
with a warning. Switching `all -> projects` requires an explicit nonempty
`project_ids` list; do not reinterpret hidden legacy links without confirmation.

Owner/admin management responses include `scope_mode`, `auto_attach`,
`policy_revision`, and the linked project IDs needed to populate the edit form. A
non-admin creator sees all their links. Existing links the creator could not newly add
are returned as retained-only, not silently omitted from a later replacement. Other
callers must not learn project associations they could not otherwise see; omit the
full list or return only accessible links. List endpoints must obtain permitted
project links in bulk; do not introduce an N+1 query.

Use a distinct eligible response type in Cockpit. `EligibleDatasource` adds
`default_selected` but does not require the complete management-only project list.

### 8.2 Atomic persistence

Creating/updating a scoped connector is one logical write:

1. validate connector fields, scope combinations, null/presence semantics, and
   expected policy revision;
2. authorize every requested project;
3. lock the datasource policy row (`FOR UPDATE` on update);
4. insert/update the datasource row;
5. diff requested links against existing links;
6. keep retained rows untouched, insert additions (forcing the existing KB
   `read_only=true` invariant), and delete removals;
7. increment `policy_revision` and commit.

The transaction prevents observers from seeing `scope_mode='projects'` without its
intended links during an ordinary edit. Retained rows must preserve their existing
`linked_at`, `read_only`, and `description`; the current link upsert overwrites
overrides when passed empty settings, so delete-and-reinsert is not acceptable.

Neo4j/pgvector cannot participate in the Postgres transaction. Enqueue a durable,
coalescing reconciliation row inside that transaction rather than rolling back a
committed connector policy on an external-store failure.
Key the queue by `(project_id, datasource_id)` and store the requested policy revision,
attempt count, next attempt, and last safe error. The worker re-reads authoritative
Postgres state: sync the note if the link still exists, delete it otherwise, then
settle/retry with bounded backoff. This also handles delete-then-readd ordering and
connector description changes. The implementation does not run synchronous
sync/delete fast paths after policy writes; only the reconciliation worker invokes
the strict external-store callbacks, so an unfenced request cannot acknowledge or
overwrite newer queued work.
Datasource name/description/type-config updates that alter note content enqueue every
current project link in bulk in the same transaction.

Existing project-link endpoints remain useful for project administration. They must
respect the new invariant:

- linking adds an allowed project but does not change `scope_mode` automatically;
- adding a link to a `projects`-scoped connector widens its creator-chosen scope and
  therefore requires both connector-owner/admin authority and target-project owner
  authority. Hide other users' restricted connectors from the Project Details link
  picker. A project owner may still remove a connector from their own project;
- `all`-scope links retain their existing sharing/settings meaning;
- adding a private `all`-scope link requires connector creator/admin plus target-
  project management authority; a target-project owner may link a public connector.
  Retained links in a full-set edit are not additions and are not reauthorized;
- a connector creator may always revoke their connector's project link even after
  losing project ownership, so credential sharing cannot trap them;
- unlinking the final project from a project-scoped connector leaves it available
  nowhere;
- only an explicit datasource update can switch the mode to `all`.

All link endpoints increment the policy revision. Removing a project in the connector
form warns that its per-project access mode, description, and knowledge entry are
deleted.

### 8.3 Management versus eligible reads

`GET /api/datasources` remains the management/catalog read and continues returning all
rows visible under the existing admin/creator/project-membership gate. In particular,
do not assume its present gate treats an unlinked public row as catalog-visible.

`GET /api/datasources/eligible?project_id=...` becomes the execution-context read. For
a non-admin caller its conceptual predicate is:

```sql
WHERE d.job_id IS NULL
  AND caller_has_execution_access
  AND (
        d.scope_mode = 'all'
        OR (
            cardinality($selected_project_ids) > 0
            AND NOT EXISTS (
                SELECT 1
                FROM unnest($selected_project_ids) work_project_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM project_datasources pd
                    WHERE pd.datasource_id = d.id
                      AND pd.project_id = work_project_id
                )
            )
        )
      )
```

The response includes:

```json
{
  "scope_mode": "projects",
  "auto_attach": true,
  "default_selected": true
}
```

`default_selected` is computed server-side so Cockpit and non-UI creators do not grow
separate copies of scope/default policy. The eligible endpoint does not need to return
the connector's complete project list; the caller already supplied the authorized
execution projects, and exposing unrelated links would leak project association
metadata.

The `auto_attach` field may be useful for an owner's catalog badge, but consumers must
use `default_selected`; for a shared connector `auto_attach` can be true while
`default_selected` is false for the current effective owner.

## 9. Central authorization and default-selection service

Extract one orchestrator service/helper used by every creation path. It should expose
two related operations:

```python
authorize_datasource_ids(
    actor,
    effective_work_owner_id,
    datasource_ids,
    target_project_ids,
    workspace_backend,
    allow_admin_explicit_override,
) -> list[str]

default_datasource_ids(
    effective_work_owner_id,
    target_project_ids,
    workspace_backend,
) -> list[str]
```

`authorize_datasource_ids` validates ownership/public/**target-bound** project access,
the new scope, deduplicates IDs, and enforces explicit lite-tier errors. An admin
override is an explicit-use decision and cannot leak into default lookup.

`default_datasource_ids` queries canonical rows owned by the effective work owner with
`auto_attach=true`, applies the same scope checks, adds only explicitly defined
project/system-managed defaults, and silently removes clone-based repositories for
lite backends. It must not reuse the current `is_admin=True` eligible query, which
returns every datasource. It never writes by itself; the caller materializes the
returned selection.

The current `_authorize_thread_datasource_ids` is user/access-aware but not target-
project-aware. Fold or delegate it into this shared policy so job creation, thread
creation, live session updates, inheritance reauthorization, and MCP cannot drift.

Every dispatcher/resume boundary must call the same target-aware authorizer before it
builds a credential payload. Current job dispatch/resume performs no such check, and
session revalidation lacks target projects. Always derive a session's canonical
projects through `_thread_project_ids()`/thread mounts; the idle-pool path must not
read the usually absent `thread.project_ids` field.

The service should return normalized descriptors (or IDs plus policy revisions) from
one database snapshot so callers do not re-fetch rows to inspect types. Audit the
selection origin (`explicit`, `default`, `inherited`) separately from authorization.

Scope failure uses the existing non-enumerating response:

```text
One or more selected connectors are unavailable
```

The response must not reveal whether an ID exists, belongs to another project, or is
owned by someone else.

### 9.1 Make an empty materialized selection authoritative

Today an empty `job_datasources` result triggers a compatibility fallback to legacy
`datasources.job_id` rows in both job resolution and parent inheritance. That makes a
real explicit `[]` indistinguishable from “not migrated.” Retire the fallback as part
of this feature:

1. backfill legacy `datasources.job_id IS NOT NULL` associations into
   `job_datasources` with `ON CONFLICT DO NOTHING`;
2. reject new non-null `DatasourceCreate.job_id` writes on REST and MCP management
   APIs, remove the field from new client models, and retain the database column only
   for historical compatibility until a later cleanup migration;
3. make job resolution and `list_job_datasource_ids` read only
   `job_datasources`;
4. treat the presence of `threads.metadata.datasource_ids`, including `[]`, as
   authoritative. `_inherit_parent_datasource_ids` must check key presence, not list
   truthiness, before considering a parent-job fallback.

New threads always persist `metadata.datasource_ids`, even when empty. No runtime
heuristic may reinterpret an empty explicit attachment set.

## 10. Creation-path behavior

### 10.1 Root job/session API

For root creations:

```python
resolve effective owner, project set, expert/config, and workspace backend first

if datasource_ids was supplied:  # explicit null already rejected
    selected = authorize_datasource_ids(..., datasource_ids, ...)
else:
    selected = default_datasource_ids(...)
```

Default lookup/authorization, the job/thread insert, and attachment materialization
need one defined linearization point. Add a DB/service operation that, in one
transaction, revalidates the selected policy snapshot and then:

- inserts the job and batch-inserts the complete `job_datasources` set before the row
  can be observed by dispatch; or
- inserts the thread with `metadata.datasource_ids` already present.

Do not create the resource and then link one connector at a time. The current job
path logs individual link failures and continues, and the thread path updates metadata
after insert; either can run with a missing/partial data contract. A failed batch rolls
back the creation. Repository/workspace provisioning and dispatch trigger only after
the committed resource already carries its full selection.

Record safe provenance at materialization: effective work owner, initiating actor,
creation path, origin (`explicit`, `default`, `inherited`), project context, connector
IDs/policy revisions, and timestamp. Never record credentials or connection URLs.

### 10.2 Cockpit

Cockpit receives the eligible set, prechecks `default_selected`, and always sends the
full result. Therefore a UI creation is explicit at the API boundary even when the
user leaves the default checkboxes untouched.

### 10.3 MCP and AI-created work

An MCP/AI call that omits `datasource_ids` receives the effective user's automatic
defaults. A call supplying IDs receives exactly those IDs; `[]` opts out.

All MCP clients currently using truthiness checks such as `if datasource_ids:` must
use `if datasource_ids is not None:` so an explicit empty list survives transport.
Tool descriptions must explain omission versus empty-list behavior.

This applies to every sync/async/root/project MCP client, not only one wrapper. The
project-job client currently has the same truthiness bug. MCP connector-management
CRUD also gains explicit `scope_mode`, `project_ids`, `auto_attach`, and
`policy_revision` fields with the same validation. Their safe defaults remain
`all`/manual; an agent must opt in explicitly, and project-scoped MCP principals may
not name projects outside their authoritative scope.

An agent-created job tied to a persistent thread continues inheriting the thread's
selection before root defaults. The authoritative thread/parent supplies the effective
user and project scope; originless shared-key creation remains rejected by the existing
security boundary.

### 10.4 Caller policy matrix

Every path that creates work must be classified; no direct `db.create_job` call may
invent attachment behavior:

| Creation path | Omitted-selection policy |
|---|---|
| Cockpit job/full session form | UI submits the reviewed full array, including `[]` |
| REST root/project job or full session | Defaults after the rollout compatibility gate |
| MCP root/project job | Defaults; explicit `[]` survives every client layer |
| Session-created worker/delegation | Inherit authoritative thread, then parent job; never recompute root defaults |
| Project loop iteration | Resolve the loop owner's live defaults for its project |
| Automation cron/run-now | Resolve the automation owner's live defaults for its project |
| Scholar/critic/curator/lifecycle child | Copy and reauthorize the exact parent set |
| Officer/conference system session | Defaults for its authoritative effective owner/project |
| Benchmark replica | Explicit `[]` by default, or one selection frozen in the benchmark run spec; never live ambient defaults |
| Userless/internal thread without an authoritative principal/project | Explicit empty set |
| Retry/resume | Reuse and reauthorize persisted IDs; never add current defaults |

Bench isolation is intentional: ambient preference changes between replicas would
invalidate reproducibility. If benchmark connectors are later supported, freeze IDs
and policy revisions once at run creation.

Route direct system callers through the same creation/materialization service. The
automation cron's surrounding transaction is not sufficient today because
`PostgresDB.create_job()` acquires its own connection.

### 10.5 Project loops

Project loops currently attach every project-linked connector explicitly. Replace
that bespoke behavior with `default_datasource_ids(loop_owner, [project_id], backend)`.
Only available connectors with `auto_attach=true` are linked to the new loop job.

### 10.6 Automations

Automation-fired root jobs currently attach no connectors. Until automations gain an
explicit datasource selection, omission uses the owner's defaults for the automation's
project. A future nullable automation selection follows the same tri-state contract:

- persisted SQL `NULL`: live defaults at each run;
- `[]`: no connectors;
- IDs: exact pinned selection, reauthorized for each run.

The stored automation/loop owner must still be an approved effective user and a member
of the target project at each run. Missing/revoked ownership fails the run safely; it
must not fall back to a system/admin connector universe.

### 10.7 Child and lifecycle jobs

Delegation, scholar, critic, and curator jobs continue inheriting/copying the parent
selection. They do not recompute root defaults. Inherited IDs are reauthorized against
the child/effective project context, and lite children drop inherited repositories under
the existing rule before the child's own set is materialized. This creation-time tier
filter is not the later “silently reduce a stored contract” behavior prohibited above.

If reauthorization denies a scholar, critic, or delegation child, use the existing
parent-unblock/failure path; a denied child must not strand a waiting parent.

### 10.8 Resume and live session updates

- Resume never adds newly automatic connectors.
- Session attach/resume revalidates stored IDs against current access and scope before
  delivering credentials.
- Live datasource updates accept a full explicit set and do not apply defaults.
- KB live-update restrictions remain unchanged; this feature only changes eligibility
  and initial selection.
- A project-set update is validated against the desired connector set as one logical
  settings change. It must not leave a session carrying a connector that is invalid
  for the new project context.
- Currently attached but newly unavailable rows are rendered as locked warnings,
  separate from eligible additions. Config-only edits preserve them without
  resubmitting connector IDs; an actual connector edit must remove/replace them or
  surface the fail-closed server response. Do not hide and silently re-add them.

## 11. Project and system-managed connectors

### 11.1 Native project knowledge base

The project's own KB is already available implicitly from project scope, and its
auto-provisioned `kb` datasource is a management surface that collapses into the same
native binding when selected. New native KB connector rows should nevertheless be
created with:

```text
scope_mode = projects
project_ids = [native_project_id]
auto_attach = true
```

This makes the catalog/picker truthful without creating a duplicate knowledge binding.
Backfill existing native KB rows identified by `config.native_project_id` the same way.
The central default service recognizes that server-owned marker as the sole v1
project-managed exception to the `created_by == effective_work_owner` rule, so every
authorized member's work in exactly that project receives the native knowledge
binding. Multi-project sessions continue receiving the existing native binding for
each selected project; the synthetic rows do not use the ordinary connector all-match
test or create duplicate bindings. Do not generalize this exception to arbitrary
connector rows.

These fields are server-owned invariants for native rows. Ordinary connector forms
render “Included with project” and cannot change their scope, project IDs, owner
default, or link them to another project. Runtime bindings use
`config.native_project_id`; permitting a second project link would expose the original
project's knowledge under a misleading scope. Project deletion should delete its
synthetic native datasource row (or deterministically garbage-collect it), not leave
an orphaned project-scoped management row.

### 11.2 Personal WebDAV and deployment-seeded defaults

Personal WebDAV and environment-seeded default connectors require explicit policies;
provisioning must not inherit an accidental schema default:

- ownerless personal WebDAV rows linked to the user's default project use
  `scope_mode='projects'`, that one project, and `auto_attach=false` unless their
  provisioner establishes an effective owner and a deliberate automatic policy;
- environment-seeded public/default rows use `scope_mode='all'` and
  `auto_attach=false` unless a separate administrator-owned organization policy is
  designed.

The seed upsert must write/preserve this policy explicitly on re-initialization rather
than unexpectedly resetting an operator edit.

Project cloud folders remain workspace mounts and are not recreated as WebDAV
datasources.

## 12. Migration and rollout

### 12.1 Existing rows

Backfill by provenance:

```text
native KB identified by config.native_project_id:
    scope_mode = projects; retain native project link; auto_attach = true

ownerless, private canonical row with project links (for example personal WebDAV):
    scope_mode = projects; retain existing links; auto_attach = false

other canonical rows:
    scope_mode = all; auto_attach = false

legacy job_id rows:
    backfill job_datasources association, then retire from canonical/default lookup
```

`scope_mode='all'` preserves current owner availability and prevents existing linked
connectors from unexpectedly disappearing outside their linked projects.

`auto_attach=false` intentionally changes the create-form default from “everything
eligible is checked” to “nothing is automatic until the creator opts in.” Backfilling
`true` would preserve the old UI appearance but would also cause automation, loop, and
omitted-selection MCP paths to begin attaching every existing eligible connector on
deployment. That is an unacceptable silent credential expansion.

The ownerless-linked exception avoids broadening a resource that currently has no
owner execution path outside its project. Existing project links and their per-project
settings are never rewritten by this migration.

### 12.2 Compatibility/deploy order

Changing root-request omission from “none” to “defaults” is an API compatibility
break. Use a temporary capability/feature gate; “deploy in order” alone is not a gate:

1. Apply the additive migration, policy backfills, legacy-job-link backfill, and
   query indexes. Deploy the safe backend changes with both
   `DATASOURCE_SCOPE_AUTO_ATTACH_V1_ENABLED=false` (the default) and omission
   behavior disabled. The capabilities response advertises
   `datasource_scope_auto_attach_v1=false`, so the Cockpit keeps the new form
   hidden while orchestrator replicas are mixed.
2. Deploy every orchestrator replica with scope-aware reads/writes, target-aware
   authorization, materialized-empty support, and a reported capability such as
   `datasource_scope_auto_attach_v1`. Keep a deployment flag such as
   `DATASOURCE_DEFAULTS_ON_OMISSION=false`. Do not allow mixed-version policy
   writes; older Pydantic models may silently ignore new fields.
3. Deploy Cockpit and MCP clients that preserve explicit `[]`, guard eligibility
   races, and hide the new form until all backend replicas advertise support. Trusted
   MCP/AI/system callers request owner defaults explicitly with
   `use_datasource_defaults=true`; that opt-in remains independent of the generic
   omission gate.
4. After every API replica and client runs the new contract, set
   `DATASOURCE_SCOPE_AUTO_ATTACH_V1_ENABLED=true`; only then does the capability
   advertise the UI. Roll back the flag before rolling back any backend replica.
5. Enable connector policy editing and UI preselection. Cockpit already submits an
   explicit full set, so this does not require root omission semantics.
6. Route loops, automations, officers, and updated MCP tools through an explicit call
   to the central default service. Keep benchmarks/userless work explicit-empty.
7. Measure/log legacy root clients that still omit `datasource_ids`, publish the
   breaking contract, then enable omission-as-default for the REST create API.
8. Remove the temporary gate after the compatibility window and verify native KB,
   personal WebDAV, eligible/default, and empty-selection behavior.

This prevents an old UI that encoded “Deselect all” by omission from unexpectedly
receiving credentials when a user first enables an automatic connector.

### 12.3 Scope edits and running work

Other than copying legacy `datasources.job_id` associations into the canonical
junction, no migration changes an existing explicit `job_datasources` set or thread
metadata. Existing running agents are not hot-disconnected. Narrowed access takes
effect at the next normal authorization/credential-delivery boundary; broadened scope
never adds a connector to existing work.

## 13. Security and authorization

- Project scope is an enforced eligibility restriction, not cosmetic UI metadata.
- Project IDs in connector writes require the normal project-management authorization;
  callers cannot link a connector to a project they merely know by UUID.
- Access obtained through a project link is usable only in matching target project
  contexts; it is not a portable grant for other projects or standalone work.
- Selection errors remain non-enumerating.
- Connector credentials remain encrypted at rest and redacted from REST responses.
- `auto_attach` affects only the connector owner's work. Public/shared eligibility
  never lets a publisher set someone else's defaults.
- Admin catalog access never participates in default lookup.
- The server, not Cockpit, computes effective defaults.
- Scope/auto updates should emit a security audit record containing connector ID,
  old/new scope modes, project-ID counts (or authorized IDs if the existing audit policy
  permits), and old/new auto flags—never credentials or connection strings.
- Restricting scope must not attempt unsafe mid-turn credential removal; revalidation
  occurs before the next delivery.
- Native project KB policy fields and project link are immutable outside their
  provisioner/deletion lifecycle.

## 14. Observability and error handling

Creation logs should record only safe selection metadata:

```text
connector_defaults user=<id> projects=<count> eligible=<n> automatic=<n>
```

Operational logs can stay count-based, but a durable security audit event must be
able to reconstruct why credentials were delivered. Record effective owner,
initiating actor, creation path, selection origin, project IDs the actor may audit,
selected connector IDs and policy revisions, and timestamp. Record connector policy
changes with old/new scope, link diffs, auto flag, actor, and revision. Do not record
credentials, connection URLs, or secret config values.

Useful counters/events:

- eligible connectors by scope mode;
- automatic defaults materialized per creation path;
- implicit repository defaults filtered for lite backends;
- scope-revalidation denials;
- omitted-selection calls by client/version during rollout;
- connector knowledge reconciliation backlog/failures;
- project-scoped connectors left with zero links after project deletion.

The last condition is valid but merits a catalog warning such as “No projects selected —
unavailable to jobs and sessions,” with a direct edit action.

## 15. Testing

### 15.1 Backend

- Availability matrix: owner/all, owner/projects matching/nonmatching/no project,
  public/all, public/projects, and project-member sharing.
- Private all-scope connector linked to A: an A member cannot use it in B or
  projectless work; project-granted access is target-bound.
- Default matrix: every availability case with `auto_attach` true/false and effective
  work owner matching/nonmatching the connector creator.
- Admin defaults never include unrelated users' private connectors; separately test
  any retained admin explicit override.
- Default project behaves like an ordinary UUID; projectless excludes project scope.
- Multi-project sessions use all-match semantics.
- Raw API cannot explicitly attach a project-scoped connector outside its projects.
- Missing/inaccessible/out-of-scope IDs share the same 403 detail.
- Omitted, `[]`, and nonempty `datasource_ids` retain distinct semantics; explicit
  JSON null is rejected.
- Root job/session omissions materialize defaults.
- Empty job selection never invokes the legacy `datasources.job_id` fallback; empty
  thread selection prevents parent fallback.
- Parented jobs inherit before defaults; explicit `[]` opts out.
- Job/thread creation and their complete selection commit atomically; a batch-link
  failure rolls back and dispatch cannot observe a partial set.
- Project loops use automatic defaults rather than every project connector.
- Automation jobs receive defaults when no explicit selection exists.
- MCP root/project sync/async clients transport an explicit empty list; connector CRUD
  transports and validates policy fields.
- A `project:<uuid>` MCP principal cannot select, scope, or default a connector outside
  its authoritative project context.
- Benchmark replicas remain explicit-empty or share one frozen run selection.
- Userless work without an authoritative principal remains empty.
- Lite implicit repositories are filtered; explicit repositories are rejected; KB stays.
- `scope_mode=projects` create/update requires project IDs.
- Removing the last link never broadens scope.
- Deleting the last linked project leaves the connector unavailable.
- Public/shared `auto_attach` affects the creator only.
- Scope/auto/project-link writes are atomic and stale policy revisions return 409.
- Full project-set update preserves retained `read_only`, `description`, and
  `linked_at`; added/removed links enqueue idempotent knowledge reconciliation.
- A project member cannot transitively re-share/re-scope a private restricted
  connector; creator/admin expansion and project-owner removal paths are covered.
- Native project KB backfill/provisioning produces project scope + auto true.
- Native project KB cannot be rescaled, relinked, or moved; project deletion cleans it.
- Ownerless personal WebDAV backfill remains limited to its existing project.
- Management list still returns all creator-owned connectors regardless of scope.
- Catalog authorization/filters run before cursor pagination; an owned connector older
  than the first 100 raw rows remains reachable and pages have no gaps/duplicates.
- Bulk project-link enrichment avoids N+1 reads and does not leak hidden project IDs.
- Scope revocation fails closed at root dispatch/resume, scholar/critic/delegation
  dispatch, warm/cold session attach, and idle-pool resume; denied children unblock
  their parents.
- Membership loss, concurrent unlink/create, project deletion during creation, and
  knowledge-store failure retain the documented fail-closed/committed-DB behavior.

### 15.2 Cockpit

- Shared availability controls render for every connector type.
- Selected-project mode requires a nonempty multiselect.
- Edit form waits for and restores scope, auto flag, policy revision, addable links,
  and retained-only links; load failure cannot widen to all.
- Project search/multiselect, authorization, pagination, and selected-count behavior.
- Removing a link warns about its overrides/knowledge; switching to all preserves
  links unless explicitly cleared.
- Normal Save and create-then-test share one policy payload builder.
- Catalog filters are server-side and Visibility/Availability/Auto badges remain
  semantically distinct.
- Eligible rows default to `default_selected`, not all checked.
- Reversed/stale eligible responses cannot overwrite the current project context;
  creation is blocked during load/error and Retry is available.
- Switching project preserves touched choices for retained IDs, initializes only new
  IDs from defaults, and removes out-of-scope IDs. Reset refetches/restores current
  defaults.
- Deselect-all sends `datasource_ids: []` for both job and session creation.
- Live session picker seeds from attached IDs, not auto defaults.
- Live settings show unavailable attached rows and do not silently re-add/drop them;
  late responses from another thread are ignored.
- Instant/draft session waits for project resolution and offers a compact connector
  opt-out before first send.
- Public/shared auto copy makes its owner-only effect explicit.
- Repository/KB lite-tier behavior remains intact.
- English and German translation parity via `npm run i18n:check`.

### 15.3 Verification

- Focused Python datasource/job/session/MCP/automation/loop tests.
- Cockpit Vitest suites for datasource forms, picker, job create, and session create.
- `ruff check` and format checks for touched Python paths.
- Cockpit build and `npm run i18n:check`.
- Running-app walkthrough:
  1. create an auto project-scoped connector for Project A;
  2. confirm it is checked in A;
  3. confirm it is absent in Project B, the default project, and projectless creation;
  4. create a loop/automation/MCP job in A without IDs and inspect
     `job_datasources`;
  5. create the same with `[]` and confirm no links;
  6. delete/unlink A and confirm the connector becomes unavailable rather than global.

## 16. Acceptance criteria

1. Every user-created connector type supports all-project or selected-project scope.
2. Project IDs are persisted through `project_datasources`, not a datasource array.
3. An explicit scope mode prevents zero links from broadening availability.
4. The creator can always manage all owned connectors in the catalog.
5. Out-of-scope connectors are absent from pickers and rejected by direct attachment.
6. `auto_attach=true` checks an available connector only in its creator's work;
   shared/public users remain unchecked.
7. UI, root API/MCP, automations, and loops share one default-selection policy.
8. Explicit IDs replace defaults; an explicit empty list means none.
9. Child jobs inherit their parent's materialized selection before root defaults.
10. Defaults are persisted explicitly and never recomputed by the runtime resolver.
11. Changing auto-attach does not modify existing jobs or sessions.
12. Project-granted access is target-bound; neither admin visibility nor membership in
    A creates an automatic/portable grant in B.
13. Multi-project sessions require every selected project to match a restricted
    connector.
14. Job/thread creation and the full attachment set are committed atomically, and an
    empty set is authoritative without legacy fallback.
15. Scope narrowing or membership loss fails closed before the next credential
    delivery and never silently reduces a job's data contract.
16. Retained `project_datasources` rows keep their project settings, and stale
    full-set edits cannot erase concurrent changes.
17. Lite backends do not fail because of an implicit repository default.
18. The default project is not treated as an all-project escape hatch.
19. Project deletion cannot broaden a restricted connector.
20. Catalog filtering occurs before pagination so every owned connector remains
    reachable.

## 17. Deferred extensions

- Project-owner-controlled per-project defaults/overrides on
  `project_datasources` for every project member.
- Per-user opt-in defaults for public/shared connectors the user does not own.
- Admin-controlled organization-wide automatic connectors.
- Automation-specific pinned connector selection UI.
- “Unavailable because…” picker diagnostics for authorized management users; v1 keeps
  out-of-scope rows out of execution pickers entirely.
- One active project for connector resolution inside a multi-project session, if
  all-match proves too restrictive.

## 18. External design evidence

The research supports the core shape, with important limits on the analogy:

- GitHub Apps persist an explicit **all versus selected repositories** mode separately
  from repository relationships, and a scoped installation token may narrow but not
  widen the installation's repository set. This supports explicit `scope_mode`,
  relational project links, and “scope is an upper bound.” GitHub has an exception for
  repositories created by the app; SRW should not copy that implicit expansion for
  newly created projects. See [installation choices](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app),
  [installation management](https://docs.github.com/en/apps/using-github-apps/reviewing-and-modifying-installed-github-apps),
  and [scoped installation tokens](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app).
- HCP Terraform variable sets distinguish global, selected-project, and
  selected-workspace relationships and recommend narrowing credential-bearing sets.
  Its update contract also distinguishes omitted relationships from an explicit empty
  array. This supports explicit mode + junction rows + full-set updates. Terraform
  applies sets dynamically, however; SRW intentionally snapshots selected IDs for
  existing work. See [variable-set scope](https://developer.hashicorp.com/terraform/tutorials/cloud/cloud-multiple-variable-sets)
  and the [variable-set API](https://developer.hashicorp.com/terraform/enterprise/api-docs/variable-sets#update-a-variable-set).
- Kubernetes' ServiceAccount admission controller materializes default
  `imagePullSecrets` into a new Pod when none were supplied, leaving an inspectable
  concrete list. Kubernetes also fails startup for a referenced non-optional missing
  Secret and recommends namespace/least-privilege boundaries. This supports
  creation-time materialization and fail-closed revalidation. Kubernetes does **not**
  establish SRW's empty-list contract, and some Secret updates propagate differently.
  See [ServiceAccount admission](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/#serviceaccount-admission-controller)
  and [Secret security/optionality](https://kubernetes.io/docs/concepts/configuration/secret/).
- GitLab recommends environment-scoping sensitive CI variables instead of exposing
  them to every job, reinforcing narrow credential scope and the safe
  `auto_attach=false` migration. SRW uses stable project UUID relationships, not
  GitLab-style string/wildcard scopes. See [environment-scoped variables](https://docs.gitlab.com/ci/environments/#limit-the-environment-scope-of-a-cicd-variable).
- Vercel separates an all-resource management view from project-specific integration
  access, supporting a complete creator catalog plus a context-filtered picker. It is
  not evidence for auto-attachment. See [integration project access](https://vercel.com/docs/integrations/install-an-integration/manage-integrations-reference#manage-project-access).
- RFC 7396 formalizes omitted object members as unchanged and arrays as whole-value
  replacement. SRW borrows that clarity for update field presence but does not claim
  RFC compliance unless it actually adopts `application/merge-patch+json`. See
  [JSON Merge Patch section 2](https://www.rfc-editor.org/rfc/rfc7396#section-2).

## 19. Implementation map

The code audit identified these concrete work areas:

| Area | Primary locations | Required change |
|---|---|---|
| Schema | `orchestrator/database/migrations/app/`, generated `schema_current.sql` | policy columns/revision, provenance-aware backfill, legacy job-link backfill |
| DB policy/read paths | `orchestrator/database/postgres.py` | filtered/paginated catalog, all-match eligible/default query, batch atomic materialization, remove legacy fallback, update explicit SELECT/RETURNING fields |
| Authorization | `orchestrator/security/access.py`, shared service under `orchestrator/services/` | separate management, target-bound explicit execution, and owner-only defaults |
| REST/job/session lifecycle | `orchestrator/main.py`, session routers/services | request models, connector/link transaction, create funnel, dispatch/resume checks, native KB invariants, live unavailable state |
| System creators | `orchestrator/services/automations.py`, `project_loops.py`, `bench.py`, scholar/critic helpers | apply the caller policy matrix and common atomic creation service |
| MCP/agent clients | `orchestrator/mcp/client.py`, `orchestrator/mcp/server.py`, `src/tools/orchestrator/jobs.py`, `src/api/orchestrator_client.py` | preserve `[]`, document omission, expose management policy fields, inherit correctly |
| Cockpit data/form | `api.model.ts`, `api.service.ts`, `datasource-list.component.ts` | distinct management/eligible models, availability controls, authorized project picker, revision, server filters/paging, one payload builder |
| Cockpit creation/live | job/session create components, `datasources-group.component.ts`, draft chat service, live settings pane | server defaults, touched-state reconciliation, request race/error gates, explicit empty submission, instant-session review, unavailable attached rows |
| Localization/tests | both i18n catalogs and focused Python/Vitest suites | copy parity and the matrices in section 15 |

Implementation should first extract the policy and atomic materialization services,
then migrate callers. Patching each route independently would preserve the drift this
feature is intended to remove.
