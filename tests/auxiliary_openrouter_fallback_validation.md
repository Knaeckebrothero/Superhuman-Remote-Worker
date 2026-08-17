# Auxiliary provider routing + main-model fallback — validation runbook

**Type:** unit (automated, done) + live cluster verification (manual, pending).
**Status:** code implemented + unit-verified on `develop` (uncommitted at time of
writing); live layers §2/§3/§4 not yet run.
**Fix / root cause:** `knowledge-base/knowledge/issues/openrouter_auxiliary_misrouted_to_openai.md`.
**Sibling runbook (primary/dispatch path of the same bug class):**
`knowledge-base/knowledge/tests/openrouter_system_provider_routing_verification.md` — that one covers
`provider_kind='system'` *catalog* rows; **this** one covers the **auxiliary**
LLM, whose `provider` was dropped at `AuxiliaryConfig` parse.

## What this validates

Two coupled fixes:

1. **Part A — routing.** An OpenRouter auxiliary
   (`auxiliary: {model: openrouter/…, provider: openrouter}`) must hit
   `openrouter.ai`, **not** `api.openai.com`. Previously `AuxiliaryConfig` had no
   `provider` field, so the injected provider was dropped and `create_llm`
   defaulted to the OpenAI factory → the `sk-or-v1…` key 401'd against OpenAI.
2. **Part B — fallback + loud failure.** When the *dedicated* aux model is
   unreachable, aux tasks (compaction/summarization, memory, curation, citation,
   title) **fall back to the main session model** and keep running — LOUDLY
   (heartbeat `aux_degraded` lights up), never silently. The session halts only
   when there is no fallback **or** the fallback also fails (compaction →
   `SummarizationFailed("aux_unavailable")`).

## Coverage map

| Layer | Proves | Needs | Status |
|---|---|---|---|
| §0 Unit tests | routing auto-detect + every fallback branch + surfacing | local pytest | ✅ done |
| §1 Static check | the fix is present in the deployed image | repo / pod | pending |
| §2 Live — real repro | end-to-end: OpenRouter aux routes to `openrouter.ai` (or falls back loud) | dev cluster + deploy | pending |
| §3 Live — forced fallback | Part B end-to-end without OpenRouter (deliberately broken aux) | k3d | pending |
| §4 Surfacing | `aux_degraded` reaches the DB / admin badge while on fallback | dev or k3d | pending |

**Why k3d can't run §2 as-is:** the k3d model registry has no OpenRouter model,
and the default aux (`gemma-4-moe`) resolves to the *same* endpoint
(`ai.h4ll.app`) as embedding, so the exact misroute can't form. §3 forces the
fallback path artificially instead. The real routing fix is proven by §0 (unit)
+ §2 (dev, the exact repro).

---

## §0. Automated unit tests (quick gate, no cluster)

```bash
source venv/bin/activate
python -m pytest tests/test_auxiliary_fallback.py -q
# expect: 14 passed
```

What they prove:
- `TestAuxiliaryProviderParsing` — `_parse_auxiliary_config` reads `provider`;
  the field exists on `AuxiliaryConfig` (regression guard for the dropped field).
- `TestCreateLLMProviderRouting` — `create_llm` routes an `openrouter/` model to
  `_create_openrouter_llm` when `provider is None` (the actual bug), honours an
  explicit provider, and leaves plain models on OpenAI.
- `TestAuxiliaryMainModelFallback` — aux success (no fallback used); aux failure
  → loud fallback to main; no-fallback → raise; both-fail → raise; recovery
  clears the flag; and the key guarantee **`test_fallback_success_does_not_mask_aux_down`**
  (a fallback success + a caller `record_success` must NOT clear the heartbeat
  `degraded`).

Full touched-area regression (optional, ~4 min):
```bash
python -m pytest tests/test_auxiliary.py tests/test_auxiliary_fallback.py \
  tests/test_loader_routing.py tests/test_config_overrides_loader.py \
  tests/test_persistent_session.py tests/test_memory_manager.py \
  tests/test_summar*.py tests/test_*memory*.py tests/test_*knowledge*.py -q
# expect: all passed (975 at time of writing)
```

## §1. Static check — the fix is in the deployed code

```bash
# Local repo
grep -n "provider=aux" src/agent.py src/api/persistent_app.py          # 3 hits (threading)
grep -n "elif config.model and config.model.lower().startswith(\"openrouter/\")" src/core/loader.py
grep -n "AUXILIARY FALLING BACK TO MAIN MODEL" src/services/auxiliary.py

# In a running agent pod (Tilt syncs src/ on k3d; dev ships the image):
CTX=k3d-srw NS=srw   # or CTX=main NS=superhuman-remote-worker
AGENT=$(kubectl --context=$CTX -n $NS get pods -o name | grep -E 'agent-(s|j)-' | head -1)
kubectl --context=$CTX -n $NS exec ${AGENT#pod/} -c agent -- \
  grep -c "AUXILIARY FALLING BACK TO MAIN MODEL" /app/src/services/auxiliary.py
# expect: 1  (fallback code present)
```

## §2. Live — re-run the real repro on dev (highest fidelity)

The exact incident session was `182a8fc1-625c-4112-a9cc-0c2439506f1e`
(aux = `openrouter/minimax/minimax-m3`, primary = `gpt-5.5`, dev cluster).

1. Deploy the fix to dev (build → GHCR → Fleet). Confirm the agent image tag
   advanced and §1's in-pod grep returns 1.
2. Create a NEW session with the same config (OpenRouter aux). Send one message.
3. **Expected — PASS (either is acceptable):**
   - **Routing works:** a title is generated (thread leaves "Untitled Session"),
     and the agent log shows the aux call going to `openrouter.ai` — NOT a 401
     from `platform.openai.com`.
   - **Or graceful fallback:** if the OpenRouter key/model is itself bad, the
     session still works and the log shows `AUXILIARY FALLING BACK TO MAIN
     MODEL … retrying on main model 'gpt-5.5'`, and `aux_degraded=true` (§4).
4. **FAIL:** "Untitled Session" with a silent `AUXILIARY MODEL DEGRADED` /
   `platform.openai.com` 401 and no fallback line = fix not effective.

Find the live agent pod for a thread + read its aux lines (the pod may be a
dual-mode `-j-` agent, and logs are UTC):
```bash
CTX=main NS=superhuman-remote-worker
THREAD=<thread-uuid>
for p in $(kubectl --context=$CTX -n $NS get pods -o name | grep -E 'agent-(s|j)-'); do
  n=${p#pod/}
  kubectl --context=$CTX -n $NS logs "$n" -c agent --tail=4000 | grep -q "$THREAD" && echo ">>> $n"
done
# then, on the matching pod:
kubectl --context=$CTX -n $NS logs <pod> -c agent | \
  grep -iE "Auxiliary override applied|AUXILIARY (FALLING BACK|MODEL DEGRADED)|Title generation|openrouter.ai|platform.openai.com"
```

Cross-check the audit store (only `main` calls are logged there; the *absence* of
aux rows + presence of the degrade log was the original tell):
```bash
PW=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv AUDIT_POSTGRES_PASSWORD)
kubectl --context=$CTX -n $NS exec srw-auditdb-0 -- bash -lc \
  "PGPASSWORD='$PW' psql -U srw -d srw_audit -P pager=off -c \
   \"select call_type, model, count(*) from llm_requests \
     where timestamp > now() - interval '1 hour' group by 1,2 order by 3 desc\""
```

## §3. Live — forced fallback on k3d (Part B, no OpenRouter needed)

Point a session's dedicated aux at a **dead** endpoint so the aux call fails and
the main-model fallback must cover it.

1. `tilt up` (agent rebuild ~50s). Confirm §1 in-pod grep = 1.
2. Create a session with a config override that breaks the aux transport, e.g.:
   ```json
   {
     "auxiliary": {
       "model": "gemma-4-moe",
       "provider": "openai",
       "base_url": "http://127.0.0.1:1/v1",
       "api_key": "dead"
     }
   }
   ```
   (A dedicated aux distinct from the main model so `fallback_llm` is wired, with
   an unreachable `base_url`.)
3. Send a message that triggers aux work (any turn triggers title gen; a longer
   conversation triggers memory/compaction).
4. **Expected — PASS:**
   - The session **responds** (main model unaffected).
   - Agent log shows `AUXILIARY FALLING BACK TO MAIN MODEL: aux model 'gemma-4-moe'
     failed on task '…' … retrying on main model '<main>'`.
   - A title is still generated (via the main model).
   - `srw-agent-*` pod stays `Running`, 0 restarts (no crash).
5. **FAIL:** session hangs / pod crashes / no fallback log / silent "Untitled".

## §4. Surfacing — `aux_degraded` reaches the DB while on fallback

During §2 (fallback branch) or §3, the heartbeat must light the flag:
```bash
CTX=main NS=superhuman-remote-worker   # or k3d-srw / srw
APW=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv POSTGRES_PASSWORD)
kubectl --context=$CTX -n $NS exec srw-postgres-0 -- bash -lc \
  "PGPASSWORD='$APW' psql -U srw -d srw -P pager=off -c \
   \"select left(id::text,12) id, config_name, agent_mode, aux_degraded, \
     metadata->'aux' aux_meta, to_char(last_heartbeat,'HH24:MI:SS') hb \
     from agents where thread_id::text like '<thread-prefix>%'\""
```
**Expected — PASS:** `aux_degraded = t` while the aux model is unreachable, and
`metadata.aux` carries `on_fallback: true` + `last_fallback_error`. When the aux
model recovers (or the session ends), a later heartbeat clears it.
**Known open item:** during the original incident `aux_degraded` stayed `f`
despite the agent logging `DEGRADED` — confirm the dual-mode session-agent
heartbeat actually carries `health.heartbeat_summary()` to the orchestrator. If
`aux_degraded` never flips here, that heartbeat wiring is a separate bug (noted
in the issue doc's follow-ups).

---

## Quick pass/fail summary

- **§0 must be green** before anything ships.
- **§2 (dev, real repro)** is the authoritative sign-off for Part A.
- **§3 (k3d forced)** is the authoritative sign-off for Part B.
- **§4** confirms the "never silent" guarantee is actually observable.
