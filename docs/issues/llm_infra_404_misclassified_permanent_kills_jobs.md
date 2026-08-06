# Infra-level 404 (HTML body) misclassified as permanent — bypasses the entire LLM outage stack

**Status:** Slices 1–3 SHIPPED (committed; sweep-verified at HEAD 2026-08-06 —
`test_graph_helpers.py` 129 green incl. `TestInfraEdgeHelpers`). The code has
since RELOCATED from `src/graph.py` to `src/core/llm_retry.py` (thin re-export
shim left behind; see `docs/done/llm_retry_and_fallback_reimplemented_per_call_site.md`)
— a Slice-1 comment there self-cites this doc. Slice 4 (reranker parity:
`_is_transient` still treats only 5xx/429 as transient, no 404-body-shape
check) confirmed still NOT built. Owed: k3d e2e (live outage replay) per the
verification section below.

## Incident (2026-07-17, dev cluster)

MiniMax's API edge (nginx) returned its default HTML `404 Not Found` page for
~10 minutes (~07:02–07:13 UTC). The request never reached the API application —
a provider-side deploy/routing blip, not a config error on our side. Two loop
jobs had in-flight MiniMax-M3 calls during the window; both were hard-failed on
**attempt 1** with the raw HTML page as the job's error message:

| Job | Config | Impact |
|---|---|---|
| `790888aa-c8a6-4492-90de-660ee95128f3` | scholar (loop iter 37) | Died on its *first* main-LLM call; 8 audit entries; nothing lost, safe to re-queue |
| `3b961b53-2d95-4bbe-9dac-c1dec75a8a35` | developer | Died 747 audit entries in; **~3h of work lost** |

Audit signature (identical on both):

```
ERROR: {'type': 'llm_error', 'message': '<html>...<center><h1>404 Not Found</h1></center>...nginx...',
        'attempts': 1, 'recoverable': False, 'classification': 'permanent'}
```

A critic job created **07:13** completed normally against the same model — the
outage self-healed well inside the pause+backoff re-dispatch window. Both jobs
would have finished untouched had the stack engaged.

## Root cause chain

1. **openai SDK** (`openai/_base_client.py`, `_make_status_error_from_response`):
   when the error body parses as JSON, the exception message is
   `"Error code: 404 - {body}"` and `.body` is a **dict**. When it does NOT
   parse (nginx HTML page), the message is **the raw body text verbatim** (no
   status prefix) and `.body` stays a **string**. Either way the exception is
   `NotFoundError` with `.status_code = 404`.
2. **Classifier** (`_classify_llm_error`, `src/graph.py:434`):
   `_PERMANENT_STATUS = {400, 401, 403, 404}` (line 471). The 400 branch
   disambiguates on the body (rate-disguised 400s, `tool_use_failed`,
   stream-disconnects → transient); **the 404 branch returns `permanent`
   unconditionally, with no body inspection.**
3. **Early return** (`src/graph.py:2548`): `classification == "permanent"`
   short-circuits *before* every resilience tier — in-process backoff retries
   (Tier 1), the `llm_unavailable` freeze → orchestrator pause+backoff
   re-dispatch (Tier 2, `graph.py:2802`), and the circuit breaker (Tier 3).
   `attempts: 1`, `should_stop=True`, `error` key set.
4. **Persistence** (`orchestrator/services/completion.py:677`):
   `error.get("message")` → `jobs.error_message` verbatim → Cockpit renders the
   raw nginx HTML as the job's "Reason".

### Why the stack has a single point of failure

The `permanent` early-return exists for good reason: retrying a genuine
"model not found" produced the 2026-05-12 infinite-retry cluster outage
(`docs/done/agent_infinite_retry_on_permanent_llm_errors.md`). But the verdict
is a bypass-everything gate, and it has now been wrong twice in the same
direction:

- **408 stream-disconnect** labeled `invalid_request_error` → permanent →
  3.5h lost (scholar `35b23256`; fixed in
  `docs/done/transient_408_stream_disconnect_misclassified_as_permanent.md`).
- **This incident**: infra 404 with HTML body → permanent → ~3h lost.

Both are transport failures wearing a deterministic-rejection costume. Ironies
worth preserving:

- The classifier's own **text fallback** (`graph.py:579`) only claims a 404 as
  permanent when the message also mentions "model" — the structured
  `status_code` path is *stricter than its own fallback*. Had the exception
  arrived stringified, this incident would have been classified transient.
- The SDK hands us a perfect discriminator for free: **`.body` is a dict** for
  a real API-level 404, **a string** for an infra-level one.

## Fix directions (to be firmed up by recon)

- **A. Classifier: disambiguate the 404 branch** (the core fix). Mirror the
  400 branch: walk to the exception carrying `status_code == 404`; if `.body`
  is a dict whose error object indicates model-not-found (or any parseable API
  error) → `permanent`; if `.body` is a string / HTML / unparseable →
  `transient` (rides Tier 1 backoff → Tier 2 `llm_unavailable` freeze →
  pause+backoff re-dispatch). Consider the same check for 401/403 — an infra
  HTML page can carry any status.
- **B. Error-message legibility/sanitization**: never persist a raw provider
  body as `jobs.error_message`. When the body isn't a parseable API error,
  wrap it: `LLM endpoint returned HTTP 404 (MiniMax-M3) — non-API response
  from provider edge`, raw body truncated into a detail field. Also audit the
  Cockpit rendering path — if `error_message` is ever bound as HTML, a
  provider-controlled body is an injection vector.
- **C. Parity**: the streaming handler (`src/agent.py:~1222`) mirrors the
  non-streaming classification — confirm it takes the same fix. Same for any
  other error classifier in the tree (aux LLM, embeddings/reranker, persistent
  sessions, orchestrator-side calls).
- **D. Tests**: unit cases for 404-with-dict-body (stays permanent),
  404-with-HTML-body (transient), 401/403 variants if A extends there;
  regression test that the `permanent` path still fails fast on genuine
  model-not-found.

## Codebase recon findings (4-agent sweep, 2026-07-18)

### The flow as it exists

- **Single lever.** `determine_job_status` (completion.py:675-740) hard-fails
  any completion carrying an `error` key unless `should_stop` is set AND
  `freeze_type ∈ ERROR_IMMUNE_FREEZE_TYPES` (includes `llm_unavailable`). The
  permanent branch returns `error` + no `freeze_data` (graph.py:2573-2581);
  the Tier-2 freeze returns `freeze_data` + no `error` (graph.py:2842-2846).
  That key choice — made by the classifier verdict — is the entire
  fail-vs-pause decision.
- **Classifier has one consumer** (graph.py:2547) but **two 404-permanent
  heads**: the status branch (`_PERMANENT_STATUS`, :471→:530) and the
  class-name fallback (`NotFoundError` → permanent, :539-545). Both skip the
  body. The *text* fallback (:579, needs "404"+"model"; :602 status gate)
  would already classify a bare HTML string as transient.
- **Retry stack that a `transient` verdict rides**: Tier 1 in-process —
  `ToolRetryManager(max_retries=config.limits.llm_inproc_retries=5)`,
  exponential 1→2→4→8→16s +10% jitter (~31s). Tier 2 — freeze
  `llm_unavailable` (gated on `CHECKPOINTER_BACKEND=postgres`; that IS the
  Helm default, `helm/values.yaml:627`), orchestrator pauses
  (`main.py:13408-13509`), attempt counter in `context.llm_outage` (survives
  re-dispatch), timer in `freeze_data.next_retry_at`; sweeper (~30s tick,
  `main.py:11400-11502`) CAS-clears freeze_data when due → dispatcher
  `_resume_job_on_agent` → agent resumes from Postgres checkpoint;
  credentials/mounts re-injected fresh on every re-dispatch. Backoff
  envelope `min(3600, 30·2^(n-1))` full-jitter; ceilings 12h duration / 60
  attempts / 2h reset window. Subjob bypass is RESOLVED — `llm_unavailable`
  subjobs share the pause branch unless the parent is failed/cancelled.
- **⚠ The determinism fingerprint would defeat a naive fix.**
  `llm_outage_fingerprint` (completion.py:498-529) fails the job when two
  consecutive `llm_unavailable` pauses carry an identical normalized
  `error_summary` containing any `\b4\d\d\b` token. The nginx page contains
  `404` → it fingerprints; digits are stripped in normalization so every
  cycle hashes identically. Under a transient-only fix, the 2026-07-17
  timeline is: Tier 1 (~31s) → pause 1 (~30-60s) → resume+retry → pause 2 →
  **fingerprint match → "Deterministic LLM request rejection" fail at ~t+3-4
  min**, while the outage lasted ~10 min. The fingerprint's own docstring
  says genuine outages "MUST keep pausing/retrying" — an infra-edge body is
  an outage, not a request-shaped rejection, so it must be exempted.
- **SDK safety net is off by design**: worker sets `llm.max_retries: 0`
  (config/defaults.yaml:20 — graph is the single retry authority); the
  KeyRing rotation layer retries only 401/403/quota-429. Nothing below the
  classifier ever retries a 404.
- **Siblings**: the reranker has its own classifier with the same blind spot
  (`_is_transient`, `src/services/memory/plugins/reranker.py:52` — only
  5xx/429 transient; a 404 raises → uncontained `MemoryPipelineError` via
  `manager.py:173-180`). Aux LLM falls back to the main model (non-fatal);
  persistent sessions have no classifier — an LLM 404 fails one *turn*, the
  session survives (out of scope). Orchestrator-side callers (triage, TTS,
  transcribe) all degrade gracefully.
- **Surface**: `error_message` renders in exactly one UI spot (job-list
  failure line + tooltip, `job-list.component.ts:182-183`), escaped
  interpolation — **no XSS hazard**. The pasted "Loop iter 37 · … Reason:"
  line = `jobs.description` (`project_loops.py:489`) + the cockpit
  `failureReason` label + raw `error_message`. Raw column also goes out via
  `GET /api/jobs`, `GET /api/jobs/{id}`, and MCP formatters
  (`formatters.py:65,89,329`), untruncated. Failure notifications embed
  `freeze_data.error_summary[:300]`, never the column.
- **Tests**: classifier coverage lives in
  `tests/test_graph_helpers.py::TestClassifyLlmError` (helper
  `_make_sdk_error(class_name, status, body=...)`, :556); the existing 404
  test uses a JSON body so it survives the fix. Orchestrator outage machinery
  covered by `tests/test_llm_outage_resilience.py` (incl.
  `TestLlmOutageFingerprint`). Nothing drives `create_execute_node` itself.

### Prior-incident invariants the fix must not break

- `agent_infinite_retry_on_permanent_llm_errors` (2026-05-12): a genuine
  model-not-found 404 must fail fast (≤1 audit row, error names the model) —
  never loop.
- `transient_408_stream_disconnect_misclassified_as_permanent` (2026-07-14):
  bias for retry on transport-shaped errors; wrongly-transient badness is
  bounded (streak cap 5, then pause path + determinism fingerprint), while
  wrongly-permanent badness is unbounded (hours of work destroyed).
- Outage feature: the freeze must carry **no `error` key**; reset window
  (7200s) must stay > backoff cap (3600s); ceilings must fail loudly exactly
  once.

## Planned fix flow

**Slice 1 — classifier body-shape disambiguation (`src/graph.py`).**
Add a small helper (pattern: `_is_insufficient_quota`) that walks to the
exception carrying the 404 and inspects `.body`:
- `body` is a dict (parseable API error) → `permanent` (unchanged: genuine
  model-not-found; the 2026-05-12 invariant holds).
- `body` is a string / None / unparseable (nginx/edge page, empty closed
  stream) → `transient` — rides Tier 1 backoff → Tier 2 `llm_unavailable`
  freeze.
Apply in **both** heads: the `status_code == 404` branch and the
`NotFoundError` class-name fallback. 401/403 deliberately unchanged (codex
carve-out already handles the known proxy case; a wrongly-transient auth
error would pause up to 12h against a genuinely bad key — worse trade).

**Slice 2 — fingerprint exemption (agent + orchestrator).**
When the 404 body was non-JSON, the agent's Tier-2 freeze sets
`freeze_data.deterministic_exempt: true`; `llm_outage_fingerprint` returns
`None` when that flag is set. Infra-404 then rides the full outage ceilings
(12h / 60 attempts) exactly like a 5xx outage. A *truly* deterministic 404
(wrong base_url path returning an API-shaped JSON 404) still fails fast via
Slice 1's dict-body → permanent; a wrong path returning HTML is bounded by
the ceilings + operator notification rather than the fingerprint.

**Slice 3 — error-message legibility (`src/graph.py`).**
Helper `_summarize_llm_error(e, model)`: when the provider body is non-JSON,
compose `LLM endpoint returned HTTP <status> (model '<model>') — non-API
response from provider edge: <body[:200]>` for `error.message` (permanent
path) and `freeze_data.error_summary` (freeze path). Raw body stays in audit
(`[:500]`) for forensics. No orchestrator/cockpit change needed — the
ceiling-fail message already embeds `error_summary[:200]`, and the cockpit
binding is escaped.

**Slice 4 (optional follow-up) — reranker parity.**
Extend `_is_transient` in `reranker.py` to treat a 404/4xx with a non-JSON
body as transient (contained by the manager's pre-scorer-order degradation)
so the same edge blip can't hard-fail jobs through the memory pipeline.

**Tests** (`tests/test_graph_helpers.py`, `tests/test_llm_outage_resilience.py`):
- 404 + dict body → permanent (existing test, unchanged).
- 404 + HTML string body → transient; 404 + `body=None` → transient.
- `NotFoundError` class fallback: dict body → permanent, string body →
  transient.
- Freeze with `deterministic_exempt` → `llm_outage_fingerprint` returns None;
  two identical HTML-404 pause cycles keep pausing; a genuine
  `bad_request_error` summary still trips the fingerprint.
- Message hygiene: composed summary contains status + model, never `<html>`.

**Verification (k3d)**: point a model's base_url at a route that returns an
nginx-style HTML 404 → job must pause (`llm_unavailable`, exempt), keep
re-dispatching past two cycles, then complete after the route is restored.
Negative control: nonexistent model id on a healthy endpoint → fast
`failed` with a readable reason.

## Acceptance criteria

- An LLM call that receives a 404 with a non-JSON body is retried in-process
  and, if the outage persists, freezes as `llm_unavailable` for pause+backoff
  re-dispatch — the job is never marked `failed` on attempt 1.
- An infra-404 outage longer than two pause cycles does NOT trip the
  determinism fingerprint — it keeps pausing until recovery or the 12h/60
  ceilings (replaying the 2026-07-17 ~10-min outage timeline must end in
  `completed`, not `failed`).
- A genuine API-level 404 (JSON error body / model-not-found) still fails fast
  as `permanent` (no regression on the 2026-05-12 incident class).
- `jobs.error_message` never contains a raw HTML provider body; the
  user-facing reason names the model, status, and that the response came from
  the provider edge, with the raw body truncated to a detail field.
- Unit tests cover both 404 shapes on every classifier that has the branch.
