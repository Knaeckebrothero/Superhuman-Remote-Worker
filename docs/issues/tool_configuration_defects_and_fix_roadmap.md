---
tags:
  - issue
  - roadmap
  - cockpit
  - orchestrator
  - sessions
  - config-resolution
related:
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
  - "[[registered_tools_no_config_can_grant]]"
  - "[[session_uploads_never_extract_archives]]"
  - "[[tool_config_policy_vs_membership]]"
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
  - "[[tool_configuration_live_gates_2026-08-03]]"
  - "[[tool_configuration_deferred_findings]]"
  - "[[resume_job_grant_recheck_fails_open]]"
  - "[[job_mode_reasoning_pick_silently_reset]]"
  - "[[duplicate_expert_bypasses_user_experts_kill_switch]]"
---

# Tool configuration — consolidated defect register and fix roadmap

**Status:** IMPLEMENTED **and live-gated** 2026-08-02/03 on `develop`, **not
pushed**. Phases 0–3 are done; each of the eight tasks went implement →
adversarial review → fix → scoped re-review. The final whole-branch review
returned four merge blockers and **all four are now closed** — three by the fix
wave, the fourth by six live gates across three rounds, every one passing (one
defect found by round 1, fixed, re-gated in round 3).

**Remaining before this is finished, none of it code review:** push, deploy to
dev, and re-run the cheap half of the gates against a built cockpit bundle rather
than a dev server. Then the one owed action in the follow-up register: a
read-only `jsonb_typeof` scan of any target database, because Task 5 turned
malformed stored `tools` values into a hard failure where they were previously
ignored in silence.

**Follow-on, 2026-08-03 (3 commits, also unpushed):** the one decision this
register left to the human — what to do about the six `*_bundle` writes — was
taken and became a feature, [[agent_authored_catalog_entries]], live-gated in
[[catalog_authoring_live_gate_2026-08-03]]. It closes defect-9's residual path
(triage item 7), the 4b-gates-5 hazard, and §3c of
[[registered_tools_no_config_can_grant]], each by structure rather than by
adding a rule. Read that doc's "two hazards" section before touching this area:
`SESSION_TOOL_OVERRIDE_NAMES` was serving two purposes and `ToolsConfig` is
transcribed at two call sites.

**This file and its three siblings stay in `docs/issues/` and `docs/features/`
until then**, deliberately. The house convention for `docs/done/` is work that is
fixed *and* verified on a deployment — the closest sibling,
[[session_tool_group_checkbox_disagrees_with_the_agent]], names its commit, its
`sha-*` image and the dev session it was live-verified on — and none of this is
pushed, so `develop` is not what any deployment is running.
Two of the four also have genuinely open content on their own terms:
[[registered_tools_no_config_can_grant]] items 1/2/5 are unshipped, and
[[session_uploads_never_extract_archives]] has no live gate at all. The move to
`docs/done/` is a single commit once dev is running this code and the gates
re-pass there.

- Gate evidence, with the numbers: [[tool_configuration_live_gates_2026-08-03]]
- Everything triaged as deferred, plus what was settled and must not be
  re-litigated: [[tool_configuration_deferred_findings]]
- Filed out of the run: [[resume_job_grant_recheck_fails_open]],
  [[job_mode_reasoning_pick_silently_reset]],
  [[duplicate_expert_bypasses_user_experts_kill_switch]]

**Owner:** unassigned.

## Merge blockers (final review, 2026-08-03) — all four closed

1. ~~**The expert write boundary was missed.**~~ **FIXED.** The four sites the
   review named (`orchestrator/main.py` create / update / import / fork-a-default)
   plus a **fifth it missed** — `POST /api/experts/{id}/duplicate`, which had
   neither the deny-scan nor the PDP and forks a row that may be another
   principal's — now run `validate_tool_override_fragment` through
   `_validate_expert_fragment`, which returns the canonical fragment so the row
   is persisted normalised and the save-time PDP only ever reads a list. The
   verified escalation (`tools.canvas: ["run_command"]` stored by any approved
   user, invisible to a PDP that keys off the category, bound as shell by a
   loader that regroups by *registry* category) is closed, and so is a quieter
   one: `normalize_tool_policy` runs when the row is READ, so a shape it refuses
   (`tools.shell: true`) was storable and made the expert unresolvable
   afterwards. Covered by `TestExpertWriteBoundary`.
   *Residual on that fifth route, filed separately:* duplicate still skips
   `_enforce_expert_save`, so the user-experts kill switch and the save-time
   grants PDP do not run there —
   [[duplicate_expert_bypasses_user_experts_kill_switch]].
2. ~~**A per-tool code grant renders as an un-untickable ticked box.**~~
   **FIXED.** `code_granted_tools()` adds the per-*tool* tier the category map
   structurally could not see, and `compose_tool_view` gained the mirror of "off
   is a promise": when everything the agent bound is a `grant: "code"` name, the
   category is `on` with `settable: false`, `decided_by: registry` and a reason
   naming the tools and their gates. It fires only when unticking would change
   nothing — a bound set that also holds a config-granted name stays an ordinary
   ticked box. Covered by `TestOnIsRevocable`, one case per re-append site.
3. ~~**Live gates for Phases 1.2 and 1.3 never ran.**~~ **RUN 2026-08-03 —
   six gates, three rounds, all passing.** The cluster blockers (gitea's
   sqlite→postgres guard, migration drift, an ad-hoc pod image) were cleared
   first; Phase 1.1's gate had already run and passed under Task 6. Full
   evidence: [[tool_configuration_live_gates_2026-08-03]].

   *Round 1* — **Phase 1.2** proved by a **control**, not by an absence: a
   session with `tools.research: []` bound **32** tools with zero of the eight
   `research` members, while a stock session on the same expert, tier and image
   bound **40**, the extra 8 being exactly `research`. (The control is what makes
   it proof — `knowledge` reports `configured: 10, bound: 0` for unrelated
   reasons.) Agreement was three-way: pod log 32 = the agent's own
   `GET /session/toolset` 32 = the endpoint's 32, exact set match, with
   `decided_by: "request"`. Smuggling `tools.canvas: ["run_command"]` returned
   **400 at eight write boundaries** in total (five here, three in round 2),
   each with a clean-body control on the same route; the strongest is the
   automations control, which stored `{"canvas": true}` **normalised to the three
   canvas tool names** — a shape-mismatch rejection could not have done that, so
   the 400 is the validator's verdict. **Phase 1.3** passed on three of four
   items (25 rows, 0 untranslated keys, all four provenance banners, locked-on
   rows muted-not-warning, a degraded pane rendering 0 rows) and found the
   defect described below.

   *Round 2*, after the owner authorised a DB-only non-admin user on the
   throwaway cluster — **Phase 1.4** in both directions. Read side: `shell` reads
   `unavailable / settable:false / decided_by:"grant"` with the reason *"requires
   the shell_tools capability grant"* and `configured: []` — the config never
   asked, and the refusal fires anyway. Strongest evidence: **one response
   carrying two causes and two sentences** — `shell` by=grant next to
   `git`/`browser_direct` by=backend with the tier sentence, on one session, one
   tier. Reversible with a single `capability_grants` row, and the identical
   `POST /api/experts` body is **422** as the non-admin and **200** as the admin.
   And **D5 held where it matters**: `agent_catalog: true` live-measured bound
   exactly **5** tools with **all six `*_bundle` writes absent**, `workflows: true`
   bound 7, `orchestrator: true` excluded `get_stuck_jobs`/`steer_worker_job` —
   principal-independently, because the exclusion is a registry mark read by
   `_grantable` rather than a grant lookup.

   *Round 3* — the round-1 defect, fixed and re-driven with the failing state
   reproduced first so the drive is known to be sensitive. One `Add (4)` gesture
   dispatched one `config.update`, the server persisted it normalised, and the
   agent rebound **55 → 85 tools** (set-diff 30 added, 0 removed; three of the
   thirty are the shell tools, `shell_execute` absent as its mode-alias twin) —
   after which the row **heals itself** back to an ordinary settable checkbox.

   Blocker 2 was precisely what a gate catches and eight rounds of static review
   nearly missed; round 1's defect is the third time in this series that a green
   suite endorsed something wrong and a rendered check caught it. What is still
   not gated is listed in the gate doc — chiefly server-produced `agent_partial`,
   and anything at all on a real deployment.
4. ~~Doc status headers stale~~ — **FIXED.** All three corrected:
   this file, `registered_tools_no_config_can_grant.md` (shipped vs remaining,
   per item) and `tool_config_policy_vs_membership.md` (commits 1–6 landed, 7
   optional and untouched).

### Known consequence of blocker 2's fix — **CLOSED 2026-08-03**

**Resolved, and not the way this section predicted.** The live pane now carries
an explicit *add* affordance on a locked-on row: the checkbox stays checked and
`disabled` (unticking a code-granted category remains impossible, visually and
in dispatch), and beside it the row names the config-grantable tools it lacks
and offers a one-gesture `Add (n)`. The write is additive by construction —
`toolsFragment` refuses every unsettable key in **both** directions again, with
no per-key exemption for a caller to strip, and the request rides its own
tracked path because a locked-on category's boolean is `true` before the gesture
and `true` after it. Browser-verified end to end on a sandbox-tier session:
untick → 0 frames; Add → one `config.update` carrying the enumeration → the
override persisted → the agent rebound with `run_command`, `cancel_command`,
`shell_read` and `shell` back to `settable: true`. Detail:
[[tool_configuration_live_gates_2026-08-03]], round 3.

**No second contract field was needed**, and the reason is worth recording:
`enumerate_only` already ships the config-grantable membership of every category
that refuses `true`, so the client can subtract the bound set and *name* the
additions. Where the enable policy is `true` the client cannot see the expansion
and deliberately offers nothing — which is also the right answer, because the
locked categories that take `true` (`product_help`, `session_task`, the
connector categories) expand to nothing at all. The affordance therefore appears
on exactly one row, `shell`, on exactly the topology that needs it.

The original diagnosis, kept because two proposed one-line fixes were both
wrong: a category the runtime holds *partially* — `shell` on the default
topology, held by `srw_cloud_status` alone — was locked in **both** directions on
the live pane, because `settable: false` is one boolean and the cockpit never
emits an unsettable key.

> **This IS a regression. Corrected 2026-08-03 — the fix wave's own safety
> argument was falsified on re-review.** The claim was that the enable path was
> already dead because the diff baseline never moves after an apply. That is
> true of `tools-group.component.ts::getOverrides()` and false of the pane that
> actually dispatches: `settings-pane.component.ts:523` runs
> `this.lastApplied = desired` on every apply, and `applyChanges` builds
> `baselineOn` from `lastApplied`, not from `prefillFromResolved`. The reviewer
> drove the real component: with `settable: true` an untick → apply → re-tick →
> apply dispatched `{tools: {shell: {only: [run_command, shell_read]}}}`, a
> working enable; with `settable: false` the identical sequence dispatched
> nothing.
>
> The lock also self-selects the worst population. `session_base.yaml` ships
> `tools.shell: []` and `persistent_session.py:1526` appends `srw_cloud_status`
> whenever a cloud mount is active — which default projects have — so a stock
> session's shell bound set is exactly `[srw_cloud_status]` and locks, while a
> session that already holds `run_command` has a mixed set and does not. **The
> only sessions locked out of gaining shell are the ones without it.**
> Granting shell to a running session from the settings pane was possible before
> this fix and is not now, on the default topology.
>
> Not a product-wide capability loss — a new session predicts rather than
> measures, so it does not lock, and the expert route is unaffected. But it is a
> new defect on the highest-stakes tool group, introduced by a blocker fix, and
> it needs a decision rather than a footnote.
>
> **Two proposed one-line closes, both wrong, kept as a warning.** (a) "skip
> unsettable keys only in the *off* direction" — this makes the write path
> reachable and the *control* still is not: the locked row is already checked, so
> the only gesture a checkbox offers is unticking, and a checkbox that appears to
> turn off and springs back is the original Critical finding restored. (b) "remove
> `disabled` from the template" — same defect, one step earlier. The real shape is
> that a ticked box has no "turn on" gesture at all, so the additive half needs a
> control of its own. See the CLOSED note above.

**What was achieved:** `[]` no longer conflates membership with policy, so "on"
is expressible; all 12 form categories are honoured where 4 were; every tool
write boundary rejects rather than drops, including the expert surface the first
pass missed — **eight of them driven live, all 400** — and the agent is the sole
authority on what it bound, with one implementation of that answer. Task 5's
identity property — no shipped config's grants change — held across all eight
tasks, with `tests/fixtures/config_tool_grants.json` still at md5
`9ddc4f79…`, unmoved since Task 2's deliberate `kb_*` grant.

**What was not, as of the eight tasks:** no creation form could tell the truth
structurally, because a prediction cannot see the backend gate, runtime
injection, or datasource attachment — and no cockpit surface branched on `origin`
in a way that changed the control, so a forecast rendered as switch positions.
Job create was the weakest surface: no server-computed view at all.

> **Substantially closed 2026-08-04 (`44c268d9`).** Job create now reads the same
> endpoint the live pane and New Session read: `ToolGroupPreviewRequest` gained
> `expert_type`, a worker request defaults its base to `worker_base`, and the form
> passes `resolvedToolset` / `readsResolvedToolset` / `gatedCapabilities`. So it
> renders every category the server returns with three states and grant reasons,
> instead of six hardcoded rows that showed an ungranted user a tickable Shell
> box.
>
> The *structural* half of the sentence above still stands and is not a bug: a
> creation form has no agent, so it forecasts, and it says so — `origin:
> "prediction"` with a reason, rendered as a banner. What is gone is the part that
> was fixable: the forecast is now server-computed and grant-aware on both forms.
>
> **The expert editor followed on 2026-08-04 (`68ac7bde`)**, which makes it
> **four for four**: the live pane measures, and both creation forms plus the
> expert editor forecast from the same server-side resolution. The editor was the
> one that mattered most — an expert's toolset is what every job and session
> built from it inherits — and it asks for `base ⊕ fragment` rather than by
> `expert_id`, because an id layer underneath cannot express a key the author just
> deleted. Detail in [[tool_configuration_deferred_findings]] §7.8.

**Carried out of the run, not lost with it:** about forty findings triaged as
*minor, deferred*, ten rulings settled at real cost, and three tickets that were
owed. All in [[tool_configuration_deferred_findings]] — read its §1 before this
branch reaches a database with hand-authored experts.

**Purpose:** single entry point for the nine defects found in one investigation.
The detail lives in the three linked docs; this one exists so the work can be
sequenced and picked up without reading all four.

## Motivating incident

Dev session `1930dec9-181d-4fd5-a030-90b3d0b363d6`, 2026-08-01. A user attached a
`.zip` and asked the agent to work on the letter inside it. The agent could not
open the archive, fell back to asking for shell, and had none. It then advised
the user to switch the workspace to "Container" and enable shell tools at
session creation — advice that could not work, since `sandbox` *is* the
container tier and the Shell checkbox cannot grant anything.

The user's response — *"I know I've used live persistent sessions with shell
tools"* — was correct and was the key to the whole investigation. Sessions had
shell by default for four months. It was removed eleven days earlier by a commit
titled a chore, whose body states "No functional changes introduced."

Everything below was found pulling that thread.

## Defect register

| # | Defect | Sev | Detail in |
|---|---|---|---|
| 1 | `57430a2a` (2026-07-22) silently removed `tools.shell` from the session base. **Not to be reverted** — see Decisions; off-by-default is now the intended state. The defect is that it happened silently and left no way to turn shell back on. | medium | [[session_create_tool_toggles_cannot_enable_a_group]] |
| 2 | Unticking 8 of the 12 tool categories on the New Session form is silently discarded server-side — **fails open**. The user is shown a restriction that was never applied. | **high** | ↑ same |
| 3 | No tool group can be *enabled* from the New Session form; the re-enable branch reads member names from the layer it is overriding, which is `[]`. | medium-high | ↑ same |
| 4 | A Reasoning selection is discarded on model/expert change **with no feedback**. The discard itself is correct and must stay — see the note below. | low | ↑ same, Part 3 |
| 5 | Ten registered tools have no reachable grant path; five curation prompts unconditionally order the curator to call two of them (`kb_lint`, `kb_index`) with no `has_tool` guard. | medium | [[registered_tools_no_config_can_grant]] |
| 6 | `product_help` and `session_task` are registry categories with no `ToolsConfig` field, so naming them in YAML is silently discarded. | low | ↑ same, §5 |
| 7 | Session uploads never extract archives (the worker path does), and `read_file` reports any binary as a raw UTF-8 codec error. | medium | [[session_uploads_never_extract_archives]] |
| 8 | The **job**-create path has no tool allowlist at all — the smuggling scenario `session_tool_overrides.py` exists to prevent is open on the job surface, with only the dispatch PDP behind it. | **security, triage** | [[tool_config_policy_vs_membership]] §open items |
| 9 | `_validate_expert_fragment` deny-scans credentials but not tool names, so a hand-authored expert may be able to name a `*_bundle` write tool and pass the session gate. **Reasoned from code, not exercised.** | **security, triage** | [[registered_tools_no_config_can_grant]] §3c |

## The common cause

Defects 1–6 are all downstream of one representational choice: `tools.<category>`
is a list of tool names, and that single field carries both **membership** (which
tools) and **policy** (whether the group is on). All 24 registry categories have
at least one tool, so no group is ever legitimately empty — every `[]` in every
config is purely a disable marker.

That overload has three consequences, and each produced defects above:

- **"Off" is self-describing; "on" is not.** Disabling needs no information;
  enabling needs the full member list, which the code looks for in the layer it
  is overriding. → defect 3.
- **Name lists cannot track the registry.** A tool added to an existing category
  reaches no existing config. → defects 5 and 6.
- **Configs are unreadable as documentation.** `shell: []` looks like a statement
  about shell's contents; it is a policy flag on a group of 5 tools. That is how
  a diff removing a capability read as tidying a list. → defect 1.

Two structural findings sit underneath, both worth knowing before touching any of
this:

**There is a tool-grant layer written in Python, not YAML.** `sleep`,
`notify_user`, the `task_*` tools, `read_product_guide`, all 28 datasource tool
names, `approve_job`/`return_job_with_feedback` and `loop_plan` are injected at
runtime by code that appears in no config file
(`src/api/persistent_session.py:1408-1557`, `src/agent.py:3066-3068`,
`datasource_tool_categories`, `orchestrator/main.py:12961`,
`project_loops.py:898`). A YAML-only audit over-reports unreachable tools by 67.
Any design that treats YAML as the grant surface is wrong.

**Nothing can answer "what config did this agent actually get?"** Every
conclusion in this investigation came from reading files and inferring; the only
ground truth available was a line in a pod log (`Loaded 65 tools for persistent
session`). This is the real reason the system feels unknowable, and it is why
defect 1 survived eleven days.

## Decisions taken (2026-08-02)

**D1 — Priority 1 is that the UI tells the truth.** Whatever the settings surface
shows for a tool group must equal what the agent ends up with, in every case.
This outranks every individual defect below; defects 2, 3 and 6 are all instances
of it and are fixed as part of it rather than separately.

**D2 — Agents do not get shell by default.** `tools.shell` is *not* restored to
`config/session_base.yaml`. The 2026-07-22 removal produced the right end state
by the wrong means. What must change is that the state becomes legible
(`shell: false`, not `shell: []`) and reversible.

**D3 — Shell must be enablable two ways: from the UI, and via an expert.** The
expert route already works (`developer` declares its own `tools.shell`). The UI
route requires `shell` to join the settable vocabulary, which the closed
allowlist currently prevents. This closes the open product question that earlier
revisions of this doc left to triage.

**D4 — The `shell_tools` capability grant stays the real gate.** Already enforced
against the *merged* config at both session attach and job dispatch
(`_enforce_dispatch_grants`, `orchestrator/main.py:4754`), with an admin bypass.
So wiring the UI toggle grants nothing to a user who lacks the grant, and the
`developer` expert is subject to the same check. **Consequence to accept:** a
non-admin user needs an explicit `shell_tools` grant before *either* route gives
them shell. If pilot users are expected to self-serve shell, that is a separate
grant-policy decision, not a config one.

**D5 — The nine tools the registry has and the closed vocabulary does not get
classified in registry metadata**, as admin-only or never-in-sessions, so `true`
expands to the safe set *by construction*. Not by a hand-maintained parallel
list — that is what `SESSION_TOOL_OVERRIDE_NAMES` is today, and its divergence
from the registry is only visible if you go looking. Gates Phase 1.2.

**D6 — The resolved answer comes from the agent, not from a re-implementation.**
The agent reports its bound toolset after attach and the API serves that.
Rebuilding the injection logic orchestrator-side would drift, and drift between
two implementations of the same fact is the original bug. **Consequence to
design for:** the creation form has no agent yet, so it can only show a
*prediction*; the live pane shows *actual*. They must be visibly different in
the UI — a predicted state presented as fact is D1 violated at a new seam.

**D7 — Three-state controls, not checkboxes:** *on* / *off* / *unavailable with
a reason*. Two states cannot express the truth D1 requires.

**D8 — `shell_tools` stays admin-granted.** No self-serve for non-admin users
through either the UI or an expert. Shell is code execution and the existing
grant already behaves correctly.

**D9 — Grant `kb_index` / `kb_lint` to the curator** rather than guarding the
five prompts. The tools were built for that job. It is a behaviour change and
lands as its own commit.

**D10 — The worker base keeps its current (empty) `shell` and `delegation`.**
Every real expert backfills what it needs; only a job on the bare base is
affected, and under D2 that is the intended default. Now pinned by the snapshot.

**D11 — The job-create path's missing tool allowlist is a gap, not a design.**
Close it with the generic validator from Phase 1.2 rather than a second bespoke
one.

### Triage item 7 — resolved 2026-08-02, and it does not escalate D5

> **Superseded 2026-08-03: the answer is now NO, it does not pass.** The six
> `*_bundle` tools moved to a `catalog_authoring` category behind a
> deny-by-default capability grant ([[agent_authored_catalog_entries]]), so
> naming one under `agent_catalog` is foreign vocabulary and 400s, and naming it
> under `catalog_authoring` requires the grant — verified live at the HTTP
> boundary ([[catalog_authoring_live_gate_2026-08-03]], checks 1b and 5). The
> gated-keys list quoted below has a new member.
>
> This section's core finding is what made that safe to turn into a *feature*
> rather than delete: the tool acts as its owner, so ownership and
> `_enforce_save_grants` already applied. The 08-03 work re-derived it
> independently and extended it — `expert_type` is `Literal["worker","session"]`
> with no privileged value, a non-owner update 403s, project-scoped automations
> need editor, and an automation's stored `config_override` is validated at the
> only boundary it crosses.

**Question:** can a hand-authored expert name a `*_bundle` write tool and pass
every gate? **Answer (2026-08-02): yes, it passes — but it is not a privilege
escalation.**

Verified locally, not against the cluster:

- `capability_grants.evaluate()` returns **no violations** for a user holding
  zero grants. The gated keys are `shell`, `delegation`, the connector
  categories, `browser_direct`, models and autonomy — `agent_catalog`,
  `workflows` and `orchestrator` are not among them
  (`src/core/capability_grants.py:147-185`).
- `_validate_expert_fragment` (`orchestrator/main.py:27905`) only deny-scans
  credential sections.
- `SESSION_TOOL_OVERRIDE_NAMES` never applies — it validates *request*
  overrides, not expert configs.
- The name survives the load: a fragment declaring
  `tools.agent_catalog: [set_expert_bundle]` resolves with that tool present,
  and `filter_tools_by_backend` touches only `shell`/`browser_direct`/`git`.

**Why it is nonetheless not an escalation:** the tool acts *as its owner*. It
calls the orchestrator's own REST API with `_get_client(user_id=context.user_id)`
(`src/tools/orchestrator/catalog.py:864`), so ownership checks and
`_enforce_save_grants` still apply — an agent cannot write an expert granting
shell to a user who lacks the `shell_tools` grant. `dry_run` also defaults to
`True`. The agent can do what its owner could already do from the cockpit.

**Residual, worth tracking but not urgent:** an agent that can rewrite experts
can *persist* changes that affect future agents. That is a blast-radius and
prompt-injection-reach concern, not a cross-user boundary break. It is an
argument for D5 classifying these as admin-only, which was already the decision.

**Not verified:** that a live session created this way binds and successfully
executes the tool end to end. The config and validation path is proven; the
runtime round trip is not.

### What D1 actually requires

"The UI shows the final config" is a stronger requirement than "the checkbox
matches the override", and it has consequences worth stating before work starts:

- **The read must include every layer, including the ones that are not YAML.**
  The runtime injection layer (`persistent_session.py:1408-1557` and friends)
  grants `sleep`, `notify_user`, the datasource categories and more. A resolved
  view that omits it will show `core: off` while the agent holds two core tools —
  a new disagreement in place of the old one. Today's `/tool-groups` endpoint is
  a *lean* resolve with a documented skip ledger and covers 4 of 24 categories;
  it is the right seam but not yet the right answer.
- **It must include the gates, not just the merge.** `filter_tools_by_backend`
  drops `shell`/`browser_direct`/`git` on lite workspace tiers, and the
  `shell_tools` grant can deny shell outright. Both change the final answer.
- **Therefore a checkbox is the wrong control.** At minimum three states are
  needed: *on*, *off*, and *unavailable* with a reason ("needs the shell_tools
  grant", "not supported on this workspace tier", "granted by the runtime, not
  configurable"). A two-state checkbox cannot express the truth D1 demands, and
  forcing it to try is what produced defects 2 and 3.
- **Both write surfaces must accept the whole vocabulary the UI displays.**
  Creation and live currently accept a hand-curated four. Anything the UI renders
  that the boundary silently drops is defect 2 again under a new name.

## Verdict on the bigger question

**Fix the seams; do not rewrite the config system.** The base → expert → project
→ request → runtime layering is a reasonable shape and caused none of these
defects. The failures were the `[]` overload, enablement logic computed in
several places with one path answering the opposite, silent drops at every
boundary, and no observability. Under a feature freeze with two live pilots, a
rewrite is the change most likely to repeat `57430a2a`. This is argued at length
in [[tool_config_policy_vs_membership]] §"Recommendation up front".

## Roadmap

Ordered so that each step stands alone and stopping anywhere leaves the system
better than now. Phase 0 is roughly an hour and returns the lost capability.

### Phase 0 — pin the current state

**0.1 Golden snapshot test** — `tests/test_config_tool_grants_snapshot.py`.
Walks both bases, `interactive.yaml` and every `config/experts/*/config.yaml`
through `load_and_merge_config` + `get_all_tool_names`, asserting the sorted
result against a checked-in fixture. Add the unknown-category-key assertion
while there (`schema.json` cannot catch defect 6).

*This is the commit that would have caught `57430a2a`*, and it makes every later
step verifiable: from here on, the acceptance criterion for most work is "the
snapshot did not move." Under D2 there is no shell revert to land first, so this
is now the true first commit — it pins the current state, which is also the
intended one.

**0.2 Note on the worker base.** `57430a2a` also emptied `tools.shell` (3 names)
and `tools.delegation` (2 names) in `worker_base.yaml`. Nothing observable broke:
the developer, critic, scholar, designer, bughunter and product-qa experts each
declare their own lists, so only a job on the bare base is affected — and under
D2 that is the desired default anyway. The snapshot pins it as-is. No action.

### Phase 1 — make the resolved config knowable (D1, the spine)

This phase is the priority. Everything in Phase 2 gets easier once it exists, and
defects 2, 3 and 6 are closed by it rather than separately.

**1.1 A true resolved-tools read.** Generalise `_merged_session_tool_groups` /
`GET /api/persistent/threads/{id}/tool-groups` from 4 categories to all 24, and
from a lean merge to the *final* answer — merge, plus the runtime injection
layer, plus `filter_tools_by_backend`, plus the `shell_tools` grant outcome.
Return per category: state (`on` / `off` / `unavailable`), reason when
unavailable, and provenance (which layer decided it).
*Acceptance:* for a live session, the endpoint's per-category answer matches the
agent pod's `Loaded N tools` log exactly. That comparison is the test — pod log
against endpoint, on a real thread, for at least one session on each workspace
tier.
*Risk to watch:* injection happens agent-side at attach, after the orchestrator's
resolve. If the orchestrator cannot compute it faithfully, the honest fallback is
to have the agent report its bound set back and serve that, rather than to
approximate. An approximation here recreates the original bug.

**1.2 One vocabulary at both write boundaries.** Replace the hand-curated
`SESSION_TOOL_OVERRIDE_NAMES` with registry-membership validation over all
categories, at both create (`main.py:22569`) and runtime update
(`main.py:21473`). Unknown or unhonourable keys are **rejected**, not dropped —
the precedent is `_validated_reasoning_level` ("garbage fails loud here instead
of being silently dropped", `main.py:3591-3593`).
*Closes defect 2.* *Acceptance:* a create request naming a category the boundary
will not honour returns 400, not 201.
*Blocked by:* the 4b decision below — widening the vocabulary is exactly what
makes `agent_catalog: true` reach the six unaudited `*_bundle` writes. Take that
decision before this ships, or ship with the curated-expansion mitigation and an
expiry.

**1.3 The cockpit renders from 1.1, in three states.** Both surfaces — creation
and live — read the resolved endpoint and show *on / off / unavailable + reason*.
Live stops filtering to four categories; creation stops deriving re-enable names
from the layer it is overriding.
*Closes defect 3.* *Acceptance:* every category the UI renders is one the
boundary accepts, and its displayed state matches 1.1's answer.

**1.4 Shell becomes settable (D3).** Falls out of 1.2 and 1.3 once `shell` is in
the vocabulary. Keep `shell_tools` as the gate (D4) and surface a denial as
*unavailable — needs the shell_tools grant*, never as a silent drop or a plain
unticked box.
*Acceptance:* an admin can enable shell on a new session and the agent binds
`run_command`; a user without the grant sees it unavailable with the reason, and
a request that names it anyway is refused.

### Phase 2 — the schema

Seven commits, fully specified in [[tool_config_policy_vs_membership]]
§Sequencing. Phase 1 above overlaps its commits 1, 5 and 6; what remains:

- `normalize_tool_policy` + registry-derived expansion. No config changes;
  snapshot must not move.
- `grant: "code"` / `gate:` classification for the 38 code-only tools — makes
  `true` behaviour-preserving for `core` and `shell`, and is what lets 1.1
  describe the injection layer instead of guessing at it.
- **4a** `[]` → `false` for the 15 policy categories; **delete the key** for the
  11 machine-owned connector categories.
- **4b** Decide the nine tools the registry has and the closed vocabulary does
  not. Under D1 this is no longer a side risk — it **gates 1.2**.
- **4c** Migrate the 23 full declarations to `true`.
- Optional per-persona `only`/`except` adoption. Pure readability.

Under D2, `config/session_base.yaml` gains `shell: false` at 4a — the legible
form of the state `57430a2a` left behind.

### Phase 3 — the rest of the register

**3.1 Curation prompts (defect 5).** Either grant the curator
`kb_index`/`kb_lint`, or guard the five prompt variants with
`{% if has_tool(...) %}` as the `delegate_work` references already are. Granting
is probably right — the tools were built for this job — but it is a behaviour
change and should be explicit.

**3.2 Archive handling (defect 7).** Extract `.zip` at the session upload seam in
`orchestrator/services/thread_uploads.py`, mirroring the worker path, with
explicit traversal and size caps rather than the worker version's incidental
protection. Separately, give `read_file` a binary branch so it reports an archive
listing or `[binary file: N bytes]` instead of a codec error. Independent of
everything above — this is the defect that started the investigation and it can
be done any time.

**3.3 Reasoning reset feedback (defect 4).** **Do not remove the reset.** It is
correct and load-bearing: reasoning vocabularies are per-family and do not
translate — `gemma` is a binary `on`/`off` toggle, `gpt-5.6` is an effort enum
`low…max`. Carrying `max` into a toggle family is meaningless, and carrying a
stale sampler value across families is what produced hard 400s and motivated the
clearer in the first place (`21f55ab1`, "the stale `top_k` lesson").

The whole defect is the **absence of feedback**. The select silently snaps back
to the family default, which is visually indistinguishable from never having
been touched — so the user believes a level is set that is not. Same *form* as
D1: a UI showing a state the backend does not hold. Fix: say so when it happens.

Downgraded and explicitly *not* recommended: making the reset conditional on the
new family supporting the level. It would help only in narrow cases
(`gpt-5.6-sol` → `gpt-5.6-terra`, identical option sets; `high` valid in both
`gpt-5` and `gpt-5.6`), it is already covered server-side for enum→enum by
`_clamp_reasoning_level` walking the effort ladder to the nearest supported value
(`src/core/loader.py:2939`), and it cannot work at all for enum→toggle. Not worth
the branch.

**Unverified premise:** the trigger in the motivating session was never
confirmed. The absence of `llm.reasoning_level` from the thread's
`config_override` proves the value was not sent; that it was *cleared by a model
or expert change* is an inference from those being the only two clearers. If the
user did not change either after picking, a third path drops the value and this
entry is mis-diagnosed. Confirm before fixing.

Three traps that must not be lost between docs:

- **4a is not a uniform sweep.** 11 of the 26 base empties are machine-owned
  connector categories where `[]` means "config does not manage this", not
  "off". Convert those and the day `false` becomes a real veto, every migrated
  config kills its own datasources.
- **`shell` must never become `true`** — `run_command` and `shell_execute` are a
  mode-alias pair rewritten at `loader.py:4507-4510`.
- **4b gates 5.** `SESSION_TOOL_OVERRIDE_NAMES` is hand-curated, not a registry
  transcription (`agent_catalog` 5 vs 9, `workflows` 7 vs 9, `orchestrator` 14 vs
  17). A naive `agent_catalog: true` newly grants `set_expert_bundle` and
  `set_skill_bundle` — catalogue *writes* — to any session ticking "Experts &
  Skills". The curated list encodes a safety judgement the registry category does
  not carry; that judgement must move into registry metadata before membership
  moves to the registry.

  **Resolved 2026-08-03, and better than "move it into metadata": the judgement
  moved into the category structure.** `agent_catalog` is now 5 vs 5 and
  `workflows` 7 vs 7, because the six writes live in `catalog_authoring`
  ([[agent_authored_catalog_entries]]). Metadata that must agree with a name list
  is still two statements of one rule; a category whose membership *is* the
  answer is one. `orchestrator` remains 14 vs 17 and keeps the `grant: "explicit"`
  mark, because that category genuinely mixes privilege levels.

### Triage, not scheduled

- ~~**Defect 9** (`*_bundle` privilege path)~~ — **resolved**, see triage item 7
  above. It passes every gate, but acts as its owner through the normal API, so
  it is not an escalation. Downgraded to a blast-radius argument for D5.
- **Defect 8** (job-path allowlist) — **decided** by D11: a gap, closed with the
  Phase 1.2 validator. Remains the only unaddressed security-shaped item.
- ~~Should the Shell checkbox work as a per-session toggle?~~ **Decided** — see
  D2/D3/D4. Off by default, settable from the UI and via experts, gated by
  `shell_tools`. Now Phase 1.4, not triage.
- Open, if pilot users are meant to self-serve shell: `shell_tools` is
  deny-by-default and admin-granted, so today they cannot. That is a
  grant-policy question, not a config one, and nothing in this roadmap changes
  it either way.

## Provenance, and what was got wrong on the way

Recorded because the earlier revisions of these docs are still in git history and
three of their conclusions were wrong. Anyone re-deriving from them should know:

- **"Shell was never available in sessions"** — wrong. It was the default from
  2026-03-31 to 2026-07-22. The error came from reading the
  `# Shell and application-control groups are opt-in at the expert layer` comment
  as established policy; `git blame` shows it was written *by the commit that
  removed the capability*, on the same line. A justification introduced alongside
  a change is not evidence of prior intent. The same trap is present a second
  time at `config/experts/developer/config.yaml:76`.
- **"`sleep`, `notify_user`, `request_workspace_upgrade`, `srw_cloud_status` are
  ungrantable"** — wrong, and the reason matters: they are granted by the runtime
  injection layer described above. Centurion's wake/sleep cycle was verified
  working live (thread `d67ee261` called `sleep` at 2026-08-02 09:15 UTC), not
  inferred from config.
- **"Critic and curator have effectively read-only workspace tools"** — wrong.
  Their lists contain `write_file` and are byte-identical to the developer's,
  which suggests a copied snapshot rather than per-persona intent. More of the 40
  subset declarations are drift than first reported; treat any claim that a
  subset encodes intent as needing evidence.
- An early scan used `config/*.yaml`, which **misses `config/experts/*/config.yaml`**
  — the majority of tool declarations. Any glob over configs must be recursive.
