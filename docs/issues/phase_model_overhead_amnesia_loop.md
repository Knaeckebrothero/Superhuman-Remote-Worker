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
related:
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[officer_conference_live_fire_findings]]"
  - "[[subagents_never_used]]"
  - "[[dual_app_persistent_app_redundancy]]"
  - "[[memory_bugs]]"
  - "[[context_summarization_rework]]"
---

# Phase-model overhead: the forced-compaction amnesia loop

**Status:** 🔴 **OPEN** — filed 2026-07-31 after a code-side deep dive
prompted by "the phase model isn't suited for small tasks (and maybe not
for long ones either)". Nothing below is fixed. The measurements are from
phase archives pulled live before the dev API went down; the token-side
confirmation is still owed (see *Not yet verified*).

**One-line summary.** The strategic/tactical loop forcibly wipes the
agent's working context at every strategic→tactical boundary, then
mandates that the next strategic phase reconstruct that same state from
git — and charges ~15k tokens of transient injection for every turn of
that reconstruction, on a memory tier that gets *less* useful as the job
gets longer.

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

`src/graph.py:2907`, inside `archive_phase`:

```python
force_summarize = is_strategic  # True when completing strategic phase
```

`compact_on_archive: true` (`config/worker_base.yaml:275`) means every
phase boundary calls `ensure_within_limits`; `force=True` on the
strategic side makes it summarize **regardless of token pressure**. The
inline comment states the intent plainly: *"This gives tactical phases a
'fresh conversation' with just the plan summary."*

So the phase that just did the thinking has its reasoning replaced by a
prose summary the moment it finishes.

### F2 — The git archaeology is a *consequence* of F1, not independent bureaucracy

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
| memory | up to **10,000 tok** | `config/worker_base.yaml:296` `budget_tokens` |
| knowledge | ~2,500 tok | `src/graph.py:713` (budget estimate) |
| todo list, **verbatim bodies** | ~2,135 tok initial / ~1,000 tok transition | `src/graph.py:853` `format_for_injection()` |
| `verify-before-done` SKILL.md | ~909 tok, **every tactical turn** | `config/worker_base.yaml:201` `trigger: phase:tactical`, `enforce: false` |
| phase system prompt | ~1,400–1,600 tok | `config/prompts/{strategic,tactical}*.txt` |

**≈15k tokens of scaffolding before a single line of conversation
history**, on every turn, in a phase whose entire product is two ticked
todos and three staged ones.

Note the interaction with F1: `format_for_injection()` emits the *full
body* of every todo. During a strategic phase that means the entire
ceremony template is re-injected verbatim on every turn of that phase.

### F4 — The memory tier starves relevance as the job grows

`src/services/recall_store.py:1166`, assembly order:

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
(`src/graph.py:2723`, `CaptureEvent(kind="phase_boundary")`) *and* every
5 turns (`config/worker_base.yaml:298` `observer_interval: 5`). More
phases → larger pinned tier → smaller relevance share → worse-informed
strategic phases → more archaeology → more phases.

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

- **P-1 — Stop forcing summarization at strategic→tactical**
  (`src/graph.py:2907`). Let compaction be threshold-driven like
  everywhere else. One line. This is the root: with context intact across
  the boundary, the git archaeology becomes *optional* and the intended
  "tick and continue" design becomes expressible at all. Risk: strategic
  phases inherit tactical tool-result bloat — mitigated by the existing
  `clear_old_tool_results` + threshold path, and empirically survivable
  (the persistent/session runtime runs for days without a forced wipe).
- **P-2 — Make REVIEW-AND-ADAPT conditional.** Fast path when every
  tactical todo completed **and** the P1-C deliverable manifest — already
  written at every boundary to `output/manifest_status.json` — agrees the
  contract paths moved: append one outcome line to `plan.md`, stage the
  next todos, continue. Load the full block only on a failed todo, or on
  a **manifest/todo disagreement**. This is the anti-rubber-stamp check:
  artifact-based rather than narrative-based, so it is simultaneously
  cheaper and harder to fake than reading a diff. Depends on P-1 for
  honesty.
- **P-3 — Cap the pinned memory tier** at a fraction of budget (~30 %) so
  hybrid search always gets a share, and make phase-boundary extraction
  outcome-gated (this is P2-A from the postmortem). Without it, P-1/P-2
  savings are eaten back as the pool grows.
- **P-4 — Trim the per-turn floor.** 10k memory budget is very large for
  a worker; todo bodies can inject verbatim once and then as ID+status;
  `verify-before-done` does not need re-injection on every tactical turn.

---

## 5. What this means for the "add a ReAct runtime" proposal

The session opened with the idea of adding a proper ReAct-loop runtime
alongside the phase model. Two corrections came out of the dive.

**First, the runtime already exists.** `src/persistent_graph.py::run_persistent_loop`
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

**Second — and this reverses the sequencing — a ReAct runtime does not
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

## 6. What already works (keep)

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

## 7. Not yet verified

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
  before/after and compare the strategic-phase duration sequence.
- Interaction between P-1 and the `message_count_threshold: 300` /
  `max_tool_calls_per_phase: 500` limits, which have never been exercised
  with un-forced boundaries.
