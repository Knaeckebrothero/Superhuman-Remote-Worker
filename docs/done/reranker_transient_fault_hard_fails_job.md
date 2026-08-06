# A transient reranker fault hard-fails the whole job (no retry, no degrade)


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Shipped in `c56e6199` — bounded transient-only retry in RerankerScorer + manager degrade-to-pre-scorer-order; config knobs live in the renamed config/worker_base.yaml + config/session_base.yaml.

**Status:** investigated 2026-07-04; **fix A+B IMPLEMENTED 2026-07-05** — bounded transient-only retry in `RerankerScorer._rerank` (2 extra attempts, exponential backoff; timeouts/connection drops/5xx/429 only) + `TransientScorerError` on exhaustion, which `MemoryManager.assemble` contains by degrading *that one turn* to pre-scorer (hybrid) order (loud in `stats.errors`; next turn goes back through the scorer). Structural failures (4xx/auth/route/shape) still raise `MemoryPipelineError` and stay job-fatal — the original "configured ⇒ required" guarantee holds. Config knobs `memory.reranker.retries`/`retry_backoff` (defaults 2 / 1.0 s) in both defaults files. Tests: `tests/test_memory_manager.py` (+5: retry-then-succeed, exhaustion→TransientScorerError, disconnect+5xx transient, 401 structural no-retry, manager degrade keeps order + stats.errors). **Body count that motivated it: 5 jobs** — the two below plus loop iter 12 `88cceacd` (developer, 500 at 11:21Z, ~2 h 19 m of work lost) and *both* iter-14 critics `ae7691c0`/`e785923c` (ReadTimeouts at 13:36Z/13:38Z, during redeploy churn). Every failed job in the Better-Resavio loop so far had this exact signature.
**Severity:** high for the RSI loop — a single ~10 s network blip discards a 30-minute job and its unmerged work
**Component:** `src/services/memory/` (reranker scorer + manager), `src/graph.py` execute node, `src/agent.py` failure classifier
**Observed on:** main cluster (homelab), MiniMax-M3 loop + research jobs
**Related:** `docs/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md` (the decision that introduced this hard-fail), `project_llm_outage_resilience`, `project_reranker_transport_decoupling`

---

## TL;DR

The memory reranker scorer (memory overhaul Phase 3, "GATE B") is treated as **required-when-configured**: any runtime exception it raises becomes a `MemoryPipelineError` that propagates uncaught out of the graph's `execute` node and fails the entire job. The scorer makes a single HTTP call to the rerank endpoint with a 10 s timeout and **no retry**. So a transient upstream fault — a `ReadTimeout` or a 5xx from the shared qwen3 embed/rerank router — kills an otherwise-healthy job outright. It has already fired at least twice on 2026-07-04. The design was deliberate (never silently/permanently degrade memory order), but it conflates a *transient* transport blip with a *structural* misconfiguration; only the latter warrants a hard fail.

## Backend forensics (2026-07-05): what the reranker actually is, and why it blips

The rerank endpoint chain: agent → `https://ai.h4ll.app/v1/rerank` = **model-orchestrator router** (namespace `model-orchestrator`, same main cluster) → `http://uni-workstation:8082` over the **Tailscale sidecar** — Qwen3-Reranker-8B on the university workstation. That box also serves TEI embeddings (:8081), the gemma vLLM pool, whisper, and kokoro — a shared, remote, single-instance GPU host.

- **Baseline is healthy**: probed from the router pod, 5/5 rerank calls at 0.14–0.90 s. Router logs show hundreds of 200s per hour.
- **The `ReadTimeout` failures are latency spikes, not outages**: at 13:36–13:38Z (both iter-14 critics) the router logged *zero* rerank 4xx/5xx — the backend was still answering, just slower than the client's 10 s budget. GPU/queue contention on the shared box; note the duplicate critic (double-spawn regression) had *doubled* the memory-pipeline load on it at that exact moment.
- **The single 500 in 12 h (11:19:22Z, killed iter 12)** is `httpcore.RemoteProtocolError: Server disconnected without sending a response` — the uni-box rerank container dropped the TCP connection mid-request (restart/OOM/preemption); the router has no retry either and surfaced it as a raw 500.

So nothing is *chronically* wrong with the reranker — it is a best-effort remote GPU service with episodic multi-second stalls and rare connection drops, being consumed with guaranteed-availability semantics (10 s, one attempt, job-fatal). That mismatch is this issue. The client-side transient retry/degrade (fix A+B below) covers both observed signatures.

Related but distinct (found in the same router logs): the TEI embedding backend rejects batches > 64 (`422: batch size N > maximum allowed batch size 64`, N observed 66–1126), and `src/services/embedding_service.py::embed_batch` never splits its input — so any KB note chunking to > 64 chunks fails deterministically and the KB reindexer re-attempts it every tick (422 storms at 09:06–09:28, 11:18…). Deserves its own fix (clamp/split in `embed_batch`).

## Symptom (observed)

Two jobs on 2026-07-04 failed with `error_message` of the form `required memory scorer 'reranker' failed at runtime: <exc>`:

| job | kind | model | failed at | reranker exception |
|-----|------|-------|-----------|--------------------|
| `f3c59632-7b71-48d4-9fd8-95a5c16790ed` | Loop iter 9 · DEVELOPER (project `68137e29`, Hotel Rheinland ERP) | MiniMax-M3 | 19:33:35Z | `ReadTimeout:` |
| `9fb6d213-00fa-4bcd-9d25-8fcb04a6eebd` | "Research 4 V2" (scholar) | MiniMax-M3 | 12:50:38Z | `HTTPStatusError: Server err…` (5xx) |

For `f3c59632` the forensics are unambiguous:

- The agent was **healthy and productive for ~30 minutes** — a clean TDD loop writing RED tests and running pytest right up to the end (audit entries [507]–[530]).
- The **final** audit entry [531] is a `memory_retrieve` at `19:33:06`. Every prior turn that same minute paired `memory_retrieve` → `memory_inject`; this one has **no** following `memory_inject` and **no** next LLM call.
- The job flipped to `failed` at `19:33:35` — ~29 s later, mid-turn.
- No `job_frozen.json`, no `freeze_data`, no ERROR-level audit entries. The VM was then torn down (`context.vm.status = deleted`), which is why the job log file is gone and the workspace overview is empty.
- Branch `job/f3c59632` was **never merged** (`merge_status = None`) → iter-9's developer work is lost. The loop itself self-continued: iter 10 (SCHOLAR) was created at `19:33:20`.

Both prior turns' reranker calls succeeded, so this was a **single transient blip** on an otherwise-healthy endpoint, not an outage — a retry would almost certainly have succeeded.

## Root cause (full propagation chain)

1. **`src/services/memory/plugins/reranker.py:87-99` — `RerankerScorer._rerank`**
   A single `httpx` `POST {EMBEDDING_BASE_URL}/rerank` (`qwen3-reranker-8b`), `timeout = memory.reranker.timeout` (**default 10 s**, `config/defaults.yaml:295`), followed by `response.raise_for_status()`. **No retry, no backoff.** A slow endpoint raises `httpx.ReadTimeout`; a 5xx raises `httpx.HTTPStatusError`. `.score()` (line 121) does not catch either.

2. **`src/services/memory/manager.py:156-167` — `MemoryManager.assemble`**
   The scorer loop deliberately does **not** contain scorer failures (unlike retrievers/policies, which are contained per-plugin). On any scorer exception it records the failure and `raise MemoryPipelineError("required memory scorer '{name}' failed at runtime: {type}: {e}")`. The `except MemoryPipelineError: raise` at line 183 intentionally propagates it *past* the kernel backstop that would otherwise swallow it. Rationale (docstring lines 130-134): "configured ⇒ required — the caller fails the turn loud." **This is independent of `memory.required`** — it fires whenever the `reranker` scorer is bound, which is the default for every worker job (`config/defaults.yaml:272-273`) and persistent session (`config/persistent_defaults.yaml:166-167`). (`memory.required`, `src/agent.py:766`, is a *separate* fail-closed guard that runs at startup when the embedding-backed stores can't init.)

3. **`src/graph.py:1174` — `await memory_service.assemble(...)`**
   This call is **not** wrapped in try/except. The parallel Memory-Light path immediately below it (`src/graph.py:1216-1261`) *does* wrap retrieval and logs failures as "non-fatal" — so the two memory paths have **opposite** failure semantics. Crucially, the `assemble()` call sits **before** the LLM retry loop (`src/graph.py:1514`, `while True: … asyncio.wait_for(llm_with_tools.ainvoke(...))`), so it is **outside** the transient/permanent error classifier (`src/graph.py:346+`) that only wraps the LLM invoke. Memory-assemble failures get neither retry nor classification.

4. **`src/agent.py:1000-1041` — graph run + failure classifier**
   `MemoryPipelineError` propagates out of `self._graph.ainvoke(...)` and is caught by the generic `except Exception as e`. The only recoverability check here is `is_vm_error = isinstance(e, WorkspaceUnavailableError)`. A `MemoryPipelineError` is not that, so the agent returns `error_state = {type: "job_error", recoverable: False, should_stop: True, message: str(e)}`.

5. **Orchestrator** (`orchestrator/main.py`)
   A `type == "workspace_unavailable"` error is routed into bounded recovery/re-dispatch (`main.py:10590+`, up to `WORKSPACE_RECOVERY_MAX_ATTEMPTS`). A plain `job_error` is not — the job is marked `failed` with `error_message = str(e)`. Hence the observed terminal state.

So the reranker fault falls into the non-recoverable bucket **purely because it isn't a recognized transient type** — there is no code path that retries it, degrades it, or re-dispatches it.

## Why the current design over-reaches

The hard-fail was introduced by `openrouter_auxiliary_crashes_session_via_memory_reranker.md` (decided 2026-07-03, "hard-fail, never degrade"). That decision was **correct for its trigger**: the reranker was mis-wired to the OpenRouter auxiliary transport, so it hit a wrong/absent `/rerank` route and failed **deterministically every turn** while *silently* degrading memory order to legacy — plus a crash-loop retry pod cascade. For that failure class, "fail loud, no blind retry" is right: retry is futile and silent degradation hides a real bug.

But that decision conflates two distinct failure classes:

- **Structural / deterministic** — wrong base_url, missing `/rerank` route, auth 401/403, response-shape mismatch (`ValueError` in `_rerank`). Retry is futile; failing loud is correct.
- **Transient transport** — `ReadTimeout`, `ConnectError`/reset, 5xx from a momentarily-overloaded endpoint. Retry is **not** futile; the very next call typically succeeds. Killing a 30-minute job over one of these is a large, avoidable loss.

The reranker rides the **embedding** endpoint (`EMBEDDING_BASE_URL`), and the RSI loop hammers that same qwen3 router for embeddings on every memory write and KB op — so brief contention (a 10 s stall or a transient 5xx) is expected, not exceptional. The two observed failures are exactly this class (`ReadTimeout`, `HTTPStatusError`), not structural.

### Doc/comment contradiction to fix regardless

`config/defaults.yaml:295` and `config/persistent_defaults.yaml:184` both annotate `timeout: 10` with "**a slow rerank degrades to legacy order, contained**." That is **false** for the current code — a slow rerank raises `ReadTimeout` → `MemoryPipelineError` → the job dies. The comment describes the pre-Phase-3 (or intended) behavior; either the code or the comment must change.

## Frequency & blast radius

- Two hard-fails in ~7 hours on 2026-07-04 (see table), each from a *different* transient cause. This is a recurring pattern, not a one-off, and it scales with loop throughput and shared-router load.
- Per failure: the VM is deleted, the branch is unmerged, and the turn's progress is discarded. For loop iterations this silently drops a unit of compounding work; the loop advances to the next iteration as if nothing happened.
- Sibling transient-death classes visible in the same job table (out of scope here but worth a combined "loop resilience" pass): repeated `VM workspace connection lost … WorkspaceUnavailableError` on critic subjobs (marked `recoverable` yet still `failed` — the "subjobs bypass recovery" caveat), and `autonomy_ceiling` grant failures.

## Proposed fix

Distinguish transient from structural at the point of failure. Two layers, ideally both:

**A. Source-level bounded retry (primary, cheapest, loses no turn).**
In `RerankerScorer._rerank`, retry on transient `httpx` faults only — `ReadTimeout`/`ConnectTimeout`/`ConnectError`/reset, and `HTTPStatusError` with `status >= 500` — with a small bounded backoff (e.g. 2 retries, 0.5 s → 1.5 s). Do **not** retry `ValueError` (shape) or 4xx (auth/route) — those stay immediate-raise. A single retry would have saved both observed jobs. This is **bounded**, so it does not reintroduce the crash-loop the sibling doc warns about (that cascade came from an *infinite* retry against a *structural* failure).

**B. Manager-level transient carve-out (backstop for a sustained-but-not-structural stall).**
In `MemoryManager.assemble`, if a scorer exhausts its retries with a **transient** transport error, degrade to pre-scorer (hybrid) order **for that turn only**, log loudly, and record it in `AssembleStats.errors` — while **structural** errors continue to raise `MemoryPipelineError`. A single-turn, loudly-logged degrade does not violate the sibling doc's real invariant ("never *silently* / *permanently* degrade"): it is neither silent nor permanent, and the next turn re-runs the scorer.

Option C (classify `MemoryPipelineError` as `recoverable` at `src/agent.py:1012` and route it through the `workspace_unavailable`-style pause/recover) is possible but strictly worse than A: re-dispatch reloads from the checkpoint and still costs the turn, whereas a source retry keeps the turn moving. Keep C only as a last-resort for a truly sustained endpoint outage (which the `memory.required` startup guard + LLM-outage pause already partly cover).

Recommended: **A + B**, plus fix the stale config comment.

## Verification sketch

- Unit: extend `tests/test_memory_manager.py` — a scorer that raises `httpx.ReadTimeout` then succeeds is retried and the turn proceeds (A); a scorer that raises `httpx.ReadTimeout` on every attempt degrades to input order with a recorded `stats.errors` entry and does **not** raise (B); a scorer that raises `ValueError`/401 still raises `MemoryPipelineError` (structural unchanged).
- Reranker unit: `RerankerScorer._rerank` retries on 503/ReadTimeout, does not retry on 400/shape-mismatch.
- Local e2e on k3d: point the reranker at a stub that fails the first call transiently; confirm a worker turn survives and injects memory on the retry rather than the job going `failed`.

## Notes / follow-ups

- This regression landed with the Phase-3 reranker (GATE B) around 2026-07-03; before it, memory-retrieval failures were non-fatal. It is new behavior, not long-standing.
- Consider a single "loop transient-death" epic covering this + the critic-subjob VM-recovery bypass + `autonomy_ceiling` on loop-spawned critics — they share the shape "transient/config fault silently ends a unit of loop work."
