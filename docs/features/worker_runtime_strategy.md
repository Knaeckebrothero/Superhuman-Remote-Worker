---
tags:
  - feature
  - architecture
  - agent
  - strategy
  - harness
related:
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[platform_for_agents]]"
  - "[[agent_open_source_split]]"
aliases:
  - worker runtime strategy
  - react mode
  - JobDriver
  - harness adoption
  - guardrail loosening
---

# Worker runtime strategy — ReAct mode, foreign harnesses, and the loosening path

> **TL;DR / Decision.** The question was "how do we add a ReAct mode to the worker agents — or should we tear down the strategic/tactical split?" After walking three build options (a ReAct `JobDriver` on the persistent loop; a promoted engine/driver interface; adopting foreign harnesses like Claude Code/Codex as the worker), the decision is **none of them now**: continue the 07-31 direction of **loosening the phased worker's guardrails in place**, one measured slice at a time, until it converges on what every long-horizon system converges on — a ReAct loop with an externalized, recited plan and verification gates. The JobDriver and engine promotion are **shelved, not dead**. Foreign-harness adoption is **rejected** for the general worker (topology + commoditization grounds), with one narrow future exception (a Claude Code lane purely for Max-subscription economics). The binding discipline is **paired measurement between slices** — the re-measurement owed since 07-31 is the first work item.

**Status:** Decision record of a design session, 2026-08-02/03. No implementation in this session. Extends the 07-31 decision in [[phase_model_overhead_amnesia_loop]] ("keep split, huge tactical phases, re-measure, then decide the runtime split") and complements [[platform_for_agents]] (which remains the strategy doc for *hosting* foreign harnesses as a product — a different question from *adopting* one as our worker).

---

## 1. Starting question

"Can we add a proper ReAct mode to the worker agents? We already have persistent sessions, but those are wired as sessions. Maybe a selectable mode — or maybe tear down the strategic/tactical switch entirely."

The session walked four positions in sequence; each is recorded below with its reasoning so it doesn't get re-derived.

## 2. Ground truth the discussion stood on (verified 2026-08-02)

- **Four ReAct-style runtimes exist; the standing rule is "do not build a fifth"** (counted 07-31): `src/graph.py` (the only `StateGraph` — phased worker), `src/persistent_graph.py::run_persistent_loop` (plain `while True` turn loop — sessions AND the officer), `AuxiliaryLLM.agent()`, and the light-subagent runner. Designated consolidation survivor: the persistent loop (per-turn re-read of tools/prompt/context, per-turn commit+push, caller-supplied `messages`, fingerprint stuck guard, ~10 test files drive it headlessly).
- **The worst measured phase-model pain is already fixed but unmeasured.** `force_summarize = False` at the strategic→tactical boundary (`src/graph.py` ~2957, with the full amnesia rationale in a comment); planning default is ONE big execution phase; `request_replan` replaced the destructive rewind; budget re-scoped to `max_tool_calls_per_job: 5000` (`max_tool_calls_per_phase: 0`). All shipped in `99c9aba0`. The 49–66% ceremony numbers predate these fixes. **The 07-31 re-measurement gate never fired** (dev cluster was down that session).
- **Mode-selection plumbing already exists**: per-job `config_override` / orchestrator-side `resolved_config` ride `JobStartRequest`; a runtime key needs zero transport changes.
- **The persistent loop is driver-ready in principle**: it is transport-agnostic behind the `PersistentLoopCallbacks` dataclass (~16 callbacks, many optional); production wiring is the WebSocket app (`persistent_app.py:433`), tests wire fakes, and the officer already runs job-like autonomous work on it (as an orchestrator-driven thread).
- **But session policy is baked into the loop in places**: in-turn LLM retry ceiling hard-coded to 3 attempts ("a user is watching" — `_SESSION_LLM_MAX_ATTEMPTS`, `persistent_graph.py` ~567) vs. the worker's patient `limits.llm_inproc_retries`; the loop has no terminal state (runs until cancelled); `job_complete` is a worker tool, strategic-only (`src/tools/core/job.py`).
- **MCP server**: KB tools (search/get/update knowledge notes) are live on it now — a post-June change; **memory/RecallStore is still not exposed** (only `get_memory_stats`). That remains the unbuilt "prize" layer from [[platform_for_agents]] §3.2.

## 3. Candidate A — the JobDriver (jobs on the persistent loop)

**Definition** (the term comes from the 07-31 notes; nothing is built): a third wiring of `PersistentLoopCallbacks` with no human behind it, plus job-lifecycle glue. `get_user_input` returns the job bootstrap first (description/instructions/context), then "continue, you're unattended" nudges when a turn ends unfinished — also the natural injection point for steering messages. `permission_check` auto-approves (sudo gate stays workspace-side). Token/audit callbacks write to the job log and `llm_requests`. Around the loop: `process_job`'s existing setup (workspace SSH, credentials, datasources, config), a `job_complete` tool, a step/token budget → `budget_exceeded` freeze. On completion the driver returns the same `{should_stop, goal_achieved, freeze_data}` dict the graph returns, so `report_completion`, critic spawning, and orchestrator status authority work unchanged. Selection: `execution.runtime: phased | react` per expert or per job.

**Why it led for most of the session**: no fifth runtime; reuses the survivor loop; steering becomes a user-turn injection (cleaner than phase-boundary injection); v1 resume = feedback-style fresh bootstrap + workspace archaeology (per-turn commit+push already makes the workspace the durable state).

**Why it's shelved**: see §7 — not wrong, just not needed while the loosening path is live and unmeasured.

## 4. Candidate B — "proper driver selection" (engine promotion)

Raised as: isn't wrapping the session machinery a hack — shouldn't there be a first-class driver system? The honest middle: **promote the seam that exists**. `PersistentLoopCallbacks` already *is* ~90% of a driver interface; the work is parameterizing the session-hard-coded policies (retry ceiling, idle semantics, termination as a real engine concept), keeping the WebSocket transport as driver #1 with **bit-identical session behavior**, then writing the JobDriver as driver #2 first-class instead of cosplaying as a session (the officer already cosplays; a second cosplayer entrenches it).

Two deliberate non-goals: runtime *selection* stays a plain config branch in `process_job` (no interface needed for a two-way if), and the phased graph **never** implements the driver interface — abstracting over it forces lowest-common-denominator design around the runtime we may retire. The cross-runtime contract stays the `freeze_data` completion dict, which both runtimes already speak and the orchestrator adjudicates.

**Why shelved**: it's the right *shape* for consolidation, but it's a refactor of the most user-visible, most bug-historied code path (sessions), taken before the measurement that would justify consolidation at all.

## 5. Candidate C — foreign harnesses as the worker (Claude Code / Codex / OpenClaw)

Motivation: "building and optimizing infra *and* harness at the same time is too much — swap our harness for theirs; we keep workspaces, scheduling, memory, audit." Explored to a concrete architecture before being rejected; recorded because most of it is reusable if the narrow exception (§10) is ever exercised.

- **What transfers cheaply**: one container image (workspace + harness CLI + thin driver); the driver is the harness-agnostic slice of `src/api/app.py` + `orchestrator_client.py` with `process_job`'s body replaced by "spawn harness headless, pump its event stream" (stream-json for Claude Code; per-harness event mapping is the marginal cost). Service tools go on the **MCP server once** — every harness speaks MCP — instead of per-SDK wiring; per-harness tool work rounds to writing `.mcp.json` with a job-scoped `mcp_tokens` token. Our skills are markdown (`.claude/skills/`-compatible); expert personas become a system-prompt-append / generated CLAUDE.md. Workspace tools are **deleted, not wired** — the harness brings its own.
- **The topology finding (the crux)**: foreign harnesses have **no remote-filesystem seam** — their tools are local syscalls, the models are post-trained on those exact tools, and every first-party remote product (Claude Code on the web, devcontainers, Codex cloud) runs the harness *inside* the environment. Forcing our SSH shape via MCP-shim replacement tools works mechanically but buys the worst quadrant: their harness degraded (model driving unfamiliar lookalikes) plus our tool layer still maintained. The viable topology is harness **inside the workspace pod**; sshd stays as the platform's control/inspection interface (cockpit IDE/files, driver lifecycle ops, delegation clones) — SSH was the *mechanism* by which a remote agent reached the workspace, not the invariant itself. The invariants that matter (pod-boundary isolation, durable provisioned PVC/VM, credential partitioning) survive co-residency; credential partitioning via driver sidecar (orchestrator token), job-scoped MCP token, and an auth-proxy sidecar holding the Anthropic token (`ANTHROPIC_BASE_URL=127.0.0.1`, codex-proxy pattern pod-local — needs a spike to confirm subscription auth through a local gateway).
- **Subscription economics** (the sweetener, and the part that survives as §10's exception): Claude Max usage is sanctioned only through the real Claude Code binary (the Agent SDK spawns it; `claude setup-token`); raw Messages-API use of subscription credentials is not — the mirror image of our codex-proxy, which Anthropic's terms don't permit for Claude. Constraints: personal seat, weekly/concurrency caps, no resale as customer capacity, metering shows zero marginal cost, and the token must live where job code runs (same class as the Gitea-admin-credential issue) absent the auth-proxy.
- **Why rejected for the general worker**: (a) the user's requirement was remote-over-SSH tools, which forfeits exactly the harness quality being bought; (b) the commoditization argument below removed the motivation.

## 6. The commoditization analysis

- **True**: tool *implementations* are commodity ("a read tool is a read tool"); the June platform doc reached the same finding from the code side. Environment dominates tool polish — an agent that can run and test its code beats a better-tooled one that can't. SRW's differentiated assets are environment and data assets (SSH workspaces with real dev stacks, critic/verification, change records, KB/memory).
- **Correction**: the harness was never the tools. The recurring bill is the *system*: error-classification taxonomy (408/404 misclassification), compaction policy (the amnesia root cause took measurement + literature to find), per-code-path streaming workarounds, tool-pairing repairs, stuck detection, durable steering, resume. An overnight self-improvement run produces the 99% read tool; it does not produce a correct retry taxonomy. The one genuinely non-commodity residual is **model↔harness co-training** (Claude models RL'd on Claude Code's exact tool semantics) — not replicable from outside, but depreciating: models get more robust to harness variation every release, which erodes their edge *and* forgives ours.
- **The economic conclusion**: "commodity" is an argument about **spend**, and it cuts against building as hard as against buying. Coherent strategies: *buy* (rejected above) or *own-but-freeze* — one boring, model-robust runtime, maintained in bug-fix mode, letting base-model progress do the optimizing. Not coherent: four runtimes under active tuning while calling the layer commodity. Owning preserves what Claude Code can't give us: multi-provider support (the model-family matrix) and deep integration with steering/officer machinery.

## 7. Decision — continue loosening the phased worker in place

The final position, which is also the standing 07-31 plan: **evolve `graph.py` by subtraction** rather than build a ReAct runtime and migrate to it.

- A from-scratch ReAct worker doesn't stay from-scratch: to survive ~2k-request unattended jobs it re-grows plan state, progress tracking, re-planning triggers, and verification — you rebuild your own scaffolding on a new substrate *while* migrating every integration hanging off the old one (checkpoint resume, autonomy levels, critic gating, cockpit todo/archive surfaces, officer sitreps). Addition plus migration, versus subtraction only on the already-instrumented path.
- **The convergence point is the same either way.** The equilibrium every serious long-horizon system lands on is ReAct + an externalized, *recited* plan + verification: Manus's constantly-rewritten todo.md, Claude Code's TodoWrite/plan mode/subagents, Devin's planner. "Loosen until we get there" and "build react" are two roads to the same artifact; loosening is the road made of deletions. (Also why "Devin isn't a basic react loop" is correct — and why the scaffolding we already have is the part worth keeping, made advisory instead of bureaucratic.)
- **Compensating controls make loosening safe**: each hard guardrail removed is replaced by soft supervision that didn't exist when the guardrails were designed — officer sitreps + steering, `request_replan` as the legitimate in-flight adaptation path, the job-level budget parking runaways for a human. Structure traded for oversight.
- The 07-31 measurement gate ("re-measure, then decide the runtime split") **has not fired**; consolidating before that data exists would repeat the original mistake in the other direction.

## 8. Next loosening slices (each reversible, one variable at a time)

1. **Slice 0 — the owed re-measurement**: ✅ **DONE 2026-08-03** — results in [[phase_model_overhead_amnesia_loop]] §10. Headline: ceremony share fell ~55–65% → ~30–45% of LLM spend (developer + scholar, small n, directional); the compounding-strategic-phase curve is gone; but per-turn prompt size roughly doubled (~25k → 33–50k median) because context now survives boundaries while the 15k injection floor is untouched. **Consequence: the next slice is P-3/P-4 injection economics (pinned-tier cap + floor trim), not further phase-structure loosening** — slices 2–4 below are demoted behind it. Also: the ONE-execution-phase default binds almost nowhere (worker_base small jobs were already minimal; scholar/developer override it), automations run developer config (an inbox task ran 29 archives), and worker_base still lacks a clean post-reform sample — the §9 suite is needed for that.
2. **Small-job floor**: the 66%-ceremony finding was two strategic brackets around a 3-minute task. Let trivially-scoped jobs complete without the full bracket — `job_complete` reachable earlier / initial planning collapsed to a single turn.
3. **Merge phase-gated toolsets**: `is_strategic` stops controlling tool availability (kills the tool-enum-vocabulary bug class as a side effect); the alternation becomes advisory.
4. **Todos from gate to recitation**: `check_todos` stops forcing transitions; todos become the plan surface the model maintains because it helps (Manus-style recitation), archives kept for cockpit/officer.

**Endpoint check**: when 3+4 land, the strategic/tactical switch is vestigial — the ReAct mode exists *inside* `graph.py`, arrived at by erosion, and nothing was built.

## 9. Measurement protocol (what "see if it gets better" must mean)

The naive loop ("run jobs, eyeball, tweak, rerun") lies three ways here:

1. **Variance** — N=3 anecdotes confirm anything; identical job+config diverges run-to-run.
2. **Confounds** — between run batches, the model catalog, providers, and images move. Pin model, config sha, image tag (the nightly loop's input-sha CI exists for exactly this reason).
3. **Outcome metric** — "completed" status is nearly worthless: job `396a5d4c` completed 17 phases / 1645 audit entries / zero contract deliverables. Judge outcomes (tests green, deliverable exists + critic passes, citations verify), with cost as the denominator, never the target — otherwise loosening optimizes for confident cheap failure.

**The instrument (a weekend of scripting, not an eval platform)**: a fixed suite of 10–20 representative jobs with checkable outcomes (small fix / medium feature / scholar task / long refactor), drawn from real historical jobs; paired runs config-A vs config-B (exactly one slice different), a few repetitions, submitted overnight by script or as a Centurion campaign; one row per job from existing tables (`llm_requests` counts/tokens, phase-archive timing split, freeze types, `request_replan` count, budget trips, critic verdict, deliverable gate); infra-failure runs (provider 408 nights) flagged and excluded.

**Keep two activities separate**: the A/B answers "did slice X help" (one bit, high confidence); *diagnosis* — officer-style blind reads of failed transcripts with a failure-cause tag (lost plan after compaction / guardrail burned legitimate work / tool wedge / bad decomposition / provider) — is what picks the *next* slice. The 07-31 deep-dive was diagnosis; the six slices were interventions; the paired re-run is the missing third step.

**Calibration rules**: an effect invisible across ~20 paired runs is too small to matter at our scale — ship or drop, stop measuring; and keep the suite running as an occasional baseline even between changes, so "our change regressed it" is distinguishable from "the world moved."

## 10. Shelved register (conditions to reopen, so nothing is re-derived)

- **JobDriver / engine promotion (§3–4)**: reopen if (a) the measured loosening path stalls — slices stop improving outcomes and the remaining overhead is structural to the graph, or (b) the session engine needs surgery for its own reasons, making the parameterization free, or (c) a consolidation decision is made post-measurement to retire `graph.py`. The design above is the version to build (promote the callback seam; selection stays a config branch; `freeze_data` stays the contract; phased graph never joins the interface).
- **Claude Code lane (§5)**: reopen *only* for Max-subscription economics on bulk coding jobs — one narrow expert, co-resident topology, auth-proxy sidecar spike first, homelab/own-workloads only (personal-seat terms; no customer capacity). No platform ambitions attached.
- **MCP-shim of `src/tools/` over SSH**: documented fallback if a harness must be integrated that cannot be installed into the workspace (closed SaaS agent). Cheap to stand up (the tool suite exists); never the lead architecture — it forfeits the harness quality being bought.
- **Services-MCP memory gap**: RecallStore on the MCP server remains the unbuilt differentiated layer from [[platform_for_agents]] §7 — independent of all of the above and still the cheapest test of the platform thesis.
