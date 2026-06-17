---
tags:
  - feature
  - architecture
  - memory
  - memory-manager
  - recallstore
  - knowledge-base
  - retrieval
  - evaluation
aliases:
  - memory overhaul
  - MemoryManager
  - memory abstraction layer
  - passive memory
  - RecallStore redesign
  - memory eval harness
related:
  - "[[agent_memory_current_state]]"
  - "[[headless_persistent_sessions]]"
  - "[[persistent_session_history_windowing_and_compaction]]"
  - "[[agent_open_source_split]]"
---

# Agent Memory Overhaul — the MemoryManager

> A **passive, model-independent memory layer**: one `MemoryManager` abstraction that
> assembles the right memories for every LLM call — the model is a *consumer*, never the
> manager. Build the seam first; everything else becomes a plugin behind it.

**Status:** Design v2 / **Phases 1–4 implemented + measured; GATE-B stack
flipped on, shipping to dev (2026-06-14)**. A 7-phase overhaul; **four phases
done + the production stack now active in the defaults YAML (dev rollout for
real-session validation); GATE A soak/delete + Phases 5–7 remain.**
The map:

- **Phase 1 (MemoryManager seam) — code complete, flag ON, committed.** Both
  graphs route through `assemble()`/`capture()` behind `memory.manager.enabled`
  (on in both defaults files); the live k3d verify PASSED in both modes (three
  catches found + fixed 2026-06-11 — dispatch round-trip flag loss, session
  config_name plumbing incl. the dual-app attach route, k8s ✕-route via
  orchestrator detach-then-delete). **Two operational steps remain — GATE A:**
  **step 3 soak on dev** (passive; the flag is the one-line rollback, watch the
  seam failure surface) → **step 4 delete the legacy `memory_service is None`
  blocks** (the "zero direct store calls" acceptance is the post-deletion state;
  the equivalence suites become reference copies).
- **Phase 2 (eval harness) — ✅ COMPLETE (2026-06-12).** `eval/memory/` drives
  the seam offline against LongMemEval_S; harness + LLM judge + contradiction
  probe, all three acceptance gates closed. Baseline: the current production
  pipeline (`persistent_current`) retrieves **R@5 0.20 / answers 0.25–0.30** vs
  **0.94 / 0.74–0.76** for trivial verbatim storage of the same history
  (`flat_verbatim`) — the quantified case for Phases 3–5. Offline; never
  touches what the soak exercises.
- **Phase 3 (first plugin wave) — ✅ COMPLETE (2026-06-12), NOT yet rolled
  out.** Four measured slices took the seam from R@5 0.20 → **1.0 at 3.7 items
  / 111 tokens per question (80× cheaper than the full dump)**, end-task 0.40 ≥
  the full-dump 0.45 (the entire remaining gap is one knowledge-update question
  = Phase 4), reader abstention perfect. The winning stack — `reranker` scorer
  + relative `gate` + `bounded`-10 — is built, registered, and harness-validated
  but **inert by default** (nothing in the defaults-YAML pipeline). **GATE B:**
  the production flip is a separate decision pending real-session evidence —
  the harness is N=20 synthetic with a gemma reader, not the product workload.
- **Phase 4 (lifecycle supersede) — ✅ COMPLETE + MEASURED (2026-06-14), NOT
  yet rolled out.** Bi-temporal columns + retrieval filter (migration
  `vector/0006`), ingestion verdicts ADD/UPDATE/MERGE/NOOP via the aux LLM
  (`memory.ingestion.enabled`, default off) with retire-and-exclude supersede,
  and a `write_gate` completeness toggle — all inert by default, full suite
  green. **The measured win (the thing no retrieval policy could fix):**
  contradiction probe `original_injected` 1.0 → **0.25** (stale fact retired,
  not just out-ranked) + reader current 1.0; and on the N=20 end-task slice
  **knowledge-update 0/4 → 3/4** with **overall 0.40 → 0.50**, R@5 and tokens
  unchanged, 14 % of the organic corpus retired. KU recovered past its 0.5
  target. **GATE B flipped 2026-06-14** (the Phase-3+4 stack is now in both
  defaults YAMLs), **shipping to dev** for real-session validation before prod —
  the harness evidence is N=20 synthetic / gemma-reader, so dev is where
  reranker latency + aux load under real traffic get proven.
- **Phases 5–7** — ablate-and-cut (find + delete cargo-cult consolidation),
  buckets productized (personal/shared + cockpit panel), frontier verdicts
  (graph-keep, learned scorer). Decision-gated or dependent on Phase 4.

**Open items after Phase 4** (none block the measured wins; all are rollout or
follow-up, not core correctness):
- **The two rollout gates:**
  **GATE A** = Phase-1 soak on dev → delete the legacy `memory_service is None`
  blocks (still pending — needs the soak).
  **GATE B** = ✅ **flipped 2026-06-14** — both defaults YAMLs now carry the
  measured Phase-3+4 stack (`scorers: [reranker]`, `policies: [gate, bounded]`,
  `memory.ingestion.enabled: true`, `extraction.write_gate: false`).
  **Shipping to the dev cluster first** for real-session validation — the one
  thing the offline harness can't show is **reranker latency + the added aux
  load under real traffic** (every assemble now reranks, every store may run a
  verdict, and the router flaps). The legacy fallback stays in place behind the
  flags, so rollback is a one-edit revert. Prod only after dev looks clean; the
  N=20-synthetic / gemma-reader caveat is unchanged until then.
- **GC / retention (B6) is NOT closed by Phase 4.** Supersede *retires, does not
  delete* (sets `valid_to`) — by design for point-in-time history, but it means
  the table still grows (supersede actually *adds* a row: the new fact plus the
  retired old one). A GC job that deletes rows retired beyond a retention window
  — keying off the new `valid_to`/`superseded_at` columns — remains future work
  (loosely mapped to Phase 4 originally; now a Phase-4 follow-up / folds into
  Phase 5's ablate-and-cut).
- **`review_floor` tune** (the Phase-4 residual): the 2/8 contradiction misses +
  the 1/4 KU miss were old/new pairs phrased below the 0.6 similarity floor, so
  the verdict ADD'd them as distinct instead of superseding. A floor and/or
  extraction-phrasing tune could close them — cheap follow-up, measured on the
  same harness.
- **Two user-side measurement items**: judge calibration is **93.1 % vs the
  97 % target** (needs a ~100-item label pass), and the request digest's
  windowing payoff is **unmeasured** (only question-time parity is proven —
  needs an ingest-time A/B or a production soak).
- **B9 enum nits**: the `memory_type`/`source` CHECK constraints still carry the
  pre-overhaul enum values; fold into the next constraint-touching migration.

Restructured 2026-06-10 around the MemoryManager abstraction after design
alignment (v1 of 2026-06-07 was phase-first; superseded, phases preserved
below in new order). Every bug worth fixing *before* the seam is fixed
(B1/B2/B4 + logging); the remaining bugs are absorbed by phases by design.

**Implementation log:**
- **2026-06-10 — Step 0 (ground-clearing) merged to develop** as #111 + #112:
  - **B1 fixed** (#111): persistent extraction repaired — matrix-resolved prompt
    threaded from session setup into the loop + both teardown sites, real
    `observer_interval` cadence, re-resolution on `config.update`; 10 regression
    tests against the real `MemoryConfig` (`tests/test_persistent_memory_extraction.py`).
  - **B2 verified**: **zero HNSW indexes on dev AND prod** (pgvector 0.8.2) —
    all dense retrieval is a seq scan today; halfvec expression-index fix viable,
    not yet implemented. `ef_search` tuning (Phase 3) is gated on that migration.
  - **Dead-code sweep + B9 + B3 honesty** (#112, ~2,300 lines): memory_observer,
    memory_migrator, dead search helpers, `_load_query`, their tests; dead
    `MemoryConfig` knobs deleted from code + both YAMLs; persistent
    `assemble_memories.enabled: false` (config no longer lies).
- **2026-06-11 — B1 live-verified on k3d** (real session over the agent WS,
  5 turns + `/done`): in-loop trigger fired at `observer_interval`,
  `/done` teardown extracted+stored 5/5 `source='observer'` rows attributed
  to the thread, `Final memory extraction complete` logged for the first
  time ever. One aux-router timeout on the in-loop task (non-fatal, known
  flaky backend) — details + two small follow-ups in `memory_bugs.md` B1.
- **2026-06-11 — B2 fixed + k3d-verified** (migrations `vector/0002–0005`):
  dense channels of all five hybrid-search functions now order by
  `subvector(embedding, 1, 4000)::halfvec(4000)` with matching HNSW
  expression indexes (halfvec HNSW caps at 4000 dims — the doc's
  `halfvec(4096)` idea was not viable; qwen3's MRL training makes the
  4000-prefix rank-identical in testing). Premise correction: scoped
  btrees meant dense retrieval was btree+sort per scope, not a table seq
  scan — the indexes are the planner-gated hedge for scope growth.
  `hnsw.iterative_scan = relaxed_order` pinned per function. Phase 3's
  `ef_search` tuning is unblocked.
- **2026-06-11 — B4 fixed + pre-flight complete**: dimension guard inside
  `EmbeddingService` (per-response check vs `EMBEDDING_DIMENSIONS`, one
  ERROR + latched fail-fast `EmbeddingDimensionError`, background
  `verify_dimensions()` probe at both RecallStore init sites, health on
  both status endpoints; `TestDimensionGuard`, 8 cases). Plus the B1
  follow-up: the four memory catches in `auxiliary.py` log
  `type(e).__name__` so openai errors stop logging as empty strings. B9's
  enum nits deferred into the next migration touching the constraint
  (Phase 4 gc). Remaining bugs (B3/B5–B8/B10/B11) are deliberately left
  for their designed phases — the old code stays frozen so Phase-1
  equivalence fixtures pin unmodified behaviour (mapping in
  `memory_bugs.md` §Suggested order).
- **2026-06-11 — Phase 1 slice 1 (kernel) shipped**: `src/services/memory/`
  package — types (§2.1 vocabulary: `AssembleRequest`/`MemoryPayload`/
  `InjectionBlock`/`AssembleStats`/`CaptureEvent`/`MemoryRuntime`), plugin
  protocols, `MEMORY_PLUGIN_REGISTRY` (TOOL_REGISTRY-style import-time
  registration + loud `UnknownMemoryPluginError`), and the `MemoryManager`
  binder (`from_config` resolves `memory.pipeline` names; `assemble()`
  orchestrates retrievers→scorers→policies order-preserving with per-plugin
  containment + stats; `capture()` dispatches by event kind, never raises).
  `MemoryConfig` gained `manager_enabled` (`memory.manager.enabled`, the
  cutover guard, default off) + `MemoryPipelineConfig`. Nothing constructs
  the manager in production yet; no YAML entries until cutover (no config
  theatre). 30 kernel tests (`tests/test_memory_manager.py`).
  **§2.1-sketch refinements locked:** `Retriever.retrieve(req)` takes the
  whole request, not `(query, bucket, k)` — v1 buckets layer over the
  already scope-bound stores, per-bucket fan-out is Phase-6; `CaptureEvent`
  carries no scope refs (manager is constructed per job/session, already
  scoped); explicit `Policy` protocol added for the pipeline's `policies`
  stage. The §6 pipeline-sketch names (`dense`, `sparse`, `rrf`, …) describe
  the Phase-3+ decomposition; the slice-2 transplant registers honest
  composite names instead (RRF lives inside the SQL functions — splitting it
  into four pseudo-plugins would be ceremony, not a transplant).
  Remaining slices: 2 worker read path + equivalence fixtures, 3 persistent
  parity, 4 capture() write path, 5 cutover behind the flag.
- **2026-06-11 — Phase 1 slice 2 (worker read path transplant) shipped**:
  `plugins/legacy.py` registers `recall_two_tier` (wraps the two-tier
  `RecallStore.retrieve()` *including* the decrement-then-retrieve TTL tick
  with its own containment) and `kb_notes` (wraps `hybrid_search`,
  `match_count=5`; passes `project_ids=[…]` which the store normalizes to
  the same single/multi SQL paths as both legacy calling conventions —
  one retriever serves both graphs). `build_worker_query_text(TaskFrame)`
  transplants the top-todo+phase query formation. `_render_blocks` now
  carries the legacy injection mechanics verbatim: `assemble_memory_block`
  / `assemble_knowledge_block` called with `model=` only (never a budget —
  byte-equivalence; the KB block stays uncapped until Phase 3/B5) + the
  synthetic `recall_memories`/`kb_search` tool pairs. **Equivalence
  fixtures** (`tests/test_memory_worker_equivalence.py`, 14 cases):
  manager payload vs a verbatim reproduction of graph.py:888-1037 —
  normalized-id byte comparison, golden block snapshots, store
  call-signature pins (decrement-before-retrieve order, positional query,
  no budget kwarg, `match_count=5`), conditional parity (no store / no
  scope / empty results / retrieval failure / TTL-tick failure), model
  threading, stats counts. graph.py untouched — the legacy path stays
  live until cutover. Next: slice 3 (persistent parity).
- **2026-06-11 — Phase 1 slice 3 (persistent parity) shipped**:
  `build_persistent_query_text(messages)` (last HumanMessage, str-coerced,
  "" when none — the legacy path retrieves with "" rather than skipping)
  + `MemoryRuntime.retrieval_timeout` reproducing the persistent path's
  per-store-call 5 s guard *per call* (None = worker/unbounded): each
  retriever bounds its own awaits via `_bounded`, so a hung memory lookup
  still never starves the KB lookup, and a timed-out decrement never
  blocks retrieval — same outcomes as the legacy split `wait_for`s.
  Shared fixtures extracted to `tests/_memory_fixtures.py` (the
  `_fs_backend.py` pattern); **persistent equivalence suite**
  (`tests/test_memory_persistent_equivalence.py`, 11 cases) pins the
  payload against a verbatim reproduction of persistent_graph.py:527-659:
  byte parity, identical store await-signatures legacy-vs-manager
  (incl. multi-project UUID list), empty-query parity, and timeout parity
  (memory-timeout→KB proceeds, KB-timeout→memory proceeds,
  decrement-timeout→retrieve proceeds, None→`wait_for` never called).
  Deliberately call-site (cutover's concern): pair *insertion position*
  (persistent inserts after the SystemMessage; the payload is
  position-agnostic) and the per-inner-iteration pair re-creation —
  content is invariant within a turn, only the synthetic id suffix
  differs, and nothing consumes those ids beyond prefix checks.
  persistent_graph.py untouched. Next: slice 4 (capture() write path).
- **2026-06-11 — Phase 1 slice 4 (capture() write path) shipped**: seven
  writers in `plugins/legacy_writers.py`, one per legacy call site (full
  site→writer map in its module docstring): `interval_extractor` (worker
  in-loop, *modulo* gate via the real `_should_extract_memories`, gap
  window, aux-task-flag gate), `persistent_interval_extractor` (*elapsed*
  gate, fixed-width window, no phase kwarg, no task-flag gate — the
  per-mode asymmetries are real legacy behaviour, preserved as two
  registered writers the per-mode YAML pipelines pick between),
  `phase_boundary_extractor`, `teardown_extractor` (session_end +
  idle_archive, legacy log lines kept so the k3d B1 signals stay
  greppable; B11's `_terminate_session` lands on it at cutover),
  `memory_assembler` (TTL curation, reads
  `extra["current_injection_text"]`), `compaction_memory` (the
  graph.py:842 compaction-summary store — a sixth write site the doc's
  list missed), `queued_memory` (the graph.py:3438 drain of
  todo_complete-queued memories = the doc's "todo_complete queuing").
  `CaptureEvent` gained `turn_count`; `MemoryRuntime` gained
  `auxiliary_config` + `assembler_prompt` (prompts read at event time —
  persistent re-resolves on config.update). Interval state is
  writer-internal: matches persistent exactly (loop-local, resets to 0
  even on resume — legacy does too); for the worker it replaces
  checkpointed `last_observed_turn`, so a resume can only *widen* one
  extraction window (completeness>precision, capped by
  `_MAX_OBSERVATION_WINDOW`). State advances at trigger regardless of
  task outcome (the documented B1 follow-up; teardown is the net).
  `trigger_phrase_gen`/`cosine_dedup` from the §6 sketch are NOT separate
  writers — they already ride inside ExtractMemoriesTask /
  RecallStore.store. **Equivalence**
  (`tests/test_memory_capture_equivalence.py`, 21 cases): per-site
  verbatim legacy reproductions vs writers over identical event
  sequences with spy await-list equality (REAL gates + REAL loader
  configs), kwarg-shape pins (worker passes phase+window, persistent no
  phase, boundary no window, teardown exactly 4 kwargs), gate/disable
  cases, per-item drain containment, manager kind-routing. Known timing
  delta, accepted: legacy fires extraction+assembly as parallel
  create_tasks; capture() awaits writers sequentially inside one
  background task — same calls, serialized (arguably kinder to the
  flaky aux router). Graphs/persistent_app untouched.
  Next: slice 5 (cutover behind `memory.manager.enabled`).
- **2026-06-11 — Phase 1 slice 5: the cutover, wired behind the flag
  (default off).** Both graphs now construct and route through the
  manager when `memory.manager.enabled` is true. **Construction**:
  worker in `build_phase_alternation_graph` (graph.py, right after the
  store extraction — all deps in scope: stores via tool_context with
  the legacy `has_knowledge()` gate, aux LLM post-fallback-wrap, both
  matrix-resolved prompts, `retrieval_timeout=None`); persistent at the
  end of `PersistentSession._setup_memory` (`retrieval_timeout=5.0`, no
  assembler prompt; threaded into `run_persistent_loop` →
  `_execute_turn` as a new `memory_service` param). The instance is
  named `memory_service` throughout — `memory_manager` is taken in
  graph.py by the vestigial workspace.md manager. Bind failures
  (unknown pipeline name) deliberately **raise at setup** — a
  misconfigured cutover must fail loudly, not limp on the legacy path
  (the aux-outage lesson applied to construction). The persistent
  `config.update` handler keeps the runtime in lockstep
  (`auxiliary_llm` + `extraction_prompt` mutated for the B1 hot-swap;
  `memory_config` deliberately NOT re-pointed — the legacy loop
  freezes its interval at loop start). **Gating pattern**: every legacy
  block stays byte-identical and gains a `memory_service is None`
  guard term, with the manager branch alongside — read swaps splice
  `payload.messages()` at the exact legacy positions (worker: inside
  `_inject_transient_messages` after the todos message, so safety
  rebuilds re-splice; persistent: after the SystemMessage each
  inner-loop iteration), and the six write sites emit their
  CaptureEvents (worker turn_end carries `turn_count`/`phase`/the
  payload's memory-block text as `current_injection_text`; compaction
  carries the summary; the drain emits only when the queue is
  non-empty). The legacy debug signals ("Memory injection: N memories
  retrieved", "Knowledge injection: N notes retrieved") are re-emitted
  at the manager call sites so the k3d greps stay valid; the worker
  `memory_inject` audit_step keeps its legacy `{count, total_tokens}`
  shape fed from the payload's memory block and additionally carries
  `stats` (= `AssembleStats.to_dict()` — the eval-harness/cockpit tap).
  **B11 closed at flag-on**: `_terminate_session_inner` gained a
  guarded, awaited `capture(session_end)` for ALL terminate reasons
  (contained like its sibling teardown steps — memory must never skip
  cleanup); `_handle_archive`/`_handle_idle_archive` set the
  `final_memory_extracted` session flag so archive→terminate never
  double-extracts. **Config**: the YAML pipeline defaults landed
  (defaults.yaml: recall_two_tier+kb_notes / interval_extractor,
  phase_boundary_extractor, memory_assembler, compaction_memory,
  queued_memory; persistent_defaults.yaml:
  persistent_interval_extractor, teardown_extractor) with
  `manager.enabled: false`. **Tests**
  (`tests/test_memory_cutover.py`, 19 cases): YAML defaults parse +
  every shipped name binds (registry drift guard), construction
  runtime-field pins for both modes (+ flag-off never constructs),
  execute-node drive (query formation, splice position, legacy-store
  spies untouched, audit shape, turn_end + compaction events,
  interval-state keys absent from results), archive/tools-node event
  pins, `_execute_turn` insertion pin, teardown + B11
  exactly-once/guard pins, and **B10**: every message `assemble()`
  emits must satisfy `is_workspace_injection_message` (manager renders
  through the same `create_*_injection_messages`, and unrenderable
  kinds contribute provenance-only blocks). Legacy-path test doubles
  gained explicit `memory_service=None` (6× MagicMock archive sessions,
  1× SimpleNamespace REST session, the B1 teardown `_make_session` —
  the MagicMock-fabrication pattern; any future session-shaped mock
  hitting memory paths needs the same). Remaining for Phase-1 closure:
  k3d verify → flip → soak → delete legacy — step-by-step commands and
  pass signals in the **Phase-1 closure runbook** under §5 Phase 1.
- **2026-06-11 — Phase 2 slice 1 (harness skeleton) shipped +
  smoke-verified on real infra**: `eval/memory/` — LongMemEval loader
  (typed records, committed tiny fixture, type-stratified subsetting),
  the LongMemEval→seam mapping (question=project, haystack
  session=job → memory `job_id` IS retrieval provenance; fresh uuid5
  scopes per run), two ingestion modes (**seam**: production-faithful
  replay emitting the exact persistent-loop assemble/capture sequence,
  TTL dynamics included; **verbatim**: flat round-granularity index,
  dedup off via `dedup_threshold: 1.01`, `remaining_turns=0` → pure
  hybrid retrieval — the published-baseline + smoke arm), question-time
  assemble + provenance collapse, Recall@k/NDCG@k/coverage/first-hit
  vs answer-location labels with per-type aggregation, cost columns
  (tokens injected, assemble latency), resume-safe JSONL runner +
  markdown reports + arm-vs-arm deltas. Infra builders mirror
  production construction sites (loader path = the expert reload;
  manager = `_setup_memory`'s shape; vector DB via the agent-side
  `PostgresDB` + the production migrations runner onto a dedicated
  eval database). 38 offline tests (`tests/test_memory_eval_harness.py`,
  fakes through the `HarnessHandles` factory seams). **Smoke (k3d
  pgvector + real qwen3 embeddings + cluster aux routing)**: flat arm —
  stored counts == round counts (dedup-off proof), evidence at rank 1;
  seam arm — full chain fired (bind log, interval gate correctly closed
  on short sessions, per-session `Final memory extraction complete`,
  0-stored warning surfaced) and already produced the first honest
  finding: gemma's conservative extraction stored 0 memories on most
  short fixture sessions → single-session recall 0 (the brief-05
  completeness-over-precision argument, now measurable). Remaining
  Phase-2 slices: full **_S** runs (flat = published-baseline anchor,
  seam = current-system baseline numbers), end-task LLM-judge,
  contradiction-survival probe, recall-shape instrumentation.
- **2026-06-12 — Phase 2 slices 2–4 shipped + ALL acceptance gates closed
  (real LongMemEval_S runs, judge, calibration, contradiction probe).**
  New modules: `eval/memory/judge.py` (reader over the captured
  production-rendered injection block + the **verbatim LongMemEval
  `evaluate_qa.py` judge prompts**; `--re-judge` swaps judges over fixed
  hypotheses; `--calibrate` vs hand labels) and
  `eval/memory/contradiction.py` + `fixtures/contradiction_probe.json`
  (8 fact→supersede probes with known old/new values; retrieval-order +
  current/stale/miss reader scoring, no judge LLM). 57 offline tests.
  Run-relevant fixes found by the real data: result rows capture
  `injected_context`/question/gold at answer time (post-`--cleanup`
  judging); rare token-dense rounds exceed the embedding server's 8192
  ctx (observed 8264 *qwen* tokens at 5816 cl100k — qwen ≈1.42×) →
  `EMBED_TOKEN_CAP` 5000 cl100k; the paper's judge `max_tokens=10`
  returns **empty content on reasoning models** → 2048 + post-`</think>`
  verdict parse; `PrimedEmbedding` batch-primes verbatim ingestion.
  Infra notes: catalog's only embedding endpoint (`-strix` = Strix Halo)
  serializes ~0.8 unique texts/s — the plain router ids
  (`qwen3-embedding-8b`, `gemma-4-moe`) are the L40S deployments, same
  key, ~10×; flat N=100 ran in ~35 min after the switch.
  **Gate 1 — published-baseline anchor ✅** (`flat_verbatim`, stratified
  N=100 seed 0, R@1 0.63 / **R@5 0.940** / R@10 0.980 / **NDCG@5 0.773**
  / coverage@5 0.891, first-hit 1.86): the paper publishes retrieval
  tables only for **_M** (Tables 3/9/10; no _S rows exist), so the anchor
  is placement + shape — ours sits above their _M round-K=V dense rows
  (Stella R@5 0.66/N@5 0.50) in exactly the direction a 10× smaller
  haystack + an 8B embedder + hybrid RRF predict, reproduces their
  qualitative ordering (temporal-reasoning hardest: 0.812), sane k-curve,
  nowhere degenerate-perfect.
  **Gate 2 — A/B with deltas ✅ (the headline)**: `persistent_current`
  (seam replay, gemma extraction, N=20 = exact prefix of the flat
  subset): **R@5 0.200 (−0.740), NDCG@5 0.121, first-hit 14.1**; end-task
  **0.25 (gemma judge) / 0.30 (llama-70B judge)** vs flat **0.76 / 0.74**.
  Decomposition (from injected-set provenance): extraction is NOT the
  session-level bottleneck (18/19 evidence sessions produced ≥1 memory)
  and TTL-pinning is NOT dominant (top-5 injected items spread over the
  haystack, only 14% from the last fifth); the evidence-derived memory
  simply ranks ~13th median among **~122 injected items** (~56 tok each —
  the whole store fits the 10k budget, injection IS the store), and the
  reader then **over-abstains: 68% of answerable questions get "I don't
  have that information"** with the answer in context; only 3/19 are
  substantively wrong. **Fact-level diagnostic (3 flat-hit/seam-miss
  questions re-run without cleanup, evidence-session memories read
  against gold — `runs/seam_diag`): three distinct failure modes, one
  each** — (1) *knowledge-update*: the answer fact survived extraction
  verbatim ("two hours daily") but the stale value ("one hour") coexists
  as an equal-standing memory (no supersede) and neither ranked top-5 of
  110 → Phase 4's exact case; (2) *single-session-user*: the user's
  favorite-rice **preference was never extracted as a user fact** — the
  answer phrase survives only incidentally inside a procedural
  onigiri-shaping memory (discriminating fact denatured); (3)
  *multi-session count*: 11 evidence-session memories all capture decor
  **preferences while systematically dropping the countable events**
  (bought/assembled/sold/fixed) the question aggregates over. So
  extraction bias (durable traits over episodic events / attributed
  preferences) is a co-equal root cause with ranking and missing
  supersede — and it's prompt-level, i.e. the cheapest possible harness
  A/B.
  **Gate 3 — current-system baseline recorded ✅** (the numbers above are
  it). **Judge calibration**: 29-item stratified hand-labelled slice
  (`eval/memory/data/judge_labels_flat_s100.json`) → **93.1% agreement
  for BOTH judges, with the identical 2 disagreements** (both genuine
  gray-zone: preference-rubric leniency, knowledge-update referent
  ambiguity); judge-vs-judge agreement **98/100**. At n=29 each item is
  3.4pp, so the residual is label-set size + item ambiguity, not judge
  quality; `llama-3.3-70b-versatile` (same lineage as the paper's open
  judge) is the recorded judge. >97% as a formal claim wants a ~100-item
  label set (user pass).
  **Contradiction probe (Phase-4 baseline)**: flat — update injected 8/8,
  update-above-original 0.625, reader current 8/8; seam — **update
  injected only 5/8, update-above-original 0.125, reader current 0.625 /
  stale 0.125 / miss 0.25**. Phase 4's supersede has its target.
  **Phase-3 implications, sharpened**: the big lever is not search
  (hybrid finds verbatim rounds at rank 1.9) but what the writers store
  and how injection orders it — (a) ranking/reranker over short
  extracted facts, (b) bounded injection (122-item full-store dumps bury
  the answer and trigger over-abstention), (c) extraction completeness
  at fact granularity / dual-granularity storage (the paper's K=V+fact:
  +9.4pp recall) as a first-class arm, (d) abstention-vs-recall trade
  in the reader frame. Caveats recorded: seam n=20 (±~10pp end-task),
  reader = gemma-4-moe, single seed, one transient 3s aux outage during
  ingestion (~3 sessions' extractions lost, stored counts unaffected
  in-family).
- **2026-06-12 — Phase 3 started: slices 1–2 built (prompt variant +
  reranker plugin + requery harness mode); first ingest run pending
  infra.** Slice 1: `memory_extraction_prompt_complete.txt` committed —
  user preferences/possessions/episodic events/quantities as first-class
  extraction targets ("Events that happened" with counts/dates verbatim,
  supersede-aware phrasing), "completeness over precision" replacing the
  old prompt's "2 strong memories beat 6 weak ones"; the old prompt's
  framing was purely engineering-task-oriented, which is root causes
  (2)+(3) from the Phase-2 fact-level diagnostic verbatim. Harness gained
  `extraction_prompt_file` (arm-level prompt override) and
  **`--requery-from`** (ingest once without `--cleanup`, then measure any
  question-time arm — scorer/budget/policy — against the identical stored
  corpora in minutes; scope run_id inherited from the source run's
  run_meta.json, writers stripped in requery mode). Slice 2 (code
  complete): `RerankerConfig` (`memory.reranker`: model/base_url/api_key/
  top_k/timeout/keep_pinned_first; transport defaults to the auxiliary
  endpoint so dispatch needs no new credential plumbing) +
  `plugins/reranker.py` `RerankerScorer` (Cohere-shaped `/rerank`;
  memory-kind only, TTL-pinned head preserved, top-k cap, fully-valid-
  response-or-raise → manager containment, channel_scores["rerank"]
  recorded) + registry import + 8 unit tests; full suite 6049 green.
  Arm YAMLs staged: `persistent_complete` (slice 1) and `complete_rerank`
  (slice 2, requery mode). **Measurement status:** the first slice-1
  ingest aborted twice on infra — (a) the router's qwen3-reranker-8b
  initially returned near-noise scores (2/4 on a trivial battery,
  order-independent — the Qwen3-Reranker instruction template wasn't
  applied server-side; **fixed by the operator 2026-06-12**), (b) both
  gemma routes went down mid-ingest (router API-key rotation; ~1285
  outage-contaminated rows wiped, results cleared for a clean restart).
  Early signal from the one pre-outage completed question: the
  completeness prompt stored **229 memories vs 132** with the old prompt
  (+73 % extraction volume). **Next (post-compact runbook):**
  re-extract creds (catalog recipe; router key rotated) → verify reranker
  battery now discriminates → relaunch slice-1 ingest (no `--cleanup`,
  limit 20 seed 0) → slice-1 A/B + judge vs `seam_s20` → slice-2 rerank
  requery (`--requery-from runs/seam_complete_s20`) → bounded-injection
  requery sweep (slice 3) → doc the keep/cut deltas.
- **2026-06-12 (evening) — Phase 3 slices 1–3 MEASURED; retrieval solved
  on the seam, end-task bottleneck rotated to reader + supersede.** Infra
  first: new router key verified, reranker battery post-template-fix
  **4/4** (was 2/4) with the expected near-binary spread (relevant doc
  0.24–1.0, distractors 0.000); a 240-doc probe call returns in 4.3 s and
  pulls a planted answer from position 137 to rank 1 at 0.997. The full
  measured wave (N=20 stratified, same seed-0 prefix as `seam_s20`;
  reader gemma-4-moe, judge llama-3.3-70b-versatile):

  | arm (runs/) | R@5 | NDCG@5 | first-hit | tok/turn | end-task |
  |---|---|---|---|---|---|
  | `seam_s20` baseline (old prompt, full dump) | 0.20 | 0.121 | 14.1 | 6 806 | 0.30 |
  | `seam_complete_s20` slice 1 (full dump) | 0.20 | 0.094 | 16.5 | 8 923 | **0.45** |
  | `complete_control_s20` requery control | 0.10 | 0.063 | 16.0 | 8 926 | — |
  | `complete_rerank_s20` slice 2 (full dump) | **0.95** | 0.928 | 1.35 | 8 925 | 0.40 |
  | `complete_rerank_b10_s20` slices 1+2+3 | **1.00** | 0.986 | 1.00 | **312** | 0.35\* |
  | `complete_rerank_b25_s20` | 1.00 | 0.986 | 1.00 | 888 | 0.35 |
  | `complete_bounded10_s20` bounded w/o reranker | 0.20 | 0.084 | 4.8 | 428 | **0.05** |

  **Slice 1 (completeness prompt)**: extraction volume ~doubles (122→236
  avg memories/question); fact-level A/B on the Phase-2 diagnostic
  questions confirms the targeted transformation — episodic events now
  extracted with dates/quantities ("assembled an IKEA bookshelf ~two
  months ago", "bought a 15-lb bag … for $45"; 9→18 event-like memories
  on the aggregation question), the rice preference now a clean
  attributed user fact instead of denatured-in-procedure. Session-level
  retrieval flat (expected — double the competition, same ranking), but
  **end-task 0.30→0.45** and over-abstention 13/19→9/19: the reader
  commits when the facts exist. By type: ss-user 0.33→1.0, multi-session
  0→0.25, temporal 0.33→0.67; **knowledge-update 0.5→0.25** (more volume
  = more stale/current coexistence — Phase 4's case sharpened).
  **Slice 2 (reranker)**: **R@5 0.10→0.95 against the same corpora**
  (requery control vs rerank arm at identical snapshot state) — ordering
  was the remaining retrieval problem and a cross-encoder solves it
  outright. End-task on the full dump however 0.45→0.40 with
  knowledge-update → 0: pure-relevance ordering ranks the stale fact
  next to the current one (the legacy RRF order at least had a recency
  channel). Ordering alone doesn't help a reader that sees everything
  anyway. **Slice 3 (bounded policy — `plugins/bounded.py`, NEW)**:
  post-scorer cap (`memory.bounded.max_items`/`max_tokens`,
  `pipeline.policies: [bounded]`; 10 unit tests). Placement is the
  point: `memory.budget_tokens` trims **inside the retriever in legacy
  hybrid order** — bounding before the scorer would cut the evidence the
  reranker exists to surface, so the cap must live in the policy stage.
  Result: **R@5 1.0 at 312 tokens/turn (29× cheaper than the dump)**;
  end-task 0.35 vs 0.45, and the two lost questions decompose to **one
  genuine knowledge-update fact-binding miss** (both facts in top-10;
  stale "27 in my local park" phrasing-matches the question, updated
  "32" lost its park-binding at extraction — reader picks stale) **and
  one judge gray-zone flip** (near-identical preference answers judged
  differently) — i.e. parity within noise at 4 % of the tokens, with
  over-abstention down again (6/19). The no-reranker bounded control
  craters to **0.05**: bounding is only safe once ordering is solved,
  now measured. **Architectural finding (feeds Phases 4–5):** ~95 % of
  every seam corpus is TTL-pinned — cross-session memories freeze
  pinned once their job scope stops ticking (`decrement_ttl` only
  touches the current scope), so the two-tier read path degenerates to
  dump-the-store; Phase 2's "injection IS the store" was the pinned
  tier, not search. `keep_pinned_first: false` (+ `top_k: 256`) is
  therefore required for the reranker to act on these corpora.
  **Protocol notes:** requery arms mutate corpus state (per-assemble
  TTL decrement + retrieve-path re-pinning/access-bumps) → snapshot
  `remaining_turns`/`access_count`/`last_accessed` once post-ingest and
  restore between arms (side table `eval_ttl_snapshot_s20` in
  `srw_eval`); the reranker's 10 s default timeout trips on cold/
  contended full-store batches (17/20 contained passthroughs in one run
  — detected via `AssembleStats.errors`, containment worked as designed;
  arm override `timeout: 60`, concurrency 3 → 0 failures). **Slice-3
  acceptance ("bounded ≥ full-injection at lower tokens"): met within
  noise on end-task, exceeded on retrieval/cost; the systematic
  remainder is knowledge-update, which is Phase 4's supersede by
  construction. Slice 4 (gate threshold + request digest + unified
  budget, B5) is the open Phase-3 item.** Next measurement: re-run the
  contradiction probe over a completeness+rerank corpus as the Phase-4
  opening baseline.
- **2026-06-12 (night) — Phase 3 slice 4 MEASURED: relative gate ships,
  absolute gate falsified; Phase 3 COMPLETE. Phase-4 opening baseline
  recorded.** Built: `plugins/gate.py` GatePolicy (`memory.gate.
  {threshold,channel,mode}`, `pipeline.policies: [gate, bounded]`),
  request digest (`memory.query.digest` default-off; unified
  `build_digest_query_text` in `src/services/memory/query.py`, flag
  consulted at all four AssembleRequest build sites — worker execute,
  persistent turn, harness ingest + question time), B5 one-budget
  (`memory.bounded.include_knowledge`: KB notes count against
  `max_tokens`; `max_items` stays memory-only so a 200-candidate memory
  channel can't starve the KB block), per-item `score` persisted in
  harness results (post-hoc threshold analysis). `ef_search` deferred:
  retrieval is saturated at eval scale (R@5 1.0), nothing to tune yet.
  Requery measurements over the same restored `seam_complete_s20`
  corpora (gemma reader, llama-70b judge):

  | arm (runs/) | R@5 | NDCG@5 | mem/q | tok/q | end-task |
  |---|---|---|---|---|---|
  | `complete_rerank_b10_s20` (slice-3 ref) | 1.00 | 0.986 | 10.0 | 312 | 0.35 |
  | `complete_rerank_gate_s20` absolute 0.05 | **0.80** | 0.692 | 2.15 | 72 | not judged (disqualified) |
  | `complete_rerank_gaterel_s20` relative 0.01 | **1.00** | 0.950 | **3.7** | **111** | **0.40** |
  | `complete_rerank_b10_gaterel_s20` gate+cap | 1.00 | 0.950 | 3.7 | 111 | 0.40 |
  | `complete_rerank_b10_digest_s20` digest on | 1.00 | 0.986 | 10.0 | 310 | parity (see below) |

  **Absolute thresholds are falsified on this corpus**: at 0.05 the gate
  deleted ALL evidence on 4/19 answerable questions (R@5 0.80). A direct
  score-distribution probe explains it — qwen3-reranker's absolute scale
  varies by orders of magnitude per query (top evidence 0.9991 on one
  question, 0.0325 / 0.0201 / **0.0019** on others) while the
  evidence/distractor **separation** stays strong (5–800×). No absolute
  cutoff can keep 0.0019-evidence and drop 0.005-distractors elsewhere.
  **Relative mode** (floor = `threshold ×` the assemble's top score,
  shipped as `memory.gate.mode: relative`, measured at 0.01) restores
  R@5 1.0 / first-hit 1.0 at **3.7 items / 111 tokens per question** —
  2.8× under bounded-10, **80× under the full dump** — with end-task
  **0.40 ≥ b10's 0.35** (recovers the preference question ten noisy
  items had cost; the slice-4 acceptance "gating cuts tokens with no
  end-task regression" is met) and the **reader's abstention behaviour
  goes perfect** (abstention question: 281→78 injected tokens, reader
  correctly declines; abstention_score 1.0). Gate and gate+bounded
  selections are byte-identical at s20 scale (the relative floor keeps
  ≤10 everywhere) — bounded stays in the production stack as the cap
  against floor-jitter and coverage leaks. **Limits, measured honestly:**
  (a) P4's "inject *nothing* when nothing qualifies" is NOT reachable
  from rerank scores alone — the abstention question's top score
  (0.1057) outranks three answerable questions' evidence tops, so no
  score-function separates "answerable but weakly phrased" from
  "unanswerable with a topical near-miss"; a calibrated/ensemble channel
  (Phase 5+/7) reopens it. (b) **top_k-coverage leak**: candidates past
  the reranker's `top_k` stay unscored and pass the gate fail-open
  (caught on affe2881 — 263 candidates vs top_k 256 → 7 junk items kept
  at exactly the spot the gate should have cut; gate arms now run
  top_k 512 + a results audit rule: any kept item with score 0.0 in a
  gated arm is leak evidence). Fail-open on unscored items stays — it is
  what makes a contained scorer outage degrade to the legacy dump
  instead of an empty injection. (c) NDCG dips 0.986→0.950 (coverage
  0.988→0.938): the floor sheds some secondary same-evidence duplicates
  on multi-session/KU questions; end-task shows no harm. **Digest:**
  question-time queries are byte-identical to legacy (one-message
  answering session, stripped-equal), so parity is by construction; the
  5/20 marginal context diffs are rerank endpoint jitter among
  near-zero ties at the top-10 boundary (R@5/NDCG unchanged). The
  digest's behavioural payoff — windowed mid-session queries — is not
  measurable at LongMemEval question granularity; flag stays off
  pending an ingest-time A/B or production soak. **Knowledge-update,
  the through-line:** rerank ordering itself drops KU 0.25→0.0 *at
  unchanged token count* (stale ranked immediately next to current
  beats burying both mid-dump — the legacy RRF order at least carried a
  recency prior), and no slice-4 policy can fix a selection problem
  that is really a lifecycle problem. **Phase-4 opening baseline
  (contra probe over a completeness corpus + rerank requery,
  `runs/contra_complete{,_rerank}`):** update_injected 1.0 /
  update_above_original **0.75** (seam baseline 0.125, flat 0.625) /
  reader current **1.0**, stale 0, miss 0 on the 8-probe fixture —
  completeness+rerank already rescue the small-corpus case, and the
  remaining gap is exactly what supersede closes: the 2/8 stale-first
  ties relevance can't break, the at-scale binding-loss miss from the
  s20 decomposition, and KU end-task 0/4. Phase-3 acceptance closes:
  reranker lifts R@k ✅, gating cuts tokens with no regression ✅,
  bounded+gated ≥ full-injection ✅ (0.40 vs 0.45 where the entire gap
  is one KU question = Phase 4 by construction, at 1.2 % of the
  tokens). Production flip (defaults YAML pipelines) deliberately NOT
  included — rollout is a separate decision on real-session evidence;
  all new config sections are inert without explicit opt-in.
- **2026-06-14 — Phase 4 slices 1–3 IMPLEMENTED (code complete, inert by
  default; measurement = slice 4, not yet run).** The lifecycle write path:
  ingestion verdicts + bi-temporal supersede, behind `memory.ingestion.enabled`
  (default off) and `memory.extraction.write_gate`.
  **Slice 1 (bi-temporal schema + retrieval filter, behaviour-preserving):**
  migration `vector/0006_bitemporal_memory.sql` adds `valid_from` / `valid_to`
  / `superseded_at` / `superseded_by` (self-FK, ON DELETE SET NULL) to
  `memories`, backfills `valid_from = created_at`, two partial scope indexes
  `WHERE valid_to IS NULL`, and CREATE-OR-REPLACEs the **three** memory
  hybrid-search functions to filter `valid_to IS NULL` in every channel
  (bodies otherwise byte-identical to 0002's halfvec versions — the casts +
  `SET hnsw.iterative_scan` preserved). `knowledge_*` functions deliberately
  untouched: `knowledge_index` already supersedes via `status='active'` and the
  KB is the model's active notebook (P0). `recall_store` `find_similar` /
  `get_ttl_active` / `decrement_ttl` filter `valid_to IS NULL`; `get_stats`
  reports a current/superseded split; `MemoryRecord` gains the columns.
  Behaviour-preserving by construction (every row valid_to NULL until a writer
  retires it) — the filter is NOT flag-gated (a retired memory must never be
  served). **Live-validated** in a throwaway pgvector-0.8.2 container: 0001→0006
  apply clean, a retired row is excluded from `memory_hybrid_search` despite
  matching every channel, columns/FK/partial indexes present. Equivalence
  suites stay green.
  **Slice 2 (ingestion verdict + supersede):** `IngestionVerdict` schema +
  `IngestionVerdictTask` (`auxiliary.py`), `IngestionVerdictService` +
  `maybe_attach_ingestion_verdict` (NEW `src/services/memory/ingestion.py`),
  prompt `config/prompts/memory_ingestion_verdict.txt`. `RecallStore.store()`
  gains a verdict branch keyed on `self.ingestion_verdict is not None` (None
  everywhere today → legacy cosine-dedup byte-identical, equivalence pinned):
  `find_similar_many` (currently-valid top-k) → **cost guard** (no neighbour ≥
  `review_floor` → straight ADD, zero LLM calls; ≤1 verdict call per stored
  memory) → `adjudicate` → ADD / NOOP (bump existing) / UPDATE (insert new +
  `supersede` the stale rows, linking `superseded_by`) / MERGE (insert the
  aux-merged content + supersede). New store primitives `find_similar_many` +
  `supersede`; the INSERT and the dedup-bump refactored into shared `_insert` /
  `_bump_existing`. Fail-safe: an aux outage or malformed verdict degrades to
  ADD (never lose a write, never wrongly retire). Wired at the three
  manager-construction sites (worker graph builder, persistent `_setup_memory`,
  harness `build_manager`) — independent of the manager cutover, since it's a
  write-path change used by both legacy and seam writers. The verdict rides the
  existing aux LLM (no new transport/credential plumbing).
  **Slice 3 (completeness):** `memory.extraction.write_gate` (default True =
  legacy importance floor); false drops the write-time gate (a skipped fact is
  unrecoverable; relevance is now gated at retrieval by the reranker+gate). No
  `trigger` knob added — boundary-driven extraction is already always-on via
  the `phase_boundary` + teardown writers with the interval extractor as the
  turn fallback; an unwired knob would be config theatre.
  **Tests:** +28 (`test_recall_store` verdict ADD/NOOP/UPDATE/MERGE +
  find_similar_many/supersede/write_gate; `test_memory_ingestion` service
  fail-safe + maybe_attach + config parse). Full suite **6453 passed** (only
  the known `test_connect_disconnect` ordering flake), lint+format clean.
  **Slice 4 (measure) staged, NOT run:** arms `persistent_complete_verdict`
  (fresh seam ingest — verdicts change the write path, can't requery) +
  `contra_complete_verdict` (the contradiction fixture with supersede ON +
  the rerank read stack). Target shifts from "update out-ranks original"
  (baseline 0.75) to **original_injected → 0** — the stale fact is *retired*,
  never injected, so relevance ordering no longer has to win — with reader
  current 1.0 and the KU end-task slice recovering toward 0.5. Needs the
  cluster + `EVAL_AUX_API_KEY` + a ~4.5h ingest (eval writes to `srw_eval`
  only). Production flip (defaults YAML) is the separate GATE-B rollout
  decision; all Phase-4 config is inert without explicit opt-in.
- **2026-06-14 — Phase 4 MEASURED on k3d (`srw_eval`); supersede fixes
  knowledge-update.** Ran the full Phase-4 write path against real gemma
  (extraction + verdicts) + llama-3.3-70b judge (groq). Migration 0006 applied
  to `srw_eval`; creds resolved from the catalog's `-strix` endpoints
  (decrypted via `resolve_catalog_model(...)['api_key']`), router accepts the
  plain `gemma-4-moe`/`qwen3-embedding-8b` L40S ids.
  **(a) Contradiction probe** (`runs/contra_complete_verdict`, verdicts ON +
  rerank read stack, 8-probe fixture): **`original_injected` 1.0 → 0.25**
  (the stale fact is *retired*, not just out-ranked), **`update_above_original`
  0.75 → 1.0**, reader **current 1.0 / stale 0 / miss 0**. DB confirmed the
  10 retired rows linked to their replacements — the washing-machine cases
  exactly (`Pixel 7 → iPhone 15`, `Greenfield Labs → Northwind Robotics`,
  `dentist Jun 12 → rescheduled Jun 26`). 2/8 kept the original — there the
  old/new pair was phrased below the 0.6 `review_floor` so the verdict ADD'd
  them as distinct (tunable). This probe isolates supersede (independent of
  write_gate).
  **(b) End-task / KU slice** (fresh seam ingest `runs/seam_complete_verdict_s20`,
  N=20 seed 0, verdicts ON + write_gate off → then requeried through the
  production-candidate read stack `complete_rerank_b10_gaterel` and judged):
  20/20 ingested, 0 whole-question failures, **14 % of the organic corpus
  retired** (1,518 / 10,494 — ~1 in 7 extracted facts superseded by a later
  UPDATE/MERGE). vs the Phase-3 reference over the verdicts-OFF corpus
  (`complete_rerank_b10_gaterel_s20`: overall 0.40, KU **0/4**):
  **knowledge-update 0/4 → 3/4 (0.75)**, **overall end-task 0.40 → 0.50**,
  retrieval R@5 1.0 (unchanged), 111 tok/q (unchanged), abstention 1.0
  (correctly declines the 1 abstention question). The KU question from the
  original Phase-2 fact-level diagnostic (`cc5ded98`, "two hours" vs stale
  "one hour") now answers correctly because the stale value was retired at
  ingest. The 1 remaining KU miss (`dad224aa`) + ss-preference 0.0
  (n=3, judge gray-zone, within noise of the Phase-3 0.33) are the residual.
  **Acceptance (all met):** contradiction probe flips to current-fact answers
  ✅; KU slice improves ✅ (0 → 0.75, past the 0.5 target); no end-task
  regression (overall up +0.10) ✅; verdict-call budget held (the `review_floor`
  cost guard; the 84 contained aux failures over ~7 h were router noise,
  absorbed per-op, zero whole-question loss) ✅. **Caveats:** N=20, per-type
  n=3–4 (KU is 3-of-4), single seed, reader gemma / judge llama-70b (same as
  prior phases for comparability); the s20 delta combines supersede +
  write_gate-drop (the contradiction probe isolates supersede). Production
  flip (defaults YAML) remains the separate GATE-B decision — but the harness
  case for it is now made. Slice-4 complete → **Phase 4 done.**
- **2026-06-14 — GATE B flipped (Phase-3+4 stack activated in the defaults),
  shipping to dev.** Both `config/defaults.yaml` and
  `config/persistent_defaults.yaml` now carry `pipeline.scorers: [reranker]`,
  `pipeline.policies: [gate, bounded]`, the `reranker`/`gate`/`bounded` config
  (relative gate 0.01, bounded-10, reranker `keep_pinned_first: false`,
  `top_k: 512`), `ingestion.enabled: true` (verdict_top_k 5, review_floor 0.6),
  and `extraction.write_gate: false`. One production-tuning divergence from the
  eval config: **reranker `timeout: 10` not 60** — the eval's 60 was for cold
  full-store batch throughput; in a live turn a slow rerank must fail-open fast
  (it degrades to legacy order, contained) rather than stall the turn. The
  reranker rides the auxiliary endpoint's transport (injected at dispatch — the
  same one extraction uses, so it's always present where memory works at all).
  Legacy paths stay in place behind the flags (one-edit rollback). Verified: the
  activated defaults parse + bind the full pipeline, the verdict service attaches
  during worker construction, all memory suites green (256); the
  `test_memory_cutover` defaults-bind/construction fixtures were updated to feed
  the new plugins their transport + a settable store. The remaining validation —
  reranker latency + aux load under real traffic — is what the dev rollout is
  for; prod flip (and GATE A's legacy deletion) follow dev evidence.
  **k3d pre-push smoke (2026-06-14):** from the orchestrator pod (synced config
  + real catalog transport + live router), the flipped pipeline **binds with
  the real dispatched aux transport** (the bind the unit test had to stub —
  `scorers:[reranker] policies:[gate,bounded]`, no raise), and a retriever
  failure **degrades gracefully** (contained, empty payload, no crash). The
  agent image (`tilt-f95a1d91…`) carries the flip. Live `/rerank` latency on
  the router: **50 docs 0.96 s / 150 docs 2.13 s / 300 docs 4.22 s** — all well
  under `timeout: 10`, so the live-turn assemble (≤150 candidates ≈ 2 s) won't
  stall; the eval's 6.9 s assemble was concurrency-3 contention, not the live
  single-turn cost. Could NOT run a full agent assemble in the orchestrator pod
  (its image lacks the agent's pgvector codec / neo4j / citation deps — not a
  prod issue) — the true live-turn path validates on the dev rollout with real
  agent pods.
**Companions:**
- [`agent_memory_current_state.md`](agent_memory_current_state.md) — ground truth: every
  current capability classified wired/dead/conceptual with `file:line` evidence.
- [`../issues/memory_bugs.md`](../issues/memory_bugs.md) — B1–B11, bugs fixable
  independently of this redesign. B1, B2, B4 fixed (2026-06-10/11); the
  remainder are mapped to overhaul phases in its §Suggested order.
- [`ai-memory-research/`](../../ai-memory-research) — five deep-research briefs + verified
  reports under `results/`.

---

## 1. Vision & principles

The current system was built from a correct first-principles instinct: *memories should be
discrete facts in a database, retrieved automatically by relevance — not a flat file the
model rewrites.* The research validated that instinct and exposed where the execution
drifted. This redesign commits to the original vision, properly:

**P0 — Passive. The model is a consumer, not a memory manager.** Memory "pops up"
associatively, like human recall — the model never has to *decide* to remember. This is a
hard requirement, not a preference: the platform is multi-model by design (the whole
`config/` prompt/settings matrix exists because gemma, gpt-oss, kimi, minimax, deepseek
behave differently), and any architecture that depends on the model calling a recall tool
gives frontier models a small win and weaker models amnesia, while taxing everyone's
task-focus with memory-management cognitive load. The field splits into two camps —
**model-managed** (MemGPT/Letta, frontier labs' built-in file memory: the model pages and
reflects) vs **system-managed** (Mem0, Zep: a platform pipeline extracts, stores, and
assembles). We are camp two. The proven findings transfer: Self-RAG's lesson was never
"the model must decide when to retrieve" — it was *"indiscriminate injection hurts;
retrieval must be gated."* The gate moves into the system. (The KB stays the model's
*active* notebook — deliberate `kb_write` notes when it chooses. Active note-taking is a
feature; mandatory memory self-management is a defect.)

**P1 — Foundations first. One seam before any behaviour change.** The first deliverable is
a unified `MemoryManager` interface shared by worker and persistent agents. Today the
assembly logic is forked across ~120 lines of `graph.py` and ~90 of `persistent_graph.py`
plus two store classes — and the fork is where the bugs bred (B1: persistent extraction
broken three ways; B3: assembler enabled-but-never-called on persistent). Get the
abstraction right and every idea in §4 becomes a plugin swap behind a stable interface;
get it wrong and every improvement is two-file surgery forever.

**P2 — Memories are discrete facts in shareable buckets, not files.** Individual pieces
assemble more flexibly than documents, and buckets (session / project / personal / shared)
can be attached, detached, toggled, and shared across agents and users — the same UX
pattern as datasource attachment. File-based memory is the competing paradigm and it loses
on our terms: it scales poorly, goes stale fast, and works for proprietary assistants only
because the model was trained for it.

**P3 — Staleness is a lifecycle problem, not a retrieval problem.** The
"washing-machine-ad" failure (one stale fact recalled forever — *"the homelab has L40s"*)
is the field's #1 unsolved problem, and a passive system is *more* exposed to it because
no one is curating by hand. The cure lives on the write path: ingestion verdicts
(ADD/UPDATE/MERGE/NOOP) + bi-temporal supersede (retire, don't delete). Non-negotiable
part of the target.

**P4 — Ensemble relevance, gated injection.** A memory is injected because multiple
independent signals agree it matters *for this call* — dense, sparse, trigger-phrase,
reranker, (later) learned scorer — and **nothing is injected when nothing qualifies**.
Below-threshold ≠ "inject the best of a bad lot."

**P5 — Measure, then change; measure, then cut.** No memory *behaviour* change ships
without a before/after on the eval harness (the Phase-1 refactor is behaviour-preserving
and is gated by equivalence tests instead). The existing consolidation layer
(assembler/TTL/dedup) is unproven by the field's own literature — ablate it and cut what
doesn't earn its keep.

**P6 — The manager is a kernel: config in, memories out.** Like an operating system —
essentially a collection of drivers behind one stable interface — the MemoryManager
contains almost no memory *opinion* of its own. It takes a declarative configuration
(which buckets, retrievers, scorers, policies, extensions, with what parameters), binds
the named components, and emits the memories that go into the conversation. Every
mechanism is a driver — **including the ones we currently believe aren't our future**:
model-driven paging and recall tools live on as *model-facing extensions*,
capability-gated per model family through the same matrix machinery that already varies
prompts and settings per family. Nothing is privileged; today's RecallStore behaviour is
itself just the first registered pipeline configuration, and a competing paradigm is a
config change away from an A/B, not a rewrite.

### Locked decisions (carried + new)

| # | Decision | Source |
|---|---|---|
| 1 | **System-managed (passive) memory**; recall tools are optional progressive enhancement, never load-bearing | design discussion 2026-06-10; brief 01 camp analysis |
| 2 | **MemoryManager seam first**, behaviour-preserving, shared by both graphs | design discussion; audit (fork = bug source) |
| 3 | **Eval-first for behaviour changes**; equivalence-tests for the refactor | briefs 05, 02 |
| 4 | Keep `qwen3-embedding-8b` + RRF (`rrf_k=50`); **add a reranker, don't fine-tune the embedder** | brief 03 (RMM collapse when retriever RL'd) |
| 5 | **Gate lives system-side** (ensemble score threshold), not model-side | briefs 03/05 + P0 |
| 6 | **Supersede, don't delete** — bi-temporal valid/transaction time, in pgvector columns | brief 04 (Graphiti); P3 |
| 7 | **Measure-then-cut** the consolidation layer (assembler@7 / TTL@10 / dedup@0.85) | brief 02 (efficacy refuted) |
| 8 | **Graph-keep is instrumentation-gated**; graph retrieval becomes a *plugin* we can A/B, not an architectural commitment | brief 04 + audit (graph not on hot path today) |
| 9 | No community summarization, no embedding-model swap, no parametric memory, no episodic/semantic store split in v1 | briefs 04/03/02 |
| 10 | v1 manager is an **in-process library** (`src/services/memory/`); a standalone memory service is a later option if cross-agent sharing demands it | per-call latency; simplest migration |
| 11 | **Composition is declarative config** (`memory.pipeline`: named plugins + params), resolved per expert/model-family via the existing `$extends`/matrix machinery; **model-facing extensions are a first-class, capability-gated plugin category** | P6; design discussion 2026-06-10 |

---

## 2. Target architecture

```
                       ┌─────────────────────── MemoryManager ───────────────────────┐
 before each LLM call: │  assemble(AssembleRequest) ──────────────► MemoryPayload    │
                       │                                                             │
                       │  BUCKETS     session │ project │ personal │ shared …        │
                       │              attachable · toggleable · shareable            │
                       │  RETRIEVERS  dense │ sparse │ trigger-phrase │ tags/regex   │
                       │              │ graph │ recency │ agentic-search   (plugins) │
                       │  SCORERS     RRF → cross-encoder rerank → ensemble →        │
                       │              learned model (later)                          │
                       │  POLICIES    always-inject flags · token budgets ·          │
                       │              relevance gate · latency tiers · dedup-on-     │
                       │              assemble · provenance labels                   │
                       └─────────────────────────────────────────────────────────────┘
 on lifecycle events:     capture(CaptureEvent)  ──► WRITERS (async, aux-LLM)
                          boundary extraction · trigger-phrase generation ·
                          ingestion verdicts (ADD/UPDATE/MERGE/NOOP) ·
                          bi-temporal supersede · curation · GC
```

### 2.1 The interface (sketch — Phase 1 refines)

```python
# src/services/memory/manager.py
class MemoryManager:
    """Single seam for all memory: assembly (read) + capture (write).
    Both graphs hold exactly one of these; neither touches stores directly.
    The manager is a binder (P6): from_config resolves named plugins from a
    registry and wires the pipeline — it holds no memory logic itself."""

    @classmethod
    def from_config(cls, cfg: MemoryConfig) -> "MemoryManager": ...
        # binds memory.pipeline entries via MEMORY_PLUGIN_REGISTRY
        # (same registration pattern as TOOL_REGISTRY, src/tools/registry.py)

    async def assemble(self, req: AssembleRequest) -> MemoryPayload: ...
    async def capture(self, event: CaptureEvent) -> None: ...        # fire-and-forget
    def extension_tools(self) -> list[BaseTool]: ...   # model-facing extensions, may be []

@dataclass
class AssembleRequest:
    query_text: str            # digest of the upcoming call (see §4 query formation)
    task_frame: TaskFrame | None   # todos/phase (worker) | None (persistent)
    budget_tokens: int         # total memory+KB budget for this call
    buckets: list[BucketRef]   # resolved from job/thread scope; toggleable

@dataclass
class MemoryPayload:
    blocks: list[InjectionBlock]   # ready-to-inject synthetic tool pairs
    stats: AssembleStats           # candidates/scores/tokens — harness + telemetry feed

@dataclass
class CaptureEvent:
    kind: Literal["turn_end", "phase_boundary", "compaction",
                  "session_end", "idle_archive", "todo_complete"]
    messages: list[BaseMessage]    # window to extract from
    # … scope refs, turn counters

# plugin protocols (src/services/memory/plugins/)
class Retriever(Protocol):
    async def retrieve(self, query: Query, bucket: Bucket, k: int) -> list[Candidate]: ...
class Scorer(Protocol):
    async def score(self, query: Query, cands: list[Candidate]) -> list[Scored]: ...
class Writer(Protocol):
    async def on_event(self, event: CaptureEvent, store: MemoryStore) -> None: ...
class MemoryExtension(Protocol):
    """Model-facing driver (optional): contributes tools so capable models can
    interact with memory directly — recall, pin, page, correct. Capability-gated
    per model family; the passive pipeline never depends on them (P0)."""
    def tools(self) -> list[BaseTool]: ...
```

Design notes:
- **`assemble()` is called by both graphs at the same point** (just before the LLM call,
  after context limits are ensured — unifying the current worker-after / persistent-before
  compaction divergence) and returns ready-to-inject blocks. Injection message *mechanics*
  (synthetic tool pairs, `memory_inject_`/`knowledge_inject_` prefixes, summarization
  stripping) are unchanged and move inside the manager.
- **`capture()` replaces every scattered extraction/curation call site** — the worker's
  interval + phase-boundary triggers, the persistent loop's interval trigger, the
  session-end/idle-archive calls in `persistent_app.py` (which have never worked — B1),
  and `todo_complete` queuing. One event vocabulary; writers subscribe to event kinds.
- **The KB is a bucket.** `kb_write`/`kb_update` tools keep writing as today (active
  notebook, P0); the manager *reads* the KB through the same retrieval plane with
  provenance labels — one budget, one gate, killing the separate uncapped KB block (B5).
- **`stats` is first-class.** Every assemble emits what was considered, scored, gated, and
  injected. This is simultaneously the eval-harness tap, the cockpit "why did it say
  that" surface, and (later) the learned scorer's training-data flywheel.
- **Composition is data-driven, and extensions ride existing hot-swap.** `from_config()`
  binds whatever `memory.pipeline` declares — so swapping a scorer, adding a retriever, or
  enabling a model-facing extension is a config change, not a code change, and resolves
  per expert/model family through the normal `$extends`/matrix path. Extension tools join
  the agent's tool set through the same per-turn `get_current_tools()` refresh that
  already serves model hot-swap and plan-mode toggles in persistent sessions — enabling a
  memory extension mid-session needs no new machinery.

### 2.2 Buckets

A bucket = a named, scoped memory collection with its own lifecycle policy.

| Bucket | Scope key today | Notes |
|---|---|---|
| `session` | `job_id` (threads reuse it) | per-conversation working memory |
| `project` | `project_id` | today's `project_scoped=true` behaviour |
| `personal` | *(new)* user-level | preferences, corrections — follows the user across projects |
| `shared/custom` | *(new)* arbitrary | team/org buckets, attach like datasources; on/off per agent |

v1 **layers buckets over the existing `job_id`/`project_id` columns** (no schema surgery
in the foundation phase); a real `bucket_id` migration comes with Phase 6 when
`personal`/`shared` materialize. Bucket attach/detach/toggle deliberately mirrors the
datasource-attachment UX — and bucket *sharing* is a product feature (multi-tenancy M1
tie-in), not just plumbing.

---

## 3. Current state → target mapping

Ground truth with evidence: [`agent_memory_current_state.md`](agent_memory_current_state.md).

| Today (audited 2026-06-10) | Becomes |
|---|---|
| Injection logic forked: `graph.py:888-1037` vs `persistent_graph.py:526-658` | both graphs call `manager.assemble()` |
| `RecallStore.retrieve()` two-tier: TTL-pinned tier = **whole non-expired set, relevance-blind, no LIMIT** (`recall_store.py:670`) + hybrid top-150 | always-inject = explicit **policy flag on few memories** (bounded); everything else earns injection through scoring + gate |
| KB block: hardcoded 5 notes, **no token cap** (`graph.py:971`, `persistent_graph.py:594`) | KB = one bucket on the shared plane; one budget |
| Query = top-todo+phase (worker) / last user msg (persistent) | unified **request digest** (recent window + task frame), §4 |
| Observer every 5 turns + phase boundary; persistent: ~~empty prompt + locked cadence~~ (**B1 — fixed 2026-06-10**, persistent now at worker parity for extraction) | `capture()` events; boundary-driven extraction with turn fallback |
| Assembler TTL boost/deprecate, **worker-only** (B3) | curation writer behind the seam, both paths, flag-controlled — and an explicit **ablation candidate** (P5) |
| Dedup = cosine≥0.85 merge-on-write (`recall_store.py:356`) | ingestion verdict: top-K → aux-LLM ADD/UPDATE/MERGE/NOOP |
| Trigger phrases (`retrieval_messages`, embedded, UNIONed in dense channel) — **our original idea, already wired** | kept and extended: the anticipatory channel gets a matching upgraded query side |
| Neo4j: write-mostly, never read on hot path; edges ~empty (curator default-off) | graph retrieval = optional **plugin**; pgvector self-sufficient (fixes B8 coupling); keep/cut verdict in Phase 7 |
| `MemoryConfig` knobs, several dead (B9 — **dead knobs deleted 2026-06-10**) | `memory.*` config maps 1:1 to manager components |
| Grow-only table, no GC (B6) | GC writer + retention policy |

---

## 4. The idea catalog (all of it — parked, plugin-shaped)

Everything we want the architecture to *permit*, tagged by evidence strength:
**[proven]** quantified + survived verification · **[convergent]** field-standard practice,
efficacy unproven · **[hypothesis]** ours/novel, needs the harness · **[ours-built]**
already implemented and wired today.

**Retrievers**
- Dense vector (qwen3-embedding-8b, 4096-d) **[ours-built]** — keep.
- Sparse keyword (Postgres tsvector) **[ours-built]** — keep; BM25/SPLADE upgrade optional later.
- **Trigger-phrase / anticipatory channel** **[ours-built, original]** — observer generates
  "in what future situation is this needed" phrases (e.g. login credentials → *"I need to
  log in to test this"*), embedded separately, matched against the incoming call. Neither
  Mem0 nor Zep nor A-MEM has this. Extend, don't replace.
- Recency channel **[ours-built]** — keep inside RRF.
- Tags / regex / exact-match retriever **[hypothesis]** — cheap, precise for IDs, env names, error codes.
- Graph traversal (multi-hop over KB edges) **[convergent, task-dependent]** — only wins for
  multi-hop/sensemaking recall (brief 04); ship as an A/B-able plugin, Phase 7 verdict.
- Agentic search (aux agent inspects the upcoming call and queries arbitrary sources)
  **[hypothesis]** — powerful, expensive; async/signal-gated tier only (§ latency).

**Scorers**
- RRF fusion (dense 0.6 / sparse 0.3 / recency 0.1, rrf_k=50) **[ours-built]** — keep as
  stage 1; lift hardcoded weights into config (B9 adjacent).
- **Cross-encoder reranker** over fused top-50 **[proven, +17pp MRR on a same-shaped stack]**
  — `bge-reranker-v2-m3` self-hosted; start **in-cluster CPU** (fails loudly local, no
  workstation-GPU dependency — the aux-outage lesson), promote to GPU if latency demands.
- **Ensemble gate** — inject only above a combined-score threshold; injecting *nothing* is a
  valid and common outcome **[proven as a principle — Self-RAG, gate relocated system-side]**.
- **Learned scorer** (xgboost / small NN: features = per-channel scores, memory metadata,
  request features → inject? TTL?) **[hypothesis]** — needs labeled pairs; fed by the
  `AssembleStats` flywheel + harness. Phase 7+.

**Policies**
- Always-inject flag (bounded "core": preferences, project constraints — relevance-independent
  by design) **[convergent]** — replaces TTL-pinning-as-injection; selection via explicit
  pins + curation writer, hard token cap.
- Per-call token budget across **all** memory+KB blocks (closes B5) **[ours, fixes audit gap]**.
- **Latency tiers** — fast path (vector + trigger + rerank, tens of ms) on every call;
  expensive paths (agentic search, graph walks) async or signal-gated, never blocking every
  turn. Hard `assemble()` deadline like persistent's existing 5 s guard.
- Dedup-on-assemble (don't inject near-duplicates in one payload) **[hypothesis]**.
- Provenance labels per injected item (bucket, source, age, score) **[hypothesis]** — lets
  the model weigh trust, and makes the cockpit memory panel possible.

**Writers (the passive capture side)**
- Boundary-driven extraction (phase end, compaction, session end, idle, topic shift) with
  turn-count fallback **[convergent — brief 05]**; completeness > precision: drop the
  write-time importance gate (audit: it's ~theatre anyway), gate at retrieval.
- Multi-pass / self-questioning extraction (second aux pass: "what did this window contain
  that we failed to capture?") **[convergent — brief 05]**.
- Trigger-phrase generation **[ours-built]** — extend to generate richer hypothetical
  contexts (the login-credentials pattern).
- **Ingestion verdicts**: new candidate → top-K similar → aux-LLM ADD / UPDATE / MERGE /
  NOOP **[convergent — Mem0/RMM pattern]**; replaces lossy cosine-0.85 merge; the write-side
  hook for supersede.
- **Bi-temporal supersede**: `valid_from/valid_to` + `ingested_at/retired_at` columns +
  `superseded_by` FK on `memories` (and `knowledge_index`); default retrieval filters to
  currently-valid; point-in-time queries keep history **[convergent — Graphiti; the
  washing-machine fix]**. pgvector columns first; **no graph required** (brief 04's
  decoupling insight).
- Curation writer (what's pinned in the always-on core; demotion housekeeping) — the
  assembler reborn with a different job; **ablation candidate from day one** (brief 02).
- GC / retention (closes B6: today rows below the 0.4 retrieval floor are stored forever
  yet permanently unreachable).

**Model-facing extensions (the model-managed camp, as drivers — per P6)**
Progressive enhancement for capable model families, never load-bearing (P0). Gated through
the model-family matrix exactly like prompts/settings; a weak model simply gets an empty
extension list and loses nothing.
- `recall_memory` tool — deep paging beyond the injected slice **[convergent — MemGPT]**.
- `remember` / `pin` tool — explicit writes into the always-on core or a chosen bucket
  **[convergent — frontier-lab pattern]**.
- Page/file-view extension (MemGPT-style virtual context) **[convergent]** — the
  abstraction subsumes the competing paradigm rather than fighting it; runnable as an A/B
  arm on the harness against the passive pipeline.
- Memory-correction tool ("that's outdated/wrong") — model-initiated supersede, feeding
  the same bi-temporal path **[hypothesis]**.

---

## 5. Roadmap

Phases 1–2 are **the commitment** (foundation + instrument). Phases 3+ are
evidence-driven: each lands behind a `memory.*` flag, defaults to current behaviour, and
flips only on a green harness delta (P5). Bug track B1–B11
([`memory_bugs.md`](../issues/memory_bugs.md)): **everything worth fixing before the
seam is done** — step 0 (2026-06-10): B1 + B3-honesty + B9 + vestigial-code sweep;
pre-flight (2026-06-11): B2 halfvec migrations shipped + k3d-verified (Phase 3's
`ef_search` tuning unblocked) and B4 dimension guard. The remaining bugs
(B3-wire/B5/B6/B7/B8/B10/B11) are absorbed by phases by design — the old code stays
frozen until Phase-1 equivalence fixtures pin it (mapping in `memory_bugs.md`
§Suggested order).

### Phase 1 — The foundation: MemoryManager seam · ~1–1.5 wk ← **implementation + closure steps 1–2 complete (k3d verify PASSED, flag ON committed, step-1 catches fixed 2026-06-11); soak → delete legacy remain** (see implementation log + closure runbook)
Extract all assembly + capture logic from `graph.py` / `persistent_graph.py` /
`persistent_app.py` into `src/services/memory/` behind the §2.1 interface. Both graphs
hold one `MemoryManager`; neither touches `RecallStore`/`KnowledgeStore` directly. v1
internals = **current behaviour transplanted** (same RRF, same two-tier retrieve, same
budgets, same injection messages) — a strangler refactor, not an improvement. The
**plugin registry + `from_config()` composition ship in this phase** (P6), with current
behaviour as the sole registered pipeline — the kernel arrives first, drivers follow.

By construction this also:
- **absorbs the B1 fix** — B1 was fixed standalone 2026-06-10 (matrix prompt resolved at
  session setup + threaded into loop and teardown sites, real `observer_interval`), so
  Phase 1 transplants a *working* baseline; the session-end/idle-archive call sites
  become `capture(kind="session_end"|"idle_archive")`, and the B1 regression suite
  (`tests/test_persistent_memory_extraction.py`, pinned against the real `MemoryConfig`)
  doubles as Phase-1 equivalence collateral;
- **makes B3 truthful** — the assembler/curation writer is callable from both paths,
  flag-controlled (config honesty-fixed to `enabled: false` on persistent 2026-06-10;
  whether it *survives* is Phase 5's ablation);
- unifies query formation (request digest: recent window + task frame) — *flagged*, since
  it's a behaviour change: `memory.query.digest` default off until Phase 2 measures it;
- gives `AssembleStats` from day one (telemetry + future training data).

**Acceptance:** all existing memory/KB tests green; new equivalence tests assert
worker-path payloads are byte-stable vs pre-refactor fixtures; persistent path asserts
*parity with worker* (prompt loaded, cadence config-driven, teardown capture fires);
`graph.py`/`persistent_graph.py` contain zero direct store calls; lint+tests at file
granularity per CLAUDE.md verify loop.

**Phase-1 closure runbook (status 2026-06-11: steps 1–2 DONE — step-1 verify
PASSED with findings below, all catches fixed same day, flag ON committed;
step 3 soak is current).** All code is on develop: seam + plugins (slices
1–4), cutover wiring + per-mode YAML pipelines + 20 wiring tests (slice 5 +
the step-1 round-trip regression) + the step-1 fix round
(`tests/test_session_config_plumbing.py`, 12 pins). Closure is four steps;
the unit suites can't cover step 1 (real stores, real aux LLM, real timing),
and deleting legacy before step 3 would destroy the flag's rollback path.

1. **Live k3d verify, flag on.**
   - *Flip locally (don't commit):* set `manager: enabled: true` under `memory:`
     in `config/defaults.yaml` + `config/persistent_defaults.yaml`. With Tilt
     running, `config/*` propagates via the agent image rebuild (~50 s).
     Worker-only surgical alternative: submit one job with
     `config_override: {"memory": {"manager": {"enabled": true}}}` — agent.py
     deep-merges overrides before config parsing.
   - *Bind signal (both modes):* `MemoryManager bound: {...}` with the pipeline
     summary at job start / session setup
     (`kubectl --context=k3d-srw -n srw logs -l srw/managed-by=agent-provisioner -f`).
     A typo'd pipeline name fails the job/session at setup **by design**.
   - *Worker:* run a job in a project (so the KB path is live). Expect
     `Memory injection: N memories retrieved` / `Knowledge injection: N notes
     retrieved` once memories exist; the `memory_inject` audit steps now carry a
     `stats` dict. Compaction-summary capture only fires on long jobs — optional.
   - *Persistent:* session ≥ 5 turns → `Memory extraction triggered at turn 5`;
     end via `/done` → `Final memory extraction complete`; **the new B11 path**:
     end a session via the cockpit ✕-button (DELETE thread → `/session/detach`)
     → `Terminate(rest_detach): final memory capture complete` — this log line
     did not exist before slice 5. Idle-archive
     (`Idle archive: memory extraction complete`) can be left to soak, as B1 was.
   - *Rows:* `kubectl --context=k3d-srw -n srw exec sts/srw-pgvector -- psql -U
     srw -d srw_vector -c "SELECT source, count(*) FROM memories WHERE created_at
     > now() - interval '1 hour' GROUP BY source;"` — expect `observer` rows
     (plus `compaction` on long jobs).
   - *Failure surface:* grep the same logs for `failed (contained)` /
     `Memory writer` warnings and check `stats.errors` in the audit data — both
     should be empty. Containment means a broken plugin degrades loudly instead
     of killing the turn, so the absence of warnings *is* the pass signal.

   **Step-1 execution findings (2026-06-11, k3d — PASSED, three real catches).**
   Verified live: bind logs in both modes with the right per-mode pipelines;
   worker read path (`Memory injection: 2 memories retrieved` + `Knowledge
   injection: 5 notes retrieved` on job `fbccb2e5`; audit `memory_inject` rows
   carry legacy `count`/`total_tokens` plus the new `stats` dict —
   `per_retriever {recall_two_tier: 2, kb_notes: 5}`, `errors: []`); worker
   write path (interval extraction `extracted 2, stored 2`, phase-boundary +
   compaction windows exercised, observer rows in pgvector for both the scholar
   and main jobs); persistent in-loop (`Memory extraction triggered at turn 5`,
   thread `774b31fc`); persistent teardown via `/done` archive (`extracted 3,
   stored 3` + `Final memory extraction complete`, 3 pgvector rows, thread
   `ee9c2df8`). Zero seam-layer containment warnings anywhere; the only
   failures were aux-router `TimeoutError`s contained inside
   `extract_and_store_memories`' non-fatal handler — identical to legacy (the
   flaky router even tripped the `AUXILIARY MODEL DEGRADED` latch once). The
   catches:
   1. **Dispatch round-trip dropped the flag (FIXED).** Every dispatch path
      re-parses the live config after `dataclasses.asdict()` + `deep_merge`
      (job `config_override` in `src/agent.py`, session assembly and
      `config.update` in `persistent_app.py`). `asdict` emits the flat
      dataclass field `manager_enabled`; the parser read only the YAML nesting
      `manager.enabled` → the flag silently reset to **False on every
      dispatched job/session**. Fixed in `_parse_memory_config` (accepts both
      shapes); pinned by `test_manager_flag_survives_dispatch_round_trip` in
      the cutover suite.
   2. **Sessions can bind the worker pipeline** through two pre-existing
      config_name plumbing holes (bare `ThreadCreateRequest` defaults to
      `"defaults"`; `/session/attach` pool reuse ignores the thread's
      config_name) — post-cutover that silently drops `teardown_extractor`.
      Filed as `docs/issues/session_config_name_plumbing.md` — **FIXED +
      live-verified the same day** (new API default; config_name carried on
      all three orchestrator attach sites and forwarded by BOTH agent attach
      routes, incl. the dual app the job pool actually runs; pool-attached
      session on a worker-booted pod bound the persistent pipeline).
   3. **The ✕-button signal above is unreachable on k8s**: the orchestrator's
      thread DELETE deletes the agent pod directly (never calls
      `/session/detach`), so `_terminate_session` — and with it the B11
      capture — cannot fire on that route. The teardown writer was verified
      through the archive route instead. **CLOSED the same day** via
      orchestrator detach-then-delete in `_release_thread_resources`
      (live-verified: DELETE waited 59.5 s for `extracted 3, stored 3` +
      `Terminate(...): final memory capture complete` before teardown; see
      memory_bugs.md B11). The runbook's ✕-button signal is therefore valid
      again — on dual pool pods it now also reads `Terminate(rest_detach)`.
   Also fixed en route (unrelated to memory): `docker/Dockerfile.agent.dev`
   still carried the playwright layer that fc42d052 removed from the prod
   Dockerfile — every Tilt agent rebuild had been failing since that commit.

2. **Flip the default** ✅ DONE 2026-06-11: `manager: enabled: true` committed in
   both defaults files (with the step-1 fixes: loader round-trip, config_name
   plumbing, detach-then-delete).
3. **Soak on dev** ← CURRENT (passive — starts when the push rolls out via
   CI → Fleet, ~30 min). Real workloads do the work; the check-in is a
   five-minute grep, not a phase:
   - *Seam failure surface (must stay empty):*
     `kubectl --context=main -n superhuman-remote-worker logs -l srw/managed-by=agent-provisioner --since=24h | grep -E "failed \(contained\)|Memory writer"`
     — these were ZERO on k3d; any hit is a real seam bug. Aux-router
     `TimeoutError`s inside `Memory extraction failed (non-fatal)` are the
     known flaky-router infra noise, identical to legacy — not a soak failure.
   - *Rows keep accruing:* the pgvector `memories` count by `source`/day on
     the dev vector DB; plus the usual signals (bind lines on new
     jobs/sessions, "Final memory extraction complete" on session ends).
   - *Exit criterion:* a few days of normal dev usage — multiple real jobs
     AND sessions — with an empty seam surface. Rollback at any point is the
     one-line config revert; that is the flag's whole job.
4. **Delete the legacy blocks:** remove the `memory_service is None` branches and
   the direct-store code from graph.py / persistent_graph.py / persistent_app.py.
   This is when the "zero direct store calls" acceptance is met; the equivalence
   suites' verbatim reproductions become the reference copy of the old behaviour
   and `tests/test_memory_cutover.py` keeps guarding the wiring.

### Phase 2 — Eval harness against the seam + baseline · ~1–1.5 wk ← **✅ COMPLETE 2026-06-12 (all acceptance gates closed with real _S runs; numbers in the implementation log)**
A standalone offline harness (`eval/memory/` — run recipes +
LongMemEval→seam mapping in [`eval/memory/README.md`](../../eval/memory/README.md))
that drives **`MemoryManager.assemble()` directly** — the seam makes this
dramatically simpler than v1's plan (no graph spin-up). Sessions are ingested
**incrementally** (memories accrue through `capture()` as in production, not
batch-loaded). Reports per config arm:
- **Retrieval:** ✅ run — Recall@k / NDCG@k / coverage / first-hit vs answer-location
  labels. Flat anchor R@5 0.940; current system R@5 0.200. (Bespoke
  production-trace set still to come; avoid LoCoMo as primary, ~6.4% wrong key.)
- **End-task:** ✅ run — LongMemEval's verbatim judge prompts, scored **separately**
  from retrieval (flat 0.74–0.76 vs seam 0.25–0.30 — reading really is its own
  bottleneck: 68% over-abstention with the answer in context). Calibration: 93.1%
  on a 29-item hand-labelled slice, judge-vs-judge 98% — formal >97% claim wants a
  ~100-item label pass.
- **Contradiction-survival probe:** ✅ run — `contradiction.py` + committed 8-probe
  fixture; seam baseline: update-above-original 0.125, reader stale 0.125/miss 0.25.
- **Cost:** ✅ — tokens injected + assemble latency per arm (seam injects its whole
  ~122-item store ≈ 6.8k tok; flat budget-trims to ~21 rounds ≈ 9.7k).
- **Ablation switches:** ✅ — arms deep-merge raw config overrides (the full
  `memory.*` surface incl. pipeline lists) over any base YAML.
Recall-shape: covered by the by-type slices (`multi-session` coverage@k is the
multi-hop signal); finer per-hop breakdown only if the Phase-7 verdict needs it.
**Acceptance:** reproduces a published LongMemEval baseline within tolerance (✅ —
placement + shape vs the paper's **_M** dense rows; the paper publishes no _S
retrieval table); A/Bs two configs and emits deltas (✅ — flat vs persistent_current,
−0.74 R@5 / −0.46..0.51 end-task); **baseline numbers for the current system
recorded** (✅ — `runs/seam_s20`, N=20 stratified prefix, seed 0).

### Phase 3 — First plugin wave (measured): reranker + gate + bounded core · ~1–1.5 wk ← **✅ COMPLETE 2026-06-12, all 4 slices measured (results tables in the implementation log); production flip = separate rollout decision**
Slice order (re-prioritized by the Phase-2 decomposition — extraction bias and
injection order are the measured root causes, not search):
1. **Completeness-biased extraction prompt** ✅ measured — end-task 0.30→0.45, facts
   verifiably present at fact granularity; knowledge-update degrades (more
   stale/current coexistence → Phase 4).
2. **Reranker as a Scorer** ✅ measured — R@5 0.10→0.95 same-corpus; needs
   `keep_pinned_first: false` + `top_k` ≥ store size on multi-session corpora
   (~95 % TTL-pinned) and a generous timeout for full-store batches.
3. **Bounded injection** ✅ measured — `bounded` policy (post-scorer cap): R@5 1.0 at
   312 tok/turn (29×); end-task parity-within-noise vs the full dump; without the
   reranker it craters (0.05) — ordering is the prerequisite, now proven.
4. ✅ measured — gate ships with two modes: absolute thresholds FALSIFIED (R@5 0.80 —
   reranker score scale varies orders of magnitude per query); `mode: relative`
   (floor = 0.01 × top) holds R@5 1.0 at 3.7 items / 111 tok/q (80× under the dump),
   end-task 0.40 ≥ b10's 0.35, reader abstention 1.0. Request digest built behind
   `memory.query.digest` (question-time parity by construction; windowing payoff
   needs ingest-time A/B). B5 one-budget via `memory.bounded.include_knowledge`.
   `ef_search` deferred — retrieval saturated at eval scale. Always-inject core
   policy demoted: Phase-2 showed TTL-pinning is NOT the dominant ordering failure
   at question time.
**Acceptance (harness):** reranker arm lifts Recall@k (✅ — +0.85 R@5) and end-task
(✗ on the full dump — reading order doesn't matter when everything is injected; the
lift shows up via slice 3 instead); gating cuts injected tokens with no end-task
regression (✅ — 111 vs 312 tok at 0.40 vs 0.35, abstention behaviour improves to
perfect); bounded core + gated slice ≥ full-injection quality at materially lower
tokens/turn (✅ — 0.40 vs 0.45 at 1.2 % of the tokens, where the entire remaining
gap is one knowledge-update question = supersede = Phase 4 by construction).
**Production-candidate stack** (rollout decision pending): `scorers: [reranker]`,
`policies: [gate, bounded]` with `gate: {threshold: 0.01, mode: relative}`,
`bounded: {max_items: 10}`, reranker `keep_pinned_first: false` + top_k sized to
cover the store (gate arms leak unscored tail items fail-open otherwise).

### Phase 4 — Lifecycle writers: verdicts + bi-temporal supersede · ~1–1.5 wk ← **✅ COMPLETE + MEASURED 2026-06-14 (GATE-B flipped in defaults → shipping to dev)**
Ingestion verdicts (ADD/UPDATE/MERGE/NOOP, aux-LLM) ✅; bi-temporal columns via
`migrations/vector/0006_bitemporal_memory.sql` ✅ (NOT `knowledge_index` — it already
supersedes via `status='active'`, and the KB is the model's active notebook, P0);
supersede policy (retire-and-exclude, point-in-time queryable via `valid_from`/`valid_to`)
✅; write-gate dropped via `memory.extraction.write_gate` (completeness>precision) ✅.
Boundary-driven extraction was already always-on (phase_boundary + teardown writers,
interval as turn fallback) — no new trigger knob. Cost guard ✅: the `review_floor`
similarity gate means a genuinely-new fact is a straight ADD with zero LLM calls, so
verdict calls are bounded to ≤1 per stored memory. Behind `memory.ingestion.enabled`
(default off); `store()` keeps the legacy cosine-dedup path byte-for-byte when the
verdict service is unwired. **Slice 4 (measure) ✅ DONE 2026-06-14** (arms
`persistent_complete_verdict` + `contra_complete_verdict`, fresh seam ingest on
`srw_eval`, requeried through the rerank read stack + judged) — see the
implementation-log entry for the full result.
**Opening baseline (recorded 2026-06-12, `runs/contra_complete_rerank`):** over a
completeness+rerank corpus the probe reads update_above_original 0.75 (seam 0.125,
flat 0.625) and reader current 1.0 / stale 0 / miss 0 on the 8-probe fixture — the
small-corpus case is already rescued upstream; what supersede must close is the 2/8
stale-first ties relevance can't break, the at-scale binding-loss miss (s20
decomposition), and the knowledge-update end-task slice (0/4 on every reranked arm —
relevance ordering puts stale immediately next to current, measurably worse than the
legacy recency-blended order at equal tokens). Success = original_injected → 0 on
superseded facts, KU end-task recovers toward its 0.5 baseline.
**Acceptance (harness) — all met 2026-06-14:** contradiction-survival probe flips to
current-fact answers ✅ (reader current 1.0, `original_injected` 1.0→0.25);
knowledge-update slice improves ✅ (0/4 → 3/4, past the 0.5 target; overall end-task
0.40 → 0.50); no regression elsewhere ✅ (R@5 1.0, 111 tok unchanged); verdict-call
budget held ✅ (review_floor cost guard). N=20 caveat as Phase 3.

### Phase 5 — Ablate & cut · ~0.5 wk + data wait
A/B the inherited consolidation layer — curation writer (née assembler), TTL semantics,
dedup — via harness switches. **Remove what doesn't move the needle** (finding cargo-cult
is a win: simpler system, fewer background LLM calls). Also the vestigial-code sweep
(~1,600 lines cataloged in current-state §6).
**Acceptance:** documented keep/cut per op, each backed by a harness delta.

### Phase 6 — Buckets productized · ~1 wk
Real `bucket_id` schema (migration), `personal` + `shared` buckets, attach/toggle UX
(datasource-attachment pattern), sharing semantics (multi-tenancy M1 tie-in). Cockpit
memory panel fed by `AssembleStats` provenance (optional stretch).

### Phase 7 — Verdicts & frontier plugins · decision + experiments
- **Graph-keep verdict** from Phase-2 instrumentation: if recall is single-hop, fold KB
  source-of-truth into pgvector and retire Neo4j from the memory path (it already isn't on
  the hot path — audit); if multi-hop appears, wire the graph retriever plugin properly.
- **Learned scorer** experiments once the AssembleStats flywheel has data.
- Agentic-search retriever behind the async tier, if a use case demands it.

---

## 6. Config & rollout

- **The pipeline itself is config** (P6). Sketch — names resolve against the plugin
  registry; per-expert/model-family variation via the normal `$extends` + matrix path:

  ```yaml
  memory:
    manager:
      enabled: true            # Phase-1 cutover guard
    pipeline:
      retrievers: [dense, sparse, trigger_phrase, recency]
      scorers:    [rrf]                  # Phase 3 adds: reranker, gate
      policies:   [token_budget, ttl_pinning]   # Phase 3: core_always_inject replaces ttl_pinning
      writers:    [interval_extractor, phase_boundary_extractor,
                   trigger_phrase_gen, cosine_dedup]   # Phase 4: ingestion_verdict, bitemporal, gc
      extensions: []           # e.g. [recall_tool, pin_tool] for capable model families
  ```

- Further knobs under `memory.*` (parsed in `_parse_memory_config`, both
  `config/defaults.yaml` + `config/persistent_defaults.yaml`): `memory.query.digest`,
  `memory.reranker.{model,endpoint,candidates}`, `memory.gate.threshold`,
  `memory.core.{budget_tokens,pin_sources}`, `memory.budget_tokens` (unified, KB
  included), **`memory.ingestion.{enabled,verdict_top_k,review_floor}`** and
  **`memory.extraction.write_gate`** (Phase 4, shipped 2026-06-14 — the verdict
  supersede landed as `memory.ingestion`, not the placeholder `memory.bitemporal`),
  `memory.gc.{enabled,retention_days}`, `memory.buckets.*` (Phase 6),
  `memory.hnsw.ef_search`.
- ~~Delete the dead knobs (B9)~~ — **done 2026-06-10** (`dense/sparse/recent_results`,
  `observer_model`, `observer_base_url`, `embedding_model`, `storage` removed from
  `MemoryConfig`, the parser, and both defaults YAMLs); the new interface starts from an
  honest config surface.
- Every flag defaults to current behaviour; flip per-expert via `$extends` after green
  harness runs. Migrations as numbered files under `migrations/vector/` (`.notx.sql` for
  `CREATE INDEX CONCURRENTLY`); never edit the frozen `vector_schema.sql`.

## 7. Risks

- **Refactor regression risk (Phase 1)** — mitigated by equivalence fixtures + the
  parity test suite; worker behaviour is the reference, persistent is *intentionally*
  upgraded to parity (its current behaviour is partly broken — B1).
- **Reranker latency** per turn — CPU-first sizing on top-50 candidates; the gate reduces
  reader load; measured on the harness cost arm before default-on.
- **Background-LLM cost** of verdicts/curation — async on the aux LLM, bounded calls;
  watch the aux-outage failure class (silent degradation — `AuxHealth` + B4 dim-guard).
- **Judge calibration** (>97% claim dips on preference/abstention slices) — calibrate on a
  hand-labelled slice, report per-category.
- **Benchmark mismatch** — LongMemEval is needles-in-filler; the production-trace set is
  the one that ultimately matters for a project-scoped coding agent.
- **Scope creep** — the catalog (§4) is deliberately bigger than the roadmap; only
  Phases 1–2 are committed, everything later must win on the harness.

## 8. Open questions

- Does consolidation/curation improve end-task or just save tokens? (Phase 5)
- Single-hop vs multi-hop recall split on real traces → graph verdict. (Phases 2, 7)
- Does a graph's multi-hop benefit come *entirely* from cross-document entity resolution
  — which our agent-authored Notes largely lack? (Phase 7; candidate for a targeted
  research re-run before the verdict.)
- Does forgetting/GC ever *raise* accuracy by removing distractors, or only save space? (Phase 5)
- Reranker on our latency/quality frontier: bge-reranker-v2-m3 CPU vs GPU vs API. (Phase 3)
- Bucket ACL model for `shared` (per-user grants? org-level?) — defer to Phase 6 + M1.
- Where does `personal` bucket data live for multi-tenant SaaS (per-user encryption?) — Phase 6 + M1.C.

## 9. References

- [`agent_memory_current_state.md`](agent_memory_current_state.md) — the audited baseline this doc designs against
- [`../issues/memory_bugs.md`](../issues/memory_bugs.md) — independent bug track (B1–B11; B1/B2/B4 fixed)
- [`ai-memory-research/results/01_frontier_labs_report.md`](../../ai-memory-research/results/01_frontier_labs_report.md) — bounded+on-demand norm; model- vs system-managed camps
- [`ai-memory-research/results/02_academic_sota_report.md`](../../ai-memory-research/results/02_academic_sota_report.md) — consolidation efficacy refuted; Self-RAG backs gating
- [`ai-memory-research/results/03_retrieval_report.md`](../../ai-memory-research/results/03_retrieval_report.md) — reranker #1 ROI; keep RRF/qwen3; gate by score
- [`ai-memory-research/results/04_graphs_temporal_report.md`](../../ai-memory-research/results/04_graphs_temporal_report.md) — graph task-dependent; bi-temporal in pgvector columns
- [`ai-memory-research/results/05_lifecycle_eval_report.md`](../../ai-memory-research/results/05_lifecycle_eval_report.md) — harness-first; boundary extraction; completeness>precision; conflict = #1 failure

> v1 of this doc (2026-06-07, "eval-first" phase ordering) is superseded by this
> restructure; its phase content survives above in new order. Its appendix of
> subsystem gaps moved to the current-state doc (§5–6) and the bug tracker.
