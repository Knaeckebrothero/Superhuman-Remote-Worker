---
tags:
  - issue
  - memory
  - knowledge-base
  - embeddings
  - dispatcher
  - credentials
  - silent-failure
  - observability
  - project-loop
---

# Missing embedding API key silently disables BOTH memory and KB for a job

**Filed:** 2026-06-24, found while investigating "this loop job has no memory
injection" on the main cluster — loop `27cabc53` (project "Better Resavio",
`54426051-…`), Developer/Execution job
`280719ed-2748-4c4a-85f1-46252e264313` on agent `srw-agent-j-e3439a20`.

## Symptom

A job runs to all appearances normally but has **no memory injection and writes
zero memories** — and, for project jobs, **no knowledge-base injection and no KB
tools** either. There is **no error, no failed status, no audit entry, and
nothing in the debug agent activity or the LLM requests** to indicate why. The
only trace is two `WARNING` lines in the agent pod log.

Concrete incident — the three jobs of one loop rotation, same project, same
owner (`52b14734-…`), same loop, same day:

| Job | Role | Memories written | Memory/KB injection |
|---|---|---|---|
| `1b099a61` (07:57) | scholar | 21 | ✓ |
| `7a777bb0` (08:29) | critic | 37 | ✓ (`memory_inject` at startup) |
| `280719ed` (09:29) | **developer** | **0** | ✗ — never emitted a single `memory_inject` / `memory_retrieve` / `memory_store` in 850+ audit entries |

`get_memory_stats(280719ed)` = 0. The audit trail goes straight from
`initialize` → first `LLM` call with no memory step in between, whereas the
critic shows `memory_inject` immediately after `initialize`.

## Root cause

The agent process for the job had **no embedding API key**, so the embedding
service initialized degraded, which caused **both** the memory store and the
knowledge store to fail construction. Because each failure is caught as
*non-fatal*, the job continued with memory and KB silently switched off.

The chain, from the live agent log (`srw-agent-j-e3439a20`):

```
09:30:11  INFO  agent.py:614            Vector DB connection established (separate instance)   # vector DB is FINE
09:30:25  WARN  embedding_service.py:85 No API key found for embedding provider 'local'. Embedding calls will fail.
09:30:25  WARN  agent.py:2332           Failed to initialize RecallStore (non-fatal): Missing credentials … OPENAI_API_KEY …
09:30:25  WARN  embedding_service.py:85 No API key found for embedding provider 'local'. …
09:30:25  WARN  agent.py:2361           Failed to initialize knowledge base (non-fatal): Missing credentials …
09:30:25  WARN  registry.py:518         Knowledge tools require knowledge_graph and knowledge_store in ToolContext
09:30:25  INFO  legacy.py:175           recall_two_tier bound without a recall_store — contributes nothing
09:30:25  INFO  legacy.py:189           kb_notes bound without a knowledge_store — contributes nothing
```

1. **Single point of failure: the embedding service.** Both `RecallStore`
   (`src/agent.py:2306-2316`, gated by `if self.config.memory.enabled` at
   `:2287`) and `KnowledgeStore` (`src/agent.py:2342-2350`, gated by
   `if project_id`) call `get_embedding_service()`. With no key the service is
   degraded (`src/services/embedding_service.py:60` defaults the provider to
   `'local'`; `:70-71` resolves the key from `EMBEDDING_API_KEY` or
   `OPENAI_API_KEY`; `:84-86` warns when both are empty) and the OpenAI client
   construction raises `Missing credentials`.

2. **Both failures are swallowed.** `src/agent.py:2331-2332` and `:2360-2361`
   catch the exception and only `logger.warning(... non-fatal ...)`. Neither
   sets `context.recall_store` / `context.knowledge_store`.

3. **The MemoryManager binds but does nothing.** `memory.manager.enabled` is
   true, so the manager is constructed, but its retrievers/writers have no
   store: `recall_two_tier` and `kb_notes` log "contributes nothing"
   (`src/services/memory/plugins/legacy.py:175,189`). Result: no retrieval (no
   injection) and no extraction (no writes). KB tools are dropped at
   `src/tools/registry.py:518`.

### Why it's silent (the part that actually bit us)

Memory + KB are *graceful-degradation* subsystems — a sensible default for an
optional feature. But for a **project / self-improvement-loop job whose entire
coordination model is "shared memory (KB blackboard + RecallStore)"**, losing
both is a correctness-relevant failure, and it produces **zero** operator-visible
signal: not in job status, not in the audit trail, not in the cockpit, not in the
LLM requests. The only evidence is two `WARNING` lines in an ephemeral agent pod
log. This is why the missing injection was invisible until the pod log was read
directly.

### Why this job and not its siblings (narrowed, with one open item)

The embedding **api_key is injected only at dispatch** and is **never
persisted**: `resolved_config` is credential-free by design
(`orchestrator/services/config_resolver.py:74`). At dispatch,
`_inject_dispatch_credentials` (`orchestrator/main.py:1458`) resolves the
embedding model and calls `_inject_env_key_credentials(prefix="EMBEDDING", …)`
(`:1734-1761`), which reads the endpoint row and sets
`EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY` only when present
(`:3573-3578`). The credential-bearing config is POSTed to the agent but not
stored.

Ruled out as causes (all verified):

- **Config** — `developer` is `$extends: defaults` with no `memory:` /
  `auxiliary:` override; `memory.enabled` stays true.
- **Project scoping / `project_id`** — present (project KB has 23 notes; KB init
  reads the same `_job_metadata["project_id"]`).
- **Cluster-wide embedding outage** — the two sibling jobs embedded fine the same
  hour (21 / 37 memories, source=observer).
- **Model registry / endpoint** — `qwen3-embedding-8b` is `enabled`; its SYSTEM
  endpoint (`16d395f2`, Local Router → `https://ai.h4ll.app/v1`) **has a key**
  (`key_prefix sk-mo-65`), `updated_at 2026-06-12`.
- **`user_id` missing** — all three jobs share `user_id 52b14734-…`; the
  `if job.get("user_id")` gate (`main.py` user-preference block) passed.
- **Orchestrator redeploy/regression** — `srw-orchestrator` had **4h17m uptime,
  0 restarts**, spanning all three dispatches → same process, same code.

**Open item — the exact trigger.** With identical orchestrator/user/settings/
endpoint, the developer's dispatch delivered no `EMBEDDING_API_KEY` while the
siblings' did. Because the credential is transient and unlogged, it cannot be
diffed from persisted state, and the sibling pods are gone. Leading candidates,
in order:

- **(a) Endpoint key resolved empty for that one dispatch** — `endpoint_row`
  returned with `base_url` but a falsy `api_key` at `main.py:3577-3578` (e.g. a
  transient decrypt/fetch of the AES-GCM-encrypted endpoint key), so `_BASE_URL`
  got set but `_API_KEY` did not.
- **(b) `_resolve_model("qwen3-embedding-8b")` transiently failed** (
  `UnknownModelError` / no `endpoint_id`, `main.py:3564-3572`), falling through
  to the provider branch where `resolved_keys[provider]` was unavailable.
- **(c) A workspace-provisioning-path difference** — the developer is the only
  one of the three that ran on a **VM workspace at runtime**
  (`Connected to VM 10.42.3.181`) with no `snapshot` context (siblings were
  sandbox + snapshot). Not causally linked to the key loss, but the one
  structural difference worth checking for a divergent dispatch path.

## Effects

- **Loop coordination silently broken.** The Execution agent ran ~2.5h with no
  memory injection, no auto-injected KB notes, and **no KB read/write tools** —
  while its own kickoff instructs it to "READ [the KB] FIRST" and "Record in the
  KB what you shipped." The blackboard the whole loop depends on was invisible to
  this step.
- **No signal anywhere.** Job status `processing`, no error, no audit row, no UI
  indication. Diagnosable only via the agent pod log.
- **Not job-fatal, so it persists.** The job keeps running (and burning tokens)
  in a degraded state instead of stopping or alerting.

## Proposed fix

Ordered by leverage. (1) is the real fix for the reported pain; (2)–(3) make the
underlying credential path robust; (4) chases the trigger.

1. **Surface the degradation instead of swallowing it.** In `src/agent.py`, when
   `memory.enabled` (RecallStore) or `project_id` present (KnowledgeStore) but
   init fails (`:2331-2332`, `:2360-2361`), emit an **audit event** and a job
   warning — not just `logger.warning`. For project / loop jobs, treat
   "memory+KB both unavailable" as a first-class degraded state
   (surface in cockpit; consider pausing/failing loop jobs since their
   coordination substrate is gone). This alone would have made the incident
   self-evident.
2. **Fail-loud on missing embedding credential at agent init.** When the
   embedding service has no key but a feature that needs it is enabled, log a
   single `ERROR` at init (`embedding_service.py` already has a
   `verify_dimensions()` background probe — extend it to assert presence of a
   key and report through the same channel).
3. **Validate the embedding credential at dispatch for memory/KB jobs.** In
   `_inject_dispatch_credentials` (`orchestrator/main.py:1734-1761`), after the
   embedding block, if the job has `project_id` / memory enabled and no
   `EMBEDDING_API_KEY` was injected, log a `WARNING`/`ERROR` (and consider
   blocking dispatch). Also **log the injected `env_key` *names*** (never values)
   at dispatch so a missing `EMBEDDING_API_KEY` is greppable in the orchestrator
   log going forward.
4. **Harden `_inject_env_key_credentials` (`main.py:3560-3583`).** Distinguish
   "endpoint has no key" from "decrypt/fetch failed" and do not silently emit
   `_BASE_URL` without `_API_KEY` for an endpoint that is supposed to have one;
   surface decrypt failures rather than yielding a falsy `api_key`.

## How to confirm the trigger (next run)

With the dispatch-time `env_key`-name logging from fix (3) in place, run the loop
and grep the orchestrator log for the developer/Execution dispatch:
`Dispatch: injected embedding: provider=…, model=…` and the injected key-name
set. If `EMBEDDING_API_KEY` is absent there, the loss is at resolution
(candidates a/b); if present there but absent in the agent, the loss is in
transport/hydration. Cross-check with the agent log line
`embedding_service.py` "No API key found …".

## Verification (when fixed)

- A project job that loses its embedding key produces a **visible** signal: an
  audit event (e.g. `memory_unavailable`) and/or a non-`processing`/degraded job
  state — not just pod-log warnings.
- A loop Execution job either has working memory + KB (the happy path) or is
  surfaced/paused, never silently running blind.
- Re-run loop `27cabc53` (or any project loop) and assert every step emits
  `memory_inject` at startup once the project has memories, and that
  `get_memory_stats` is non-zero for each completed step.

## Evidence (main cluster, 2026-06-24)

- Agent `srw-agent-j-e3439a20` log: the chain quoted under **Root cause** (vector
  DB up; embedding no-key; RecallStore + KB init failed non-fatal; both manager
  plugins "contribute nothing"; KB tools dropped). 28 tools loaded, none of them
  `kb_*`/knowledge tools.
- `get_memory_stats`: `280719ed` = 0; `7a777bb0` = 37; `1b099a61` = 21.
- Audit page 1: `280719ed` = `initialize → LLM` (no memory step); `7a777bb0` =
  `initialize → memory_inject → LLM → memory_retrieve → memory_inject`.
- DB (`srw-postgres-0`): all three jobs' stored `resolved_config.env_keys` are
  identical — `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL` present, **no
  `EMBEDDING_API_KEY`, no `EMBEDDING_PROVIDER`** (credential-free by design, so
  the agent defaults to provider `'local'`). Embedding model
  `qwen3-embedding-8b` enabled; endpoint `16d395f2` has a key. All three jobs
  share `user_id 52b14734-…`. `srw-orchestrator` uptime 4h17m / 0 restarts.

## Related

- [`preemption_before_first_checkpoint_replays_job_opening.md`](preemption_before_first_checkpoint_replays_job_opening.md)
  — same loop run (`27cabc53`). Its evidence table notes Run 1 had "KB tools
  absent" + `… > memory > …` instruction hierarchy vs Run 3's "KB tools present"
  + `… > knowledge base > …`. That "KB tools absent / `memory >`" signature is
  exactly the `knowledge_store`-failed-to-init fingerprint described here — worth
  checking whether some of those early cold-started runs hit this same
  embedding-key failure rather than (or in addition to) the cold restart.
- `docs/features/project_self_improvement_loop.md` — the feature whose first real
  runs surfaced both issues.
- `docs/issues/litellm_reranker_model_unregistered.md` — sibling
  "memory-stack dependency silently unavailable" class of bug.
