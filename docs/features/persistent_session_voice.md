---
tags:
  - feature
  - persistent-chat
  - cockpit
  - orchestrator
  - audio
aliases:
  - read aloud
  - text-to-speech
  - voice in
  - voice out
  - TTS chunked playback
related:
  - "[[port_fessi_functionality]]"
  - "[[podcast_generation]]"
  - "[[persistent_chat_polish]]"
---

# Persistent-Session Voice I/O (transcribe-to-composer + read-aloud TTS)

Implementation reference for the **voice features in persistent chat** (the
cockpit session UI), as currently shipped on `develop`. Two halves:

- **Voice in** — the composer mic transcribes recordings to editable text.
- **Voice out** — a per-turn "Read aloud" button speaks an assistant message,
  chunking long messages into a navigable stack of audio players.

> Scope note: this is the **persistent-chat** voice path (cockpit ↔ orchestrator,
> over HTTP). It is distinct from the **agent-side** audio handling documented in
> [[port_fessi_functionality]] (`src/services/audio_helper.py`, used when a job
> agent reads an uploaded audio file from its workspace). The only overlap is a
> shared `_extract_transcript` shape (duplicated, not imported, to keep the
> agent decoupled from the orchestrator).

Origin: ported and reshaped from the Advanced-LLM-Chat ("fessi") backend, then
re-architected for SRW's remote-workspace / per-user-credential / k8s reality.

---

## Status at a glance

| Piece | State |
|---|---|
| Voice-in: transcribe-to-composer (editable + keep audio) | ✅ shipped |
| Voice-out: read-aloud, single short message | ✅ shipped |
| Voice-out: long message → chunked, sequential, per-section players | ✅ shipped |
| Per-section navigation + auto-advance + always-visible status | ✅ shipped |
| Per-model voice config (`params_json.voice`) + Admin voice dropdown | ✅ shipped |
| Native player themed dark (`color-scheme: dark`) | ✅ shipped |
| Robustness: 502-on-failure, fail-fast timeouts, kokoro probe tolerance | ✅ shipped |
| **Markdown cleanup of spoken text** | ⚠️ falls back to raw (see Open items) |
| Fully custom (design-system) audio player | ⛔ not done (native-dark only) |

Everything below is **live on the local k3d cluster** (verified end-to-end) and
**uncommitted on `develop`** as of this writing.

---

## Voice in — transcribe-to-composer

Recording a voice message transcribes it on upload, drops the text into the
composer (**editable**), and **keeps the audio attached**. Always on with
graceful fallback (no toggle/setting); language is **auto-detected** (no hint).

**Flow:** composer mic → `stopRecording()` builds an audio file preview →
`POST /api/persistent/threads/{id}/transcribe` (multipart) → text into
`inputText` (appended, not clobbering a typed draft) → `addAttachments()` keeps
the audio chip.

**Files:**
- `orchestrator/services/transcribe.py` — `transcribe_thread_audio()` +
  `_extract_transcript()`. Uses the SDK default JSON response format (NOT
  `response_format="text"`, which leaks the raw HTTP body — some OpenAI-compatible
  endpoints return a `{"text": ...}` blob even for a text request). `_extract_transcript`
  normalizes object/dict/JSON-string-blob/plain-string shapes.
- `orchestrator/services/capability_credentials.py` — `resolve_capability_credentials()`,
  the shared per-user model+endpoint resolver (user setting > system default →
  endpoint base_url/api_key). Used by both transcribe and TTS.
- `orchestrator/main.py` — `POST /api/persistent/threads/{id}/transcribe`
  (`require_thread_owner`; 400 empty, 413 > 25 MB, 204 when no whisper model, else
  `{"text": ...}`).
- `cockpit … api.service.ts` — `transcribeVoice()` (FormData POST; 204 → `'unavailable'`,
  error → `null`).
- `cockpit … persistent-chat.component.ts` — `stopRecording()` + `isTranscribing` signal.
- i18n: `chat.composer.transcribing`, `chat.composer.transcribeError` (EN + DE).

**Related agent-side fix:** `src/services/audio_helper.py` had the same
`response_format="text"` bug; fixed identically with a local `_extract_transcript`.

**Tests:** `tests/test_transcribe.py`, `tests/test_audio_helper.py`,
`cockpit/src/app/core/services/api.service.spec.ts`.

---

## Voice out — read-aloud TTS with chunked playback

A per-turn **Read aloud** button on an assistant turn's final text. A long
message is split into ordered, speakable **chunks**, each synthesized as a
**separate short request** and rendered as its **own audio player**; players
appear as they're synthesized and auto-advance, while staying individually
replayable.

### Why chunked (the constraints that shaped it)

1. **No single long request.** Synchronous TTS of a long message would exceed
   edge timeouts (Cloudflare ~100 s on prod) and the per-request timeout.
   Chunking makes each synthesis a short request.
2. **No truncation.** Product requirement: read the *whole* message (the
   dog-walking use case), unlike Claude/Gemini read-aloud which cut off.
3. **CPU TTS is ~real-time.** Measured **~38 chars/s** for `kokoro-cpu`, so a
   4096-char chunk ≈ 108 s of synthesis (blew the old 60 s timeout) and pegged
   the pod long enough to trip its liveness probe. → chunks are sized for the
   synthesizer (~1500 chars ≈ ~40 s), not the API's 4096 cap.

### Backend — `orchestrator/services/tts.py`

- `generate_message_tts(content, language, reformulate, user_id, db)` →
  `(spoken_text, mp3_bytes)`. Returns `None` when no TTS model is configured
  (endpoint → 204). Raises **`TtsSynthesisError`** when a model *is* configured
  but synthesis fails (endpoint → 502) — so the button surfaces an error instead
  of silently no-op'ing. LRU audio + formulation + plan caches (per-process).
- `_resolve_voice(model_id, language, db)` — reads the catalog row's
  `params_json.voice` (Admin → Providers), else the per-language default
  (`alloy`/`nova`). Lets each backend use its own voice catalog.
- `_synthesize_speech(...)` — `max_retries=0`, **timeout 120 s** (a CPU chunk is
  ~40 s; headroom for slower/loaded backends).
- `_formulate_for_speech(...)` — optional aux-LLM rewrite for natural speech
  (strip markdown, tables→prose). `max_retries=0`, timeout 30 s.
- **Chunk planner:** `plan_tts_chunks(content, user_id, db)` → ordered list of
  `<= 4096`-char chunks at natural breakpoints. Returns `None` when no TTS model
  (→ 204); otherwise always ≥1 chunk.
  - `_llm_clean_and_chunk()` — aux LLM cleans + splits into a JSON array
    (`CHUNKING_SYSTEM_PROMPT`, aim `TTS_CHUNK_TARGET = 1500`). Returns `None`
    (→ deterministic fallback) on no-key / error / truncation (`finish_reason ==
    'length'`) / unparseable output.
  - `_parse_chunk_array()` — tolerant JSON-array parse (strips code fences /
    surrounding prose).
  - `_resplit_oversized()` — re-prompts the LLM to split any chunk over the
    ceiling (one pass; the LLM "adjusts the chunks").
  - `_enforce_chunk_limit()` — hard code gate: nothing over `TTS_CHUNK_LIMIT =
    4096` reaches synthesis (deterministically re-splits if needed).
  - `_split_text_into_chunks()` — deterministic paragraph→sentence packer; the
    fallback when the LLM can't deliver, sized to `TTS_CHUNK_TARGET`.

### Backend — endpoints (`orchestrator/main.py`)

- `POST /api/persistent/threads/{id}/tts` — body `{content, reformulate, language}`
  → JSON `{"text": <spoken>, "audio": <base64 mp3>}`. `204` not configured,
  `502` synthesis failure. Used per-chunk with `reformulate=false`.
- `POST /api/persistent/threads/{id}/tts/plan` — body `{content}` → `{"chunks":
  [str, ...]}`. `204` not configured, `502` planner error (rare; planner has
  deterministic fallbacks).

### Frontend — `cockpit … persistent-chat.component.ts`

`TtsMessageState` per turn: `isGenerating`, `error`, `text` (spoken, for the
fold), `chunks`, `chunkUrls` (filled as synthesized), `synthIndex` (drives the
"Generating part N" status), `playPending` (auto-advance coordination).

- `toggleTts(key, content)` — plan → **sequential** synthesis of each chunk
  (`synthIndex` updates as it goes); first section autoplays via `playPending`.
  First-chunk failure resets to the error button; a later failure stops the
  chain but keeps earlier sections playable.
- `synthTtsChunk()` — synth one chunk (`reformulate=false`, already cleaned) →
  store blob URL.
- Auto-advance: each `<audio>` carries `data-tts-key`/`data-tts-index`;
  `onChunkEnded()` plays the next (or queues via `playPending` if not yet
  synthesized); `onPlayerReady()` autoplays only the pending index;
  `onPlayerPlay()` pauses every other player (one voice at a time);
  `findTtsPlayer()` looks elements up in the `@ViewChildren('ttsAudioEl')` list.
- `api.service.ts` — `planTTS()` (→ chunks | 'unavailable' | null), `generateTTS()`
  (→ `{text, audio}` | 'unavailable' | null; `decodeBase64ToBlob`).

**UI layout:** read button → "Preparing…" spinner (planning) → section-1 player
appears with a "Generating part 2 of N…" status below → more players stack in →
done (status clears), each player labelled "Part i of N". A collapsible "Spoken
version" fold (`<details>`, reuses the reasoning-block style) shows the spoken
text when it differs from the message.

**Styling:** native `<audio controls>` with `color-scheme: dark` so the controls
render dark (vs the default white pill). Read button 44 px / `md` icon /
full-opacity; status spinner 22 px (scoped — the composer transcribe spinner
stays 12 px); status + part text 18 px.

- i18n: `chat.tts.{play, error, spokenVersion, part, preparing, generatingPart}`
  (EN + DE).

### Per-model voice configuration

- A TTS model's voice rides in its catalog `params_json.voice` (no schema
  change — the create endpoint already accepted `params_json`).
- Admin → Providers "Add model": a **Voice** field appears when `tts` is ticked,
  rendered as a **backend-aware dropdown** when the model id is recognized
  (`cockpit/src/app/core/models/tts-voices.ts`):
  - `KOKORO_VOICES` — 67-voice snapshot of the live kokoro-fastapi
    `/v1/audio/voices` (af_/am_/bf_/bm_ English + es/fr/hi/it/ja/pt/zh; **no
    German**). `OPENAI_TTS_VOICES` — documented set for `tts-1`/`gpt-4o-mini-tts`.
  - `voicesForModelId()` detects the backend from the id; unrecognized → free-text.
  - Limitation: the field is on the **create** form only (this admin view has no
    edit form), so changing an existing model's voice means re-adding it or a DB edit.
- Tests: `cockpit/src/app/core/models/tts-voices.spec.ts`.

**Tests:** `tests/test_tts.py` (planner + endpoints, incl. 502/204/truncation/
voice-from-params), `api.service.spec.ts` (generateTTS + planTTS).

---

## Configuration & infrastructure (homelab / k3d)

Model routing goes through the `ai.h4ll.app` gateway (`model-orchestrator`).
**Alias convention:** `-strix` = in-cluster homelab node4 (Strix Halo, always-on);
non-strix names (`kokoro`, `gemma-4-moe`, `whisper-large-v3`) = university L40s
servers via a VPN sidecar (intermittently reachable). See
[[project_persistent_session_main_model_401]].

- **TTS default:** `system_settings['llm.default_tts_model']` was `kokoro`
  (university, returning 503) — fixed to **`kokoro-strix`** (node4, local), and
  the `kokoro-strix` row enabled in the `models` catalog. Every other
  `llm.default_*_model` was already `-strix`; TTS was the lone non-strix oversight.
- **kokoro-cpu probes** (`HomeLab/deployments_unmanaged/kokoro-cpu/deployment.yaml`):
  kokoro-fastapi is single-threaded and **blocks `/health` during synthesis**, so
  a normal synth flipped the pod NotReady (and could restart it). Probes loosened
  to tolerate it: `livenessProbe failureThreshold: 10, timeoutSeconds: 5`;
  `readinessProbe failureThreshold: 30, timeoutSeconds: 5` (~5 min grace each).
  *Applied live; the file edit is uncommitted in the HomeLab repo.*
- **Endpoint credentials** are stored encrypted (`v1:` ciphertext via
  `_decrypt_stored`) — a raw DB read returns ciphertext, not a usable key.

---

## Open items / future work

1. **Markdown cleanup of spoken text (highest priority).** The aux/formulation
   model defaults to `gemma-4-26b-a4b-strix`, a **reasoning** model that emits
   ~9 k characters of reasoning (~2.3 k tokens) + takes ~55 s on a trivial
   311-char rewrite (and ReadTimeouts on long input). So the planner currently
   **falls back to deterministic chunking** —
   sections play, but markdown/tables read raw. Fix: point TTS formulation at a
   **fast, non-reasoning model** (e.g. `gpt-4o-mini`), ideally a dedicated
   "formulation model" slot independent of the heavy `auxiliary` default.
2. **Windowed parallel cleanup** for very long messages — deterministically
   window the raw text first (paragraph boundaries), clean each window with a
   bounded fast-model call in parallel. Beats one giant LLM call (output-token
   ceiling) without an agent loop (which would serialize + multiply latency).
3. **Fully custom audio player** matching the design system (custom scrubber /
   accent colors) — current styling is native-dark via `color-scheme`.
4. **Backend-aware chunk sizing** — `TTS_CHUNK_TARGET` is tuned for CPU kokoro
   (~1500); a GPU / OpenAI backend could use larger chunks.
5. **Edit voice on existing catalog models** — needs an edit form (create-only today).
6. **Commit the arc** — SRW changes on `develop` + the HomeLab kokoro probe edit.
