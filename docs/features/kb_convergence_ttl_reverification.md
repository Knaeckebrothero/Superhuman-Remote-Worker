---
tags:
  - architecture
  - knowledge-base
  - self-improvement-loop
  - auxiliary-llm
  - memory
  - decision
aliases:
  - kb convergence
  - knowledge base ttl
  - knowledge reverification
  - curator assembler
  - knowledge assembly task
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_repo_compounding]]"
  - "[[project_knowledge_base]]"
  - "[[agent_memory_overhaul]]"
  - "[[auxiliary]]"
  - "[[loop_review]]"
---

# KB Convergence — TTL Re-verification + the Curator's Missing "Assembler" Half

How the **project self-improvement loop** ([[project_self_improvement_loop]]) keeps its
*reasoning* layer — the project knowledge base — from accumulating stale notes forever.
This is the reasoning-layer counterpart to [[loop_repo_compounding]] (which fixed the
*artifact* layer). Where that doc made code compound on `main`, this one makes knowledge
**converge** instead of monotonically growing.

> **Status:** v1 IMPLEMENTED (uncommitted on `develop`), 2026-06-26 — **unit-verified**
> (`tests/test_kb_convergence.py`, 24 passing; full suite + ruff format clean, no
> regressions) **and k3d E2E-VERIFIED** (see [k3d E2E](#k3d-e2e-results-2026-06-26)). Fixes
> [[loop_review]] **F13** (the keystone of the reasoning cluster), **subsumes F22**,
> **de-risks F23** (still deferred pending the next-run evidence), and treats **F24** as a
> v2 prerequisite (the relevance anchor).
>
> **Deferred from this v1 cut** (both noted below): relaxing the ADD-pass dedup (optional
> prompt tuning — duplicates would otherwise linger until TTL) and removing the vestigial
> curator-subjob code (orthogonal cleanup; the dead `_trigger_curation_final_pass` harmlessly
> no-ops, so it was kept out of the functional change).

## Problem (F13)

The loop coordinates iterations through a shared knowledge base ("blackboard"). For the
loop to *compound*, knowledge has to converge: a new "acceptance criteria" note should
retire the old one; a rejected proposal should stop competing in search; duplicates should
merge. None of that happens.

[[loop_review]] **F13** measured it: `knowledge_index` notes are **100 % `active`,
0 superseded/archived**. The reason is structural — the convergence machinery exists, but
on the **wrong store**:

| Store | Lifecycle machinery | Result |
|-------|--------------------|--------|
| **`memories`** (RecallStore) | bi-temporal supersede (`valid_to`/`superseded_at`/`superseded_by`, migration `vector/0006`), `remaining_turns` TTL (default 10, `recall_store.py:285`), per-write verdict (ADD/NOOP/UPDATE/MERGE), importance gate | converges; ~72 % retired in the loop run |
| **`knowledge_index`** (KnowledgeStore) | **none.** `upsert_note` (`knowledge_store.py:133`) is a pure write-through keyed on `(project_id, note_id)`; it writes whatever `status` the caller passes and never looks at neighbours | never converges; 100 % active |

So every stale governance note ("acceptance criteria for iteration N", "current state",
non-selected proposals) stays `active` and competes in every future hybrid search forever.
That is the mechanism behind the loop's non-compounding, and it compounds the read-churn /
near-duplicate problem (F18: *3 competing "definition-of-done" notes*) and the injection
problem (F23: similarity-ranking over a polluted active set).

## Why tools cannot fix this

The KB exposes plenty of *capabilities* for hygiene — `kb_update` (set `status=superseded`),
the `SUPERSEDES`/`CONTRADICTS` links, the read-only `kb_contradictions` view. They don't
help, because **staleness is a background-accumulation problem and every tool is an opt-in
action the agent must choose to take.** [[loop_review]] **F22** is the proof: the critic
*had* the affordance to mark the 4 non-selected proposals `superseded` and didn't — it
ranked them "useful but narrower" and moved on. `kb_contradictions` is worse than it looks:
it only *lists* `CONTRADICTS` edges an agent already hand-declared (`knowledge_graph.py:558`,
filtered to active–active) — it does not *detect* anything. In the reviewed loop it would
return "No active contradictions found" essentially always, because nobody drew the edges.

**Conclusion: convergence must be a system-managed background process, not a tool.** This is
exactly the lesson the memory overhaul already applied to `memories` — and the mechanism is
already in the codebase.

## The reframe — the curator is a populate-only "extractor"; it's missing its "assembler"

Tracing how the curator actually runs (the README/config call it a "subjob" — that's stale
documentation; see [Vestigial code](#vestigial-code-to-remove)) shows it is **not a subjob**.
It is an **inline auxiliary-LLM agent task**, `CurateKnowledgeTask` (`auxiliary.py:413`),
that runs in-process at each phase boundary (`graph.py:2464`, fired async via
`asyncio.create_task`), with `kb_search/write/update/read`, capped at 15 tool iterations,
gated on `curator.enabled` (default **off**, `defaults.yaml:322`) + `auxiliary.enabled` + KB
present. It explicitly *"replaces the curator subjob"* (`auxiliary.py:416`).

It is **populate-only.** Its prompt and structured output (`notes_created`, …) are about
*extraction*. It has `kb_update` but is not driven to converge.

The fix is obvious once you see the **memory side already has both halves**:

| Layer | ADD pass (populate) | CONVERGE pass (curate/retire) |
|-------|---------------------|-------------------------------|
| **Memory** (`memories`) | `ExtractMemoriesTask` — `"memory_extraction"` (`auxiliary.py:228`) | `AssembleMemoriesTask` — `"memory_assembly"` (`auxiliary.py:470`): *"adjusts TTLs: boost relevant ones, deprecate stale ones."* |
| **KB** (`knowledge_index`) | `CurateKnowledgeTask` — `"knowledge_curation"` (`auxiliary.py:413`) | **MISSING** ← this feature adds it |

`auxiliary.py:1337` states the memory split outright: *"[the extractor] creates new
memories, the assembler curates existing ones."* **The KB only ever got the extractor half.**
F13 is, precisely, "give the KB its assembler."

## Design

### Two tasks, not one two-phase run

Add a second aux task — **`AssembleKnowledgeTask`** (`"knowledge_assembly"`), the structural
counterpart to `AssembleMemoriesTask` — rather than overloading `CurateKnowledgeTask` with a
second internal phase. Separate tasks buy us, exactly as on the memory side:

- **Separate prompts** — "extract what's new from this phase" and "is old note X still
  valid?" are different cognitive jobs; one 15-iteration loop juggling both dilutes both.
- **Separate gates** — ADD runs whenever curation is on; CONVERGE runs **only when something
  actually expired** (empty stale queue → skip → zero aux-LLM cost).
- **Separate audit/health** — distinct `"knowledge_assembly"` success/failure signals and a
  clean "N added / M refreshed / K retired" readout.

(Naming note: `CurateKnowledgeTask` is really the *extractor* despite its name; the new task
takes the `Assemble*` name to mirror `AssembleMemoriesTask` and make the symmetry explicit.)

### Division of labor — this is what removes the F22 dependency

- **ADD (`CurateKnowledgeTask`)**: capture **liberally**. Relax its current soft-dedup
  burden ("`kb_search` before writing, avoid duplicates") — write what you find.
- **CONVERGE (`AssembleKnowledgeTask`)**: the **only** place supersede / merge / retire
  happens, system-managed.

Convergence stops depending on any agent (or the populate pass) *remembering* to mark things
superseded — which is the discipline F22 proved fails. It becomes a guaranteed background
process.

### Verdicts (per TTL-expired note)

`AssembleKnowledgeTask` processes the stale queue and, per note, decides — mirroring the
memory assembler / RecallStore verdict set:

- **REFRESH** — still valid → reset TTL (and optionally bump confidence / `modified_at`).
- **SUPERSEDE** — replaced by a newer same-purpose note → `status=superseded`
  (+ `superseded_by`), drop from default retrieval.
- **MERGE** — duplicate/overlap → consolidate into one note, supersede the rest.
- **RETIRE/ARCHIVE** — no longer relevant and nothing replaced it → `status=archived`.

Never hard-delete: KB notes are deliberate authored artifacts (unlike passively-extracted
memories), so the contract is **TTL → re-verify, not TTL → delete.**

### TTL model

- **Unit = cycles, not wall-clock.** Mirror `memories.remaining_turns`. A note is stale
  because the project moved N cycles, not because a weekend passed. Wall-clock would expire
  notes over an idle weekend and keep stale ones alive during a busy hour.
- **`note_type`-aware** (the load-bearing refinement). The existing note types
  (`knowledge_store.py:399-404`: `decision`, `learning`, `question`, `goal`, `code`,
  `state`, plus `plan`/`retrospective`/`source` from the curator) do not age uniformly:

  | Class | Types | TTL policy |
  |-------|-------|-----------|
  | **Moving-target** (snapshots of a moving project) | `state`, `goal`/DoD, `plan`, `question` | **short TTL → re-verify** |
  | **Durable** (facts) | `decision`, `learning`, `code` | **long / no TTL**; only superseded by an explicit newer decision |

  A uniform TTL would either churn-re-verify durable facts (cost) or let stale state linger.
- **Convergence escape (anti-thrash):** a note REFRESH'd repeatedly should be promoted to
  no-TTL ("confirmed durable"), mirroring how `importance` promotes memories. Plus a
  **no-LLM pre-filter** that retires without a model call: superseded-by-a-newer-same-type,
  or zero reads in N cycles.

### Cadence & where each step runs

Three steps at three frequencies — only one is an LLM call:

| Step | Frequency | Mechanism | Where |
|------|-----------|-----------|-------|
| **ADD** | every phase boundary | `CurateKnowledgeTask` (existing) | in-agent (`graph.py:2464`) |
| **TTL decrement** | once per cycle | plain SQL `UPDATE … remaining_cycles = remaining_cycles - 1` (deterministic, **no LLM**) | orchestrator at loop-advance (`_advance_project_loop` / `_spawn_loop_job`, `main.py:9578`) |
| **CONVERGE** | per cycle, **gated on non-empty stale queue** | `AssembleKnowledgeTask` (new) | in-agent, at the **next job's first phase boundary** |

The one reconciliation worth calling out: TTL cadence is *cycles* (orchestrator-side) but the
aux seam fires at *phase boundaries* (in-agent). Resolution: **decrement at the cycle
boundary; run CONVERGE in the next job's first phase boundary, gated on the stale queue.**
This keeps everything inside the existing inline-aux seam — **no resurrected subjob** — while
giving true per-cycle cadence. Most phase boundaries see an empty queue and skip the pass
entirely.

## Schema changes

One migration under `orchestrator/database/migrations/vector/` (mirrors `memories`):

- `knowledge_index`: add `remaining_cycles INT` (nullable; `NULL` = no-TTL/durable),
  optional `last_verified_cycle INT`. Partial index on `remaining_cycles <= 0` (the stale
  queue), mirroring `idx_memories_ttl_active`.
- Neo4j `Note`: same TTL property so the graph and the search index stay in sync (writes go
  through `kb_write`/`kb_update`, which already write both stores).
- TTL defaults assigned by `note_type` at write time (the type→policy table above).

No edit to `vector_schema.sql` (frozen at cutover — see `docs/db_migration.md`).

## Vestigial code to remove (streamlining)

The trace turned up dead scaffolding from the pre-inline "curator subjob" era. Removing it is
part of this feature (it's what makes the docs match reality):

- `orchestrator/main.py:9754` `_trigger_curation_final_pass` + its "find a **waiting
  curator**" query (`main.py:9781`). Nothing creates that waiting curator (there is a
  `_spawn_scholar_subjob` but no `_spawn_curator`), so it always logs "No waiting curator
  found" and returns. Dead.
- `config/experts/curator/` standalone expert (full tool set + persistent-subjob
  `instructions.md`) — superseded by `CurateKnowledgeTask`. Keep `curation_instructions.md`
  only if the inline task still loads it (`completion.py:449`); otherwise drop.
- README "Curator … runs as a subjob after other jobs complete" and the curator config
  `description` "persistent subjob in parallel" — both describe the removed design; rewrite
  to "inline auxiliary-LLM task at phase boundaries."

## Relationship to the loop_review cluster

- **F13** — fixed (this is the fix).
- **F22** (no tried/rejected ledger) — **subsumed.** Marking the critic's non-selected
  proposals `superseded` becomes one verdict in the CONVERGE pass; the critic no longer has
  to remember to do it.
- **F23** (similarity injection buries the load-bearing note) — **de-risked, still
  deferred.** Injection ranks over a *converged* set once this ships, so the load-bearing
  note is no longer competing with stale duplicates. F23's own pinning fix remains gated on
  the next-run evidence (does the agent self-serve via `kb_search`?).
- **F24** (no project acceptance vector) — **v2 prerequisite.** "Is note X still *relevant to
  the goal*?" needs an anchor of truth, and it can't be the `state` notes themselves
  (circular). That anchor is F24's capability checklist.
- **F18** (read-churn + near-duplicate notes) — symptom resolved once MERGE runs.

## Phasing

- **v1 — no F24 needed. ✅ SHIPPED** (uncommitted on `develop`; unit + k3d E2E-verified):
  `AssembleKnowledgeTask` + `assemble_and_converge_knowledge` runner; `remaining_cycles` +
  `last_verified_cycle` columns (migration `0007`) with type-aware defaults; per-cycle
  decrement at the loop cycle-wrap; CONVERGE = supersede / merge / refresh / archive
  (note-vs-note, no external anchor); curation enabled for loop jobs (`project_loops.py:173`).
  **Deferred within v1** (low-risk follow-ups): relaxing the ADD-pass dedup, removing the
  vestigial subjob code, and stamping `current_cycle` on refresh.
- **v2 — after F24.** CONVERGE additionally judges *relevance to the project acceptance
  vector* (RETIRE notes that no longer map to any open capability), using the F24 checklist
  as the anchor.

## Implementation (v1 — shipped)

Files changed (all uncommitted on `develop`):

- `orchestrator/database/migrations/vector/0007_knowledge_index_ttl.sql` *(new)* — TTL columns + stale-queue partial index.
- `src/services/knowledge_store.py` — `KB_TTL_BY_NOTE_TYPE` + `_ttl_for_note_type`; `upsert_note` sets `remaining_cycles` on INSERT (ON CONFLICT preserves it); `decrement_ttl` / `get_stale_notes` / `refresh_ttl`.
- `src/services/auxiliary.py` — `KnowledgeAssemblyResult`, `AssembleKnowledgeTask`, `_TASK_CALL_TYPES["AssembleKnowledgeTask"]="knowledge_assembly"`, `assemble_and_converge_knowledge` runner (stale-queue gated, deterministic survivor refresh).
- `config/prompts/knowledge_assembler_prompt.txt` *(new)* + `config/model_config_matrix.yaml` + `src/core/loader.py` (`HARDCODED_DEFAULTS`) — register the convergence prompt.
- `src/graph.py` — load the prompt, thread it through `create_archive_phase_node`, fire CONVERGE async at each `archive_phase` (same gate as the populate pass).
- `orchestrator/main.py` — per-cycle decrement in `_advance_project_loop` when the rotation wraps (`next_index == 0`), via `vector_db`.
- `orchestrator/services/project_loops.py` — `curator.enabled = True` for loop jobs.
- `tests/test_kb_convergence.py` *(new)* — 24 unit tests.

## Acceptance criteria

1. **✅ MET** — `superseded` moved **0 → 4** on the E2E project; merge/supersede of
   governance notes exercised (4 stale `state` notes superseded). (`archived` path coded but
   not exercised in this run.)
2. **✅ MET** — gate verified: the runner returns before any aux-LLM call when
   `get_stale_notes` is empty (unit test + the no-op-on-empty Tier-1 behaviour).
3. **✅ MET** — `decision`/`learning`/`code` get `remaining_cycles = NULL` (durable), never
   in the stale queue; confirmed E2E (new `decision` notes wrote NULL TTL).
4. **✅ MET (mechanism)** — CONVERGE superseded stale notes autonomously, no critic action
   required. The specific critic-rejected-proposal flow isn't separately exercised yet.
5. **⏸ DEFERRED** — vestigial `_trigger_curation_final_pass` / `config/experts/curator/` not
   yet removed; README/config text not yet rewritten (the dead path harmlessly no-ops).
6. **⏸ PENDING** — per-cycle aux-LLM cost not yet measured on a real loop (ties to F14).

## Open questions / risks

- **Cost** — CONVERGE is an agentic aux loop. Bounded by the empty-queue gate, type-aware
  TTL, and the no-LLM pre-filter, but measure per-cycle spend (F14 already flags loop cost).
- **TTL defaults per type** — need tuning on real runs; start conservative (longer TTLs) to
  avoid premature retirement, tighten later.
- **Non-loop jobs** — a one-shot job has no "cycle" and notes don't go stale within it;
  CONVERGE is inherently loop/project-scoped. Decrement should be a no-op outside a loop.
- **Stale-queue visibility** — the in-agent CONVERGE pass must query `knowledge_index` for
  `remaining_cycles <= 0` scoped to the project; confirm the aux task has project-scoped DB
  access at that point (it already has `tool_context` with the KB).

## k3d E2E results (2026-06-26)

Verified on the local k3d cluster (Tilt-built agent image confirmed to carry the new code).

- **Migration `0007`** applied at orchestrator startup ("schema up to date (7 applied) in
  vector"); `remaining_cycles` + `last_verified_cycle` columns and `idx_knowledge_index_stale`
  present on real pgvector. Existing notes stayed `remaining_cycles = NULL` (durable) — the
  migration retired nothing on its own.
- **SQL contracts** (isolated throwaway project): the orchestrator decrement
  (`-1 WHERE remaining_cycles IS NOT NULL AND status='active'`), the stale-queue predicate
  (`<= 0 AND active`), `refresh_ttl` (reset + `last_verified_cycle` stamp), and the
  **supersede-skip invariant** (a `superseded` row is never refreshed) all behaved exactly as
  designed.
- **TTL-on-write (real agent curation path):** a curation-enabled job's new notes received
  type-aware TTLs — `state`→2, `goal`→3, `decision`→NULL — via `KnowledgeStore.upsert_note`.
- **Converge pass (the keystone):** with 6 `state` notes seeded stale, the job's
  `archive_phase` fired `AssembleKnowledgeTask` (real aux-LLM, real Neo4j+pgvector
  write-through). It **superseded 4** stale exploration snapshots and **refreshed 2** it
  judged still valid (`rc` 0→2). The project's `superseded` count moved **0 → 4** — the F13
  acceptance metric. Superseded notes drop out of injection automatically (the
  `status='active'` filter in `knowledge_hybrid_search`).

Test acceleration vs. a natural multi-cycle loop: the stale queue was seeded directly
(`remaining_cycles=0`) rather than produced by N real cycle decrements — but the decrement
itself is independently verified above, and the converge consuming that queue is identical to
what a real cycle produces. `last_verified_cycle` stayed NULL (the deferred `current_cycle`
wiring) — refresh's functional part (TTL reset) worked regardless.
