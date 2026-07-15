# Phase guardrails burn legitimate work — findings index (2026-07-15)

**What this is:** the pickup index for one connected arc found while checking scholar job
`1cab4b88-4381-4202-9194-fac868a6f6a1` ("Research phase for: Design the UI theme and complete
mockup suite for Hotel Rheinland ERP" — gpt-5.6-sol/terra, main cluster). The job was *healthy* —
iteration 490, zero audit errors, active at time of inspection — and had still burned 9.4 hours
and 516 LLM requests to produce 4 of 13 deliverables. Every finding below is verified against
code or cluster state; each names its exact site.

**Status:** as of 2026-07-15 all findings are **open / unbuilt** except as noted below. Uncommitted on `develop`.

**Changed so far (2026-07-15):** `max_tool_calls_per_phase` raised **200 → 500**
(`config/defaults.yaml:234`, plus the dataclass default and both `.get()` fallbacks in
`src/core/loader.py:1525,2265,2498`). Stale comments on the two lines that read these values fixed
(`src/graph.py:4031-4032`: `15`→`30`, `100`→`500`). Verified: `tests/test_stuck_detection.py`
29 passed, config/loader suites 735 passed, ruff clean. The stuck-detection tests inject their own
caps (10/5/2) so they are insulated from the default.

**Accepted trade-off, recorded deliberately:** the split in #7 was considered and declined in favour
of the simple bump. **The strategic freeze threshold is now also 500.** Strategic phases are 3-4
bookkeeping todos and should never approach it, so a strategic phase that runs away now burns 500
calls before `budget_exceeded` fires instead of 200. #7 stays open and is *more* load-bearing now,
not less. The bump does nothing for #10 (evidence re-fetching), which is the underlying burn.

**Unifying theme — the guardrails fire on good work and miss the runaway.** Every protective
mechanism in the phase loop misfired *inward*: a delegation timeout too short for delegation, a
phase cap that destroys the todos it interrupts, a rewind that tells the agent to call a tool it
cannot call. Meanwhile the one guard designed to stop an actual runaway — the job-level tool-call
cap — was never implemented, so a job with no valid brief ran unbounded for 9.4h. The agent
absorbed all of it and kept going, which is why nothing looked broken.

## Findings

| # | Finding | Site | Status |
|---|---------|------|--------|
| 1 | **`delegation` tool timeout is 120s** — every non-trivial `spawn_subagent` fan-out dies. Killed Phase 1 outright (0/6 todos, two separate three-reader batches). `shell` gets `900`; the graph's own batch ceiling `_TOOL_BATCH_TIMEOUT_SECONDS` is `900`. A fan-out that spawns readers doing real work cannot finish in 120s. | `config/defaults.yaml:239` (`tool_category_timeouts.delegation: 120`); ceiling at `src/graph.py:4035` | Open — **one-line config fix** |
| 2 | **`max_tool_calls_per_job` is not implemented.** Design specifies `2000` to "catch job-level runaways"; grep finds the key nowhere in `src/` or `config/`. No job-level bound exists. Job `1cab4b88` ran 9.4h / 490 iterations / ~2000+ tool calls with nothing able to stop it. | designed at `docs/features/stuck_agent_recovery.md:248`; absent from `src/`, `config/` | Open — **design/impl gap** |
| 3 | **Rewind destroys incomplete todos.** `archive_with_failure_note` archives *all* of `self._todos` and clears the list — completed and pending alike. Phase 2.1c reached the cap at 3/5; todos 4 and 5 (write the evidence packet, verify it — i.e. the actual deliverable) were discarded, forcing a from-scratch re-plan. | `src/managers/todo.py:965-998`, called from `src/tools/core/todo.py:347` and `src/graph.py:4243` | Open |
| 4 | **Both rewind paths instruct the agent to call a phase-gated tool.** `todo_rewind` is `phases: ["tactical"]`; `next_phase_todos` is `phases: ["strategic"]`. Yet the rewind's return message, its docstring, *and* the `budget_rewind` nudge all tell the agent to "create new todos with `next_phase_todos()`" — from the tactical phase where a rewind necessarily fires. The tool is not bound there. | metadata `src/tools/core/todo.py:28-54`; return msg `:357-363`; docstring `:329-332`; nudge `config/guardrails/default.yaml:242-248` | Open |
| 5 | **Rewind recovery relies on a path labelled as a resume-bug workaround.** The agent is only rescued from #4 because `check_todos` sees an empty todo list in a tactical phase and force-completes the phase — a branch whose comment describes it as recovery for "can happen after resume if todo state wasn't persisted". The designed rewind path does not work; an unrelated fallback catches it. | `src/graph.py:2860-2868` | Open |
| 6 | **`todo_rewind` has no evidence gate.** It takes the agent's word. Its stated triggers — "a dead end that makes the current tasks impossible", "an external constraint makes the current plan invalid" — are *exactly* what a false "my tools are unavailable" belief looks like from the inside. Three phases died this way (1.5b at 1/5, Phase 2 at 0/5, Phase 2.1 at 0/5), each after the agent read tool-palette prose as proof of an outage. Nothing requires it to have invoked the tool and captured a real error first. Plausibly *fed by* #4 — the agent is literally told to call a tool that isn't there. | `src/tools/core/todo.py:311-341` | Open — **largest single loss (3 phases)** |
| 7 | **One scalar governs two different failure modes.** `max_tool_calls_per_phase` drives *both* the strategic freeze and the tactical rewind. Raising it to give research phases headroom also raises the strategic runaway threshold by the same factor — but strategic phases are 3-4 bookkeeping todos and should never approach it. | `src/graph.py:4195-4231` (strategic freeze) vs `:4232-4278` (tactical rewind) | **Open — now at 500 for both.** Split declined 2026-07-15 in favour of the simple bump; see "Accepted trade-off" above. More urgent post-bump, not less |
| 8 | **Implementation diverges from design on cap behaviour.** Design: "When a hard cap is hit: freeze the job immediately with a `budget_exceeded` reason... Don't warn." Code freezes only in strategic phases; tactical phases rewind instead (see #3). Neither doc nor code comment records the change. | design `docs/features/stuck_agent_recovery.md:250` vs `src/graph.py:4232` | Open |
| 9 | **Stale constants in docs and comments.** Design table and the code comment both said the phase cap is `100` (real default was `200`, now `500`); the `progress_stall_threshold` comment said `15` (real default `30`). Anyone calibrating from either source starts from wrong numbers. | `src/graph.py:4031-4032`; `docs/features/stuck_agent_recovery.md:246,248`; actual at `config/defaults.yaml:233-234` | **Partially fixed** — `graph.py` comments corrected 2026-07-15. `stuck_agent_recovery.md` still says `100` and still documents the unimplemented `max_tool_calls_per_job: 2000` (#2) |
| 10 | **No "I already have this evidence" notion.** The agent re-fetches citations it registered itself. Phase 19, live at inspection: ~55 tool calls across 11 iterations with **0 of 5 todos complete** — `get_citation` ×24, `file_exists` ×8, `search_files` ×6, `read_file` ×12 — against a plan that explicitly budgeted 5-15 calls total using only files already on disk. This is the real efficiency defect; the phase cap is its smoke alarm, not its cause. | behavioural; no single site | Open — **raising the cap does not fix this** |
| 11 | **No brief fail-fast.** `task_brief.md` was a **119-byte stub** echoing the job description; `instructions.md` was generic default boilerplate. Nothing checks that a real brief arrived. The agent self-generated a 27.8 KB `plan.md` with 13 deliverables on top of one sentence and worked 9.4h against it. | job workspace; dispatch path | Open |
| 12 | **Cancelled parent does not stop child work.** Parent designer job `73e68890-9a76-4741-8ed2-75ca17abb6b6` was **cancelled 2026-07-13** (`SSH command failed... Key-exchange timed out waiting for key negotiation`). Child research job `1cab4b88` was created **2026-07-15** and was still running against it, with deliverables named `output/ideas/ui_73e68890_*.md`. No cancellation propagation. *(Caveat: may be intentional if `ui_73e68890_*` is a stable task-family id under the compounding-repo pattern — needs an owner call.)* | orchestrator dispatch / completion | Open — needs decision |
| 13 | **Live healthy job reads as dead through the API.** `get_todos`, `get_workspace_overview`, `list_todo_archives`, and `get_job_log` all return empty/not-found for this job while `list_job_files` and `list_job_commits` work fine. `get_job_progress` reports **0.0%** after 18 completed phases. Through the Cockpit this job looks idle or dead; it is neither. | MCP/orchestrator read path | Open |

## Cross-references to existing open work

- **#1** is the concrete root cause behind the known-but-unbuilt `spawn_subagent` "wedged" watchdog
  misfire — the fan-out is not wedged, it is hitting `delegation: 120`.
- **#11/#12** sit downstream of the still-unfixed *version-upgrade drain strips k8s workspace* defect,
  which was originally diagnosed **on job `73e68890`** — the same job whose brief is missing here.
  The brief loss and this 9.4h of work are the same incident, one cycle apart.
- **#6** may overlap with `docs/issues/agent_tool_fixed_vocabularies_invisible_to_model.md` (tool
  vocabulary/deferral prose invisible or misleading to the model). Worth reading together — if
  palette prose actively misleads, the agent was reasoning correctly on a false premise.

## Loose ends (captured so they're not lost)

- **Strategic-phase overhead is ~50% of wall clock.** Every strategic phase re-reads and rewrites a
  27.8 KB `plan.md` (REVIEW → ADAPT → PLAN-OR-COMPLETE), ~20 min each, ~2 phases/hour overall. Each
  rewind (#3-#6) costs one of these on top of the lost todos. Not yet its own doc.
- **`plan.md` heading carries contradictory labels** — `## Phase 2.1d — ... (NEXT) (COMPLETE)`. Likely
  an ADAPT-step editing artifact; harmless here but it is exactly the kind of stale
  self-report the plan's own rules forbid trusting.
- **The agent's own durable policies are better than the harness's.** `plan.md` independently derived
  "textual tool-palette prose is informational, not runtime evidence — only a real returned error can
  establish a blocker" and "do not retry unchanged subagent fan-out". It learned #1 and #6 the
  expensive way, per job, with no way to persist that to the fleet.
- **Unverified:** what the "tool-palette prose" actually was. All three rewind losses are attributed
  from `plan.md`'s own retrospectives, not from reading the LLM request at the rewind moment. Pull
  the transcript before designing the fix for #6.

## Suggested pickup order (by impact ÷ effort)

1. **#1 delegation timeout** — one line, unblocks all fan-out, cost a whole phase here. Raise toward
   the `900` ceiling that `shell` already uses.
2. **#4 + #3 rewind correctness** — point recovery at the right tool for the phase, and stop
   discarding *incomplete* todos on rewind. Small, contained, stops silent deliverable loss.
3. **#7 split the cap** before raising it — tactical (research) needs headroom; strategic does not.
   Design doc's own guidance applies: *"Don't set hard caps too low... calibrate on observed task
   complexity distributions"* (`stuck_agent_recovery.md:308`) — and equally, don't raise the strategic
   freeze by side effect.
4. **#2 job-level cap** — the actual runaway guard, still missing. This is the one that would have
   stopped a 9.4h job with no brief.
5. **#11 brief fail-fast** — refuse to dispatch on a stub brief. Cheapest possible prevention of the
   whole incident.
6. **#6 rewind evidence gate** — biggest single loss, but pull the transcript first (see loose ends).
7. **#13 observability** — the job looked dead through every UI surface while healthy. Fix before
   trusting Cockpit for triage.
8. **#10 evidence reuse** — the real efficiency defect. Largest long-term win, least contained.
9. **#9 stale constants**, **#8 design/impl divergence** — trivial, do alongside #7.
10. **#12 parent cancellation** — needs an owner decision on task-family semantics first.
