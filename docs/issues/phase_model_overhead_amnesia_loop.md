---
tags:
  - issue
  - agent-architecture
  - phase-model
  - context-management
  - memory
  - cost
aliases:
  - phase overhead deep dive
  - forced compaction amnesia loop
  - strategic phase overhead
  - fewer larger phases
  - three-phase jobs
related:
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[officer_conference_live_fire_findings]]"
  - "[[subagents_never_used]]"
  - "[[dual_app_persistent_app_redundancy]]"
  - "[[memory_bugs]]"
  - "[[context_summarization_rework]]"
---

# Phase-model overhead: the forced-compaction amnesia loop

**Status:** 🟡 **IN PROGRESS** — filed 2026-07-31 after a code-side deep
dive prompted by "the phase model isn't suited for small tasks (and maybe
not for long ones either)". The root cause (P-1), the structural
prerequisites for enlarging phases, and the behaviour change itself have
since shipped in-tree; see §5.
The measurements are from phase archives pulled live before the dev API
went down; the token-side confirmation is still owed (see *Not yet
verified*).

**Direction, decided 2026-07-31.** The split is **not** being torn down.
Tactical phases get much larger, so a job runs roughly three phases —
plan → execute → review-and-submit. That turns out to be mostly a
*prompt* change (§7); the code work is removing the things that silently
depended on boundaries being frequent.

**One-line summary (the diagnosis as filed).** The strategic/tactical
loop forcibly wiped the agent's working context at every
strategic→tactical boundary, then mandated that the next strategic phase
reconstruct that same state from git — charging ~15k tokens of transient
injection for every turn of that reconstruction, on a memory tier that
got *less* useful as the job got longer. The wipe and the memory ratchet
are now fixed; the injection floor (P-4) and the unconditional
reconstruction block (P-2) are not.

---

## 1. What this is not

The first suspicion was that the transition template tells the agent to
rewrite `plan.md` wholesale each boundary. **It does not.**
`config/templates/strategic_todos_transition.yaml` says "rewrite the
*relevant parts* of plan.md **in place** (one write_file, never append a
second copy)", and todo 2 is `PLAN OR COMPLETE` with the stop condition
first. P1-B already cut this template from ~1,800 to ~1,000 tok/turn.

The intended design — *if the tactical phase achieved its todos, check
the results, tick them, schedule the next batch* — is what the template
describes. The defect is that **the block is unconditional**: `git_tags`
+ `git_diff` + a `write_file(plan.md)` are paid at every boundary whether
or not anything diverged. There is no "phase went as planned" fast path.

That alone would be cheap. The reason it is not cheap is §3.

---

## 2. The measurements

Both jobs below are on the dev cluster, project `68137e29` (Better
Resavio). Durations are derived from phase-archive timestamps
(`archive/todos_phase_N_*.md`), each written at the *end* of its phase.

### 2a. Small job — `13fc2854` (worker_base, completed)

| Phase | Ends | Duration |
|---|---|---|
| 1 strategic | 19:30:38 | ~154 s (incl. init) |
| 1 tactical | 19:33:33 | 175 s |
| 2 strategic | 19:36:32 | 179 s |

**333 s ceremony / 175 s work — 66 % overhead.** This is the
*structural floor*: two strategic phases bracketing one tactical phase is
the minimum shape a job can have. No job can do better under the current
model.

### 2b. Long job — `396a5d4c` (developer, 17 phase archives, 1645 audit
entries, **zero contract deliverables**, one of the two contracted jobs
from the supervised night of 07-30→31)

8 strategic + 8 tactical phases, alternating, 22:14→23:22.

- Tactical wall-clock total: **2087 s**
- Strategic wall-clock total (excl. the unmeasurable first): **2007 s**
- → **49 % of wall-clock in phases that produce no deliverable content
  by construction.**

The trend matters more than the ratio. Successive strategic phases ran:

```
106s → 209s → 115s → 131s → 160s → 218s → 300s → 768s
```

**The boundary tax compounds instead of amortizing.** This is the
evidence that the model is not merely "unsuited to small tasks" — it
degrades superlinearly on exactly the long-horizon jobs it was built for.

### 2c. Live symptom

At the time of filing, recovery job `aa25440f` (step ~199) had just
written `archive/phase_1_retrospective.md` — a file the transition
template explicitly forbids ("INSTEAD OF: Writing a separate
retrospective file"). The lore channel (RecallStore pins + KB notes + old
workspace files) is outrunning the template fixes; cf. P2-A in
[[officer_blind_reads_and_worker_bureaucracy]].

---

## 3. Findings

### F1 — Every strategic phase ends by forcibly erasing its own context

> **✅ Fixed 2026-07-31** (§5, slice 1). Described below in the present
> tense as it stood at filing; the line now reads `force_summarize = False`.

`src/graph.py:2957`, inside `archive_phase`:

```python
force_summarize = is_strategic  # True when completing strategic phase
```

`compact_on_archive: true` (`config/worker_base.yaml`) means every
phase boundary calls `ensure_within_limits`; `force=True` on the
strategic side makes it summarize **regardless of token pressure**. The
inline comment states the intent plainly: *"This gives tactical phases a
'fresh conversation' with just the plan summary."*

So the phase that just did the thinking has its reasoning replaced by a
prose summary the moment it finishes.

### F2 — The git archaeology is a *consequence* of F1, not independent bureaucracy

> **Unblocked, not removed.** With F1 fixed the archaeology is no longer
> *load-bearing* — the context it reconstructs is still there. Making the
> block conditional so it is no longer *paid* is P-2, still open.

`config/templates/strategic_todos_transition.yaml:14`:

> Use git evidence, **not memory (memory may be wrong after
> compaction)**

The template is not being ceremonial. It is correctly working around
amnesia that the platform deliberately induced one boundary earlier.
`git_tags` + `git_diff` between boundary tags is the cheapest honest way
to answer "what did the last phase actually do?" *given that the answer
was thrown away.*

**Consequence for the intended design:** "tick the todos and schedule the
next batch" is not merely missing — it is **unimplementable today**. By
the time the strategic phase runs, the agent has been stripped of the
context needed to know whether the todos were achieved, so it has no
choice but to reconstruct. Any fast path added on top of F1 would be
rubber-stamping, which is exactly what we do not want.

### F3 — Every one of those reconstruction turns pays a ~15k-token floor

The transient block is assembled and spliced into the message tail inside
the `execute` node — i.e. **on every turn**, not once per phase
(`src/graph.py:546` `create_execute_node`, block assembled ~`1130-1224`,
todo list appended last at `src/graph.py:1224`).

| Injection | Per-turn cost | Source |
|---|---|---|
| memory | up to **10,000 tok** | `config/worker_base.yaml` `memory.budget_tokens` |
| knowledge | ~2,500 tok | `src/graph.py:713` (budget estimate) |
| todo list, **verbatim bodies** | ~2,135 tok initial / ~1,000 tok transition | `src/graph.py:853` `format_for_injection()` |
| `verify-before-done` SKILL.md | ~909 tok, **every tactical turn** | `config/worker_base.yaml` `instruction_files`, `trigger: phase:tactical`, `enforce: false` |
| phase system prompt | ~1,400–1,600 tok | `config/prompts/{strategic,tactical}*.txt` |

**≈15k tokens of scaffolding before a single line of conversation
history**, on every turn, in a phase whose entire product is two ticked
todos and three staged ones.

Note the interaction with F1: `format_for_injection()` emits the *full
body* of every todo. During a strategic phase that means the entire
ceremony template is re-injected verbatim on every turn of that phase.

### F4 — The memory tier starves relevance as the job grows

`src/services/recall_store.py:1186`, assembly order:

1. **Tier 1**: `get_ttl_active()` (`recall_store.py:956`) — all memories
   with `remaining_turns > 0`, **ordered by `importance DESC`**, appended
   until the budget is exhausted.
2. **Tier 2**: hybrid search, with *whatever budget is left*.

Resavio carried ~2,400 pinned rows against a 10,000-token budget (the
night-1 postmortem retired 147 poison rows, 141 of them pinned, taking
the count 2504→2363). At that scale **tier 1 consumes the entire block
and relevance-based retrieval never runs.**

This is the mechanical link between "overhead" and the honest-floor
culture documented in the postmortem: the process lore is not being
*recalled*, it is being **injected by construction, every turn**.

It also grows monotonically: extraction fires at every phase boundary
(`src/graph.py:2761`, `CaptureEvent(kind="phase_boundary")`) *and* every
5 turns (`config/worker_base.yaml` `memory.observer_interval: 5`). More
phases → larger pinned tier → smaller relevance share → worse-informed
strategic phases → more archaeology → more phases.

> **Amended 2026-07-31 — the growth was a bug, not the policy.** Extraction
> volume is not what produced ~2,400 pinned rows. `hybrid_search`'s
> access-tracking UPDATE re-armed `remaining_turns` to the full TTL for
> every row it *fetched* (up to 150/turn) while `decrement_ttl` only ticks
> −1/turn, so a memory expired only if it stayed out of the top-150 for ten
> consecutive turns — and rows the budget loop discarded, which the model
> never saw, came back pinned anyway. Retrieval was re-pinning the tier
> that was starving retrieval. Fixed in §5, slice 1. The tier-1-first
> assembly order described above is unchanged and is still worth capping
> (P-3), but it now guards a bounded pool.

### F5 — Together, F1–F4 explain the 106s → 768s curve

Each successive strategic phase faces: a longer `git_diff` range, a
larger `plan.md`, a larger pinned-memory tier crowding out task-relevant
recall, and the same ~15k floor on every turn of the reconstruction. The
compounding is not mysterious — it is four monotonically growing inputs
feeding a fixed per-turn cost.

### F6 — The shape is "subagent delegation with the memory in the wrong place"

Framing owed to the user, and it is exact. In real delegation the
**parent** retains context and the **child** is amnesic and returns a
structured result. The phase loop inverts this: the driver is wiped at
each boundary, and `plan.md` + a prose summary are drafted in as a
stand-in parent. A lossy narrative is a poor return value, so the
successor pays to reconstruct what a parent would simply have
remembered.

The irony worth recording: **the good version of this pattern already
ships in the same repo.** `spawn_subagent` delegation is built, adopted
and metered ([[subagents_never_used]]: fix `e65d5e32`, deploy
`a4596376`, cost measured 07-03; `config/experts/developer/config.yaml:78`).
The phase loop reimplemented a worse variant by accident.

---

## 4. Fix directions (causal order — each is upstream of the next)

- **P-1 — ✅ SHIPPED. Stop forcing summarization at strategic→tactical**
  (`src/graph.py:2957`). Let compaction be threshold-driven like
  everywhere else. One line. This is the root: with context intact across
  the boundary, the git archaeology becomes *optional* and the intended
  "tick and continue" design becomes expressible at all. Risk: strategic
  phases inherit tactical tool-result bloat — mitigated by the existing
  `clear_old_tool_results` + threshold path, and empirically survivable
  (the persistent/session runtime runs for days without a forced wipe).
- **P-2 — ⬜ NOT STARTED. Make REVIEW-AND-ADAPT conditional.** Fast path when every
  tactical todo completed **and** the P1-C deliverable manifest — already
  written at every boundary to `output/manifest_status.json` — agrees the
  contract paths moved: append one outcome line to `plan.md`, stage the
  next todos, continue. Load the full block only on a failed todo, or on
  a **manifest/todo disagreement**. This is the anti-rubber-stamp check:
  artifact-based rather than narrative-based, so it is simultaneously
  cheaper and harder to fake than reading a diff. Depends on P-1 for
  honesty.
- **P-3 — 🟡 PARTIAL. Cap the pinned memory tier** at a fraction of
  budget (~30 %) so hybrid search always gets a share, and make
  phase-boundary extraction outcome-gated (this is P2-A from the
  postmortem). Without it, P-1/P-2 savings are eaten back as the pool
  grows. **The mechanical half is fixed** — see the TTL ratchet in §5 —
  which removes the runaway. The *policy* half (an explicit budget cap on
  the pinned tier) is still open; it now guards a pool that no longer
  grows without bound.
- **P-4 — ⬜ NOT STARTED. Trim the per-turn floor.** 10k memory budget is
  very large for a worker; todo bodies can inject verbatim once and then
  as ID+status; `verify-before-done` does not need re-injection on every
  tactical turn.

---

## 5. What shipped (2026-07-31, in-tree, uncommitted)

Six slices. The first is the root cause; slices 2-4 exist because
enlarging phases removes guarantees that were quietly riding on
boundaries being frequent — durability, steering, and the ability to
change the plan mid-phase. Slice 5 is the behaviour change itself; slice 6
removes the last thing that would have fought it.

### Slice 1 — the amnesia loop itself

- **`force_summarize = False`** (`src/graph.py:2957`). The one-liner from
  P-1. Compaction is now threshold-driven everywhere.
- **Provider-anchored compaction trigger** (`src/graph.py`, in `execute`).
  The worker now feeds `response.usage_metadata["input_tokens"]` to
  `context_mgr.record_provider_usage`, mirroring the session loop
  (`persistent_graph.py:1968`). This had to land *with* P-1, not after:
  the trigger previously ran on a local estimate blind to 60–90 bound
  tool schemas (~10–25k tokens/request), and forced boundary compaction
  was the only thing masking that undercount. Remove the mask without
  fixing the meter and the real request runs far larger than the
  threshold can see.
- **Evidence carve-out emptied** (`src/core/context.py`,
  `preserve_content_patterns`). It retained every tool result containing
  `error:`/`failed`/`not found`/etc. *forever*. Its stated consumer, the
  strategic `<phase_audit_protocol>`, was deleted in `4eba5d47` — the
  consumer was gone, the unbounded retention stayed. It is also actively
  harmful: retaining an agent's own past errors is the measured
  self-conditioning effect (arXiv 2509.09677, accuracy ~70 % → ~15 % as
  induced past errors rise, undiminished by model scale). Side-effect
  tools (`write_file`/`edit_file`/`patch_*`) keep their exemption, and
  recent failures remain verbatim inside `keep_recent_tool_results` — the
  carve-out only ever governed results *older* than that window.
- **TTL re-arm ratchet removed** (`src/services/recall_store.py`,
  `hybrid_search` access tracking). This is the mechanical cause behind
  F4, and it is a bug rather than a policy choice: the access-tracking
  UPDATE re-armed `remaining_turns` to the full TTL for every row the
  search *fetched* — up to 150 per turn — while `decrement_ttl` only
  ticks −1 per turn. A memory therefore expired only if it stayed out of
  the top-150 for ten consecutive turns. Worse, it re-armed rows that the
  budget loop then *discarded*: rows the model never saw came back
  pinned, growing the very tier that starves relevance retrieval. All
  three runtimes funnel through that one seam.

### Slice 2 — progress durability (`src/core/progress_commit.py`, new)

All six `git_mgr.push()` calls lived in `src/core/phase.py`, so every
external view of a running job — orchestrator, cockpit, MCP workspace
tools, officer — was defined as *"committed state as of the last
phase-boundary push"*. At ~16 phases that was an incidental ~4-minute
heartbeat. At three phases it is hours of silence in which the step count
climbs and every artifact reads empty, which is **worse than a visible
stall**: missing evidence reads as evidence of no work. That is the
night-2 false-conviction shape (F7/F8 in
[[officer_conference_live_fire_findings]]) recreated structurally.

`ProgressCommitter` has two triggers, deliberately not one:

- `on_todo_complete()` from the `todo_complete` tool — the semantic one,
  giving history real messages (`todo_3: add retry to the fetch path`).
- `on_turn()` from `audited_tools` — a wall-clock floor committing `WIP:`
  after `limits.progress_wip_commit_after_seconds` (300). **This is the
  load-bearing one.** The semantic trigger is anti-correlated with need:
  an agent grinding on one hard todo emits no completions, so a
  todo-only policy goes quiet exactly when an observer most needs signal.
  It also backstops the model simply forgetting to call `todo_complete`
  — durability must not depend on a tool the model may skip.
- `flush()` is unconditional, wired into the tool-requested freeze path
  (ordered *after* `result["should_stop"]` is set, since it shares that
  `try` and losing the freeze to a git error would be worse).

Commit and push are separated on purpose: commit is local and free (per
todo), push is a Gitea round-trip throttled by
`limits.progress_push_interval_seconds` (60) and guarded by
`has_unpushed_commits()` — which is network-free and was already
documented as *"used to decide whether an end-of-turn push is
worthwhile"*. A failed push still resets the clock, so a down remote
costs one attempt per interval rather than one per turn.

*Trap worth recording:* `WorkspaceManager._git_manager` is reassigned in
several places (`src/core/workspace.py:547/562/571/637`) and a mid-job
tier upgrade swaps it. The committer therefore takes a **provider
callable**, resolved per call. A handle captured once would commit
against a dead workspace, or stop committing — with no error either way.

### Slice 3 — steering lane B re-keyed

Steering has **two** lanes, and only one was broken:

- **Lane A — `pending_guidance`** (urgent). Rides the heartbeat into
  `dual_app`'s inbox and is re-derived **every turn** at
  `src/graph.py:1121` as a transient `[SUPERVISOR GUIDANCE]` block.
  Already phase-independent and compaction-immune. **Untouched.**
- **Lane B — `queued_replies`** (the default,
  `async_reply: "next_strategic_phase"`, `orchestrator/main.py:10114`).
  Drained only in `handle_transition`, behind `if not is_strategic`.

At three phases a job has exactly one tactical→strategic boundary, so a
non-urgent reply sent during planning waits out the whole execution
phase, and one sent during review — the phase where a supervisor is most
likely to write — is **never delivered at all**. Not late: absent.

Lane B is now keyed to `todo_complete` (via
`ToolContext.request_reply_drain()`, drained in `audited_tools`) with a
`limits.queued_reply_max_wait_seconds` floor, for exactly the same
anti-correlation reason as slice 2. A finished todo *is* the natural
break the `next_strategic_phase` default was trying to express — same
intent, roughly an order of magnitude more often, independent of phase
structure. `queued_replies` now rides the heartbeat next to
`pending_guidance` (same job-row read, zero marginal DB cost); this was
also forced, since `audited_tools` has no `postgres_db`.

Two hazards found and closed while building:

1. **Duplicate accumulation.** The ack is fire-and-forget, so the
   heartbeat keeps returning delivered entries for up to one interval.
   Lane A shrugs this off because its block is transient; lane B appends
   **persistent** `HumanMessage`s, so every todo completed in that window
   would stack the same reply into history permanently. Closed with a
   content-derived `_reply_key()` plus `ToolContext._delivered_reply_keys`
   (queued replies carry no id, unlike guidance entries). Process-local
   on purpose — a successor pod has no record of the delivery and *should*
   redeliver.
2. **Backstop race.** The phase-boundary drain survives, because outside
   the dual app there is no heartbeat inbox and the DB is the only
   source. But it reads the DB directly and could re-append mail the new
   path had already delivered. `tool_context` is now threaded into
   `create_handle_transition_node` and the backstop filters through the
   same key set.

### Slice 4 — `todo_rewind` → `request_replan`

The tactical phase's only escape hatch was `todo_rewind`, and it was a
genuine rewind. It called `archive_with_failure_note`, which wrote **every
todo — including the completed ones** — into `archive/failed_<time>.md`
and emptied the list. The strategic phase that followed therefore
inherited no record of what had actually been achieved and had to
reconstruct it, which is the same amnesia pattern as F1 arriving by a
different door.

It also never asked for the strategic phase. It reached one *indirectly*,
by leaving the todo list empty until `check_todos` hit its "no todos in
tactical phase — forcing phase complete to recover" branch — a path that
exists for resume bugs.

`request_replan` keeps everything:

- **Nothing is archived-as-failed and nothing is cleared.** Completed
  todos stay completed, pending ones stay pending, and `archive_phase`
  records them at the boundary with their real statuses — which is
  exactly what the incoming strategic phase needs in order to decide what
  to carry forward. Files and commits were never touched by either
  version.
- **The boundary is requested explicitly**, via
  `ToolContext.request_replan(reason)` → `check_todos` →
  `phase_complete=True`, instead of falling through a recovery branch.
- **The reason reaches the next phase.** `check_todos` puts it in
  `state["replan_reason"]`; `handle_transition` injects a
  `[REPLAN REQUESTED]` message telling the strategic phase this was
  deliberate, not a failure, and that valid work should be carried
  forward rather than redone. It is cleared at the transition so a stale
  reason cannot steer a later phase.
- Staged todos *are* still dropped — they are a bet on the plan being
  revised, so the strategic phase should re-decide rather than inherit
  them.

The rename is deliberate. Tool names are what the model actually reads,
and "rewind" states the opposite of what the tool now does. This matters
more at three phases, where `request_replan` is the **only** way to adapt
mid-job. Note the name appears in eight config files
(`config/worker_base.yaml` plus seven expert configs), so the rename had
to sweep those too — `tests/test_config_tool_names_are_registered.py` is
what guards that.

The unrelated `max_tool_calls_per_phase` budget rewind (§7, blocker 3)
still calls `archive_with_failure_note` and is unchanged.

### Slice 5 — the prompts

Three kinds of strictness live in the prompts and they were not all the
same problem.

**Loosened — phase sizing.** The old guidance did not merely suggest small
phases, it *asked for* them: `config/templates/strategic_todos_initial.yaml`
(pre-change) read
"Target 2-5 todos per tactical phase. **Prefer many short phases over few
long ones**", and the developer template said the same. The default is now
one execution phase covering the task, with a second phase having to earn
its planning cycle. Rewritten in: the four `instructions*.md` variants, the
generic `strategic_todos_initial.yaml` / `_resume.yaml` /
`_transition_gpt_oss.yaml`, and the scholar, developer and designer
`strategic_todos_initial.yaml` + `todo_guide.md`. `max_todos` 20 → 30.

Two experts were deliberately *not* collapsed to one phase:

- **developer** — `spec`/`red`/`green`/`refactor` differ in what they are
  allowed to write, and the guide names mixing them as the failure mode
  that erases TDD. The phase count there is the methodology. What changed
  is splitting *within* a type: one red phase covering every failing test,
  8-16 todos rather than 5-10.
- **designer** — phases are user-facing groupings (core screens / edge
  cases / polish). Kept, batch raised 3-5 → 6-12.

**Retargeted — instructions written for an amnesiac agent.**
`strategic*.txt` step 7 told the agent to "regenerate deliverables from
scratch rather than editing stale artifacts". That was correct when it
could not trust what it had; with context surviving the boundary and
per-todo commits it is now actively wasteful, and it is exactly the
compounding cost F5 describes. It now says to edit in place, and to
regenerate only for artifacts predating the run. The tactical "stay on the
current task" constraint and the "if blocked on multiple todos, end the
phase early" line both now name `request_replan`, which is the mechanism
they were describing without having one.

**Deliberately NOT loosened — verification.** `verify-before-done` and
tactical steps 5-6 (read the semantic content of tool output, do not
proceed as if a failed call succeeded, do not fabricate missing data) are
untouched. The measured failure in §2b was 1,645 audit entries and **zero
contract deliverables** — lots of activity, nothing that held up. That
calls for more verification discipline, not less; the skill itself cites
the number (missing or incorrect verification is ~1 in 4 multi-agent
failures, MAST arXiv 2503.13657). The overhead was never the verification,
it was the amnesia and the reconstruction the amnesia forced.

There *is* a real cost complaint inside that: `verify-before-done` is
~909 tokens on **every tactical turn**. That is an injection-economics
problem (P-4) — inject it once per phase, or at todo-completion time when
it actually applies — not a reason to weaken the content.

**Caveat.** Prompt changes are the least verifiable part of this work
offline. Tests confirm the templates still parse and render; they cannot
confirm the model behaves differently. This slice above all needs the k3d
run before it goes near dev.

### Slice 6 — the tool-call budget becomes job-level

`max_tool_calls_per_phase: 500` was the last hard blocker, and looking at it
properly it was wrong in three ways at once.

**Wrong unit.** It reset at every phase boundary, so it bounded a *phase*
and never a *job*. With ~16 phases it was really an ~8,000-call job budget;
with three large phases it would have become ~1,500 — a **tightening**,
exactly backwards for this model.

**Wrong action.** On the tactical side it did not freeze, it *rewound* —
calling `archive_with_failure_note`, which writes every todo (the completed
ones included) into a failure archive and empties the list. That is the
identical destructive behaviour removed from `request_replan` in slice 4,
and it fired on jobs that had usually done real work.

**Load-bearing in a way nothing else is.** Grep says it plainly at the
progress nudge: *"Never freezes the job — the hard cap is the only stop."*
The progress nudge and act-ratio tripwire only inject reminders; loop
detection only masks tools; and the orchestrator has **no** job-duration
ceiling (`mark_stuck_working_agents_ready` is about agents with no job, and
`get_stuck_jobs` is a query tool, not enforcement). Removing the budget
outright would have left a wedged job burning credits until a human noticed
— which for overnight unattended runs is the whole exposure.

So: **bumped and re-scoped rather than removed.**

- `max_tool_calls_per_job: 5000` — new, counted across phases, never reset
  at a boundary. Roughly matches the old *effective* job budget, so it is a
  bump in practice as well as in shape.
- `max_tool_calls_per_phase: 0` — off by default, knob retained so an
  operator who deliberately wants a per-phase guard still has one.
- Hitting either now **freezes** with `budget_exceeded` and flushes the
  workspace first. Nothing is archived-as-failed and nothing is cleared.
  `budget_exceeded` is not in `AUTO_REDISPATCH_FREEZE_TYPES`, so it parks
  for human review and cannot livelock.
- The progress nudge no longer quotes a phase budget, and stays silent
  about remaining calls when no budget is armed — an unbounded job must not
  be told it has "0 calls remaining".

Set `max_tool_calls_per_job: 0` to disable the stop entirely. That is
supported, but it leaves **no** automatic ceiling on a runaway job; only do
it when something else is watching the spend.

The now-unreachable `budget_rewind` guardrail template was deleted from
`default.yaml`, `gemma.yaml` and `KNOWN_NUDGES`. Leaving it would have
repeated the slice-1 pattern exactly: a consumer deleted, its config left
behind to confuse the next reader.

### Verification

12,258 pass; the 11 failures are pre-existing and were proven so by
stashing the changes and re-running on a clean tree (they need live
Postgres, live MCP servers, or assert on CI workflow content). `ruff`
clean. New suites: `tests/test_progress_commit.py` (29),
`tests/test_queued_reply_steering.py` (23),
`tests/test_request_replan.py` (16).
`tests/test_supervisor_guidance.py` unchanged and green — the check that
lane A was not disturbed. Beyond unit level: `ProgressCommitter` was
exercised against a real `GitManager` on a scratch repo (multi-line todo
flattened into a subject, clean tree correctly producing *no* commit,
floor firing, `git=None` mid-job degrading quietly), and every new config
knob was round-tripped through `serialize_resolved_config` including a
non-default override.

**Not yet run on a cluster.** No job has executed against this code.

---

## 6. What this means for the "add a ReAct runtime" proposal

The session opened with the idea of adding a proper ReAct-loop runtime
alongside the phase model. Three corrections came out of the dive.

**First — there are already four ReAct loops, not one or two.** Counted
2026-07-31; every one of them dispatches `tool_calls` in a loop, and they
have all diverged:

| # | Where | Size | Shape | Drives |
|---|---|---|---|---|
| 1 | `src/graph.py` | 5,054 | The **only** `StateGraph` in the repo | jobs |
| 2 | `src/persistent_graph.py` | 2,444 | **Not a graph** despite the name — plain `while True:` at 781, inner tool loop at 1363 | sessions **and** the officer |
| 3 | `src/services/auxiliary.py::AuxiliaryLLM.agent()` | ~105 | `for iteration in range(max_iterations)` + a forced structured-output call | knowledge curation, memory assembly |
| 4 | `src/tools/delegation/light_runner.py::run_light_subagent` | 320 | deadline + forced synthesis on timeout | `spawn_subagent` |

The divergence *is* the tax. Stop conditions differ (`max_tool_calls_per_phase`,
which resets per phase, vs an iteration cap vs wall-clock deadlines);
timeout behaviour differs (2 and 4 force an answer, 1 and 3 do not);
retry policy differs (`_READER_RETRY` vs `_is_retryable_llm_error` vs
`llm_retry`); and only #2 anchored compaction on provider token counts
until slice 1 above fixed #1. That last one is this failure mode in
miniature — a correctness fix living in one loop and silently absent from
the others for months. **Do not build a fifth.**

**Second, the runtime already exists.** `src/persistent_graph.py::run_persistent_loop`
is a plain ReAct loop with compaction, memory injection, KB injection,
skills, a phase-free `SessionTaskManager` todo list
(`src/managers/session_tasks.py`), its own audit sink, its own
fingerprint-based stuck guard, headless operation, and cross-pod
survival. The centurion runs on it for days. The delta to run a *job* on
it is a bootstrap turn, a `JobDriver` implementation of the existing
`PersistentLoopCallbacks` seam that self-feeds "continue" instead of
blocking on `get_user_input()`, and reuse of `job_complete` → `freeze_data`.
The orchestrator is already runtime-agnostic (it reads the completion
POST; the P1-C contract and seal gate are orchestrator-side).

It is also the only one of the four with the seams a job driver would
need: `get_current_tools` / `get_current_system_prompt` /
`get_current_context` are re-read **at the start of every turn** — which
is precisely "a ReAct loop with changing instructions", already in
production — plus per-turn commit+push (`persistent_graph.py:949/960`), a
caller-supplied `messages` list (so resume is a driver concern, not baked
in), one production caller (`src/api/persistent_app.py:433`) and ten test
files already driving it headlessly.

**Third — and this reverses the sequencing — a ReAct runtime does not
fix any of F3/F4.** The injection block and the pinned-first assembly are
**shared** code: `config/session_base.yaml:163` carries the identical
`budget_tokens: 10000` and the same tier-1-first path. Porting jobs onto
the persistent loop as-is inherits the same ~15k floor and the same
growth curve, minus the phase boundary that at least *bounded* it.

**Therefore:** do P-1..P-4 first (small, and they benefit both runtimes),
re-measure, *then* decide the runtime question. Expectation to test: a
meaningful share of what currently reads as "phase-model overhead" is
injection economics wearing a phase-model costume.

If a runtime split is still wanted afterwards, the shape to evaluate is
`runtime: react | phased` as a job/expert-level config — react by
default for short work, phased retained for long-horizon research where
archive-and-replan earns its keep — plus consolidation of the duplicated
audit/stuck-detection between the two runtimes (same disease as
[[dual_app_persistent_app_redundancy]]).

---

## 7. The plan from here: fewer, larger phases

The split stays. Tactical phases get much larger, so a job runs roughly
three phases: **plan → execute → review-and-submit.**

**Most of this was a prompt change, not a code change** — and it has
shipped (§5, slice 5). `check_todos` ends a tactical phase on exactly one
condition, `todo_manager.all_complete()`, and nothing else. Phase size is
therefore purely a function of how many todos the strategic phase
scheduled. Nothing in the graph ever enforced small phases: we asked for
them, in the predefined strategic todos the planning phase executes.

The strongest statement was in `config/templates/strategic_todos_initial.yaml` —
*"Target 2-5 todos per tactical phase. **Prefer many short phases over
few long ones.**"* Those templates matter more than the general
"Best Practices" list, because they are the todos the planning phase
actually runs. Full list of what was rewritten is in slice 5;
`phase_settings.max_todos` is now 30 (`config/worker_base.yaml`).

**The in-flight adaptation path exists and has been rebuilt** — see §5,
slice 4. It was `todo_rewind`, a genuine rewind; it is now
`request_replan`, which keeps everything and just ends the phase early.

### Remaining blockers, in order

1. **✅ Turn-level durability** — slice 2 above.
2. **✅ Turn-level steering** — slice 3 above.
3. **✅ Job-level step budget** — §5, slice 6. `max_tool_calls_per_job`
   (5000) replaces the per-phase rewind, and hitting it freezes rather
   than destroying todo state. Remaining gap: the counter is in-process,
   so it resets on pod replacement. Moving it orchestrator-side would make
   it survive Continue-as-New; that is a hardening, not a blocker, since
   `budget_exceeded` parks for human review rather than auto-resuming.
4. **⬜ Turn-level drain.** Exactly one drain check exists
   (`src/graph.py:3474`, in `handle_transition`). A job inside a
   multi-hour execution phase would block a fleet drain for that whole
   time — which matters because every `develop` push re-tags images and
   rolling-drains the fleet (F7).
5. **⬜ Autonomy-level semantics.** `partial`, `guided` and `dependent`
   all pause at phase boundaries. At three phases they collapse into
   roughly `review` and need redefining against something else.
6. **✅ Then the behaviour change**: done — §5, slices 4 and 5.
7. **⬜ Then P-2**, the conditional review fast path — which makes the
   third phase cheap when nothing diverged.

Note that blockers 1–4 are also exactly the prerequisites a `JobDriver`
on runtime #2 would need. Nothing here is throwaway if the runtime
question is reopened later.

---

## 8. What already works (keep)

The system delivers; it pays rent it does not owe. Receipts: `58027ee7`
completed with a verified claim (179 pass / 3 pre-existing failures —
the truth gate was narrower than feared); four thesis chapters plus §5.2
came out of this stack; roughly half the jobs in the 30-job listing
pulled for this dive are green; the officer ran 2.5 h of competent
zero-touch supervision on the runtime that does *not* wipe itself.

The expensive scaffolding is already built and is not in question here:
deliverable contracts + seal gate (P1-C), evidence snapshots on stop
(P1-D), the non-destructive guidance lane (P1-A), VM workspaces, Gitea-
backed officer reads (P0-A/B), proven subagent delegation.

---

## 9. Not yet verified

- **Nothing in §5 has run on a cluster.** All of it is unit- and
  integration-tested in-tree, and `ProgressCommitter` was exercised
  against a real `GitManager` on a scratch repo, but no job has executed
  against this code. The end-to-end check is a job on k3d showing commits
  landing *between* phase boundaries and a queued reply arriving at a
  completed todo rather than at a transition.
- **Token attribution per phase.** All durations above are wall-clock
  from phase archives. The token-side confirmation (`list_llm_requests`
  on `396a5d4c`, bucketed by phase) is owed — the dev API and cluster
  were unreachable for the whole filing session (Cloudflare 530 on
  `api.srw.works` / `mcp.srw.works`, `kubectl --context=main` timing
  out). Expected shape if F3/F4 are right: per-turn prompt tokens roughly
  flat-to-rising across the job with a large constant floor, and strategic
  turns no cheaper than tactical ones.
- **Whether P-1 alone is sufficient** to stop the growth curve, or
  whether P-3 is required to see it. Testable: run one contracted job
  before/after and compare the strategic-phase duration sequence. Note
  the TTL fix in §5 changes the P-3 baseline — the pinned pool no longer
  grows without bound, so this should be re-measured before deciding
  whether the policy cap is still needed.
- Interaction between P-1 and the `message_count_threshold: 300` /
  `max_tool_calls_per_phase: 500` limits, which have never been exercised
  with un-forced boundaries. The second of those is now known to be an
  active blocker rather than an open question (§7, blocker 3).
- **Prompt-cache behaviour.** No `cache_control` is set anywhere in the
  LLM path, and per-phase tool binding invalidates the prefix at every
  flip. Fewer phases should help by construction, but no hit-rate has
  been measured, so no cost claim should be made on that basis yet.

## 10. Slice 0 re-measurement (2026-08-03, post-`99c9aba0` field data)

The token-side confirmation §9 owed, run against the dev cluster after the
reform had been live for three nights. Method: per-job `llm_requests`
(`call_type='main'`) joined against phase-archive timestamps from Gitea;
strategic share computed three ways (wall-clock, summed LLM latency,
prompt tokens) so idle waits and tool time can't masquerade as ceremony.
Analysis script + per-job JSON: session scratchpad `analyze.py` /
`slice0_perjob.json`; cohort = 15 usable jobs (5 post-reform, 10 baseline).

**Cohort hygiene** (matters as much as the numbers):

- Jobs created 08-01 → 08-02T19:00Z are excluded: `f41970ae`'s CWD-banner
  bug broke every phase-boundary push, and those jobs have **zero archives
  in Gitea** — their phase history is unrecoverable. The exclusion is not
  optional; the data doesn't exist.
- `8302c195` (worker_base, 08-03) excluded post-hoc: a two-episode job —
  last archive 07:02, critic died, then 92 more main requests 08:16→09:13
  (real work incl. `next_phase_todos`) whose archives never reached Gitea.
  Also ran at ~117k median prompt tokens on MiniMax (≈90% of the 131k
  window) in episode 1 — worth its own investigation. Consequence:
  **worker_base has no clean post-reform sample** in this cohort.
- `dfbf9368` (critic, post) is a 5-minute single-phase verification —
  degenerate, no conclusion drawn.

**Result 1 — ceremony share dropped where it can be measured** (strategic
share of summed LLM latency; prompt-token share in parens; gemma-4-moe on
both sides of each comparison):

| family | pre (per job) | post (per job) |
|---|---|---|
| developer | 57.3% / 62.7% / 38.5% (tok 65.7/64.4/42.2) | **32.3% / 42.1%** (tok 27.5/46.2) |
| scholar | 52.7% / 55.7% (tok 45.7/53.6) | **30.8%** (tok 29.9) |

Roughly: strategic ceremony fell from ~55–65% to ~30–45% of LLM spend.
n=2–3 per cell and the nightly tasks drift night-to-night (one post
"developer" job is actually the email automation, see Result 4), so this
is directional, not proof — but the direction is consistent across all
three metrics on every post job.

**Result 2 — the compounding-strategic-phase curve is gone.** The
pre-reform signature (F1): strategic durations growing 106→768s across a
job as re-derivation cost escalated. Post-reform sequences oscillate with
no trend — `becf5f64`: 427,100,318,351,294,303,132,99,88,254,120,296,269,
95,566,136. (One 8633s strategic segment in `48a2994b` is an overnight
stall inside a segment, not phase work.) This is the expected signature of
P-1: without forced boundary summarization there is nothing to re-derive.

**Result 3 — the cost moved into per-turn context, as P-1 predicts.**
Median prompt tokens per main call, same model family: pre ~22–30k →
post **33–50k** (developer 32.7k/50.3k, scholar 47.1k). Keeping context
across boundaries means every turn carries more history; `48a2994b`
totalled **93.7M input tokens** in one (654-min, 2800-request) job. The
15k static injection floor (F3) is untouched — P-3 (pinned-tier cap) and
P-4 (floor trim) never shipped and are now the **dominant remaining
lever**. Phase-structure work has hit diminishing returns; injection
economics has not.

**Result 4 — the ONE-execution-phase default binds almost nowhere.**
Small worker_base jobs already had the minimal 1S/1T/2S shape *before*
the reform (`13fc2854`, `7d67d684`, `d7d6f511`). Scholar keeps 4S/3T by
its own exploration-sweep template (overrides the base default).
Developer is exempt by design. And cron **automations run developer
config**: `becf5f64` ("Produktzusammenfassung", an inbox-summarize task)
ran 29 archives / 13 tactical phases. Two follow-ups: (a) route small
recurring automations to a collapsed config, (b) the collapsed default
currently has no cohort where it's observable — the §9 paired-run suite
is the only way to see it.

**Verdict for the loosening path**: the two shipped mechanisms show their
intended signatures (ceremony share down, compounding gone) at small n;
the next slice should be **P-3/P-4 (injection economics), not further
phase-structure loosening**, and the paired-run suite is needed to (a)
give worker_base a clean post sample, (b) control night-to-night task
drift, (c) make the ceremony numbers defensible beyond direction.
