---
tags:
  - feature
  - tools
  - grants
  - experts
  - automations
related:
  - "[[tool_config_policy_vs_membership]]"
  - "[[registered_tools_no_config_can_grant]]"
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[global_expert_management]]"
  - "[[application_tool_surface_baseline]]"
---

# Agent-authored catalogue entries

**Status:** implemented 2026-08-03 on `develop`, unpushed. **Live-gated on k3d as
a non-admin — 8 checks, all passing** ([[catalog_authoring_live_gate_2026-08-03]]).
Not yet exercised by an actual agent in conversation; see
[Verification](#verification).

A user can tell their session agent *"build me an expert that does X"* or
*"set up an automation that runs this every Monday"*, and the agent writes it.
Six tools do the work: `get`/`set` × `expert` / `skill` / `automation` bundles.

## Why this was a switch, not a build

The six tools shipped on 2026-07-09 (`75eb94b2`, `c6f030da`) as
*explicit-grant, not auto-injected*. Off-by-default was the intent and it was
honoured — but the escape hatch was never built, so no config, UI or expert
could turn them on. They sat in the registry, unreachable, for three weeks.

The scoping people assume is missing was already there. Each write calls the
**same HTTP endpoint the cockpit calls**, carrying the session owner's real
identity: `_get_client(user_id=...)` sends `X-MCP-User-Id`, and
`_get_user_from_mcp_headers` (`orchestrator/security/auth.py:636`) resolves the
actual `users` row, `is_admin` and all. So the agent acts as the user, never as
an internal service principal. Verified rather than assumed:

| Surface | Endpoint | What scopes it |
|---|---|---|
| experts | `POST /api/experts`, `PUT /api/experts/{id}`, `POST /api/experts/import` | caller becomes `owner_id`; 403 unless owner or admin; bundled experts read-only; `expert_type` is `Literal["worker","session"]` — no privileged value — and immutable on update; `user_experts` kill switch; `_validate_expert_fragment`; `_enforce_save_grants` |
| skills | `POST /api/skills`, `PUT /api/skills/{id}` | same shape, `uq_skills_name_owner` |
| automations | `POST /api/automations`, `PUT /api/automations/{id}` | caller becomes `owner_id`; project-scoped needs **editor**; non-members get 404 rather than 403 so existence does not leak; `_with_validated_tool_overrides` on the stored `config_override` |

That last cell matters beyond this feature: an automation's `config_override`
never crosses `POST /api/jobs`, and every cron fire re-plants whatever is
stored, so that boundary is the only thing that ever validates it.

## The design

### A category of its own, not membership in a read group

Before: the six lived in `agent_catalog` and `workflows`, whose checkboxes read
as *lookup* capabilities ("Experts & Skills"). A category-level `true` there
would have silently acquired control-plane writes —
[[tool_config_policy_vs_membership]] calls that "the single largest hazard in
the design", and held the line with a `grant: "explicit"` mark that excluded
them from `true`'s expansion.

Now they live in `catalog_authoring`. This is strictly better than the mark:

- `agent_catalog: true` and `workflows: true` expand to **exactly** their
  session vocabularies *by construction*, because the categories now contain
  only reads. There is no second judgement to keep in sync — the property is
  structural, and `tests/test_tool_grant_classification.py` pins it.
- `tools.agent_catalog: [set_expert_bundle]` is now **foreign vocabulary** and
  400s at every write boundary. That closes for free the one half the
  2026-08-03 live gates could not close: gate A.3 proved a category-level `true`
  could not reach the writes, but said nothing about an explicit name. It is now
  registry-membership validation doing the work, not a special case.
- The label can tell the truth. "Author Experts & Automations" is a different
  promise from "Experts & Skills", and a checkbox must not grant more than its
  label says.

The `grant: "explicit"` tier keeps only the four tools whose category genuinely
mixes privilege levels (`steer_worker_job`, `get_stuck_jobs`, `delegate_work`,
`resume_delegation_child`). Prefer rehoming over marking: a category whose name
matches its blast radius needs no exception list.

### `catalog_authoring` capability grant

`bool`, **default `False`**, `restrict_only` — and deliberately **not
backfilled**. Unlike `shell_tools` / `delegation` in migration `0030`, nobody
held this before, so there is nothing to grandfather and a backfill would hand a
new write capability to every existing user. **No migration is needed at all**:
`capability_grants` is a `(scope, key) → value_json` table, so a new key is a
code change.

Deny-by-default is also what makes this the **tier control** the feature was
asked for. The writes are owner-scoped, so the grant is not really about
containment — it is about *spend*: an enabled automation goes on to spawn jobs
on a schedule. Withholding the grant withholds a cost surface.

### Safety posture already in the tools

Both defaults are kept, and config must not be able to flip them:

- every write has `dry_run=True` by default — the model must pass
  `dry_run=false` deliberately;
- `set_automation_bundle` has `allow_enabled=False`, so an agent-created
  automation lands **disabled** for the user to enable in the UI;
- `expected_hash` gives optimistic concurrency on update, which is why the
  three `get_*` reads belong in the same group: `get → edit → set` is the
  intended loop and the hash comes from the read.

`propose_automation` stays in `workflows`: it drafts a bundle without writing
it, so it is a read-shaped tool and the mediated path remains available to
sessions that do not hold this grant.

## Two hazards found while wiring it

Both are the same species — one fact serving two purposes — and both are worth
knowing before touching this area.

### 1. `SESSION_TOOL_OVERRIDE_NAMES` had two jobs

It is the **presentation** vocabulary (which groups the product offers as one
checkbox) *and* it was being used to derive the **legacy append rule**. On the
legacy (experts-off) path an unset group reads as ENABLED, because
`persistent_session._setup_tools` re-adds canonical name lists when no disable
marker is present. Deriving that rule from the checkbox list would have made an
unset `catalog_authoring` predict six write tools **that no agent binds** — an
agent image built before this category existed cannot re-add it.

Fixed by naming the real invariant: `LEGACY_APPENDED_GROUPS` =
`{orchestrator, agent_catalog, workflows}`, a historical fact about deployed
agent code. `canvas` is absent because its legacy branch is strip-only. This
also retires the ad-hoc `if group != "canvas"` special case into a principled
rule.

The same conflation appeared a third time in a test, which iterated the
vocabulary to assert a runtime disable marker per checkbox.
`_SESSION_TOOL_DISABLED_MARKERS` has four entries and needs no fifth: a marker
exists to countermand a re-add, and nothing re-adds this group.

### 2. `ToolsConfig` is built at two hand-transcribed sites

`get_all_tool_names` is properly derived from the dataclass, but
`load_agent_config_from_dict` constructs `ToolsConfig(...)` with an explicit
kwarg per field at **two** call sites (`src/core/loader.py:2403`, `:2661`). A
new field parses and binds *nothing* until both are updated — the same silent
failure that left `product_help` and `session_task` inert until 2026-08-02.
`tests/test_tool_policy.py::test_get_all_tool_names_reads_every_field` catches
it, and did.

## Surfaces

- **Session base:** `config/session_base.yaml` declares `catalog_authoring: [ ]`.
  Not decoration — `session_tool_group_enablement` reads an **absent** key as
  *enabled*, and its docstring's stated invariant is that `session_base`
  declares every closed group explicitly.
- **Cockpit:** a row in `SESSION_TOOL_CATEGORIES` plus both locales. The
  client-side `CAT_TO_GRANT` map greys it for an author without the grant.
- **Explanation:** `GRANT_GATED_CATEGORIES` / `_GRANT_REASONS` in
  `src/core/tool_report.py`, so a blocked row says *"requires the
  catalog_authoring capability grant"* instead of rendering a bare "off".

## Coverage note worth keeping

`TestGrantMapMatchesThePDP` re-derives `GRANT_GATED_CATEGORIES` from the real
PDP — but it is parametrised over **the map's own keys**, so it could never see
a grant the PDP enforces and the map omits. An omission is the failure that
matters: the denial still happens, the *explanation* goes missing, and the user
gets an unexplained "off". Added the inverse assertion
(`test_no_gate_the_pdp_enforces_is_missing_from_the_map`) and mutation-tested it
by deleting the new entry. The client mirror `CAT_TO_GRANT` has the same shape
of hole and is **known-incomplete today**: the seven `datasource_tools`
categories have never been listed, so those authors learn at save time via 422.
Pre-existing; recorded rather than fixed here.

## Verification

Unit: 13 255 Python tests and 1 644 cockpit tests pass; `tsc --noEmit` clean;
the grants snapshot fixture did **not** move, confirming no shipped config's
resolved toolset changed.

**Live-gated 2026-08-03 on k3d as a non-admin**, because
`_enforce_save_grants` returns early for admins (`orchestrator/main.py:5043`) and
gating as an admin would have exercised the permissive path. Full evidence in
[[catalog_authoring_live_gate_2026-08-03]]; the eight checks in short: the PDP
denies through real DB grant resolution, the HTTP boundary 422s naming the grant,
a granted create returns 200 owned by that user with `true` normalised to the six
names in storage, all six survive resolution **and bind through both factories**,
an agent-created automation lands `enabled=false` with no `next_run_at`, a
non-owner update 403s, and the old `agent_catalog: [set_expert_bundle]` spelling
400s with a message naming the new category.

The binding check is the one that mattered: `catalog_authoring` is the only
category whose members come from two factories, and the `persistent_session`
specs mock `load_tools`, so nothing in the unit suite exercises that branch.

**Still owed:** an agent driving `set_expert_bundle(dry_run=false)` in an actual
conversation. One dev session — grant it, ask for an expert, read back the row.

## Deliberately not done

- **Job-mode exposure.** Job create validates the category like any other, and
  since 2026-08-04 (`44c268d9`) it *does* read the server's toolset answer — so
  if a worker expert ever declares `catalog_authoring`, the job form will render
  it with the right state and grant reason. What is still deliberate is that
  `worker_base` does not declare the category, so no job gets it by default.
  Whether a worker job *should* be able to author catalogue entries is a separate
  product call: a job runs unattended, so `dry_run` and `allow_enabled` are
  carrying more weight there than in a session with a human in the loop.
- **Completing `CAT_TO_GRANT`.** See the coverage note.
- ~~**A `duplicate_expert` fix.**~~ **DONE 2026-08-04/05** — it was indeed the
  better use of the next hour. The kill switch now holds on all five expert-write
  routes. Relevant to *this* feature beyond the hole itself: the grants half on
  `duplicate` and `expert-defaults/{type}/fork` now **strips and reports** rather
  than refusing, so a config carrying `tools.catalog_authoring` copied by a user
  without the grant lands **without** that category and says so, instead of 422ing.
  The agent-facing tools are unaffected — they write through `POST /api/experts`,
  `/import` and `PUT`, which all still refuse.
  See [[duplicate_expert_bypasses_user_experts_kill_switch]].
