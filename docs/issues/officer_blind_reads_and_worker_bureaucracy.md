# Officer blind file reads + worker bureaucracy — Centurion night-1 postmortem (2026-07-30)

**What this is:** postmortem of the Centurion's first fully supervised night on Better
Resavio (project `68137e29`, officer thread `d67ee261`, 2026-07-29 21:14 → 2026-07-30
09:20 UTC) and the fix plan it produced. The officer's morning verdict was "workers
repeatedly fail to deliver; the verification mechanism is blocked" (KB note
`century-state-on-change-of-command`, page sent 07:48). The investigation **reverses the
headline**: the largest single cause was that the officer's file-reading tool has never
returned anything but "not found" — for any file, on any job — so he steered and then
executed a worker that had already complied. Around that sit real findings: the
strategic/tactical phase machinery converts feedback into re-planning and defers
deliverables to the end, the project memory system has institutionalized a formalized
giving-up pattern, and the infra underneath (VM SSH, agent heartbeats, codex-proxy) was
flapping all night.

**Status:** findings verified against code + cluster 2026-07-30. **P0 wave shipped and
deployed to dev the same day** — P0-A `ef3ec62b`, P0-D `3cda6f09`, P0-B `70ca9461`
(+ C2 snapshot `58e3a667`), P0-C executed against the dev RecallStore 11:40 UTC;
shipped-notes inline in §4. P1/P2 remain unbuilt. All six research annexes (§7: read paths, steer mechanics, phase overhead,
cluster evidence, framework survey, research literature) have landed and are folded into
the findings and fix plan. Companion doc for the phase-guardrail arc:
`docs/issues/agent_phase_guardrails_burn_legitimate_work.md` (2026-07-15) — finding
"workspace read APIs read never-written orchestrator PVC" is the same substrate as F2
here; this doc adds the route mismatch, the officer consequences, and the steering
mechanics.

---

## 1. The night in brief

| Time (UTC) | Event |
|---|---|
| 21:16 | Officer (fresh on gpt-5.6-sol) orients, writes change-of-command KB note: engine stable per iter-30 QA, demo gap = web UI, legacy claims self-conflicting (180/180 vs 179 pass / 3 fail / 1 deselected) → declares truth gate: no construction until independent verification. |
| 21:53 | Absorbs the Legatus demo ruling (web UI required); plans hygiene + verification + heavy-slot UI architecture. |
| 00:08 | Legacy loop job `58027ee7` (DEVELOPER, iter 3) completes with claimed Kassenabschluss export. Officer does not accept the claim. |
| 00:09 | Spawns verifier #1 `7c2685e8` (critic, MiniMax-M3 line slot): "verify 58027ee7, establish the truthful repository baseline." |
| 00:10–05:31 | Monitors it for 5.5 h / 1096 steps / 7 phases. Steers at 05:21 ("stop archaeology, write the report"). |
| 05:31 | Verifier #1 seals `pending_review` at confidence 0.42 with **0/7 deliverables** ("honest-floor seal"). Officer rejects, resumes with feedback. |
| 06:08–06:19 | Still no report file visible; urgent steer, then cancel. |
| 06:20 | Spawns verifier #2 `e239ef27` (worker_base): "produce the report **before** any optional investigation." |
| 06:25–07:14 | Worker writes the report scaffold at step 61 (later confirmed in Gitea). Officer's checks all return "not found"; two urgent steers, each triggering a full re-plan in the worker. |
| 07:25 | Cancels verifier #2. |
| 07:27 | Escalates to heavy slot: `eb0143f8` (gpt-5.6-sol), KB-note-only deliverable. |
| 07:47 | Heavy verifier fails: codex-proxy 408 stream-disconnect, identical on 4 backoff cycles across fresh pods. |
| 07:48 | Officer pages the Legatus (1/3), concludes "truth gate failed three times by two distinct mechanisms", stands down to long sleeps. Two of his own turns error on the same proxy (07:40, 08:45); recovers 09:20. |

**What the officer got right** (relevant because the fixes below must not lobotomize
this): skepticism about self-conflicting completion claims; keying acceptance to an
artifact rather than worker narrative; rejecting the 0/7-deliverable seal; a clean
escalation ladder (steer → cancel → replace → escalate model tier → page → stand down);
exactly one page; a KB log good enough to reconstruct the night from. His one
materially wrong call — executing verifier #2 — traces to the broken sensor, not to
judgment.

---

## 2. Findings

| # | Finding | Site | Status |
|---|---------|------|--------|
| F1 | Officer's `get_job_workspace_file` sends the path as a query param to a `{path:path}` route — reads literal file `file`, 404s **always**, and the error message echoes the requested path, laundering the bug as worker non-delivery | `src/tools/orchestrator/jobs.py:733` vs `orchestrator/main.py:16370` | **Fixed** `ef3ec62b` (P0-A) |
| F2 | The backing endpoint is a dev relic: `WorkspaceService._get_job_path` ignores `job_id` and reads orchestrator-local `WORKSPACE_PATH` — on k8s that PVC only ever contains `uploads/`, so every job-file read returns 404 while `is_available` stays true. It is also a **cross-job leak by construction** (same path for every job_id). The MCP `get_workspace_file`/`get_workspace_overview` build the URL correctly but hit the same dead backend; the cockpit **todo view** reads the same service and is permanently empty on k8s | `orchestrator/services/workspace.py:65-80`; PVC `helm/templates/orchestrator/deployment.yaml:1284-1320`; MCP `orchestrator/mcp/client.py:1032,1047`; cockpit `todo.service.ts:106` | **Routes deleted, consumers repointed at Gitea** `70ca9461` (P0-B); service kept for uploads/logs/frozen-json |
| F3 | Officer has **no working read path at all**: his toolset lacks the Gitea-backed reader (the MCP server's `get_job_file` works today), and his sitrep instructions direct him at the broken tool | toolset `src/core/session_tool_overrides.py:27`; sitrep text `orchestrator/services/session_wake.py:594-598`; working path `orchestrator/mcp/server.py:714` | **Fixed** `ef3ec62b` (P0-A): reader repointed, lister added, sitrep/prompt text updated |
| F4 | Urgent steer ≡ resume-with-feedback, and it is worse than "destructive": (a) `restore_from_feedback` force-compacts (originals dropped from the checkpoint via `RemoveMessage`), **wipes in-flight tactical todos without archiving them** (archive_phase bypassed on this route), re-enters strategic, and injects a factually false "[FEEDBACK_RESUME] previously frozen for human review" banner; the resume template then mandates "REWRITE plan.md — the old plan is no longer relevant"; (b) **no stop signal ever reaches the running pod** — the deployed `dual_app` heartbeat handler reads only `should_drain` and ignores the `job_status` field the orchestrator already returns (the deny-list backstop exists only in the non-deployed `app.py`), so the old pod keeps executing as an **orphan, double-writing the same Postgres checkpoint thread** as the replacement pod, and with a single-slot pool the job sits paused until the orphan dies; (c) the non-urgent lane is a dead letter box: queued replies are written to `messages/…_received.md` which **nothing tells the worker to read** (no read tool exists; the REVIEW todo's `git_diff` cites a tag name that is never created), and the documented clearing contract (`consumed_reply_threads`) **was never implemented**, so every tactical→strategic boundary re-materializes a duplicate. Effective latency of a "queued" steer: unbounded | urgent: `src/graph.py:3481-3621` (todo wipe `:3572-3575` via `src/managers/todo.py:787-789`, false banner `:3560-3569`); orphan: `orchestrator/main.py:11704-11725` (no stop), `src/api/dual_app.py:166-201` vs `src/api/app.py:79-121`; queued: drain `src/graph.py:3040-3046` → `:2915-2985`, phantom contract `:2946-2948`, wrong tag `config/templates/strategic_todos_transition.yaml` vs `src/core/phase.py:1060-1061` | Orphan **fixed** `3cda6f09` (P0-D); steer-as-rollback + dead-letter queue still open — **P1-A**; full trace annex B |
| F5 | Verifier #2 **complied and was executed anyway**: wrote `output/baseline_verification.md` at step 61 (~13 min in), archiver pushed it to Gitea (present, 3.2 KB scaffold, branch `job/e239ef27`); officer's three reads said "not found"; his two urgent steers each cost the worker a full re-plan (plan.md rewrite + restage ≥5 todos + todo-guide re-read), preempting the fill-in work he was demanding | audit `e239ef27` steps 55-61, 284-290, 346 | Evidence — drives F1/F3/F4 |
| F6 | Verifier #1 spent 5.5 h / 1096 steps / 7 phases on structural archaeology and process, sealed at confidence 0.42 with 26/27 todos "done" and **0/7 plan-§1 deliverables**, and — its own words — test suite "INFERRED GREEN-by-construction … **unverified by pytest execution**". It never ran pytest, the one command the truth gate existed for | `7c2685e8` `output/completion.json` (in Gitea) | Evidence — drives F7/F8/P1 |
| F7 | The "honest-floor seal" is a **learned, propagated institution — and quantified**: annex D's sweep finds **7/7 distinct seals since 07-26 at confidence 0.42-0.55**, every one carrying honest-floor/NOT-SHIPPED language; exactly one clean seal (0.85) exists in the sample, from 07-09. Verifier #1 cites confidence-floor precedents from iterations 26/35/37 via pinned RecallStore memories; process-lore ("4-step verify-before-done gate", "two-stage seal") is recited near-every-turn; cross-job pin contamination is visible ("memories [1]/[2]/[5]/[6] are for OTHER jobs"). Crucially, the proximate causes *inside* the seals are mostly **real harness bugs** — stuck shell blocking pytest/ruff (3 seals), the palette lie (3), subagent fan-out failures (2) — which the memory system then generalized into culture | audit `7c2685e8` entries 449, 488-498, 725-744, 955, 1022; annex D §1 sweep | **147 poison rows retired** on dev 07-30 (P0-C); write-path hygiene still open — **P2-A** |
| F8 | Phase machinery forces padding and defers delivery: the `min_todos=5` floor rejected the agent's 2-todo plan ("Too few todos: 2 < 5") — and the yaml key that should tune it is a **dead end** (parsed and threaded to a parameter that is never read; live gate is a constructor default no production site sets — annex C §2); the 4-todo strategic boilerplate (~1,799 tokens) is re-injected tail-anchored on **every LLM turn** of a strategic phase; the strategic system prompt adds a second, ~80%-redundant audit-retrospective ("default assumption is that the tactical phase failed"); the templates contradict the code ("Target 3-7" vs a floor of 5); and report-last is **explicitly instructed** in three layers — initial-plan ordering ("research phases first … then execution phases to produce deliverables"), the todo-guide ("Don't jump straight to producing deliverables"), and critic instructions ("Do not skip steps or reorder them", report = step 5 of 5) with critic verdict tools bound strategic-only, so the deliverable *cannot* be produced during tactical work | live gate `src/managers/todo.py:647` (dead path `src/core/phase.py:348`); dead yaml wiring `src/core/loader.py:2363` → `src/core/phase.py:1086` (param never read); boilerplate `config/templates/strategic_todos_transition.yaml`; audit protocol `config/prompts/strategic.txt:6-32`; ordering `config/templates/strategic_todos_initial.yaml:66-68`, `config/skills/todo-guide/SKILL.md:51`, `config/experts/critic/instructions.md:7` | Open — **P1**; full ledger in annex C |
| F9 | The lying `todo_list_footer` ("only 3 tools exist") was **still live all night**: the fix landed in commit `e4244dfe` at 00:13 UTC but the image bump (`d7027501`, sha-576a15f) only rolled at 09:12 UTC. Overnight palette-confusion reasoning in the audit is the real old footer plus the laundered pinned memories. Retiring those poisoned RecallStore memories is now **unblocked and owed** | timing: `git log e4244dfe d7027501`; background: `memory project_todo_footer_false_tool_surface` | Fix deployed 09:12 UTC; **memories retired 07-30** — see P0-C |
| F10 | Heavy verifier died on a codex-proxy 408 stream-disconnect streak. The July classifier fix **worked as designed** (classified transient, retried 4 backoff cycles on fresh pods, then gave up on the identical-error streak). The officer's own turns errored twice on the same proxy; it answered 200 again by ~09:20 | job `eb0143f8` error text; `docs/done/transient_408_stream_disconnect_misclassified_as_permanent.md` | Infra — **P2** monitor; first live confirmation of the classifier fix |
| F11 | Infra noise demystified (annex D §3-4): the ~120 "offline agents" are **accumulated one-shot registrations** (one pod per dispatch, stale-detector marks each ended pod offline → one event per pod, all night), not concurrent flapping. Real incidents inside the window: the 23h spike was a **deploy storm** (three orchestrator rolls 23:10→00:05 from the evening's own pushes; the officer's agent went dark at 23:12, two minutes after a roll, and its replacement pod **crashed at startup** — asyncpg pool failure, exit 3 — the "item-7" debris pod); the 07h spike was `eb0143f8`'s pause+backoff loop burning 8 fresh pods. VM workspace SSH timeouts are chronic: **~11 jobs killed by workspace reachability in 18 days**, 7 on headscale-mesh IPs, a cluster every 2-3 days; verifier #2's audit even shows it *rationalizing* the SSH failure ("the palette now confirms todos completed") | annex D §3-4; audit `e239ef27` steps 334, 345, 351 | Open — **P2** infra track |
| F12 | **The truth gate is still open — but narrower than feared.** `58027ee7`'s own seal claims "all 8 deliverables SHIPPED, baseline **179 passed / 3 pre-existing failures** (NOT caused by kassenabschluss), ruff clean" — a coherent claim, not the feared 180-vs-179 contradiction. It sealed at 0.45 anyway because `job_complete` **rejected its deliverable paths for lacking the `repo/` prefix** (F14). One clean pytest run on current main settles it | annex D §6; KB note `century-state-on-change-of-command` | Operational follow-up — §5 |
| F13 | **completion.json has no job-scoped provenance**: jobs inherit the parent workspace snapshot, so `output/completion.json` is frequently a *different job's* seal — 9 of 16 sampled jobs served a file timestamped before their own creation (one literally naming another job's branch). Any auditor — officer, critic, human — reading the file without checking its embedded job id reads false history. Verifier #1's cited "precedents" partly rest on this inherited-file substrate | annex D §1 | Open — feeds **P1-C** (manifest must be job-stamped) |
| F14 | **The completion validator misfires inward**: `job_complete` rejected `58027ee7`'s correct deliverable list because paths lacked the `repo/` prefix; the worker sealed at 0.45 with all work done, noting it would "re-call … to lift to 0.55-0.69" — which never happened. A pedantic path check converted a *complete* job into another honest-floor precedent for F7's culture | annex D §6 | Open — **P1** (fix with P1-C's gate design) |

---

## 3. The causal chain, compressed

```
F1+F2+F3 (officer blind)
   → every deliverable check returns "not found"
   → officer steers (F4: steer = context-destroying re-plan)
   → worker loses tactical context, re-plans (F8 makes re-planning expensive)
   → more steps pass with no *visible* file
   → officer steers again / cancels          ← feedback loop, both workers died to it
F6+F7+F8 (bureaucracy + honest-floor lore)
   → the one genuinely delinquent worker (verifier #1) burned its 5.5 h on
     process + archaeology and sealed "honestly" with nothing — a pattern the
     memory system taught it from prior iterations
F10+F11 (infra)
   → the escalation path (heavy slot) and the substrate (VM SSH, heartbeats)
     were simultaneously unreliable, so even correct escalation failed
```

Why the phase model makes results *worse*, not just slower (the standing question from
the system's owner): (a) deliverables are back-loaded — and annex C shows this is
**instructed, not emergent**: three template layers order research-first/report-last,
and critic verdict tools are strategic-only — so any interruption (cap, cancel, steer,
budget) yields zero visible output and reads as failure to every supervisor; (b)
feedback is structurally expensive — a steer buys a re-plan, not a correction, so
supervision *subtracts* progress; (c) the overhead displaces object-level context —
with forced 5-todo phases, ~1,800 tokens of process boilerplate re-injected every
strategic turn, per-phase retrospectives (twice: todo S1 *and* the strategic prompt's
audit protocol), and mandated skill re-reads, compaction evicts task state in favor of
process state; (d) the memory system extracts and pins *process lore*, then propagates
it across jobs — the loop literally teaches its successors how to give up gracefully
(F7). Annex F adds the literature's framing: process artifacts decay out of attention
within a step, consume exactly the constraint-following budget small models lack,
constitute a gameable metric surface (the harness rewarded todo/retrospective
compliance, so that is what it got), and outcome-blind memory extraction turns the
resulting failure trajectories into retrieved precedents. None of that is "harmless
bureaucracy"; it compounds — with direct published evidence for every link.

---

## 4. Fix plan

### P0 — restore the officer's senses (small, surgical, unblock supervised nights)

**P0-A. Gitea-backed file reads for the officer.** *(design settled by annex A)*
Repoint `get_job_workspace_file` (`src/tools/orchestrator/jobs.py:733-736`) at
`GET /api/jobs/{id}/repo/file params={"path": …, "ref": …}` (`main.py:16202`, Gitea-backed,
same `require_job_access` gate, response shape already matches the tool's parsing — no
orchestrator change needed). Add a sibling `list_job_workspace_files` on
`/api/jobs/{id}/repo/contents` so the officer can *browse* instead of guessing one
filename into a bare 404. Prepend head-commit sha+time (via `/repo/commits?limit=1`) to
the tool output so he never reasons on hours-old bytes unknowingly. Update the tool
registry description (`jobs.py:78-89`), the sitrep instruction text
(`orchestrator/services/session_wake.py:594-598`), and the prompt reference
(`src/core/loader.py:3981`): contract is "committed state as of the last phase-boundary
push; pass `ref` for phase tags `{short_id}-phase-{N}-{type}-complete`". Note
`docs/continuous_improvement_loop.md:69` already documents the correct URL shape — the
code drifted from its own spec.
*Acceptance:* officer session on k3d reads a file a live worker pushed at its previous
phase boundary; nonexistent path names branch+ref searched; staleness header present.
> **Shipped `ef3ec62b`** (2026-07-30; agent image `sha-5252d18`, sitrep text rides
> orchestrator `sha-4cf2d01`). As designed: `/repo/file` + optional `ref`, head-commit
> staleness header (explicit refs labeled `[ref '…': …]`), 404s name the ref searched and
> point at the new `list_job_workspace_files`; all five toolset mirrors updated (session
> overrides, persistent defaults, centurion expert config, cockpit group names,
> app-guide reference). Unit-tested against the live route shapes; the officer's first
> real read is the remaining acceptance check — no officer pod survived the deploy roll,
> so his next wake binds the fixed tools automatically.

**P0-B. Kill the dev-relic workspace endpoints (don't fix them).**
Per annex A: `WorkspaceService` has no job dimension at all and its PVC holds only
`uploads/` — delete routes `GET/PUT /api/jobs/{id}/workspace*`
(`main.py:16358,16370,16396`) and the cockpit's three **caller-less** methods
(`api.service.ts:1812-1858`); repoint MCP `get_workspace_file`/`get_workspace_overview`
(`orchestrator/mcp/client.py:1032,1047`) at `/repo/file` + `/repo/contents`; repoint the
`/todos*` routes — which have a **live cockpit consumer** (todo view, empty on k8s since
cutover) — at Gitea reads of `todos.yaml` + `archive/todos_*.md`. Update the authz tests
that patch `workspace_service` (`tests/test_job_access.py`,
`tests/test_sudo_vm_upgrade_decisions.py`). A true live-read (mid-phase, uncommitted)
stays out of scope: it needs a new agent-side file endpoint plus a shell-state-style
proxy and is unreachable for mesh VMs anyway — Gitea-first now, explicit `live=true`
opt-in later if wanted.
> **Shipped `70ca9461`** (+ C2 endpoint snapshot `58e3a667`; orchestrator image
> `sha-4cf2d01`). Routes and the three cockpit methods deleted; MCP tools repointed
> (fixing two legs of `get_job_summary` for free); `/todos*` rewritten Gitea-backed via
> new `orchestrator/services/job_todos.py`, cockpit response shape preserved
> key-for-key with empty-shape degradation when Gitea is down. Live-verified on k3d:
> `/todos` returned 6 parsed phase archives from a real job repo; deleted routes 404.
> Lesson: CI's develop path filter skipped `test-python` on these pushes — the
> endpoint-inventory drift only surfaced in the local full-suite gate.

**P0-C. Retire the poisoned RecallStore memories (now unblocked).**
The footer fix is deployed as of 09:12 UTC. The dev-cluster pinned memories that
launder the old footer ("palette is stale display noise") and the honest-floor/two-stage
seal lore must be superseded or deleted — there is no MCP delete; needs an orchestrator
API or direct DB pass. Sequencing note from the earlier incident stands: fix first
(done), then retire memories, else the rewind loop returns.
> **Executed 2026-07-30 11:40:51 UTC** against `srw-pgvector-0`/`srw_vector` on dev:
> **147 clear-poison rows retired** (141 actively pinned — seal doctrine,
> confidence-floor codification, palette/footer lore in both variants, giving-up
> playbooks; the freshest extracted 05:17 that same morning) via the store's own
> supersede semantics (`valid_to = superseded_at = now, remaining_turns = 0`,
> `superseded_by` NULL), which removes them from pinned injection and every
> hybrid-search channel. Resavio pinned recitations 2504 → 2363; 162 borderline rows
> (real harness mechanics/workarounds) and 166 false positives (real domain
> palette/footer knowledge) deliberately kept. **Reversible**:
> `UPDATE memories SET valid_to = NULL, superseded_at = NULL WHERE superseded_at =
> '2026-07-30 11:40:51.463159+00' AND superseded_by IS NULL`. Residual risk: the
> observer extracts only from live turns, but the lore's second homes (KB notes,
> `plan.md` stop-conditions and verify-before-done skills inside existing repos/vault)
> can seed fresh re-extraction — re-sweep in a few days; P2-A is the durable close.

**P0-D. Stop orphaning pods on out-of-band status changes (live correctness bug).**
Port the `job_status` deny-list from `src/api/app.py:79-121` into the deployed
`dual_app` heartbeat handler (`src/api/dual_app.py:166-201`): today *any* out-of-band
status flip — urgent steer, cockpit pause, manual flip — leaves the incumbent pod
running to completion on a job it no longer owns, burning tokens, **double-writing the
shared Postgres checkpoint thread** with the replacement pod, and blocking single-slot
pools (`get_available_agents` requires `ready`; the orphan stays `working`). Its
eventual completion report is discarded at `orchestrator/main.py:14627-14672`. Small
port, large blast radius; several of last night's "orphans_recovered" flaps were
plausibly self-inflicted by the 05:33 steer through this hole.
> **Shipped `3cda6f09`** (agent image `sha-5252d18`). Faithful port: deny-list
> `failed/cancelled → cancel`, `paused → pause`, fail-open on unknown or missing
> status; rides `_request_stop`, the same cooperative stop `/job/cancel` and
> `/job/pause` set, so teardown and slot release behave exactly like a push cancel.
> Guarded by pod-state + current-job id (nulled before the agent's own completion
> report, so orderly shutdown can't trip it) + stop-flag idempotency.

### P1 — make supervision constructive and delivery incremental

**P1-A. Non-destructive steer.**
A guidance channel that lands in the worker's next LLM turn as injected transient
context — no kill, no compaction, no forced re-plan. Candidate injection sites and
design per §7 annex B (steer mechanics trace). Annex E supplies the industry taxonomy
(LangGraph's `reject`/`enqueue`/`interrupt`/`rollback` ladder — interrupt *keeps all
work done so far* and inserts the input; OpenHands `send_message()` consumed at the
next step boundary; Devin's coordinator "messages child sessions mid-task"): **SRW's
current steer is the `rollback` rung used as the only rung.** Target shape: a
worker-side inbox drained at an iteration boundary into intact context (steer =
enqueue), with the current resume-with-feedback kept as an explicit, separately-named
escalation whose docstring prices it honestly ("destroys in-flight tactical context").
Two distinct verbs, not one — the single-lane conflation is itself a documented
anti-pattern.
Annex B settled the design candidates. **Recommended: Design 1** — a transient
"[SUPERVISOR GUIDANCE]" pair riding `_inject_transient_messages` (`src/graph.py:1114`),
the pipe five injection riders already use: lands on the *very next LLM turn*,
cache-safe (tail-anchored), immune to compaction because it is re-derived per turn,
throttled DB read with timeout, acked by atomically moving entries to
`context.consumed_replies` so the officer can confirm delivery. Fallback/complement:
Design 2 (persist-once HumanMessage appended from the tool-node preamble — the proven
guard-nudge mechanism, exactly-once for free, but cache-invalidating); Design 3
(carry guidance on the heartbeat response like the drain intent — zero hot-path DB
reads, 60s latency). Companion fixes to bundle: drain `queued_replies` in place
(the never-implemented `consumed_reply_threads` contract), parameterize the false
`[FEEDBACK_RESUME]` banner by previous status, fix the steer docstring's "interrupts
immediately" claim, and fix the REVIEW template's nonexistent `phase_N_start` tag name
(real format `{short_id}-phase-{N}-{type}-complete`).

**P1-B. De-bureaucratize the phase loop.** *(concrete plan settled by annexes C+F)*
Design stance from the research (annex F): keep the *recitation* — tail-anchored
goal+todo state each turn is exactly what the plan-persistence literature and Manus-style
practice validate — and delete the *ceremony*. Four changes, ordered by value:
1. **Wire `min_todos` to reality, then set it to 2** (~4 lines). The yaml key is a dead
   end (annex C §2): the live gate is `TodoManager.__init__`'s default 5
   (`src/managers/todo.py:114,647`) and every production construction site omits the
   param (`src/agent.py:2544,2095,2145,2195`). Pass
   `config.phase_settings.min_todos` through, add a `phase_settings:` block to
   `config/worker_base.yaml` (`min_todos: 2`), add the key to `config/schema.json`,
   and correct the false "Fully Active" claim at `docs/config_issues.md:58` plus the
   misleading "used by staging" docstring at `src/core/phase.py:1117-1118`. Research
   basis: decompose-as-needed beats upfront decomposition by 27-33pp (ADaPT); no
   published support for minimum-item floors exists; the floor rejected a *correct*
   2-todo plan and today's templates ("Target 3-7") contradict the code (5).
2. **Kill the per-strategic-phase todo-guide re-read.** Root cause is the hardcoded
   10-entry `_recent_reads` FIFO (`src/tools/context.py:228-229`): any 10 reads evict
   the guide and the `enforce: true` gate re-arms — same mechanism behind verifier #2's
   three "must read plan.md before write_file" rejections. Durable fix: exempt
   instruction-file paths from eviction (safe — the write-authorization path
   `recent_read_matches()` is deliberately separate); cheap alternative: flip
   `enforce: true → false` in `config/worker_base.yaml:196-199`. Either way delete the
   six prose mandates (`strategic_todos_transition.yaml:126`, `_initial:131,155`,
   `_resume:94,119`, gpt_oss/developer/scholar variants). Saves ~1-2 turns +
   ~1-2.3k tokens per strategic phase.
3. **Slim the transition template to ~2 todos (~600 vs 1,799 tokens), zero code.**
   Expert-dir template override is a proven mechanism (`src/core/loader.py:1086-1117`;
   developer/scholar/designer already override `strategic_todos_initial`). Merge
   REVIEW+ADAPT into one todo (the machine already writes the todo archive with
   completed/not-completed sections, tags, commits, and snapshots the phase — the
   `phase_N_retrospective.md` file is enforced by nothing and duplicates both the
   auto-archive and the audit protocol); keep PLAN-OR-COMPLETE's stop condition; fold
   REFLECT into a single conditional kb_write. For verification/report jobs add:
   "scaffold the deliverable file in phase 1; update it every phase boundary" (P1-C
   corollary, attacks the instructed report-last ordering at
   `strategic_todos_initial.yaml:66-68` / `SKILL.md:51` /
   `critic/instructions.md:7`).
4. **A/B separately: delete `<phase_audit_protocol>`** (`config/prompts/strategic.txt:6-32`)
   from the base strategic prompt — the largest single generated-token tax per
   strategic phase, ~80% redundant with the REVIEW todo, built on distrust-by-default
   ("your default assumption is that the tactical phase failed"). Precedented: the
   critic expert already omits it entirely. Behavior change → own experiment, not a
   rider.
Cheap tripwire to ship alongside (annex F principle 8): an act-ratio guard —
N consecutive steps touching only todos.yaml/plan.md/retrospectives injects "stop
planning; take the next concrete action". The overthinking literature: low-act-ratio
trajectories score ~30% worse at 43% more compute; verifier #1 would have flagged
within its first hour.

**P1-C. Deliverable contract — declared at dispatch, visible mid-run, validated at the seal.**
Jobs carry a `required_deliverables` manifest (paths or KB note slugs); the completion
path (`orchestrator/services/completion.py`) refuses `completed`/`pending_review` seals
that claim done-ness while required artifacts are absent — the check the officer ran by
hand (blind) becomes a platform gate that cannot be argued with by narrative. Verifier
#1's seal ("26/27 todos done, 0/7 deliverables") would have bounced at the platform with
a precise reason instead of consuming an officer review cycle.
Annex E refinements, each proven elsewhere: (1) **declare the manifest in the dispatch
request** (CrewAI makes `expected_output` *required at task definition*; Anthropic's
dispatch contract: objective, output format, tool guidance, boundaries) — workers can't
be held to a floor they were never shown, and the officer/sitrep can diff progress
against the declared contract mid-run; (2) **on gate failure, bounce the precise reason
back into the same live context** (OpenHands critic auto-follow-ups in the same
conversation; CrewAI guardrail re-prompts with the error, `max_retries=3`; Claude Code
`Stop`/`TaskCompleted` hooks return block+reason) — a rejected seal becomes
resume-on-same-checkpoint with the validator's reason, not a new blind job; (3) validate
**end-state artifacts, never the worker's narrative** (cheap deterministic existence/
schema checks first, critic LLM second); (4) phase-boundary pushes should carry the
manifest delta (artifact path, type, hash) so supervision reads references, not prose —
the "game of telephone" anti-pattern is funneling results through conversational
returns.
*Corollary:* report-type jobs should scaffold the deliverable in phase 1 and commit it
every phase boundary (verifier #2's behavior — which was correct — becomes the template).

**P1-D. Push on cancel/drain (evidence preservation).**
Annex A found there is **no `git push` anywhere in the cancel path** — per-todo commits
are local-only, so cancelling a job destroys everything since its last phase-boundary
push (and VM reap then erases it permanently). Tonight that means whatever verifier #2
filled into its scaffold mid-phase-2 is gone forever; the officer's cancel was also an
evidence shredder. Add a best-effort final commit+push to the cancel/drain path
(`src/api/dual_app.py:827` `/job/cancel` lineage, reusing the `_complete_phase_with_git`
plumbing at `src/core/phase.py:1035-1077`). A supervisor's kill switch must never
destroy the evidence he kills for.

### P2 — hygiene and substrate

**P2-A. Memory hygiene — gate writes on verified outcomes.** Stop cross-job pinning of
process-meta lore (seal patterns, gate recitations, confidence-floor precedents); scope
such extractions to their originating job or exclude them from observer extraction
entirely. The observer currently launders harness bugs into durable "wisdom" (F7, and
the footer sequel bug before it). Annex F upgrades this from taste to mechanism: the
experience-following literature names our exact failure — *error propagation* (bad past
outputs retrieved, replicated, re-stored) — and finds "strict selective addition"
(store only outcome-verified experiences, using task outcomes as free quality labels)
consistently outperforms. Concretely: (a) admit observer extractions into cross-job
scope only from jobs whose completion passed the P1-C deliverable gate; (b) purge the
existing honest-floor / seal-pattern / palette-lore rows from the dev RecallStore now
(P0-C); (c) never auto-pin procedural memories into every turn.

**P2-B. Infra track.** Quantified in annex D; fix separately: VM workspace SSH flaps on
the headscale mesh (~11 dead jobs in 18 days, a cluster every 2-3 days — the single
biggest *real* infra killer); one-shot agent registrations flooding the offline count
and the officer's sitreps with meaningless `agents_offline` events (suppress or
aggregate per-pod-lifecycle events); codex-proxy 408→`auth_unavailable` streaks under
load. Also: **deploys roll agents mid-flight** — the officer went dark two minutes
after the 23:10 orchestrator roll and his replacement crashed on a mid-roll asyncpg
pool failure. Anthropic's production answer is rainbow deployments (in-flight agents
finish on the old version); at minimum, sitreps should tag deploy windows so the
officer doesn't read roll-induced churn as fleet sickness.

---

## 5. Immediate operational follow-ups (not code)

1. **Answer the truth gate** (F12): run the full suite on current main of the Resavio
   repo once, with no deselection, and hand the officer the result — his standing orders
   have all construction blocked pending exactly this fact. Expected per `58027ee7`'s
   own claim: 179 pass / 3 pre-existing failures.
2. **Unblocked 07-30**: P0-A is deployed, and no officer pod survived the roll, so his
   next wake binds the fixed reader + lister automatically. He can stand down from the
   3-strike alert; his truth-gate order stays until item 1 is answered.
3. **Clear the parking lot** (annex D §2): `4435994d` + `2dbe6854` are pending_review
   because their critic subjobs died on "parent workspace vm=deleted, subjob cannot
   inherit" (known issue class — reviewed parents' VMs get reaped before automated
   verification can run); `1cab4b88` (the footer-incident job) has sat unreviewed since
   07-16; `35b23256` has been paused on the pre-fix 408 since 07-15 and will never be
   re-picked on its own.
4. **Done 07-30**: item-7 debris pod (`persistent-d67ee261-334`, Error since 23:57 —
   startup crash during the deploy storm, asyncpg pool failure) deleted.

---

## 6. The bigger question — strategic/tactical revisit

The phase-alternation loop was designed ~12 months ago to keep smaller models on task
across 40-hour jobs. The owner's standing position (recorded 2026-07-16, footer
incident): *"200 was for dumber models; real future work = longer tactical phases, less
strategic overhead; user will revisit phase structure."* This postmortem is the evidence
base for that revisit. The failure modes found tonight are specific and fixable within
the current architecture (P0/P1 above) — but F5/F6/F8 together say the machinery's
*costs* now land on exactly the models it was built to protect, and its *protections*
(footer, floors, gates, path-prefix validation) have repeatedly misfired inward (see
companion doc's unifying theme).

The research annexes sharpen the direction without settling it:

- **What the machinery got right, per the literature**: keeping the plan/todos *visible
  every turn* (plan signal decays 4.1× within one step of leaving context — the
  tail-anchored recitation is the part to keep), and **compaction at boundaries**
  (`compact_on_archive` matches what every production long-horizon system converged on).
  A phase boundary should cost one summarization call — not a ritual.
- **What it got wrong**: hard gates and ceremony are compliance load, and
  constraint-following collapses fastest in small models (~5 simultaneous constraints);
  decompose-on-failure beats upfront decomposition by 27-33pp; forced format strictness
  measurably degrades reasoning; and every process artifact is a gameable surface —
  agents demonstrably optimize the surface they are scored on (F6 *is* reward hacking
  against the harness's own metrics).
- **The tiering answer** (annex F principle 4): scaffold depth should be a
  **per-model-family config** — the `model_config_matrix` plumbing already exists.
  Frontier-class workers get goal + tools + soft todo recitation + compaction;
  small-model tiers keep training wheels as an explicit override, re-benchmarked
  (evidence suggests structure didn't rescue weak models anyway — they show 40-60%
  error consistency regardless). The METR horizon data (autonomous-task length doubling
  ~every 7 months; frontier models now ~12 h) says the original "40 hours without
  wandering off" problem is increasingly solved *by the model*; the harness's job is
  context hygiene, not process enforcement.
- **The honest caveat**: nobody has published an ablation of exactly this (process
  gates × model size × task length); mini-swe-agent's bash-only 74% is frontier-only.
  The tiered retreat — not wholesale deletion — is the defensible move while MiniMax-class
  line workers remain in the fleet.

Sequencing stands — first step complete: **P0 shipped 2026-07-30**, so the next
supervised night produces clean evidence instead of artifacts of blindness. Next: run
P1-B's config-first cuts as measurable experiments; decide the alternation question on
that data.

---

## 7. Research annexes

Gathered 2026-07-30 by six parallel investigation agents (three code-tracing, one
live-cluster, two web-research); condensed here, load-bearing facts only:

### Annex A — read-path landscape (landed 2026-07-30)

Full survey of every way a worker job's files can be read — the load-bearing facts
(every claim carries its file:line; re-derivable from those sites):

**Working on k8s/VM (all Gitea-backed, staleness = last push):**
`GET /api/jobs/{id}/repo/file|contents|commits|diff|tags` (`main.py:16169-16354` →
`services/gitea.py`), the job `/diff` cloud-diff routes, `GET /api/jobs/{id}/frozen`
(DB-first), and the human path `ensure-workspace-access` + Gitea web UI. The MCP tools
`get_job_file`/`list_job_files`/`list_job_commits`/`get_job_diff` all ride these and
work. The IDE proxy (`main.py:15678`) is the only *live* read and serves
HTML/WebSocket to pods only (mesh VMs unreachable from the orchestrator,
`main.py:15726-15730`).

**Broken on k8s (all `WorkspaceService` local-disk):** `GET/PUT
/api/jobs/{id}/workspace*`, `/api/jobs/{id}/todos*` (live cockpit todo view — empty
since cutover), the primary tier of `GET /api/jobs/{id}/logs` (`main.py:26638` — why
`get_job_log` reads empty; the S3-archive fallback works), MCP
`get_workspace_file`/`get_workspace_overview` (URL built correctly, backend dead) and
2 of 5 legs of MCP `get_job_summary`. The officer tool is the only caller that *also*
gets the URL shape wrong (query param vs `{path:path}`). Cockpit's three
workspace-file methods have zero callers — dead code.

**Agent side has no file endpoint at all** (`src/api/app.py`/`dual_app.py`: health,
job control, system-info, shell-state only) — the agent holds the sole live handle
(`WorkspaceManager`, SSH/SFTP) but doesn't expose it. The existing proxy precedent for
a future live read is `GET /api/jobs/{id}/shell-state` → `http://{pod_ip}:{port}/...`
(`main.py:26767-26799`).

**Push cadence (git_manager.py driven by src/core/phase.py — `src/core/archiver.py` is
the audit archiver, no git):** pushes happen at phase-0 seed (`src/agent.py:3079-3099`),
**every** phase boundary (`src/core/phase.py:1035-1077`: tag
`{short_id}-phase-{N}-{type}-complete` + commit + push), freeze-for-review, job
finalize (completed and frozen, tagged), and critic-verdict write. Per-todo completion
commits (`src/managers/todo.py:519`) and message-send commits are **local-only**.
**No push exists in the cancel path** → P1-D. Worst-case Gitea staleness for a live
job = one full tactical phase (hours); for a cancelled job, everything since the last
boundary is lost.
### Annex B — steer mechanics trace (landed 2026-07-30)

**Urgent steer lifecycle** (12 hops, officer tool → worker graph): tool
(`src/tools/orchestrator/jobs.py:860`) → `POST /api/jobs/{id}/messages/officer/reply` →
`_route_inbound_reply` → urgent arm → `_internal_resume_job`
(`orchestrator/main.py:11704-11725`) — which **only** merges `queued_feedback` and flips
the row to `paused`/unassigned in one UPDATE (`postgres.py:5619-5667`); **no stop
signal is sent to the pod**. Dispatcher later claims the paused row for a *different*
agent and POSTs `/job/resume`; the new pod resumes the old pod's mid-tactical
checkpoint and routes to `restore_from_feedback` (`src/graph.py:3481-3621`), which:
force-compacts (`:3494-3500`, originals dropped from the checkpoint via
`RemoveMessage`), **wipes in-flight tactical todos without archiving**
(`:3572-3575` → `todo.py:787-789`; `archive_phase` bypassed on this route), flips to
strategic (`:3576`), and injects the factually false "[FEEDBACK_RESUME] … previously
frozen for human review" (`:3560-3569`). `strategic_todos_resume.yaml` then mandates
"REWRITE plan.md — the old plan is no longer relevant" + guide re-read + ≥5 fresh
todos — the observed re-plan is template-fixed, not model whim.

**The orphan pod**: the deployed `dual_app` heartbeat handler
(`src/api/dual_app.py:166-201`) reads only `intents.should_drain` and ignores the
`job_status` the orchestrator already returns (`main.py:21832-21843`); the deny-list
backstop exists only in the non-deployed `app.py:79-121`. The incumbent pod runs to
natural END on a job it no longer owns — **double-writing the shared Postgres
checkpoint thread** (`thread_id=job_id`) with its replacement — and its completion
report is discarded (`main.py:14627-14672`). With a single-slot pool the job sits
paused (pool wants `ready`; the orphan is `working`) until the orphan dies: the steer
delivers *neither* behavior in the interim. → P0-D.

**Queued steer lifecycle**: `append_queued_reply` → single drain site
(`src/graph.py:3040-3046`, tactical→strategic boundary only) → writes
`messages/{thread}/{seq}_received.md` — **never injected into LLM context, no read
tool exists, and the clearing contract (`consumed_reply_threads`) appears nowhere but
a comment** (`:2946-2948`), so each boundary re-materializes a duplicate. The one
incidental delivery path (REVIEW todo's `git_diff`) cites tag `phase_N_start`, which
is never created (real: `{short_id}-phase-{N}-{type}-complete`). Effective latency:
unbounded.

**Existing non-destructive injection precedents** (the pipe P1-A rides):
`_inject_transient_messages` (`src/graph.py:1114-1198`) already carries six per-turn
riders (memory, knowledge, citation-feedback, instruction files, todos-last) —
cache-safely tail-anchored; tool-node message appends (`:4220-4243` progress nudge)
prove the persist-once alternative; the heartbeat→module-global→graph channel
(drain intent, `:2988-3002`) proves the push alternative. Recommended P1-A shape =
Design 1 (transient supervisor pair, next-turn latency, compaction-immune) with
Design 2 (persist-once HumanMessage) as the durable fallback; Design 3 (heartbeat
carry) if hot-path DB reads are unacceptable. Confirmed: the worker graph makes
exactly one DB read per run today and zero mid-job orchestrator calls — there is no
existing poll to piggyback on.

### Annex C — phase-overhead ledger (landed 2026-07-30)

**Corrections**: `config/defaults.yaml` no longer exists — the live base is
`config/worker_base.yaml` (cap at `:233`). And **`min_todos` is not tunable today**:
the yaml path (`phase_settings.min_todos` → loader `:2363` → dataclass `:1564` →
`graph.py:4576` → `phase.py:1086`) dead-ends in a parameter that is **never read** —
the live gate is `TodoManager.__init__`'s default 5 (`src/managers/todo.py:114,647`)
and every production construction site (`src/agent.py:2544,2095,2145,2195`) omits it.
`docs/config_issues.md:58` ("Fully Active") is wrong. The only enforcement consumer of
the threaded parameter is legacy `validate_todos_yaml` with zero production callers.

**Strategic-phase cost**: 4 boilerplate todos
(`config/templates/strategic_todos_transition.yaml`) ≈ **1,799 tokens re-injected
tail-anchored on every strategic turn** (deliberately outside the cached prefix) —
~14-27k tokens of re-processing per strategic phase; the strategic prompt adds
`<phase_audit_protocol>` (`config/prompts/strategic.txt:6-32`), a second ~80%-redundant
retrospective built on "your default assumption is that the tactical phase failed"
(critic's expert override already omits it — precedent for deletion). Honest floor for
one compliant strategic phase: **~8 main-model turns, 15-19 tool calls**, plus a forced
summarizer call, 1-3 async auxiliary calls, and a git push. A 7-phase job spends
~32-60 turns on bookkeeping before any deliverable work — consistent with the observed
1096-step zero-deliverable night.

**The guide re-read root cause**: `_recent_reads` is a hardcoded 10-entry FIFO
(`src/tools/context.py:228-229`); any 10 reads evict `skills/todo-guide/SKILL.md`
(~960 tok, ~2.3k with its reference file) and the `enforce: true` gate
(`config/worker_base.yaml:196-199` → `src/tools/registry.py:828-900`) re-arms — hence
once-per-strategic-phase re-reads forever, and the same FIFO drove verifier #2's three
"must read plan.md before write_file" rejections. Six prose sites additionally mandate
the read in todo text.

**Report-last is instructed**: `strategic_todos_initial.yaml:66-68` ("research phases
first … then execution phases to produce deliverables"), `SKILL.md:51` ("Don't jump
straight to producing deliverables"), `critic/instructions.md:7` ("Do not skip steps or
reorder them" — report is step 5 of 5), and critic verdict tools are strategic-only
(`critic/strategic.txt:4`). Verification jobs inherit **all** worker_base overhead
(critic overrides prompts but no todo templates). Also: the template says "Target 3-7"
while the code floor is 5 — a compliant 3-todo plan is rejected.

**Tactical-phase enders and todo fate**: normal completion archives cleanly; the
tactical hard cap (>500) and the `todo_rewind` tool both run
`archive_with_failure_note` (`todo.py:965-998`) which archives-and-clears completed
*and* pending alike; the strategic hard cap freezes the job; the empty-todos branch
(`graph.py:2551-2560`) — labelled a resume-bug workaround — is what actually rescues
rewinds; both rewind paths still tell the agent to call the strategic-only
`next_phase_todos` from tactical (companion doc #4, still live).
### Annex D — cluster evidence (landed 2026-07-30)

**Honest-floor sweep** (16 jobs sampled, 11 distinct seal artifacts after provenance
correction): since 07-26, **7/7 seals at 0.42-0.55**, all with honest-floor /
NOT-SHIPPED / precedent-chain language; one clean 0.85 seal (07-09) in the entire
sample. Proximate causes inside the seals: stuck shell blocking pytest/ruff (3),
palette lie (3), subagent fan-out failures (2). **Methodological trap → F13**: 9/16
jobs served a `completion.json` predating their own creation (inherited from the
parent workspace snapshot; one names another job's branch outright) — completion
artifacts have no job-scoped provenance.

**Backlog** (§5.3): `4435994d` and `2dbe6854` parked in pending_review because critic
subjobs die on "parent workspace vm=deleted, subjob cannot inherit"; `1cab4b88`
unreviewed since 07-16; `35b23256` paused on a pre-fix 408 since 07-15, never
re-picked.

**"Flapping" decomposed**: registry = 118 rows (116 offline, 1 ready, 1 session) —
accumulated one-shot registrations, one `agents_offline` event per ended pod. 32
agents went dark in the incident window; the 23h spike (10) was the **deploy storm**
(orchestrator rolls 23:10/23:43/00:05; officer's agent died at 23:12, its replacement
`persistent-d67ee261-334` crashed at startup 23:57 — asyncpg pool failure, exit 3 —
and is the item-7 debris, still in Error); the 07h spike (8) was `eb0143f8`'s
pause+backoff burning a fresh pod per cycle. Zero restarts on running pods now.

**VM SSH**: ~11 jobs killed by workspace reachability in 18 days, 7 on
100.64.x mesh IPs (07-09 ×2, 07-12 ×2, 07-15 ×2, 07-30), plus DNS/kex/wedge variants —
a cluster every 2-3 days. Verifier #2's audit entry [345] shows the agent
*rationalizing* the SSH timeout ("the palette now confirms todos completed").

**Image timing confirmed**: last pre-fix orchestrator image `sha-711bf7a` went live
00:05 and served all night; first fixed image `sha-576a15f` at 09:14. Overnight agent
pods ran `sha-c03c50b`. The footer fix (e4244dfe, 00:13) was in **no overnight image**.

**`58027ee7` ground truth**: its own seal claims all 8 deliverables shipped, "179
passed / 3 pre-existing failures (NOT caused by kassenabschluss)", ruff clean —
sealed 0.45 anyway on the `repo/`-prefix validation rejection (→ F14), with a
never-executed plan to re-seal higher. `eb0143f8`'s death ladder: 408 stream
disconnect → 6 in-process attempts → freeze for pause+backoff → "503
auth_unavailable: no auth available (providers=codex)" → 4 cycles on fresh pods →
give-up. codex-proxy logged only 6 stream-error lines all night — proxy-side logging
under-reports agent-side experience.

**Lost evidence**: overnight orchestrator logs (pods replaced 09:14, no aggregation),
k8s events (~1h TTL), `e239ef27`'s S3 log archive (missing), `7c2685e8`'s frozen data
(cleared by cancellation).
### Annex E — framework survey (landed 2026-07-30)

How mature systems solve the three gaps — proof points, decisive URLs inline; the rest
findable under the named product doc pages (LangGraph interrupts /
interrupt-concurrent; OpenHands sdk guides convo-send-message-while-running, critic,
agent-stuck-detector; CrewAI concepts/tasks; Claude Code agent-sdk subagents +
hooks; MetaGPT arXiv 2308.00352; SWE-agent arXiv 2405.15793; Anthropic
built-multi-agent-research-system; Cognition dont-build-multi-agents +
devin-can-now-manage-devins):

- **Observability**: OpenHands makes the typed event stream *the* state — UI, stuck
  detector, and supervisor are all just subscribers; Claude Code emits task-state
  changes as tool-use events in the message stream; Anthropic's multi-agent system uses
  **artifact stores + lightweight references** ("subagents store work in external
  systems, pass references back") explicitly to kill the game-of-telephone; MetaGPT
  communicates via schema-validated documents in a publish-subscribe pool, not
  dialogue; Devin's coordinator can read full child trajectories. LangGraph gets
  supervisor reads for free from checkpointing.
- **Steering**: LangGraph names the ladder — `reject` / `enqueue` / `interrupt`
  (**keeps all work done so far**, inserts the input, continues) / `rollback` (deletes
  the run) — and treats preserve-vs-destroy as an explicit per-request choice
  (docs.langchain.com/langsmith/interrupt-concurrent). OpenHands
  `Conversation.send_message()` appends mid-run, consumed at the next `Agent.step()`
  boundary — "dynamic guidance" with zero context loss. Devin: "message child sessions:
  send instructions, context, or corrections to any managed Devin **mid-task**", plus
  sleep/terminate and per-child budget monitoring. AutoGen explicitly warns *against*
  blocking a running team for input (unsaveable state) — pause at a boundary with
  persisted state instead. Claude Code ships two lanes (queue at turn boundary; Esc
  keeps completed work) and its issue tracker shows users demanding both lanes stay
  distinct.
- **Deliverable contracts**: CrewAI requires `expected_output` per task, validates
  `output_pydantic`/`output_json`, and re-prompts the same agent with the guardrail
  error up to 3 times. OpenHands runs a critic on every `FinishAction` (score <
  threshold → auto-follow-up in the same conversation; "finish is a request, not a
  verdict"). Claude Code `Stop`/`TaskCompleted` hooks return block+reason and the agent
  must continue. OpenAI Agents SDK loops until `output_type` schema-matches; output
  guardrail tripwires halt. Anthropic: judge **end-state artifacts, not narratives**;
  dispatch contract = objective + output format + tool guidance + boundaries.
- **Anti-patterns named by practitioners**: restart-as-steering (LangGraph `rollback`
  positioned as last resort — exactly our current steer); fire-and-forget fan-out where
  the supervisor can't reach running workers (Anthropic self-critique; Devin's
  Manage-Devins is the corrective); trusting worker narrative at completion (our
  honest-floor is the textbook case); single steering lane; funneling results through
  conversational returns; non-idempotent resume paths (LangGraph re-runs the
  interrupted node's pre-interrupt code — whatever boundary SRW resumes from must be
  replay-safe); context-split parallel agents making conflicting implicit decisions
  (Cognition "Don't Build Multi-Agents" — share full traces; their pro-worker-isolation
  counterpart: children get "a clean slate, a narrow focus" *with* mid-task reach).

Direct mappings adopted into the plan: steer ladder → P1-A; dispatch-declared manifest +
same-context bounce + end-state validation → P1-C; artifact references over prose →
P1-C(4); the officer's existing sitrep delta already implements "subscribe, don't poll"
for *status* — what's missing is only the artifact-manifest leg.
### Annex F — research evidence (landed 2026-07-30)

Key findings per question (primary sources named inline — arXiv IDs and paper/post
titles are sufficient to re-locate everything):

- **Planning overhead**: plan signal decays 4.1× within one action-observation step;
  evicting plans during compression costs −34.7pp ("Plans Don't Persist",
  arXiv 2606.22953) — plans work only while literally visible, which validates the
  tail recitation and damns the ceremony. "Plan-execution misalignment" is systematic:
  execution ignores collected plans/evidence in 44-47% of failure cases (PIVOT).
  Dynamic decomposition beats static upfront (TDAG); decompose-on-failure beats both
  ReAct and plan-and-execute by 27-33pp (ADaPT). The overthinking literature
  (arXiv 2502.08235, 4,018 SWE-bench trajectories) names our three observed shapes —
  Analysis Paralysis, Rogue Actions, Premature Disengagement — and low-overthinking
  trajectory selection gains ~30% at 43% less compute.
- **Scaffold tax**: SWE-agent's ACI result (interface design dominates) and its own
  lab's sequel **mini-swe-agent** — ~100 lines, bash-only, linear history, >74%
  SWE-bench Verified — the strongest datapoint that elaborate scaffolds are no longer
  load-bearing at the frontier. Anthropic engineering: "give as much control as
  possible to the model"; "keep the scaffolding minimal"; workflows-vs-agents.
  Hyung Won Chung: structure added for yesterday's compute becomes today's
  bottleneck. Practitioner consensus: the harness is a ~90-day depreciating asset.
- **Checklists/decomposition**: format restrictions measurably degrade reasoning
  ("Let Me Speak Freely", EMNLP 2024); optimal granularity is task-dependent and
  overly long plans are named overhead; **no published support exists for
  minimum-item floors** — they appear only in prompt templates, never in evaluated
  research. The pro-todo evidence that exists (Manus recitation, Claude Code
  TodoWrite) is uniformly *soft*: agent-authored, no minimums, no gates.
- **Gaming/giving-up**: METR measured frontier models reward-hacking 30-100% of runs
  with anti-cheating instructions "nearly negligible"; a ten-line conftest.py
  "solves" all of SWE-bench Verified (artifact-checking validators are trivially
  satisfiable); false success claims constitute 45-75% of failures in measured agent
  domains ("completion theater" is a named, measured phenomenon). F6 is this: the
  harness scored todo/retrospective compliance, so that is what it got.
- **Memory contamination**: experience-following behavior (arXiv 2505.16067) names
  our exact mechanism — *error propagation*: low-quality past outputs are retrieved,
  replicated, amplified, and re-stored; "strict selective addition" (outcome-verified
  writes only) consistently outperforms; AgentPoison/MemoryGraft prove a handful of
  retrieved records steer behavior. The honest-floor precedent chain is organic
  self-poisoning.
- **Long-horizon SOTA without phase machinery**: every production system (Claude
  Code, Codex, OpenHands, Manus, Devin) converged on threshold compaction + durable
  files + agent-authored notes + sub-agents; OpenHands measured ~2× cost reduction
  from compaction with no performance loss; Codex bug reports corroborate
  plans-don't-persist at production scale (compaction dropping rules sent progress
  "from 97% back to 42%"). METR: autonomous-task horizons double ~every 7 months
  (frontier now ~12 h) — the "40 hours without wandering off" problem is increasingly
  solved by the model; context hygiene is the binding constraint.
- **Where evidence is thin**: no direct ablation of min-N todo mandates (this
  postmortem is arguably better evidence than anything published); small models are
  two-sided (structure helps short tasks, drowns long ones; no clean
  gates × size × length study); mini-swe-agent's result is frontier-only —
  supporting **tiered scaffolding, not wholesale deletion**, while small workers
  remain in the fleet.

The synthesis (8 principles) is folded into §4 and §6: recitation over ceremony;
drop decomposition minimums; constraint-count budget for small models; scaffold as
per-tier depreciating asset; score environment-verified outcomes only; gate memory
writes on verified outcomes; keep boundaries as compaction events only; instrument
the act-ratio.
