---
tags:
  - test
  - verification
  - sessions
  - orchestrator
  - cockpit
  - config-resolution
related:
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[tool_configuration_deferred_findings]]"
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
  - "[[tool_config_policy_vs_membership]]"
  - "[[registered_tools_no_config_can_grant]]"
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
---

# Tool configuration — live gates, 2026-08-03

**Status: RUN, six gates across three rounds, all passing.** One defect was
found by round 1, fixed, and re-gated in round 3. This closes merge blocker 3 of
[[tool_configuration_defects_and_fix_roadmap]] — the roadmap's Phase 1.2 / 1.3 /
1.4 acceptance criteria, which had never executed against a cluster.

**Where:** local k3d (`k3d-srw`, namespace `srw`) — a throwaway cluster, which
is why round 2 could create a non-admin user by direct SQL and round 3 could
revert files under a running dev server. **Nothing here was run on dev or
prod.** The branch is still unpushed, so these gates prove the code on
`develop`, not any deployed image.

**How, in every round:** `kubectl -n srw exec` into the orchestrator pod, in-pod
Python against `http://localhost:8085`, authenticating over the MCP header path
(`X-MCP-User-Id` + `X-Internal-Key` = `MCP_INTERNAL_KEY`). The
`mcp__orchestrator__*` tools were deliberately **not** used — they point at the
remote dev cluster. The UI half ran in headless Chromium driven by the cockpit's
own bundled Playwright with a throwaway profile.

| Round | Gate | Verdict |
|---|---|---|
| 1 | Task 7 — unticking takes effect; smuggling is refused | **PASS** |
| 1 | Task 8 — the UI tells the truth | **PARTIAL** (3 of 4; one new defect) |
| 2 | A — the grant-denial path, read side and write side | **PASS** |
| 2 | B — the two remaining smuggle boundaries | **PASS** |
| 2 | C — the `by=backend` tier sentence | **PASS** |
| 3 | The round-1 defect, fixed and re-driven | **PASS** (4 of 4) |

## What the browser and the pod were actually running

The single most common way a live gate lies is by testing an image that does not
carry the code. Verified before trusting anything on screen:

| Component | Image / source | Carries the series code? |
|---|---|---|
| orchestrator | `srw-orchestrator:tilt-26b1a89b3071cbe4` (the Deployment's own image) | yes — `openapi.json` lists both `/api/persistent/threads/{id}/tool-groups` and `/api/persistent/tool-groups/preview` |
| agent | `srw-agent:tilt-9ce8cd74ce2f681b` | yes, **and it serves `GET /session/toolset`** — present in both `persistent_app.py` and `dual_app.py` |
| cockpit | `srw-cockpit:tilt-ea6a40e67532da6b`, a Tilt **dev server** serving `/app/src` | yes — the six touched sources are **md5-identical to the working tree**, and the five retired TS symbols are absent from the pod |

Round 3 re-checked all six cockpit files by md5 against the tree
(`resolved-toolset.ts f54e5896…`, `tools-group.component.ts 81a7e909…`,
`settings-pane.component.ts d03b47ed…`, `agent-settings.component.ts 6db35115…`,
`en.json fcc1e04b…`, `de-DE.json e50f2377…`) before and after each revert.

---

## Round 1, gate 1 — a request-level untick reaches the agent

Thread `d04da8f2`, created with `config_override: {"tools": {"research": []}}`.

**The proof is the control, not the absence.** The agent pod bound **32** tools
and none of the eight `research` members. Absence alone would not have been
evidence on this cluster — `knowledge` reports `configured: 10, bound: 0` for
unrelated reasons — so a stock session was created with **no override at all**,
same expert, same tier, same image:

```
GATE1-research-unticked : Loaded 32 tools for persistent session
GATE1-control-stock     : Loaded 40 tools for persistent session
```

**40 − 32 = 8, and the 8 are exactly `research`** (`web_search`,
`extract_webpage`, `crawl_website`, `map_website`, `search_papers`,
`download_paper`, `get_paper_info`, `research_topic`). The untick, and only the
untick, removed them.

Corroborated four ways: `threads.metadata.config_override` holds the override
(it reached the persisted row, not just the request); the agent's own
`GET /session/toolset` is a full measurement with **no `research` key in
`categories` at all**; the endpoint answers
`{"state":"off","settable":true,"decided_by":"request","tools":[],"configured":[]}`
with `origin: agent` and `observed_at` set; and pod log 32 = agent report 32 =
endpoint 32 as an exact set match.

**And the four-group field cannot answer this question:** `"research" in
tool_groups` is `False`. Any future gate must read `categories`.

### Smuggle refusals — eight boundaries, `tools.canvas: ["run_command"]`

Five in round 1, three more in round 2 (gate B). Every one returns **400** with
the same message naming the key, the offending name, and where it really lives:

```
tools.canvas: 'run_command' is in tools.shell. A tool list may only name tools of its own
category — the loader resolves a name against the global registry, not against the key it
arrived under, so a foreign name would bind the foreign tool.
```

| Boundary | Smuggle | Clean-body control |
|---|---|---|
| `POST /api/persistent/threads` (session create) | 400 | — |
| `POST /api/jobs` | 400 | — |
| `POST /api/experts` | 400 | **200** (identical body, smuggle removed) |
| `PUT /api/experts/{id}` | 400 | — |
| `POST /api/projects` (`default_config_override`) | 400 | — |
| `POST /api/automations` | 400 | **201** |
| `PATCH /api/automations/{id}` | 400 | **200** |
| `POST /api/sessions/{id}/prepare` | 400 | **202** (twice) |

**The strongest single piece of evidence is the automations control**, because it
shows *which layer* ran. The clean POST did not merely return 201 — the stored
row proves the validator was on the path, because it **normalised the value on
the way in**:

```json
// sent:   "config_override": {"tools": {"canvas": true}}
// stored: "config_override": {"tools": {"canvas": ["clear_canvas","get_canvas","set_canvas"]}}
```

A 400 from a shape mismatch could not have done that. The PATCH control behaves
the same way (`{"tools":{"core":true}}` comes back as the six-name `core`
enumeration).

Automations were worth driving for a specific reason, confirmed by reading
`orchestrator/services/automations.py:230`: `create_job_from_automation` takes
`config_override` straight from the stored row and hands it to `db.create_job`,
with no validator anywhere on that path. `POST`/`PATCH /api/automations` is
therefore the **only** boundary a stored smuggle would ever cross — and a stored
one would re-plant on every cron fire. Both halves refuse.

---

## Round 1, gate 2 — the UI tells the truth (3 of 4)

Driven in a real browser against the running dev server, sandbox-tier session.

**Three states render — PASS.** 25 rows, **0 untranslated i18n keys on screen**.
`on` renders a checkbox `checked=true disabled=false`; `off` renders
`checked=false disabled=false`; `unavailable` renders **no checkbox**, a block
glyph, and the server's own sentence verbatim in warning colour
`rgb(154,120,34)`. Seven `unavailable` rows in total. All four provenance banners
were observed with their own border colours, and `agent_partial` correctly reads
as a **measurement** ("8 BOUND", not "8 PREDICTED") even though `observed_at` is
`null` — the trap Task 8 was written against.

**Locked-on rows — PASS.** `Product Help`, `Session Tasks` and `Shell` all
render `checked=true disabled=true` with the 🔒 glyph, and the sentence lands in
`.tool-toggle-note` at `--text-muted rgb(138,123,102)` — **not** in
`.tool-toggle-reason` at `--warning rgb(154,120,34)` — with no block glyph drawn
over bound tools. The Critical finding (a bound category rendering as blocked)
does not recur.

**Degraded pane — PASS.** Aborting the `tool-groups` request in the browser
(`page.route(...).abort()`, no cluster state touched) yields
`data-trust="unknown"`, the banner, and **0 rows** — not twelve ticked ones.

**Shell-enable — HALF FAIL, one new defect.** The untick half passed: a positive
control on the settable `Core` row produced exactly one `config.update` frame,
and unticking the locked-on `shell` row produced **0 frames**. The enable half
did not. The server correctly reported
`shell: {state:"on", settable:false, decided_by:"registry", tools:["srw_cloud_status"]}`,
but `tools-group.component.ts` bound `[disabled]="disabled() || isRowLocked(row)"`,
so the checkbox rendered `disabled=true`, and a disabled checkbox fires no
`change` event. A real click and a forced click both produced 0 frames; there
was no alternate route (`toggleAll()` filters on `isRowSettable`, the per-row
reset renders only outside `live` mode).

The dispatch machinery underneath was correct and was proven so by driving the
component past the disabled control: the frame was sent, the server persisted
it, and the agent rebound **82 → 85 tools**. What was missing was any gesture a
user could make.

> This is the third time in the series that a green suite endorsed something
> wrong, and the third time a live or rendered check caught it. The covering
> tests called `toggleCategory` / `toolsFragment` and a mounted component's
> methods — none of which observes that the rendered `<input>` is `disabled`.

### Topology correction, worth keeping

k3d's **default** session backend is `virtual`, where `supports_shell: false` and
`shell` correctly reads `unavailable` with no checkbox. The locked-on topology
the defect needs — `state: "on"` **and** `settable: false` — requires an explicit
`sandbox` backend. That tier had never been gated before this run, and it is
where the defect lived. Blast radius: live sessions on shell-capable tiers, not
every session pane.

---

## Round 2, gate A — the grant-denial path

The owner authorised a **non-admin, approved** user created by direct SQL in
k3d's Postgres, with **zero** `capability_grants` rows at any scope, so
`shell_tools` falls to its catalog default of `False`. Keycloak was never
touched and the existing admin was never demoted.

**Read side — PASS.** Live measured (`origin: agent`, 40 tools bound):

```json
"shell": {"state":"unavailable","settable":false,
          "reason":"requires the shell_tools capability grant",
          "decided_by":"grant","tools":[],"configured":[]}
```

Note `configured: []` — the config never asked for shell and the reason fires
anyway. That is the point: a user without the grant is told *why*, not handed a
plain unticked box.

**Strongest single piece of evidence — two causes, two sentences, one
response.** The same HTTP response, one session, one tier:

```
shell            unavailable  settable=False  by=grant     requires the shell_tools capability grant
git              unavailable  settable=False  by=backend   this workspace tier has no shell, so the backend capability gate drops these tools
browser_direct   unavailable  settable=False  by=backend   this workspace tier has no shell, so the backend capability gate drops these tools
```

That single response is simultaneously gate A's read side and gate C's
requirement, and it rules out the reading that one generic sentence is being
printed for every unavailable category.

**Two controls make it a measurement rather than a coincidence.** The same
`POST /api/persistent/tool-groups/preview` body with only the principal changed:
non-admin gets `unavailable / settable:false / by=grant`, admin gets
`off / settable:true / by=base`. And on the *already-running* session, with
nothing changed but one row in `capability_grants`, the reason flips reversibly:
absent → grant sentence, `INSERT shell_tools=true` → tier sentence, `DELETE` →
grant sentence again. `_resolve_runner_grants` re-reads Postgres per request;
there is no stale-grant window.

**Write side — PASS, and refused with the grant named.** Three PEPs
(`POST /api/persistent/threads`, `POST /api/jobs`, `POST /api/experts`) all
return **422** `config exceeds your capability grants: shell_tools: tools.shell
requires the shell_tools grant`, because all three funnel into
`_enforce_dispatch_grants` → `evaluate`. `tools.shell: true` gets its own **400**
from the enumerate-only validator *before* the PDP — also a refusal, not a silent
drop. Two controls: the identical `POST /api/experts` body is **422** as the
non-admin and **200** as the admin; and the same POST flips 422 → 200 → 422 as a
single grant row is inserted and deleted. After every refusal, scans of `jobs`
and `threads` returned **0 rows** — nothing was silently dropped and nothing was
silently accepted.

**A revoked grant is re-evaluated, not frozen at create.** A thread created
*while* the grant was held genuinely names `tools.shell` in its persisted
override; after the grant was deleted its live read reports `state:
unavailable`, `decided_by: grant`, `configured: ["run_command","shell_execute"]`.
`configured` still asks; the reason correctly reverts.

### The `explicit` tier holds — `agent_catalog: true` binds five, not eleven

This is the D5 property the whole registry-classification task existed for. Live
**measured** (`origin: agent`, so it is what the agent process bound, not a
forecast) on a session created with `tools: {agent_catalog: true, workflows: true}`:

```
agent_catalog  on  by=request  tools=[list_experts, get_expert, list_skills, search_skills, get_skill]
workflows      on  by=request  tools=[list_automations, get_automation, list_automation_runs,
                                      propose_automation, get_project_loop,
                                      list_project_loop_jobs, explain_project_loop]
```

Five names and seven names, with **all six `*_bundle` writes absent**
(`get_`/`set_expert_bundle`, `get_`/`set_skill_bundle`,
`get_`/`set_automation_bundle`). `orchestrator: true` likewise expanded without
`get_stuck_jobs` / `steer_worker_job`, the other two `explicit` members.

Principal-independence was checked rather than assumed: neither category is in
`GRANT_GATED_CATEGORIES`, and the preview expansion is byte-identical for admin
and non-admin. The exclusion is the registry's `grant` mark read by `_grantable`
— not a grant lookup — so `true` is safe by construction rather than by policy.

**This does not close §3c of [[registered_tools_no_config_can_grant]].** It
proves a category-level `true` cannot reach the bundle writes. Whether an expert
that names `set_expert_bundle` *explicitly* binds and executes it end to end is
still unverified.

---

## Round 2, gate C — the tier sentence

Admin-owned thread on the `virtual` tier, so `_resolve_runner_grants` returns the
bypass and no grant reason can pre-empt the tier. All three execution categories
report the tier and only the tier.

`git` and `browser_direct` are the sharper evidence: their merged config grants 5
and 9 tools respectively and the agent bound **none**, so `compose_tool_view` had
a legible cause available and used it — the tier sentence, not the weaker
"config granted N and the agent bound none" fallback.

Pinned three independent ways: within one response (`shell` vs
`git`/`browser_direct` on the non-admin session); across principals (same tier,
same config, admin vs non-admin); and across time (one live session, grant row
inserted then deleted). The grant sentence and the tier sentence never appeared
together for one category, and `decided_by` tracked the reason in every case —
precedence behaves as `compose_tool_view` documents, grant beats backend.

---

## Round 3 — the round-1 defect, fixed and re-driven

**This round changed code** (commits `39b6e2bb`, `d1cb216d` on `develop`). The
fix is a second control rather than a template tweak; the design argument and why
the two obvious one-liners were both wrong is recorded in the roadmap's "Known
consequence" section.

**The drive is known to be sensitive, because the failing state was reproduced
first.** The four implementation files were reverted to `HEAD`, Tilt re-synced,
md5 re-confirmed, and the *same session* driven:

```
[PREFIX] Shell🔒 1 bound  checkbox {checked:true, disabled:true}
[PREFIX] addBlock: null   addButton: null   allAddBlocks: []
[PREFIX] real click threw: locator.click: Timeout 3000ms exceeded
[PREFIX] CHECK1 untick frames: 0    CHECK2 NO ADD CONTROL IN THE DOM
```

Round 1's defect, verbatim. Files restored, Tilt re-synced, same session, same
agent pod, same tier, nothing else changed:

```
[POSTFIX] addBlock: "Locked on — config can still add: cancel_command, run_command,
                     shell_execute, shell_read   Add (4)"
[POSTFIX] allAddBlocks: ["shell"]        <- ONE row, not every locked row
```

| Check | Result |
|---|---|
| Unticking a locked row dispatches nothing | **PASS** — real click throws (disabled), force click no-ops, 0 frames, still checked |
| The add-affordance dispatches | **PASS** — exactly **one** `config.update` carrying `{"tools":{"shell":{"only":["cancel_command","run_command","shell_execute","shell_read"]}}}`; the button then reads `Add requested`, disabled |
| The server persists it | **PASS** — read straight out of Postgres, normalised to a list on the way in |
| The agent's bound set grows | **PASS** — see below |

**The only check that proves anything** is the last one. Agent pod
`srw-agent-s-2861b9db`:

```
15:41:01  Loaded 55 tools for persistent session
15:46:15  Loaded 85 tools for persistent session
shell-category names now bound: ['run_command','cancel_command','shell_read','srw_cloud_status']
```

**Set-diff across the rebind: 30 added, 0 removed.** Three of the thirty are the
shell tools; `shell_execute` is absent because it is the mode-alias twin of
`run_command`, as documented. **And the row heals itself** — the next read
reports `state:"on"`, `settable:true`, `decided_by:"runtime"`, so the lock
disappears and the ordinary checkbox returns, correctly: the bound set now holds
config-granted names, and unticking really would drop them.

*Observation, not a defect of this change:* the other 27 additions are the
`orchestrator` / `agent_catalog` / `workflows` closed groups, which the boot load
did not have and the post-`config.update` reload does. This is the cross-task
finding recorded in [[tool_configuration_deferred_findings]] —
`_load_tools_for_backend` re-appends full canonical membership for the four
closed groups, so *any* `config.update` would do the same, and a **narrowed**
closed group is silently widened.

**Bundle, because the budget hard-fails CI:** production build succeeds with no
budget ERROR. Initial total **2701.36 kB** raw / 576.66 kB transfer →
**48.64 kB headroom** to the 2.75 MB decimal `maximumError`. Measured against a
build with only the four implementation files reverted, **+2.49 kB raw** is this
change; the rest of the drop from Task 8's recorded 54.57 kB belongs to a
concurrent session's tool-card work, which is also in the tree.

`npx vitest run` — 1642 passed / 114 files, zero failures. `tsc --noEmit` clean.
`tests/fixtures/config_tool_grants.json` md5 still
`9ddc4f7985f65a6ec01b3d05179ef261`, unmoved since Task 2's deliberate grant.

---

## What is still not gated

1. **`agent_partial` produced by the server.** The deployed agent image serves
   `GET /session/toolset`, so the live path is always a full measurement. Its
   *rendering* was gated by rewriting the response in the browser; its
   *production* needs an agent image predating the route.
2. **The tier sentence rendered in a browser.** Gate C was server-side. Round 1
   observed `row.reason` rendered verbatim in `--warning` with the checkbox
   absent for three other causes, and there is no client-side re-derivation on
   that path, so this is a coverage gap rather than an open question.
3. **Anything on a real deployment.** All six gates ran on throwaway k3d against
   an unpushed branch. A dev deploy should re-run gate 1 (the 32-vs-40 control
   is cheap) and the shell-enable drive, because the deployed cockpit is a
   built bundle rather than a dev server.
4. **`project_experts.config_override`.** No route exposes it and `query_table`'s
   allowlist excludes it, so the one stored `tools` population that could hold a
   malformed value could not be measured from any instrument available. See
   [[tool_configuration_deferred_findings]].

## Artifacts created, and their removal

Across all three rounds: 7 threads, 3 experts, 1 automation, 1 non-admin user, 1
`capability_grants` row, 1 pod temp file (`/tmp/srwlib.py`) — **all removed**,
with post-cleanup scans returning 0 on every count (`threads LIKE 'GATE%'`,
`experts LIKE 'gate%'`, `automations LIKE 'GATE%'`, grants for the test scope,
`users` back to the single original admin). Every refused probe (five 422s, nine
400s) created nothing, verified by scanning `jobs` and `threads`.

No pods were deleted by hand, no images patched, no migrations run, Keycloak
untouched, gitea untouched. Agent pods created by the gates are gone; round 1
left a foreign agent pod (`srw-agent-s-b6dce7f7`) alone because it was not ours.

**Concurrent session:** another session was working in `cockpit/` throughout
(tool-card markdown rendering). Its files were never staged; `en.json` and
`de-DE.json` are shared, so only this series' own hunk in each was staged, via a
filtered `git apply --cached`.
