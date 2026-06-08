# Persistent session — runaway generation poisons context, session unrecoverable

> **2026-06-07 addendum:** the fix below is verified still intact. A *distinct,
> still-open* exit-137 agent-pod kill (root cause untraced) on a large
> gpt-5.5/codex session is documented at the bottom of this file — see
> "Recurrence 2026-06-07".

## Symptom (observed 2026-05-11)

Test session `7d845b7e-5e43-4fb1-8347-3a9c0b96947a` running `gemma-4-moe`
(131,072-token context). User flow:

| Turn | User input | Agent action |
|------|-----------|--------------|
| 1 | "Hey, can you see this image?" + 1 JPG (small white animal) | `read_file` on the JPG → vision-style description ✓ |
| 2 | (no text) + 1 PDF `Vertraulichkeitsvereinbarung.pdf` (~1k words) | `get_document_info` → "file not found"; then `list_files`; then `read_file` returned the PDF text fine |
| 3 | "Can you hear that?" + 1 voice message | session ended before any response |

After turn 2's small PDF read, the agent's *generation* exploded:

```
2026-05-11 10:06:09 - src.core.context - INFO - Context compaction triggered: 12 messages, 1535900 tokens
2026-05-11 10:06:09 - src.core.context - INFO - Starting single-pass summarization (22 tokens)
…
2026-05-11 10:08:09 - src.core.context - ERROR - Structured summarization failed: TimeoutError
2026-05-11 10:08:10 - src.core.context - INFO - Falling back to unstructured summarization
2026-05-11 10:08:10 - src.core.context - INFO - Unstructured fallback succeeded (264 chars)
2026-05-11 10:08:10 - src.core.context - ERROR - Summary (58 tokens) larger than original (27 tokens) — skipping compaction
2026-05-11 10:08:10 - src.llm.reasoning_chat - WARNING - Request approaching context limit: 1,550,833/131,072 tokens (1183.2%)
2026-05-11 10:08:10 - src.llm.reasoning_chat - ERROR - Context overflow at HTTP layer: 1,550,833 tokens exceeds limit of 131,072
… (8 retry attempts, all fail with the same overflow) …
2026-05-11 10:08:14 - src.persistent_graph - INFO - Streaming not supported (APIConnectionError), falling back to ainvoke
… 6× more retries, all fail …
2026-05-11 10:08:17 - src.persistent_graph - ERROR - Error in turn 2
src.llm.exceptions.ContextOverflowError: Request body has 1,550,833 tokens, exceeds model limit of 131,072
```

The agent then cleared its turn but never recovered — turn 3's voice
message arrived into the same poisoned state and got the same overflow.
User had to end the session manually.

## Root cause

The PDF was small (~1k words). The 1.5M tokens are not data — they
are the model itself, repeating tokens in a runaway generation loop,
all accumulated in a single `AIMessage` that subsequently became part
of the next turn's history.

Two co-conspirators:

1. **No output token cap on the wire.** `config/persistent_defaults.yaml`
   sets `max_output_tokens: null`. For Anthropic models, `loader.py`
   has model-aware fallbacks (Opus → 32K, Sonnet → 16K, otherwise
   `min(8192, ctx // 4)`), so a null value still produces a real cap.
   For the OpenAI provider (which is what gemma uses via vLLM), the
   loader was:

   ```python
   if config.max_output_tokens is not None:
       llm_kwargs["max_tokens"] = config.max_output_tokens
   ```

   No fallback. Null → no `max_tokens` passed to the SDK → vLLM's own
   default applies, which for the configured deployment is effectively
   unbounded for a 131K-context model. `_create_groq_llm` and
   `_create_google_llm` ignored `max_output_tokens` entirely (silent
   bug for their callers too); `_create_openrouter_llm` and
   `_create_codex_llm` had the same null-passthrough as OpenAI.

2. **A known model bug (vllm#40080)** — Gemma 4 + xgrammar JSON schema
   can enter infinite repetition loops. `config/model_config_matrix.yaml`
   already documents this and uses `repetition_penalty: 1.05` as a
   "partial mitigation." Without an output cap as the hard backstop,
   the partial mitigation isn't enough — once a loop starts, nothing
   stops it server-side.

Compaction couldn't dig the session out because the recent-message
slicer's input was tiny (only the older small turns were summarized
— "summarized 22 tokens" in the log). The 1.5M-token AIMessage lives
in the recent-keep window, never enters the summarization slice, and
the skip-compaction guard at `src/core/context.py:1482` correctly
notices that adding a summary would *grow* the conversation rather
than shrink it. So nothing changes between retries.

## Impact

- Any runaway generation poisons the session permanently — every
  subsequent turn fails the same way.
- Recovery requires the user to `/done` the session and start over,
  which loses all conversational context.
- The bug is most likely on local-vLLM gemma deployments today, but
  ANY OpenAI-compatible endpoint without server-side `max_tokens`
  enforcement is exposed.

## Fix (2026-05-11)

Layered defense across the providers:

### Primary — symmetric `max_tokens` derivation in `loader.py`

New helper `_resolve_max_output_tokens(config, limits)`:

```python
def _resolve_max_output_tokens(config, limits=None) -> int:
    if config.max_output_tokens is not None:
        return config.max_output_tokens
    ctx = config.model_max_context_tokens or (
        limits.model_max_context_tokens if limits else None
    )
    if ctx:
        return min(16384, ctx // 4)
    return 8192
```

Applied to **all five** non-Anthropic providers (`_create_openai_llm`,
`_create_google_llm`, `_create_groq_llm`, `_create_openrouter_llm`,
`_create_codex_llm`). Anthropic keeps its existing model-aware
ceilings (intentional — Opus deserves 32K, Sonnet 16K).

For gemma's 131K context this resolves to 16,384 — a single response
can never grow larger than that, and the runaway loop is killed
server-side after 16K tokens regardless of repetition penalty efficacy.

### Secondary — oversized-message backstop in compaction (shipped 2026-05-11)

`summarize_and_compact` in `src/core/context.py` now scans the
conversation for any `AIMessage` exceeding half the configured
`model_max_context_tokens` (the threshold matches "this single message
alone could plausibly break the next request"). Matching messages are
substituted with a stub:

> `[Previous response of ~N tokens elided by compaction — likely runaway generation. See workspace logs for details.]`

The substitution survives the existing skip-compaction guard (so a
poisoned session recovers even when the summarizer can't reduce
non-poisonous content further) via a `_substitution_only_result()`
return path. Originals' IDs feed `RemoveMessage` markers as before.

`AIMessage`s carrying `tool_calls` are deliberately exempted —
substituting one would orphan paired `ToolMessage`s and break the
turn. `ToolMessage` content is not in scope for this rule (it's
governed by `truncate_long_tool_results`, which fires earlier in the
pipeline and middle-truncates over-long tool results).

Combined with the **restored-messages-need-IDs** fix shipped under
`persistent_session_restored_messages_no_ids.md`, sessions resumed
from a state poisoned BEFORE this whole stack was in place now
self-heal on first compaction.

### Out of scope (deliberately, this round)

- **Streaming-side runaway detector** — defense-in-depth for endpoints
  that ignore `max_tokens`. vLLM honors it correctly so the primary
  fix is sufficient for our current deployment. Worth adding if a
  future endpoint is found to ignore the cap.
- **Insertion-time tool result cap** — was the original (incorrect)
  diagnosis. PDFReader already caps at 25K words and shell at 30K
  chars; the bug wasn't tool-result size.

## Verification

- The same test scenario (small PDF, gemma model) should generate
  ≤16,384 tokens per response, never trigger the compaction loop, and
  remain responsive across many turns.
- Logged at LLM creation time as `max_tokens=16384` in the loader's
  startup line.

## Related code

- `src/core/loader.py` — new `_resolve_max_output_tokens` helper +
  five wire sites (OpenAI, Google, Groq, OpenRouter, Codex)
- `src/core/loader.py:2034` — Anthropic's existing equivalent
  (untouched)
- `config/persistent_defaults.yaml:23` — `max_output_tokens: null`
  (still the default; the loader now derives a real value rather than
  passing it through)
- `config/model_config_matrix.yaml:241-243` — vllm#40080 documentation
  and `repetition_penalty` mitigation
- `src/core/context.py:1403-1521` — compaction slicer (the
  recent-message exemption that masks the symptom)

## Decision

**Fixed 2026-05-11.** Primary cause (missing `max_tokens` fallback for
non-Anthropic providers) and secondary backstop (oversized-message
compaction rule) both shipped. Streaming-side runaway detector
deferred — vLLM honors `max_tokens` correctly and the loader-side fix
is sufficient for our current deployment; revisit if an endpoint is
ever found that ignores the cap.

---

## Recurrence 2026-06-07 — distinct exit-137 agent-pod kill (root cause untraced)

> The 2026-05-11 fix above is **verified still intact and is NOT implicated**
> (see "Ruled out"). This documents a *different*, still-open failure mode in
> the same persistent-session area. Kept here because the investigation started
> from this doc (the symptom — "the agent stops after the summary" — looked like
> a recurrence) and it shares the summarization machinery.

### Symptom (dev deployment, ns `superhuman-remote-worker`)

Session `05220a87-288c-4dcc-bc35-90aca82a37ee` ("Building a RAG Chatbot Demo",
`gpt-5.5` via `srw-codex-proxy`, supervised, turn 11). User resumed and sent a
message. The UI rendered `SESSION RESUMED → CONTEXT SUMMARIZED`, the agent
**never continued**, and the send surfaced a red **`agent /ready timeout`**
toast. A second student independently reported the same "agent doesn't continue
after the summary" pattern, so it is not a one-off.

### Timeline (UTC, orchestrator + reaped-agent logs)

| Time | Event |
|---|---|
| 08:38:08 | Agent `ec33a336` (pod `srw-agent-s-06907673`) boots for the resume |
| 08:38:10 | ⚠️ duplicate persistent registration race: winner `ec33a336`, loser `e0687a3c` (dedup refused the loser; harmless here) |
| 08:39:31 | Structured summarization `TimeoutError` (45s aux cap) → unstructured fallback |
| 08:39:46 | Fallback OK; compacted **793 → 12** messages; session attached |
| 08:39:47 | Persistent loop started (69 tools); agent idle, awaiting input |
| 08:42:09 | **Last heartbeat.** No 08:43:09 heartbeat — loop went silent, zero further log output |
| ~08:43–08:44 | Agent container **SIGKILLed: `exit_code=137`, `phase=Running`** (reaper `category=crashed`) |
| 08:44:14 | Reaper deletes the dead pod |
| 08:44:16–25 | User's message → `POST /input` → `Agent forward failed … All connection attempts failed` → 503 |
| 08:45:48 | Orphan sweep marks agent offline + **thread `ended`** |
| 08:45:53 | Workspace snapshot (102 MB) + container deleted |
| 08:51–08:54 | Retry: `GET /connection` → **409 flood** for 180s → `agent /ready timeout` |

### Ruled out
- **NOT the runaway/explosion of this doc.** Output is capped — `gpt-5.5`
  resolved to `max_tokens=16384` via `_resolve_max_output_tokens`
  (`loader.py:2114`, applied by `_create_codex_llm:2650`). Compaction was clean
  (793→12, no `ContextOverflowError`); the oversized-`AIMessage` backstop
  (`context.py:1554`) was neither needed nor triggered.
- **NOT message-count memory.** 793 messages ≈ <1 MB — cannot explain a 2 Gi
  kill. (This was an early wrong theory; arithmetic killed it.)

### Still unexplained (the actual crash)
The agent container was SIGKILLed (137) ~3 min after going idle
post-compaction. Two candidates, **could not disambiguate**:
- **OOM** at the 2 Gi persistent-agent limit (`agent_provisioner.py` default), or
- **Liveness-probe kill** — `/health` (`persistent_app.py:1406`, a trivial
  handler) runs with `timeoutSeconds: 1` + `failureThreshold: 3`
  (`agent_provisioner.py:1094`); a frozen event loop (e.g. a GC pause) trips it.
  Sibling pod `srw-agent-j-a7d8f8e0` demonstrably logged
  `/health context deadline exceeded`, proving this misfires in the fleet.

Untraceable post-hoc because: the reaper logged `exit_code` but **not**
`terminated.reason` (the field that says `OOMKilled` vs `Error`); there is **no
Prometheus** in the cluster; and the dead pod + the originating orchestrator
pod's logs were gone by investigation time.

### Knock-on: the session wedged (separate, still-open bug)
Why the user saw `agent /ready timeout` instead of a clean recovery:
- The orphan sweep ends the thread but **never clears `threads.agent_id`**
  (`postgres.py:mark_orphaned_threads_ended` flips status only;
  `mark_stuck_session_agents_ready` clears the agent→thread side, not
  thread→agent). The thread stays bound to the dead agent.
- `GET /api/sessions/{id}/connection` then returns **409 "agent not ready"**
  (`routers/sessions.py:275`, agent `status=offline`). The cockpit's
  `_pollConnectionUntilReady` treats 409/425 as "keep waiting"
  (`persistent-chat.service.ts:877`) and never escalates.
- `_do_prepare` only re-provisions `if not thread.get("agent_id")`
  (`routers/sessions.py:150`); with the stale binding set it instead probes the
  dead pod's `/ready` for 180s and emits `failed, reason="agent /ready timeout"`
  (`routers/sessions.py:184`) — the exact toast.
- Orphaned **jobs** get `recover_orphaned_jobs()` (re-dispatch); orphaned
  **persistent threads** get only `mark_orphaned_threads_ended()` — **no
  re-provision**. So an agent death wedges the session from the UI.

### Changes shipped this round
- **Reaper now logs `terminated.reason` + `signal`** alongside `exit_code`
  (`agent_provisioner.py::_capture_agent_logs_before_reap`). The next exit-137
  will say `OOMKilled` vs `Error` — the diagnostic we were missing. **This is
  the bet: catch the next occurrence red-handed.**
- **Summarization timeout decoupled from the interactive aux budget.** The
  structured pass now passes the dedicated `summarization_timeout` (600s) via
  `auxiliary.chain(task, timeout=…)` (`auxiliary.py`; `context.py`
  `_single_pass_summarize`); `auxiliary.timeout` raised 45→120s
  (`persistent_defaults.yaml`). The **same `Structured summarization failed:
  TimeoutError`** appears in this doc's original 2026-05-11 log (120s then;
  tightened to 45s on the persistent path), so this fixes a long-standing
  symptom — but it is **not** the crash cause (the fallback succeeded; the agent
  died ~2.5 min later while idle).

### Open / next
1. **Catch the next exit-137** via the new `terminated.reason` log, then decide:
   memory bump (if `OOMKilled`) vs frozen-loop hunt (if `Error`). Cheap mitigation
   meanwhile: relax `/health` liveness `timeoutSeconds` 1→5.
2. **Recovery-gap fix** (own concern): clear `threads.agent_id` on orphan-end
   and/or make `_do_prepare` re-provision a thread bound to an *offline* agent,
   so a dead-agent session self-heals instead of wedging on 409.
3. **ContextConfig under-wired on the persistent path** (`persistent_session.py`
   builds `ContextConfig` with only `keep_recent_*`), so `gpt-5.5` (1.05M ctx)
   sessions still compact at the 80k default and use a 100k/2 oversized
   threshold — code-confirmed, separate.
4. Residual from this doc's own deferred item: **codex-proxy may not honor
   `max_tokens`** cleanly (reasoning models use `max_completion_tokens`); no
   runaway seen on this session, but it's the gap that could re-open the
   explosion path for gpt-5.x.

### References
- `orchestrator/services/agent_provisioner.py` — reaper log capture (the fix) + agent pod probes/limits
- `orchestrator/routers/sessions.py:150,184,275` — `/prepare` + `/connection` (the wedge)
- `orchestrator/database/postgres.py` — `mark_orphaned_threads_ended`, `mark_stuck_session_agents_ready`
- `src/services/auxiliary.py`, `src/core/context.py`, `config/persistent_defaults.yaml` — summarization-timeout decoupling
- `cockpit/src/app/core/services/persistent-chat.service.ts:867,877` — the 180s connection-poll budget
