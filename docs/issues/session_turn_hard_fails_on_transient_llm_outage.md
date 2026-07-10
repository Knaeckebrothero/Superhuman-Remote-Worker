# A persistent-session turn hard-fails on a transient LLM-endpoint outage (and an image-heavy turn can *cause* that outage)

**Status:** investigated 2026-07-10 from session `4b82e6db`. **Track 0 (codex-proxy memory bump) SHIPPED + live** — `helm/values.yaml` codex-proxy limit 256Mi→2Gi (commit `ef12756a`, develop; verified live: new pod `srw-codex-proxy-794994fb55` carries `memory: 2Gi`). **Track 1 (image downscaling) and Track 2 (persistent transient-retry / hold-and-reconnect) PROPOSED below, not built.**
**Severity:** high — the single most common session failure. 11 turns killed in ~11 days (see Frequency); a transient proxy blip discards a live turn with no retry, and the user's manual re-sends fail too until the endpoint recovers.
**Component:** `src/persistent_graph.py` (`_execute_turn`, turn wrapper), `src/tools/workspace/files.py` (`_handle_image_file`), `helm/values.yaml` (codex-proxy), contrast with `src/graph.py` (worker outage resilience).
**Observed on:** main/homelab dev cluster (`superhuman-remote-worker`), model `gpt-5.6-sol` via `srw-codex-proxy` (CLIProxyAPI), autonomous persistent session.
**Related:** `docs/issues/reranker_transient_fault_hard_fails_job.md` (same transient-vs-structural shape), `docs/features/llm_outage_pause_and_backoff_redispatch.md` (the worker resilience this borrows from), `project_llm_outage_resilience`, `project_codex_proxy`, `project_session_multimodal_pdf_context_explosion`.

---

## TL;DR

A user attached 5 full-resolution phone photos (~4.1–4.75 MB each, ~22 MB total) to a persistent session. The agent read them with `read_file`; the follow-up LLM call carried all 5 as base64 `image_url` blocks (~30 MB request). The codex-proxy (CLIProxyAPI, a single-replica Go proxy with a **256Mi** memory limit) buffers the whole request body in memory and was **OOMKilled** processing it, crash-looping for ~17 s. During that window the agent's outbound call got `httpcore.ConnectError: All connection attempts failed` → `openai.APIConnectionError`, whose `str()` is literally `"Connection error."`. The turn died and was persisted as a `role='error'` row.

Two independent defects:

1. **Trigger** — nothing downscales images before dispatch, so a handful of phone photos become a ~30 MB request that OOMs an under-provisioned proxy (and bloats context/tokens for zero visual gain).
2. **Fatality** — the persistent turn path has **no transient-outage resilience**. Unlike worker jobs (which classify the error, back off ~31 s, and then *pause-not-fail* for re-dispatch), a session turn relies solely on the OpenAI SDK's built-in retries (a few seconds of backoff) and, on exhaustion, surfaces the raw exception and ends the turn. A ~15–30 s proxy restart outlives the retry budget every time.

Track 0 (bump the proxy limit) is done and removes *this* OOM trigger. Track 1 (downscale images) removes the trigger class generally. Track 2 (give sessions the worker's transient-retry, adapted to in-place hold-and-reconnect) removes the fatality for *any* endpoint blip, not just this one.

## Symptom (observed)

Session `4b82e6db-a6a6-4276-b34f-b0028df2ab18`, turn 1. Timeline (UTC):

| time | event |
|------|-------|
| 11:04:13 | user message + 5 photos (message hint `[Attached files in uploads/: …jpg]`) |
| 11:04:56 | **1st LLM call succeeds** (text only) → reply + 5 `read_file` tool calls |
| 11:04:57–11:05:01 | 5 images loaded as tool results (4,112,141 / 4,557,584 / 4,627,310 / 4,754,033 / 4,213,613 bytes = **~22.3 MB**, base64 ≈ ~30 MB) |
| ~11:05:0x | **2nd LLM call** (carrying all 5 images) hits the proxy → proxy OOMKilled |
| 11:05:12 | agent `astream` → `APIConnectionError`; enters the mislabeled "streaming not supported" fallback |
| 11:05:16 | fallback `ainvoke` also → `APIConnectionError: Connection error.` → `Error in turn 1` (`persistent_graph.py:671`), persisted `role='error'` row |
| 11:05:27 | proxy finishes restarting |
| 11:06:25 | PC's WebSocket reconnects — **70 s after** the error was recorded |

Agent-pod traceback bottoms out at `httpcore.ConnectError: All connection attempts failed` → `httpx.ConnectError` → `openai.APIConnectionError` (raised at `openai/_base_client.py` `request`), i.e. a raw **TCP connect failure**, not a payload/413/overflow error. The user's page refresh is a **red herring** — the connection is headless, refresh never cancels a turn, and the reconnect happened a full minute after the failure (the `SESSION RESUMED` divider is the normal history/live seam).

## Backend forensics: why the proxy died, and why it stays unreachable a while

- **codex-proxy resources were 256Mi limit / 64Mi request** (`helm/values.yaml`, chart default; the dev overlay `deployment/values-experimental.yaml` enables the proxy and pins the image but does **not** override `resources`, so the default was authoritative). CLIProxyAPI is Go and buffers the full request/response body in memory; a ~30 MB multimodal body (plus GC overhead and any provider-format translation) blows a 256Mi cgroup. The container was **OOMKilled (exit 137)** — a container-level limit hit, not node pressure. Restart sequence: instance started 11:05:06 → OOMKilled 11:05:10 → next start 11:05:27. The agent's two connect attempts (11:05:12, 11:05:16) fell inside that dead window.
- **No HA — every restart is an outage.** The proxy runs a **single replica** with a **RWO Longhorn PVC** (`srw-codex-proxy-auth`, ReadWriteOnce) for OAuth token persistence. So *any* restart (OOM, image bump, node drain) is a hard gap, and rescheduling onto another node incurs a `Multi-Attach` volume detach/attach delay on top of container startup (observed during the Track-0 rollover: `FailedAttachVolume … Volume is already exclusively attached to one node`, resolved ~18 s later). The unavailability window a retry must survive is therefore ~15–30 s, sometimes more — well beyond the SDK's backoff.
- **Evidence the images were the trigger:** the text-only 1st call at 11:04:56 succeeded; only the image-bearing 2nd call failed; and once the turn errored (no more image request), the proxy stabilized and served other agents fine (`200 POST /v1/responses` from a different agent at 11:06:29+).

## Frequency & blast radius

`"Connection error."` is the **most common** `role='error'` content in `thread_messages` — **11 occurrences, 2026-06-29 → 2026-07-10** (query: `SELECT content, count(*) FROM thread_messages WHERE role='error' … GROUP BY 1`). They cluster around proxy bad-windows:

| date | threads hit | turns |
|------|-------------|-------|
| 2026-06-29 21:31–23:45 | `a2e6dde7`, `d35551f6`, `6b63ac7d`, `c3e0566e` (**4 sessions**) | 10 turns — incl. consecutive re-sends: `a2e6dde7` turns 2/3/4, `d35551f6` turns 16–19 |
| 2026-07-10 11:05 | `4b82e6db` | 1 (this incident) |

So a single proxy blip does **not** cost one turn — it takes out **every in-flight turn across all concurrent sessions**, and because there's no automatic hold/retry, the user's manual re-sends land on the same dead endpoint and fail too (the 2026-06-29 consecutive-turn deaths). Per failure the cost is one discarded turn's work + a dead-ended session until the user notices and retries after recovery.

## Root cause

### A. The trigger chain (image → OOM)

1. Upload lands raw, unmodified: `orchestrator/services/thread_uploads.py:218-231` writes the bytes to `<workspace>/uploads/` via SFTP verbatim (`MAX_FILE_SIZE = 100MB`, no dimension cap). The cockpit appends a `[Attached files in uploads/: …]` hint to the next user message (`persistent-chat.service.ts:1687`).
2. Agent reads each image: `read_file` → `_handle_image_file` (`src/tools/workspace/files.py:203-283`): `image_data = local_path.read_bytes()` (`:223`) → `base64.b64encode` (`:224`) → wrapped in an `<image_data mime_type=…>…</image_data>` tag (`:237`). **No resize.**
3. Graph converts tags to content blocks: `extract_image_tags` + `make_multimodal_user_message` (`persistent_graph.py:1839-1858`) build a `HumanMessage` with 5 separate `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}` blocks, each full-resolution.
4. That message is sent to `llm_with_tools.astream(prepared)` (`:1203`) → ~30 MB request → proxy OOM. **Nothing in `src/` resizes or re-encodes image pixels** (confirmed: zero `.resize(`/`.thumbnail(`/`Image.open` call sites; `src/core/image_tokens.py` computes dimensions for *token estimation only*). `Pillow>=10` is already a dependency (`requirements.txt:30`).

### B. The fatality chain (why a transient blip kills the turn)

`_execute_turn` (`src/persistent_graph.py:812`) has **no transient classifier, no backoff, no pause**:

- Streaming (`astream`, `:1203`) is a manual async iteration with **no `asyncio.wait_for`** — the stream has no wall-clock bound at all.
- On stream exception (`except Exception as stream_err:`, `:1390`): a `ContextOverflowError` cause is re-raised typed (`:1400-1403`); a class-name string match on `"ResponseNotRead"`/`"APIConnectionError"` triggers a **single, untimed** `await llm_with_tools.ainvoke(prepared)` (`:1408`) mislabeled "streaming not supported" (`:1406`); anything else `raise`s (`:1464-1465`). For a real `APIConnectionError` this just re-issues the identical request once against the same dead proxy.
- The turn wrapper (`:649-675`) catches any exception → `callbacks.on_error(_user_facing_turn_error(e), turn_id=turn_id)`. `_user_facing_turn_error` (`:213-247`) special-cases only WorkspaceUnavailable and context-overflow; otherwise returns `str(e)` = `"Connection error."`. Turn ends.

The **only** retry a session gets is the OpenAI SDK's built-in `max_retries` (`config/persistent_defaults.yaml:22` = **3**; note the worker path sets `config/defaults.yaml:20` = **0** and owns retry itself). SDK backoff totals a few seconds — it cannot span a 15–30 s restart. Key rotation (`reasoning_chat.py:875-933`) fires only on 401/403 or quota-429; a raw `ConnectError` raises before any status check, so rotation never engages.

### Contrast: the worker path already solves this (`src/graph.py`)

Worker jobs have the machine sessions lack:

- **Classifier** `_classify_llm_error` (`:365-504`) — 6-way (`permanent`, `quota_exhausted`, `cooldown`, `rate_limit`, `auth_unavailable`, `transient`), duck-typed on `status_code`/class-name/text; `ConnectError`/`APITimeout`/5xx fall through to `transient`.
- **Retry loop** `:1563` with `asyncio.wait_for(ainvoke, timeout=llm_timeout)` (`:1566`); `retry_manager` does `1·2^attempt` backoff capped 30 s + 10% jitter (`context.py:1948-1965`), `llm_inproc_retries = 5` → 1+2+4+8+16 ≈ **31 s** (`defaults.yaml:224`).
- **Pause-not-fail on exhaustion** (`:2549-2593`): writes `freeze_data={freeze_type:"llm_unavailable", …}` with **no `error` key** (so `determine_job_status` doesn't mark it failed), gated on the Postgres checkpointer; the orchestrator auto-continues re-dispatch (`_AUTO_CONTINUE_FREEZE_TYPES` includes `llm_unavailable`, `agent.py:81-89`). Circuit breaker trips a fail after 5 consecutive streaks (`:2595-2640`).

Documented in `docs/features/llm_outage_pause_and_backoff_redispatch.md`. The persistent path never inherited any of it.

## Proposed fix

Two independent tracks (Track 0 already shipped). Do both — Track 1 removes the trigger, Track 2 removes the fatality.

### Track 0 (DONE) — right-size the proxy
`helm/values.yaml` codex-proxy `resources` 256Mi/200m → 2Gi/1000m (req 64Mi/50m → 256Mi/100m), commit `ef12756a`. Live-verified. Stops *image-sized* requests from OOMing the proxy. Does **not** address other restart causes or the fatality.

### Track 1 — image quality tier (a user-facing quality ↔ cost/latency dial that also removes the trigger class)

Not merely an OOM valve. Every pixel sent is **tokens billed + latency + context consumed**, so image resolution is a quality/cost knob the user (or the task) should own. Full-res is the right default for OCR / frontend pixel-peeping / diagnostic detail; it's waste for "is there a chicken in this photo." Expose it as a setting with a sensible default rather than a hardcoded resize.

**Two levers, not equal:**
- **Resolution (max edge)** — the real lever. Image tokens scale with tiles/patches (`src/core/image_tokens.py`), and resolution is what bounds the detail the model can resolve. This is what the *setting* controls.
- **Compression** — shrinks *bytes* ~5–10× (what OOM'd the proxy) but barely moves *tokens* (token cost is dimension-based, not byte-based). Applied as an **always-on safety** above a byte threshold (~1 MB), but **format-aware, not blanket JPEG** (see below) — not a user choice.

**Compression must be format-aware** — a blanket "re-encode everything to JPEG q80" would degrade the exact cases the High tier exists for: JPEG puts ringing artifacts on text/line edges (hurts OCR, chart/diagram reading, frontend screenshot-diffing) and has no alpha channel (composites transparent PNGs onto a background). Rule: **always downscale** (the real, safe, token-cutting win), but only apply *lossy* re-encode to genuine photographs (source already JPEG); keep PNG/WebP-with-alpha as PNG and optimize losslessly. Cheap heuristic = source MIME/format: JPEG-in → JPEG-out q~82; PNG-in → PNG-out (lossless optimize), only downscaled. This sidesteps both the text-artifact and alpha-loss failures.

**Key constraint that anchors the tiers:** above a model's tiling cap, extra resolution is *invisible* — the model downsamples server-side. Each family already encodes that cap as `image_tokens.max_edge` in `config/model_config_matrix.yaml` `settings` (e.g. `2576` for the Anthropic-patch families; tile ceilings for the OpenAI/codex families). So tiers are expressed as **fractions of the family's own `max_edge`**, making them auto-correct per model — no pixel literal that rots when a model is added. "High" therefore means "the model's real max," and there is no visual quality *above* it to lose; a literal untouched 4000 px phone photo is pure waste vs. a 2576 px one.

| Tier | Max edge | Relative img tokens | Fits |
|------|----------|--------------------|------|
| **Economy** | ~768 px (few/single tile) | lowest | bulk & autonomous/loop runs, coarse "what is this" |
| **Standard** *(default)* | ~1568 px (≈1.15 MP — where major vision models plateau) | mid | general reading, "look at this photo/doc" |
| **High** | = family `image_tokens.max_edge` (~2048–2576) | highest useful | OCR, frontend screenshot diffing, charts, diagnostic detail |

Always-on (every tier): downscale to the tier's max edge, plus the format-aware byte-guard re-encode above ~1 MB (photos → JPEG q~82; graphics → lossless PNG optimize). Even "High" is bounded by the family cap + the byte guard, so no tier can reproduce the OOM.

**Token-accounting coupling:** the downscale must run *before* the content block is built and *before* `estimate_image_block_tokens` reads its dimensions (`src/core/image_tokens.py`), or context budgeting drifts from what's actually sent. The seam ordering must be: resolve tier → downscale bytes → build block → estimate.

**Resolution order (settings hierarchy — cascade, most specific wins):**
1. **Global default** — `Standard`, in `config/defaults.yaml` + `config/persistent_defaults.yaml` (autonomous/loop configs may default to `Economy` for cost).
2. **Project / workspace default** — optional inherited default for all sessions/jobs in a project.
3. **Session / job setting** — the user-facing menu item (the primary home). Injected as a per-job/thread config override, same channel as other session settings.
4. **Agent per-read override** — the agent can *spend up to* the ceiling on a specific read when it detects it needs detail: a `read_file(path, detail="high")`-style arg (map to a tier, clamp to the session ceiling so a user cap can't be exceeded). This resolves the "average low, but full detail on the two images that matter" case without paying for it on the other twenty.

**Seam — a shared util at the central choke point, not the narrow one.** For a *setting* (vs. a pure hotfix) the downscale must apply uniformly to **every** way an image enters, or "High" wouldn't mean high for browser screenshots (which is exactly frontend dev's image path). Add one `downscale_for_tier(bytes|b64, tier, family_max_edge) -> b64` util and call it from the central `make_image_content_block_from_b64` / `make_multimodal_user_message` (`src/services/image_content.py:87-118`) — the single point every image funnels through: `read_file` images (`persistent_graph.py:1853`, `graph.py:4136`), browser screenshots, PDF page renders, and the aux vision model (`vision_helper.py`). `_handle_image_file` (`src/tools/workspace/files.py:220-238`, which holds raw bytes + a `ToolContext`) can call the same util for an early/cheap pass, but the service-level choke point is the durable home (thread the resolved tier + family `max_edge` in, since that module has no `ToolContext`). The PDF branch (`_get_visual_content`, `files.py:439`) is already DPI-bounded and can stay; browser-screenshot capture DPI is a separate upstream knob.

**Wiring:**
- New `image_quality` enum (`economy|standard|high`) in `config/schema.json` and the `llm`/session config; per-family tier→fraction mapping alongside the existing `image_tokens`/`pdf_render_dpi` entries in `model_config_matrix.yaml`; resolved with the family `max_edge` at dispatch (same place derived image-token limits are computed).
- Cockpit: a select in the session settings menu (and mirrored into job/expert config so autonomous/loop runs can pin `Economy`). Default shown as `Standard`.

**Open product decisions (proposal in bold):** tier names/default → **Economy / Standard / High, default Standard**; literal "Original/Full" escape hatch → **skip** (invisible above the family cap, and the byte re-encode has to bound it anyway); scope depth for v1 → **session setting + global default first; project/workspace default and agent per-read as fast-follows** (agent per-read is the highest-value follow for the frontend case).

### Track 2 — give the persistent path transient-outage resilience (removes the fatality)
Port the worker's classify+backoff to `_execute_turn`, adapted to a **live in-process loop** (a session can't be "re-dispatched", so the analog of the worker's freeze→redispatch is **hold-turn-and-reconnect**: retry in place / re-establish the stream, keeping the WS/SSE open). Concretely:

1. **Extract a shared retry core (decided — the "proper" path).** This gap exists because the worker and persistent paths *diverged*; reimplementing retry in `persistent_graph.py` would recreate that. Instead lift `_classify_llm_error` (`graph.py:365-504`) into a shared module (e.g. `src/llm/transient.py`), plus a shared `call_llm_with_backoff(...)` helper carrying the classify+backoff loop, parameterized by the terminal action (worker: freeze+redispatch; session: in-place hold). Both graphs call it; the reranker's `_is_transient` folds into the same module. Replaces the ad-hoc `"APIConnectionError"` string match at `persistent_graph.py:1404`.
2. **Wrap the LLM call in a bounded retry** on `transient`/`rate_limit`/`auth_unavailable`, with exponential backoff sized to outlast a proxy restart (~30–60 s total, e.g. 4 attempts 2→4→8→16 s + jitter). Emit a lightweight "reconnecting…" turn state so the user sees a hold, not a dead turn. On exhaustion, *then* fall through to `on_error`. Three constraints:
   - **Retry at the LLM-call level, not the turn wrapper** (`:670-675`). The wrapper sits *after* tool execution; retrying there would re-run tool calls. The retry belongs around the `astream`/`ainvoke` invocation only.
   - **v1 scope = pre-first-token (connect-time) retries.** If the stream already emitted tokens via `callbacks.on_token` before dropping mid-stream, a naive re-run re-emits them → duplicated/garbled UI (the worker path avoids this by using atomic non-streaming `ainvoke`). Retry cleanly only when nothing has been streamed yet — which covers this incident (a connect-time `ConnectError`, zero tokens) and the common case. Treat a mid-stream drop as an explicit "reset the in-flight turn + restart" with a visible marker, not a silent re-run; scope that separately.
   - **The backoff hold must be user-interruptible** — poll `callbacks.check_interrupt()` during the sleep so a user can cancel or send a new message instead of being stuck for 60 s.
3. **Split the mislabeled fallback** (`:1404-1408`): only fall back to `ainvoke` for genuinely stream-incapable endpoints (`ResponseNotRead` / explicit unsupported); a connection/timeout/5xx is a retryable transport fault, not "streaming not supported".
4. **Add a wall-clock bound to streaming.** The `astream` loop and the `:1408` fallback have no `asyncio.wait_for`; add one when adding retry so a hung stream is bounded (matches the worker's `:1566`).
5. **Config knob** mirroring `memory.reranker.retries`/`retry_backoff` (`config/defaults.yaml:306-307`, dataclass `loader.py:1568-1579`): add `llm.transient_retries` / `llm.retry_backoff` to the `llm:` block of `config/persistent_defaults.yaml` (near `:14`, parsed around `loader.py:1925`). Keep SDK `max_retries` low (or 0, matching the worker) once the turn-level retry owns this, so the two don't multiply.

Model the in-place style on the existing live-loop counterpart to the worker freeze, the workspace-upgrade handler at `src/api/persistent_app.py:4851-4869`.

### Recommendation / sequencing
Track 1 splits cleanly: a **small hotfix core** — the `downscale_for_tier` util at the central seam applied at the `Standard` default (no UI yet) — plus the **fuller setting** (schema/config plumbing, cockpit menu, per-read override). Land the hotfix core **+ the small pieces of Track 2 (3 + 4: fix the misclassification and bound the stream)** first — contained, high-value, and together they cover this exact incident even if the proxy OOMs again. Then the fuller **Track 2 (1 + 2 + 5)** safety net and the **Track 1 setting/UI** in parallel — they're orthogonal and testable in isolation.

## Verification sketch

- **Track 1 (unit):** `downscale_for_tier` on a 4000 px / 4 MB JPEG at `Standard` returns ≤ ~1568 px re-encoded under the byte threshold; at `High` clamps to the family `max_edge`; at `Economy` ~768 px; a small image under both thresholds passes through byte-identical at every tier; the per-read `detail` override raises the tier but is clamped to the session ceiling. Assert the emitted `image_url` data-URL decodes to the expected dimensions, and that the util is hit for a browser-screenshot content block as well as a `read_file` image (uniform coverage).
- **Track 2 (unit):** a stubbed `llm_with_tools` that raises `httpx.ConnectError` on the first N calls then succeeds → the turn survives on the retry and streams normally; a persistent 5xx → the turn exhausts backoff, emits a single `on_error`, and does **not** spin; a `ContextOverflowError` still surfaces its typed friendly message (unchanged); a `NotFoundError`/401 fails fast (no retry). Assert `_classify_llm_error` is the decision point.
- **Local e2e (k3d):** point the session model at a proxy you can bounce; `kubectl delete pod` the proxy mid-turn and confirm the turn holds and completes on reconnect instead of persisting `"Connection error."`. Separately, send 5 full-res photos and confirm (a) the dispatched request is ~MBs not ~30 MB (Track 1) and (b) the proxy RSS stays well under 2Gi.

## Notes / follow-ups

- **Track 0 is live**; do not re-bump. If large multimodal turns still pressure the proxy, raise again — the limit is a ceiling, not a reservation.
- **Proxy HA is a separate weakness.** Single replica + RWO auth PVC means every restart is a user-visible outage. Options: a second replica (needs the auth token store to be RWX or externalized — non-trivial for CLIProxyAPI's file-based auth), a PodDisruptionBudget + faster readiness, or accept it and rely on Track 2 to ride restarts. Track 2 is the cheaper mitigation.
- **Classifier consolidation:** `graph.py:_classify_llm_error` and `reranker.py:_is_transient` should become one shared helper; this fix is a good forcing function.
- **Streaming has no timeout today** (`_execute_turn` astream + the `:1408` fallback) — a genuinely hung stream (not just a connect failure) is currently unbounded in sessions. Track 2 step 4 fixes this regardless of the retry work.
- This mirrors `reranker_transient_fault_hard_fails_job.md` almost exactly one layer up: a transient transport fault on a shared/remote dependency hard-fails a unit of work because the path conflates transient with structural. Same fix shape (classify → bounded transient retry → degrade/hold; structural still fails fast).
