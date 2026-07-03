# OpenRouter auxiliary model crashes session startup via the memory reranker

**Status:** investigated + fix decided + **IMPLEMENTED & k3d-verified 2026-07-03**
(uncommitted on develop) — hard-fail everywhere, no silent degradation · **Severity:** high

**Implementation (4 parts, all local tests + lint green; k3d E2E passed):**
- **P1** reranker rides `EMBEDDING_BASE_URL` (drops the aux fallback) —
  `src/services/memory/plugins/reranker.py`, config docstrings.
- **P2** persistent-path memory hard-fails loud + clean exit (no crash-loop) —
  `MemoryUnavailableError` in `persistent_session._setup_memory` (configured ⇒ required),
  caught by `persistent_app` lifespan/attach → `_exit_memory_unavailable` (`os._exit(0)`).
- **P3** orchestrator pre-flight resolves *every* role before pod spawn —
  new dependency-free `src/core/transport_resolution.py` +
  `_session_endpoint_violations`/`_endpoint_violations_detail` in `main.py`, wired into
  `provision_or_assign` and `routers/sessions._do_prepare` (emit `lifecycle:failed`).
- **P4** configured scorer (reranker) runtime failure raises `MemoryPipelineError`
  (escapes the kernel backstop; retriever/policy stages keep containment) —
  `manager.assemble` + `types.MemoryPipelineError`.

**k3d E2E 2026-07-03:** created a persistent session (thread `855bd86d`, gemma-4-moe);
agent `srw-agent-s-d47ed52f` logged `Embedding override … base_url=https://ai.h4ll.app/v1`
→ `RecallStore/KnowledgeStore initialized` → `MemoryManager bound {scorers:[reranker]}` →
`Application startup complete.` (the exact point the bug died with exit 3) → cockpit
**Connected**; a turn completed with **no** `MemoryPipelineError`/`MemoryUnavailableError`/
reranker crash. Regression gate passed. (The OpenRouter-aux crash → boots case has no
k3d model to reproduce and is covered by unit tests.)
(any session whose auxiliary model routes through OpenRouter never boots — agent pod
crashes on startup, workspace is released, UI hangs forever on "Booting agent runtime")
**Related:** `docs/done/minimax_m3_think_tag_reasoning_leak_post_gateway.md` (the
investigation that triggered this — switching the aux to OpenRouter-minimax to A/B the
reasoning leak is what surfaced the crash), `docs/features/agent_memory_overhaul.md`
(reranker = overhaul Phase 3 / GATE B)

## Symptom (observed)

Creating a persistent session on the main cluster hangs on **"Booting agent runtime"**
indefinitely. The cockpit's `GET /api/sessions/{id}/connection` polls return `425 Too
Early` forever; no workspace pod and no per-session ingress ever settle. It looks like a
workspace-provisioning failure but is not — the workspace pod provisions fine, then the
**agent pod crashes during runtime boot** and the orchestrator reaps it and releases the
workspace.

Reproduced on thread `29225385-bf22-49e9-a74b-3fff1c7ee611` (agent pod
`srw-agent-s-60129540`, exit code 3). Two sibling threads with the same config
(`0447af73`, `54ce2440`) failed identically.

## Root cause

The agent crashes at startup building the memory **reranker** scorer:

```
File "/app/src/api/persistent_session.py", line 1016, in _setup_memory
    self.memory_service = MemorySeamManager.from_config(...)
File "/app/src/services/memory/manager.py", line 89, in _bind
    bound.append((name, spec.factory(runtime)))
File "/app/src/services/memory/plugins/reranker.py", line 142, in _build_reranker
File "/app/src/services/memory/plugins/reranker.py", line 56, in __init__
    raise ValueError("reranker needs a base_url (or an auxiliary base_url)")
ValueError: reranker needs a base_url (or an auxiliary base_url)
Application startup failed. Exiting.
```

The reranker (`qwen3-reranker-8b`) is configured with **no `base_url` of its own** —
by design it "rides the auxiliary endpoint" (`config/persistent_defaults.yaml:180-184`).
`_build_reranker` (`reranker.py:135-149`) resolves:

```python
base_url = cfg.base_url or getattr(aux, "base_url", None)   # both None → raise at :56
```

For this session the auxiliary is **OpenRouter-routed minimax**, whose config carries no
explicit `base_url`, so both operands are `None` and construction raises. There is **no
containment** on this path — `from_config` binds plugins eagerly (`manager.py:85-99`),
`_setup_memory` has no try/except (`persistent_session.py:1012-1033`), so the exception
propagates through the FastAPI lifespan (`persistent_app.py:907 → 1445`) and exits the
process. An **optional** memory feature takes down the entire session.

## Why OpenRouter specifically (and nothing else) triggers it

Every model role already has an independent transport **except** the reranker, and
OpenRouter is the one provider that legitimately stores no `base_url`. Evidence — the
auxiliary section of `threads.metadata.config_override` across recent sessions:

| thread    | aux model                       | provider   | aux `base_url`               | boots? |
|-----------|---------------------------------|------------|------------------------------|--------|
| 29225385  | `openrouter/minimax/minimax-m3` | openrouter | *(empty)*                    | ❌ crash |
| 54ce2440  | `openrouter/minimax/minimax-m3` | openrouter | *(empty)*                    | ❌ crash |
| 0447af73  | `openrouter/minimax/minimax-m3` | openrouter | *(empty)*                    | ❌ crash |
| e496d293  | `gemma-4-moe`                   | openai     | `http://srw-litellm:4000/v1` | ✅ boots |
| c43d7f8b  | `MiniMax-M3`                    | openai     | `https://api.minimax.io/v1`  | ✅ boots |

OpenRouter is a first-class provider in the loader: its base URL is defaulted **at
LLM-construction time**, never written into config —
`loader.py:3297`: `base_url = config.base_url or "https://openrouter.ai/api/v1"`. So the
aux *LLM* works, but the aux *config object* the reranker reads has `base_url = None`.
Custom/system endpoints (gemma via the litellm gateway, minimax via `api.minimax.io`)
**cannot be guessed**, so they are obliged to store an explicit `base_url`, which the
reranker then borrows. The custom endpoints "work" *because they are custom*; OpenRouter
breaks *because it is built-in*. This is the opposite of the intuitive read.

### The reranker "working" on the boot-succeeding rows is partly illusory

For `c43d7f8b` the reranker endpoint resolves to `https://api.minimax.io/v1/rerank` —
which does not serve a Cohere-shaped `/rerank` for `qwen3-reranker-8b`. That call fails
per-assemble, but the failure is **gate-contained** (degrades to legacy order, logged in
`AssembleStats.errors`) — non-fatal. So reranking only actually does anything when the
aux endpoint happens to be the router that serves `qwen3-reranker-8b` (the litellm
gateway / `ai.h4ll.app`). Everywhere else it has been silently no-op'ing. The design flaw
is that **the reranker's transport is coupled to whatever chat model the user picked as
their auxiliary**, when it should point at wherever `qwen3-reranker-8b` actually lives.

## Cascade (why the UI hangs instead of erroring)

1. Agent pod boots, attaches workspace `10.42.3.74`, clones repo, mounts cloud → all fine.
2. `_setup_memory` builds the reranker → `ValueError` → `Application startup failed. Exiting.` (exit 3).
3. Orchestrator reaps the crashed pod, **snapshots + releases the workspace container**
   (`container_provisioner.py:514/439`), marks the thread `status=ended`.
4. It then spawns a **retry** agent pod (`srw-agent-s-aba139be`) that dangles `1/2`,
   polling `GET .../workspace 200` against the now-deleted workspace — never progressing.
5. Cockpit only ever sees `425` on `/connection` → "Booting agent runtime" forever.

## Fix (decided 2026-07-03 — hard-fail, never degrade)

**Design decision:** a session must never run half-working. If a **configured** memory
component cannot do its job, the session fails **loudly** — it does not silently degrade.
The current code already fails loud in the crudest sense (raise → `exit(3)`), but it fails
*late* (after full provisioning), *invisibly* (error in a reaped pod log; UI hangs on
`425`), and *with a crash-looping retry*. The fix keeps the hard failure but makes it
**early, visible, and only-when-genuinely-broken**. Four parts:

### 1. Reranker owns its transport — so we only fail when truly unserviceable

Decouple the reranker endpoint from the auxiliary. `_build_reranker` (`reranker.py:135`)
resolves:

1. `memory.reranker.base_url` (explicit) — when hosted somewhere specific
2. **default → the embedding endpoint** (`EMBEDDING_BASE_URL`) — `qwen3-reranker-8b` and
   `qwen3-embedding-8b` are the same family on the same router; that endpoint is always
   present and always carries an explicit `base_url`
3. **drop the `aux.base_url` fallback** — wrong sibling; source of both the crash and the
   silent no-op

Effect: the OpenRouter-aux case now **boots with a working reranker** (the embedding
endpoint really serves it) — a full agent, not a crash and not a half-agent. A hard
failure remains *only* when there is genuinely **no** reranker endpoint anywhere. Update
the `config/*_defaults.yaml` reranker-block comment ("rides the embedding endpoint").

### 2. Fail loud EARLY — pre-flight validation of *every* configured role

Not just the reranker — resolve the effective transport for **every configured model
role** (primary LLM, auxiliary, embedding, reranker, and any future role) and reject the
session if any configured role has no usable endpoint. This closes the whole class, not
just the one instance: any cross-provider combo that would crash a pod is caught before it
spawns.

Mechanics: a role-resolution helper mirrors the loader's base_url logic
(`loader.py:3297` — provider-defaulted URLs like OpenRouter's `openrouter.ai/api/v1` count
as resolved; endpoint-backed models must carry an explicit `base_url`; the reranker
resolves per §1: explicit → embedding). Run it at session create / `provision_or_assign`
(`provision_or_assign.py:102`) / `_do_prepare` — the *same layer* as the existing
`_session_grant_violations` check (`main.py:1189`) — and on failure reject with an HTTP
error + a `lifecycle:failed` reason surfaced in the cockpit, **before** the workspace or
agent pod is created. Per-role, actionable message, e.g.:

> auxiliary `openrouter/minimax/minimax-m3` resolves, but reranker has no endpoint: no
> `memory.reranker.base_url` and EMBEDDING_BASE_URL is unset. Set one, or drop the reranker
> from `memory.pipeline`.

Constraint: the orchestrator pod has `src/` synced but can't import the full agent loader
(missing `aiosqlite`). The resolution helper must be a lightweight, import-safe module
(no heavy agent deps) usable from both the orchestrator (pre-flight) and the agent
(runtime), so the two paths can't drift. No doomed pod, no 5-minute hang, no dangling
crash-loop retry.

### 3. Fail loud LATE — runtime backstop, no silent exit, no blind retry

Any memory failure that still reaches agent startup routes through the existing
**`memory.required`** contract (`src/agent.py:735`, `config/defaults.yaml:248`) instead of
an uncaught exception: emit a loud audit + `lifecycle:failed` with the real reason and
stop. That contract lives on the worker path today; **extend it to the persistent-session
path** (`persistent_session._setup_memory` / `persistent_app` lifespan). A deterministic
startup config crash must **not** be auto-retried — crash-loop guard at the orchestrator
(this also removes the dangling retry-pod cascade above).

### 4. "Silently off" is structurally impossible — configured ⇒ required

Do **not** gate the loud behaviour on a flag the user must remember. Any plugin present in
`memory.pipeline` is treated as **required**:

- **Build time**: a factory that can't resolve its transport → hard error (surfaced
  pre-flight per §2; startup backstop per §3). `from_config` keeps failing on unknown
  plugin names too.
- **Run time**: a configured reranker whose endpoint errors mid-session **fails the turn
  loudly** (surfaced to the cockpit + audit) rather than degrading to legacy order —
  **decision 2026-07-03**. The `AssembleStats.errors` silent-degrade path in
  `manager`/`reranker.score` is removed for configured plugins: a transient upstream blip
  becomes a visible failed turn, by design. (Trade-off accepted: upstream rerank-endpoint
  flakiness now surfaces as failed turns instead of quiet fallback.)

## Verification sketch

- Unit — resolution: `_build_reranker` with `cfg.base_url=None`, aux `base_url=None`,
  `EMBEDDING_BASE_URL` set → `RerankerScorer` pointed at the embedding endpoint (no raise).
  With all three empty → hard error (not a skip).
- Unit — pre-flight: a thread config with OpenRouter aux + no reranker base_url + no
  `EMBEDDING_BASE_URL` → `provision_or_assign` / `_do_prepare` returns the validation
  failure with the actionable reason; **no pod is created**.
- Unit — runtime: a configured reranker whose endpoint raises during `score` → the turn
  surfaces a loud error (not a legacy-order fallback).
- k3d E2E (happy path): session with aux = `openrouter/minimax/minimax-m3` and a live
  embedding endpoint → agent reaches ready, session connects, reranker actually runs.
- k3d E2E (loud fail): same aux but `EMBEDDING_BASE_URL` unset / reranker base_url absent →
  session-create rejected with the reason in the cockpit; no workspace/agent pod spawns; no
  `425` hang; no retry crash-loop.

## Notes / follow-ups

- **Operational cleanup** (addressed by fix §3, noted for context): the reaper path leaves
  a dangling retry agent pod polling a deleted workspace, and the thread is marked `ended`
  while a pod still exists. A deterministic startup-crash (exit 3) must suppress the
  workspace release + retry entirely — the retry is guaranteed to fail identically.
- This unblocks the MiniMax reasoning-leak A/B in
  `minimax_m3_think_tag_reasoning_leak_post_gateway.md`: once the reranker no longer
  depends on the aux transport, the aux can be freely pointed at OpenRouter to compare
  against the `api.minimax.io` direct endpoint.
- Immediate operator workaround (no code): set the session's auxiliary to a model served
  by the qwen3 router (system `MiniMax-M3` or a `gemma` variant) instead of the
  `openrouter/...` entry; those carry an explicit `base_url` and boot.
