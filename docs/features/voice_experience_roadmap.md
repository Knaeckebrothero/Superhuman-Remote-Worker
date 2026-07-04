---
tags:
  - feature
  - cockpit
  - voice
  - tts
  - stt
  - ux
aliases:
  - read aloud revamp
  - voice in voice out
  - tts overhaul
  - voice roadmap
related:
  - "[[custom_llm_endpoints]]"
  - "[[auxiliary]]"
  - "[[db_backed_llm_config]]"
---

# Voice Experience Roadmap (voice-in / voice-out)

> Make read-aloud feel first-class instead of bolted-on: a transparent status
> box that morphs into a custom themed audio player, voices picked from the
> *content's* language, high-quality external voices now, and fully custom
> fine-tuned voice personas later (built by the loop agent, not by hand).
> Foundation first: framework → UI → voices → custom voices & optimization.
>
> **North star: reliability.** Hitting Read must start a visible,
> predictable process that always ends in either audio or an honest error.
> Total duration matters far less than time-to-first-audio and never leaving
> the user guessing. Only the reformulation LLM call is inherently blocking —
> everything after it overlaps with playback.

## Status

**Design refined 2026-07-04 after a 6-agent research pass** (product UX
patterns, provider landscape, browser audio engineering, progress-UX
research, cockpit + orchestrator integration maps). Phase 0 framework
questions answered; player architecture decided (§Phase 2). Phase 5 (custom
voices / personas) is **explicitly deferred** and earmarked for the
self-improvement loop agent; its pipeline design is recorded here so the
agent has the full plan.

**Phase 0 implemented 2026-07-04** (honesty + diagnosability + metering; see
the checklist below for per-item status). Verified locally: `pytest
tests/test_tts.py tests/test_transcribe.py` (52 passed), `ruff check`, cockpit
`npm run build` (clean, no budget warning) + full `npm test` (788) + the new
`voice-capabilities.service.spec.ts`.

**Live-cluster smoke (k3d, Tilt) 2026-07-04 — PASS.** `GET
/api/voice/capabilities` → `200 {tts:true, stt:true}` (kokoro + whisper-large-v3
configured). A real read-aloud on a `test`-owned thread (gemma chunk-plan + a
genuine ~207 KB kokoro MP3) wrote three `usage_events` rows to `srw_audit`:
`tts`/`tts-character` (qty 183), and `llm`/`prompt-token` + `completion-token`
(357/972, `source='orchestrator'`, `details.via='tts'`, `stage='chunk-plan'`) —
the metering gap is closed end-to-end, not just in unit tests. Not smoke-tested
on-cluster (both capabilities are configured / no interactive browser attached):
the disabled-button state and tap-to-play prompt — both covered by the unit
tests + the verified availability payload; and STT `stt-request` metering, which
rides the same now-proven ledger path.

**Phase 1 (the box) implemented + verified 2026-07-04.** Extracted read-aloud
into `ui/read-aloud/AppReadAloudComponent`; the status box ticks through the
staged microcopy with elapsed counter, cancel, per-part retry, auto-retry, and
the honest "rewriting skipped" note. Local: full cockpit `npm test` (795) +
`npm run build` clean (**SCSS budget warning gone** — defect 7) + a new
component spec; backend `pytest` (53). Live k3d smoke of the `/tts/plan`
contract: `{chunks, rewritten}` returned, first chunk 468 chars (≤500) split at
a sentence boundary, and both `rewritten:true` (gemma cleaned the markdown) and
`rewritten:false` (deterministic fallback) branches confirmed. The box's visual
states weren't driven headlessly (no browser attached) — covered by the unit
tests + template typecheck.

**Phase 2 (the custom player) core implemented + verified 2026-07-04.**
`ReadAloudPlaybackService` (root, single primed `HTMLAudioElement` + prefix-sum
virtual timeline) replaces the native `<audio>` chrome; the box now hosts a
themed player (play/pause, known-region + estimated-tail seek bar, section
prev/next, persisted speed, current/`~total` time), with Media Session and
Safari click-priming. Frontend-only (no backend change). Local: full cockpit
`npm test` (**803**) + `npm run build` clean + a playback-engine spec
(timeline math / seek routing / one-at-a-time). Interactive + device behaviors
(scrub, speed persistence, lock-screen, Safari first-audio) aren't drivable
headlessly; stretch flair (highlight-as-spoken, waveform, ping-pong,
mini-player) is deferred (see Phase 2 checklist).

**Phase 3 (external voices) v1 implemented + verified 2026-07-04.** The OpenAI
`gpt-4o-mini-tts` lane: `instructions` style-prompt passthrough,
content-language voice selection (`_detect_language` EN/DE + per-language
`voices` map), and a user `default_tts_voice` setting with a Settings →
"Read-aloud voice" picker. Local: cockpit `npm test` (803) + build clean;
backend `pytest` (**61**). Live k3d smoke: EN→`alloy`, DE→`nova` (attempted —
Kokoro has no German voice, the honest 502 that gpt-4o-mini-tts fixes), and a
`default_tts_voice=af_heart` set via the settings API won the next synthesis.
Vendor wow-lane adapters (ElevenLabs/Hume/MiniMax) + streaming are deferred to
the ear bake-off (need a chosen vendor + keys); dropping `gpt-4o-mini-tts` in as
the TTS default (Admin → Providers, needs an OpenAI key) is the one remaining
user action to light up the external lane in dev.

**Phase 4 (STT hardening) implemented + verified 2026-07-04.** Fixed the
honesty gap (transcription failure returned `204` = "off"): new
`TranscriptionError` → `502`; size-scaled STT timeout + `max_retries=0` for long
clips; the 20-min recording cap is now enforced (auto-stop) with a soft "stops
soon" warning + m:ss elapsed. Local: cockpit `npm test` (803) + build clean;
backend `pytest` (**64**). Live k3d smoke: garbage audio → `502` (was a silent
204), and a real kokoro→whisper round-trip → `200` with an accurate transcript
plus an `stt-request` ledger row (also confirming Phase 0's STT metering live
for the first time). Only first-party code phase left is nothing —
**Phase 5 (custom voices/personas) is the deferred loop-agent project.**

## Current state (as-is, verified 2026-07-04)

### Voice-out (read aloud)

- **Entry point**: a `volume_up` button under each assistant turn in
  persistent chat (`cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:930`,
  TTS state/methods at `:76-93` and `:2310-2465`, styles at
  `persistent-chat.component.scss:1610-1735`). Desktop cockpit only.
- **Flow** (client-orchestrated, two endpoints):
  1. `POST /api/persistent/threads/{id}/tts/plan` — auxiliary LLM strips
     markdown / describes code / prose-ifies tables **and** splits into
     ordered chunks (~1500-char target, 4096 hard gate, deterministic
     fallback). One opaque call; the UI shows only "Preparing audio…".
  2. Client synthesizes chunks **sequentially** via
     `POST /api/persistent/threads/{id}/tts` (OpenAI-compatible
     `audio.speech`, MP3 as base64 JSON). Each chunk renders as its own
     **native `<audio controls>`** element with "Part N of M" labels;
     auto-advance on end; collapsible "Spoken version" panel.
- **Backend**: `orchestrator/services/tts.py`. Per-process LRU caches (audio /
  formulation / plan). `max_retries=0` everywhere (a down endpoint fails in
  seconds, not minutes).
- **Voice selection**: admin-only. `_resolve_voice()` reads the model catalog
  row's `params_json.voice`; fallback is hardcoded by **UI language**
  (`alloy` EN / `nova` DE) — not by content language. No user-facing voice
  choice anywhere.
- **Deployed backend**: Kokoro-82M (kokoro-fastapi) — CPU serving synthesizes
  at ~real-time (~38 chars/s measured), which is why chunks are capped small.
  67 voices, no German voice, no style instructions. (ai.h4ll.app also runs
  Kokoro on L40S GPUs with spare capacity.)

### Voice-in (dictation)

- Mic button → `MediaRecorder` (webm/opus) → `POST .../transcribe` →
  transcript dropped into the composer (editable), audio kept as attachment.
- `orchestrator/services/transcribe.py`: user's Whisper model, language
  **auto-detected**, no caching. Server cap 25 MB (413 above). **No
  client-side duration cap** and no duration display while recording.
- 10+ minute recordings are **untested** (size-wise fine — opus ≈ 0.2–0.5
  MB/min — but backend timeout/latency behavior unknown).

### Framework facts (Phase 0 questions, answered)

- **TTS/STT run entirely orchestrator-side.** The agent pod is never
  involved; the thread in the URL is only an auth scope
  (`require_thread_owner`, `orchestrator/security/access.py:528` — ownership
  check, no thread-status check). **Reading an old message in an ended /
  offline session works today; no pod needs to start.** The reformulation
  aux model likewise resolves per *user* (settings → system default), not
  per session — read-aloud is fully session-independent.
- Model + key resolution: user setting → system default → endpoint-anchored
  or built-in credentials (`orchestrator/services/capability_credentials.py:23-71`).
  TTS off = resolver returns `None` = endpoint answers `204`.
- **Metering gap**: TTS synthesis, STT, *and* the aux formulation/chunking
  LLM calls are entirely unmetered/unaudited — they call the resolved
  endpoint directly and never touch `usage_events` (which is only fed by the
  now-removed gateway's spend log). See Phase 0.

### Known defects / UX debt

1. **Silent feature-off** (likely the "students said it didn't work"
   reports): no TTS model configured → 204 → the cockpit deliberately shows
   *nothing* (`persistent-chat.component.ts:2347`).
2. **Autoplay block swallowed**: `play()` rejection is silently caught.
   Worse: on Safari, activation is per-element and transient activation
   expires after ~5 s in every browser — a fresh `<audio>` element played
   30 s after the click can reject with `NotAllowedError` by design. A
   second plausible "it didn't work" vector.
3. **First-audio latency**: CPU Kokoro ≈ 40 s of synthesis before the first
   sound on a long message; only a small spinner hints anything is happening.
4. **Native players are ugly**: N stacked `<audio>` pills + "Part N of M"
   clutter; nothing fits the cockpit theme.
5. **Wrong language, wrong voice**: voice keyed off UI language, not content
   language; Kokoro can't speak German at all.
6. **Silent formulation degradation**: with no aux model (or aux failure),
   the plan falls back to a deterministic split of the **raw markdown** —
   the voice reads asterisks and table pipes, and nothing says so.
7. **Chat stylesheet over budget**: `persistent-chat.component.scss` is
   ~58.7 kB against a 48 kB `anyComponentStyle` error budget — the player
   must be extracted into its own component (which also moves ~3 kB of TTS
   styles out).

## Design principles (research-backed)

From the 2026-07-04 research pass: a product survey (ChatGPT, Gemini,
Perplexity, Copilot, Edge Read Aloud, ElevenLabs Reader, Speechify,
NotebookLM, Matter) plus a primary-source-verified evidence review
(Nielsen 0.1/1/10 s thresholds; Harrison UIST'07/CHI'10 progress-perception;
Buell & Norton "Labor Illusion" 2011; Maister waiting-lines psychology;
Material/Apple HIG/NN/g/PAIR guidance; ElevenLabs TTFA docs; Perplexity's
published "visible intermediate progress increases willingness to wait").
Rules, ranked by impact on perceived reliability:

1. **Acknowledge the click in <100 ms, in place.** The status box renders
   instantly *in the slot the player will occupy* and morphs into the
   player there (Apple HIG "show something as soon as possible";
   Vercel placeholder→replace pattern). Never a detached spinner or toast.
2. **Time-to-first-audio is the metric; never wait for full synthesis.**
   Playback starts the moment chunk 1 exists while the rest generates
   (ElevenLabs' named TTFA pattern; Bouch 2000: partial delivery extends
   "good"-rated tolerance ~4×). Corollary: **make chunk 1 deliberately
   short** (~500 chars) to pull TTFA down — first audio is worth more than
   any progress bar.
3. **Named stages with unit counts — never a bare spinner, never a fake
   percent.** "Rewriting text for speech…" → "Generating audio — part 2 of
   5…". Itemized named operations beat a uniform bar and *raise* perceived
   value (Labor Illusion); the chunk count gives count-determinate progress
   even when time is unknown (NN/g: ≥10 s demands determinate feedback;
   Material: switch to determinate the moment progress is measurable).
4. **One overall indicator that never visibly stalls — especially near the
   end.** Pauses read slowest and the penalty is amplified near completion
   (Harrison peak-end). Keep micro-motion (pulse/elapsed-seconds) on the
   active stage; don't map a 30 s stage onto the bar's last 10%.
   Front-load variability: users tolerate open-endedness at the *start*
   (our slow LLM rewrite is stage 1 — present it honestly), then the part
   countdown provides steady cadence.
5. **Coarse honest expectations, never precise promises.** "Usually ~30 s–2
   min on this voice" once, plus live elapsed time; no countdowns
   (Maister: a blown estimate is worse than none; repeated "almost done"
   reads as dishonest). Per-backend telemetry can tighten the range later.
6. **Fail loud, stage-named; keep everything that succeeded; retry only the
   failed unit.** Parts 1–2 stay playable while "Retry part 3" regenerates
   one chunk (NN/g edit-don't-restart; ElevenLabs re-renders only changed
   paragraphs). Auto-retry transients with visible backoff ("retrying —
   attempt 2 of 3"), cap it, then hard-fail with state preserved and the
   non-AI fallback named: the text is right there to read (PAIR).
   ChatGPT's silent icon-revert is the category's most-cited trust killer.
7. **Non-blocking and cancellable.** Chat stays usable; generation
   continues if the user scrolls away; ✕ cancel at every stage, and
   cancelling keeps already-synthesized parts (Nielsen: >10 s operations
   need a signposted interrupt). One active playback at a time (already
   implemented, keep it).
8. **Speed control is the single most-demanded TTS control industry-wide**
   (ChatGPT's longest-lived feature request; extension ecosystems exist
   purely to add it). `playbackRate` + `preservesPitch` make it free.
9. **Cache the artifact.** Replay/scrub-back must be instant; re-paying a
   90-second synthesis on every replay is fatal. (Server-side LRU exists;
   the client keeps blob URLs for the message lifetime.)
10. **Structural seek beats a time scrubber for markdown** — Edge navigates
    by paragraph, Speechify by sentence. Chunks give us section jumps for
    free; a fine scrubber within the known region complements them.
11. **Highlight-as-spoken is the line between "chat toggle" and
    "reader-grade"** (all dedicated readers do it; no chat assistant does).
    Chunk-level highlight is nearly free and doubles as progress. Word-level
    needs provider timestamps; out of scope for v1.
12. **Stream status and value, not raw intermediates** — typed per-stage
    events (stage-started / chunk-ready / chunk-failed / done; the Vercel
    `submitted → streaming → ready | error` machine is the minimal
    contract). Don't scroll the rewritten script past the user (NN/g 2026:
    streamed verbosity overwhelms). Anti-pattern confirmed by evidence: **no
    skeleton screen** — skeletons are for page-structure loads and tested
    *worst* for widget-sized waits (Viget).

---

## Roadmap

### Phase 0 — Framework & honesty fixes (small, do first)

- [x] Verify TTS needs no agent pod / works for offline sessions →
      **confirmed, orchestrator-only** (see above). Document only, no code.
- [x] **Kill the silent 204**: new `GET /api/voice/capabilities` reports
      `{tts, stt}` (a model *resolves* — user setting or system default);
      `VoiceCapabilitiesService` (root, fetch-once, **fails open**) feeds
      `ttsUnavailable()` / `sttUnavailable()`. Read-aloud button renders
      disabled with a `volume_off` glyph + "not set up" tooltip; the mic button
      the same. Only *positively-known* unavailability disables — null/true
      stays enabled and the 204/502 path is still the backstop.
- [x] **Autoplay-block surfacing** (the catch-and-show half of the activation
      trap): `play()` rejections are no longer swallowed — a blocked section
      sets `playBlocked` and shows an accented **Tap to play** affordance
      (`resumeBlockedPlayback` re-arms from the fresh gesture; a successful
      play clears it). ⏳ The full fix — priming ONE persistent `Audio` element
      in the click handler and reusing it for every chunk — is **Phase 2** (it
      replaces the per-chunk native `<audio>` elements wholesale).
- [x] **Close the metering gap**: `generate_message_tts` /
      `plan_tts_chunks` / `transcribe_thread_audio` now emit `UsageEvent`s to
      the `UsageLedger` (TTS → `tts-character`, aux formulation/chunking →
      `prompt-token`/`completion-token` under `source='orchestrator'`, STT →
      `stt-request`). **Non-load-bearing**: gated on `ledger.is_available`, all
      writes swallow errors, STT metering sits *outside* the transcribe try so
      it can never drop a transcript. Feeds rate-limiting v2's ledger.
- [x] Structured failure logging for TTS/STT: every warn/exception now names
      the `stage=` (formulate | chunk-plan | synthesize | transcribe), the
      `model=`, and the `base_url=` (never the key) so the next "it didn't
      work" report is diagnosable from the log alone.

### Phase 1 — The box (transparency)

**Implemented 2026-07-04** as `cockpit/src/app/ui/read-aloud/`
(`AppReadAloudComponent`) — a standalone OnPush widget that owns the whole
read-aloud lifecycle for one message. All the TTS state/methods were extracted
out of `persistent-chat.component.*` into it, which also relieved the
over-budget stylesheet (defect 7 — the SCSS budget warning is gone). Playback
still uses native `<audio>` elements; the custom player is Phase 2.

One status box appears in the message the moment **Read** is clicked and
lives through the whole lifecycle; the player materializes *inside the same
box* when the first chunk is ready.

- [x] New standalone component: instant-ack (`Preparing to read aloud…`) →
      `Rewriting text for speech…` → players + `Generating audio — part N of
      M…`. The near-instant chunk-split gets **no stage of its own** — it's the
      moment M becomes known and the part-count turns determinate.
- [x] Elapsed-seconds counter appears after 10 s in the rewrite stage; ≥25 s
      swaps to an explained-wait line (`Still rewriting — the language model is
      responding slowly…`), never "almost done"; one coarse expectation line
      (`Usually ~30 s–2 min…`), no countdowns.
- [x] ✕ **cancel at every stage** (`cancel()`); cancelling keeps
      already-synthesized parts (`Read-aloud cancelled — N parts kept`), or
      returns to the button when nothing was synthesized yet.
- [x] Plan returns `{chunks, rewritten}` (annotated `/tts/plan`);
      **chunk 1 kept short (~500 chars)** via `_shorten_first_chunk` +
      a chunking-prompt hint. *(Content-`language` detection stays Phase 3.)*
- [x] Per-stage errors with unit-level retry: earlier parts stay playable,
      `Retry part N` regenerates one chunk then resumes the chain; bounded
      visible auto-retry (`retrying (attempt 2 of 3)…`), then hard-fail with
      state preserved + `Try again`.
- [x] Formulation-skipped surfaced: `rewritten:false` → the box shows
      `Rewriting skipped — reading the original text.` instead of pretending
      it cleaned up.
- [x] "Spoken version" disclosure moved inside the box (collapsed; only when
      the rewrite differs from the original).
- [x] Non-blocking: the box keeps progressing if the user scrolls away; once
      playing it demotes to a quiet line (`Playing · generating part N of M…`)
      and on completion becomes `Ready · M parts · m:ss`.

**Microcopy spec** (from the verified progress-UX brief; EN — DE mirrors in
`de-DE.json`):

| State | Copy |
|---|---|
| Instant ack (0–300 ms) | `Preparing to read aloud…` |
| Rewrite | `Rewriting text for speech…` → after 10 s append ` · 14s` |
| Rewrite slow | `Still rewriting — the language model is responding slowly…` |
| Plan done | `Preparing audio (5 parts)…` |
| Synthesizing | `Generating audio — part 1 of 5…` |
| Playing + generating | `Playing · generating part 3 of 5…` |
| Done | `Ready · 5 parts · 3:12` |
| Cancelled | `Read-aloud cancelled — 2 parts kept` |
| Rewrite failed | `Couldn't prepare the text for speech — the language model didn't respond. [Try again]` |
| One part failed | `Part 3 of 5 failed to generate. Parts 1, 2, 4 and 5 are ready. [Retry part 3]` |
| Auto-retrying | `Audio server didn't respond — retrying (attempt 2 of 3)…` |
| Hard fail | `Audio generation failed after 3 attempts (part 3 of 5). Your finished parts are kept. [Retry] · [Details]` + `You can keep reading the text above.` |
| Offline | `Connection lost — will resume when you're back online.` |

Failure-copy formula (NN/g + Microsoft): what happened → what's preserved →
one constructive action; never blame the user; error codes behind
`[Details]`, never in the primary line.

**Acceptance**: click Read on a long markdown-heavy message → box appears
instantly (<100 ms), stages tick through visibly with counts and elapsed
time, first audio plays without any native `<audio>` chrome, a mid-sequence
chunk failure keeps earlier parts playable and offers "Retry part N",
cancel works at every stage, and a user with no aux model sees "rewriting
skipped".

### Phase 2 — The player (custom, themed)

**Architecture (decided 2026-07-04 from the browser-audio research):**

> **One persistent `HTMLAudioElement` behind a virtual-timeline controller,
> primed synchronously in the click.** Chunk seams fall on sentence/section
> boundaries, so a 50–150 ms src-swap seam is imperceptible; the element
> route keeps pitch-corrected `playbackRate`, Media Session, native
> buffering, and iOS compatibility for free. **Rejected**: MSE (raw-MP3
> SourceBuffer is Chrome-only — Firefox wontfix, Safari fMP4-only) and pure
> Web Audio scheduling (no pitch-preserving speed; ~23 MB/min decoded PCM;
> manual everything). **Optional enhancement**: two-element ping-pong
> preload (standby element preloads the next blob; swap on `ended`) shrinks
> seams to ~20–60 ms — prime *both* elements in the gesture on Safari.

**Core implemented 2026-07-04** as `ui/read-aloud/read-aloud-playback.service.ts`
(`ReadAloudPlaybackService`, root-provided) + a custom player in the Phase-1
box. Native `<audio>` chrome is gone. The stretch/flair items (highlight,
waveform, ping-pong, mini-player) are deferred — noted below.

Implementation checklist:

- [x] `ReadAloudPlaybackService` (root, signal-based): one shared primed
      `HTMLAudioElement`, prefix-sum virtual timeline (`offsets`), seek routing
      (`offsets[i] + el.currentTime`), `ended`-advance across chunks (waits when
      it runs dry mid-message), and the **one-playback-at-a-time** rule (a
      singleton element ⇒ a new `start()` takes over). Durations are probed
      off-screen per chunk by the component.
- [x] Player UI in the box: play/pause, unified seek bar over the **known**
      region + a faint estimated **tail**, section prev/next (`skip_previous`/
      `skip_next`), speed control (0.75–2×, persisted via
      `ChatPreferencesService.playbackSpeed`), current / `~total` time.
- [x] Estimated total: `remainingChars / ~15 s-per-char` for the tail,
      `~` prefix until the last chunk lands; a seek into the unsynthesized
      region is parked as `pendingSeek` and resolved on chunk arrival.
- [x] `playbackRate` gotchas: sets `defaultPlaybackRate` too and re-applies on
      every `loadedmetadata` / src swap; `preservesPitch` + `webkitPreservesPitch`;
      scrubber driven by `requestAnimationFrame`, not `timeupdate`.
- [x] Media Session API: `setActionHandler` play/pause/seek/prev/next +
      `setPositionState` (feature-detected — Firefox lacks it — clamped
      `position ≤ duration`).
- [ ] **(deferred)** Chunk-level highlight-as-spoken — needs chunk→char range
      metadata from the plan.
- [ ] **(deferred)** Waveform/EQ flair (`createMediaElementSource` + reuse
      `VoiceAudioProcessor`/`TimeBasedBarVisualizer`; iOS-risky).
- [x] Keep every blob URL until the component is destroyed (seek-back works),
      then revoke.
- [ ] **(deferred)** Two-element ping-pong preload (shrinks src-swap seams);
      single-element seams are already imperceptible on sentence boundaries.
- [ ] **(deferred)** Global mini-player when the source message scrolls away.

**Acceptance**: no native `<audio>` visible anywhere in chat *(met)*; scrubbing
back into an already-played section works instantly *(met — virtual timeline +
blobs kept)*; speed persists across sessions *(met — localStorage pref)*;
pausing via lock-screen controls works on mobile *(implemented — Media Session;
needs a device to confirm)*; audio starts on Safari even when synthesis took
40 s *(implemented — element primed in the click gesture; needs Safari to
confirm)*. The interactive/device behaviors weren't driven headlessly (no
browser attached) — covered by the playback-service spec + template typecheck.

### Phase 3 — The voices (external plug-and-play)

**Framing decision (2026-07-04)**: external providers *are* the
plug-and-play path — for users who don't need the privacy of a local model,
a hosted API is the easiest route to genuinely good voices, so v1 quality
comes from there. Self-hosting high-quality voices is a deliberate *later*
investment (see Phase 5); Kokoro stays as the existing local/privacy
fallback meanwhile.

Provider landscape (researched 2026-07):

| Provider | Fit | Notes |
|---|---|---|
| **OpenAI `gpt-4o-mini-tts`** | **Start here — zero code change** | Same `audio.speech` endpoint; free-text `instructions` style prompt (persona-ready); ~$0.015/min; 99 langs (English-accented tint on DE/FR); streaming-capable |
| **ElevenLabs v3 / Multilingual v2 / Flash** | Wow-factor lane | Best-in-class DE/FR; v3 audio tags (`[whispers]`, `[laughs]`); ~$50–100/1M chars; needs adapter; v3 is HTTP-stream only + slower |
| **Hume Octave 2** | Wow-factor alternative | Infers emotion from meaning + free-text acting instructions + voice-from-prompt; ~half ElevenLabs price; unlimited cloning on paid tiers; adapter |
| **MiniMax speech-2.8** | Already a customer | 40+ langs, emotion presets (no free-text), hex-in-JSON API (adapter); **cloning API ~$3/voice** → Phase 5 asset |
| **Inworld TTS-1.5** | Value outlier | $5–10/1M chars, top of quality arenas, 15 langs incl. DE/FR; adapter |
| ~~Groq~~ | **Disqualified** | playai-tts deprecated 2025-12; replacement (Orpheus) is English-only |

**v1 implemented 2026-07-04** (the OpenAI `gpt-4o-mini-tts` lane, backend +
settings picker). The vendor wow-lane adapters + streaming are deferred to
the ear bake-off (they need a chosen vendor + keys).

- [ ] **Step 1 (config-only, needs an OpenAI key + admin)**: add
      `gpt-4o-mini-tts` in Admin → Providers, set as TTS default. Everything
      below is wired for it; this is the remaining user action to make the
      external lane live in dev.
- [x] **`instructions` passthrough**: `_synthesize_speech(..., instructions=)`
      passed through only when set (tts-1/Kokoro reject it), resolved from the
      catalog `params_json.instructions` — the style-prompt hook for
      gpt-4o-mini-tts and Phase 5 personas. (Unit-verified; needs a
      gpt-4o-mini-tts backend to exercise live.)
- [x] **Content-language detection**: `_detect_language` (EN/DE heuristic —
      umlauts + German function words) picks the voice from the *content*, not
      the UI language; per-language `params_json.voices` map + built-in default.
      *(Live-smoke: EN→`alloy`, DE→`nova` (attempted; Kokoro has no German
      voice → the honest 502 — exactly what gpt-4o-mini-tts fixes). A fuller
      LLM-tag detector is future work.)*
- [x] **User-facing voice picker**: `default_tts_voice` user setting
      (`UserSettingsUpdate` + `_pick_voice` priority: user → catalog `voice` →
      catalog `voices[lang]` → default) + a Settings → "Read-aloud voice"
      dropdown (fed by `voicesForModelId`, free-text fallback for unknown
      backends). *(Live-smoke: set `af_heart` via the settings API → the next
      synthesis used it, beating the content-language default.)*
- [ ] **(deferred — ear bake-off + keys)** Provider adapter seam in `tts.py`
      (dispatch on `params_json.provider`; `RerankerScorer` httpx pattern,
      ~50 lines each) for ElevenLabs / Hume / MiniMax.
- [ ] **(deferred)** Streaming synthesis
      (`audio.speech.with_streaming_response`) / concurrent-chunk synthesis on
      fast backends.

**Acceptance**: a German message is read in a German-capable voice without
touching settings; the user can pick a distinctly non-Dave voice in
settings and every subsequent Read uses it; at least one
instruction-capable provider is live in dev; time-to-first-audio on the
external lane < 5 s for a typical message.

### Phase 4 — Voice-in hardening (STT)

**Implemented + verified 2026-07-04.** The core was a honesty gap: the backend
returned `204` for *both* "no STT model" and "transcription failed", so a real
failure read as "feature off" and the composer swallowed it silently.

- [x] **Backend hardened for long clips**: `transcribe_thread_audio` now uses a
      **size-scaled timeout** (`_stt_timeout`, 120 s–600 s by payload size)
      instead of a flat 60 s that would kill a long-but-valid clip, plus
      `max_retries=0` (fail fast, no multi-minute backoff).
- [x] **Recording UX**: the 20-min cap the service was handed is now actually
      enforced (auto-stop in the state subscription; generous enough for "10+
      min" while staying under the 25 MB backend cap), a **soft "Recording
      stops soon" warning** in the last minute, and the elapsed time now shows
      as **m:ss** (raw seconds read badly near 20 min).
- [x] **204-vs-error honesty**: new `TranscriptionError` → the endpoint answers
      **502** when a configured model fails (mirrors `TtsSynthesisError`); an
      empty transcript is a `""` success, not a `204`. The cockpit already maps
      502 → the `transcribeError` notice. *(Live-smoke: garbage audio → 502
      `{"detail":"Transcription failed"}` (was a silent 204); a real
      TTS→STT round-trip → 200 with an accurate transcript + an `stt-request`
      ledger row.)*
- [ ] **(deferred, conditional)** Chunked server-side transcription (split on
      silence) — only needed if long clips prove slow; the size-scaled timeout
      covers the common case.

**Acceptance**: a 12-minute rambling voice memo comes back as text without
the UI looking frozen *(backend hardened — size-scaled timeout; real
device recording needs a mic to confirm end-to-end)*, or **fails with an
honest message** *(met — 502 → composer error, live-verified)*.

### Phase 5 — Custom voices & personas (DEFERRED — loop-agent project)

Explicitly not built now. Recorded so the self-improvement loop agent can
pick it up as a project goal.

**Custom-voice fine-tune pipeline (the plan):**

1. **Sample generation**: LLM pipeline produces thousands of short scripts
   with phoneme/prosody/emotion/style coverage for the target persona.
2. **Audio generation**: zero-shot voice cloning renders candidate audio
   for every script. Provider options (2026-07 research): **MiniMax**
   (API-first, 10 s sample, ~$3/voice one-time — already a customer),
   **Hume** (unlimited cloning on paid tiers, friendliest terms),
   **Cartesia** (instant clone from $5 tier), **ElevenLabs** (quality
   benchmark, tier-gated + consent workflow). Note: OpenAI's
   `audio.speech` schema now accepts a custom-voice object (`voice:{id}`)
   — a custom-voice program is landing on the reference API.
3. **Agent curation**: an agent scores/filters/fixes the candidates
   (pronunciation, artifacts, style consistency) into a clean training set.
4. **Fine-tune**: train on RunPod or university GPUs (a handful of A100s);
   ~$200/voice is acceptable — quality is the metric, not cost.
5. **Serve**: self-hosted on the L40S headroom.

**Local high-quality serving spike** (prerequisite for step 5, and the
privacy lane for users who can't use hosted APIs): there is no true
plug-and-play high-quality self-hosted TTS today — candidates are Docker +
OpenAI-compatible(ish) servers, realistically an hour+ each to stand up and
judge: **Orpheus-FastAPI** (3B, emotive tags), **Chatterbox TTS API**
(zero-shot cloning + exaggeration control), **Speaches** (Kokoro/Piper
behind one OpenAI API), **Fish-Speech / OpenAudio S1-mini** (multilingual
incl. German). Whichever wins doubles as the fine-tune serving target.

**Persona layer** (product shape): persona = voice + style instruction +
greeting behavior; selectable per user, overridable per thread. Personality
targets on record: "big sister", French accent, Subnautica-Cyclops greeting,
Terminator/Skynet. Greetings/wake behaviors follow after personas exist.
Consent/licensing note: every cloning provider requires documented voice
consent; ElevenLabs and Hume are most explicit about commercial terms.

---

## Cockpit integration notes (from the 2026-07-04 code map)

- **New component home**: `src/app/ui/read-aloud/` (design-system widget
  convention: standalone, `OnPush`, signal `input()`/`output()`, external
  `styleUrl`, barrel `index.ts`; component-unit spec via bare instantiation,
  no TestBed). Extracting TTS out of `persistent-chat.component.*` also
  relieves the over-budget stylesheet (defect 7).
- **Box styling contract**: match the `app-tool-card` `.tc` pattern for the
  card look (`border:1px solid var(--border-color); border-radius:
  var(--radius-surface); background: var(--surface-0)`; header on
  `--panel-bg`) or `.thinking-block` for the subtle left-accent look.
  Tokens: `--text-primary/secondary/muted`, `--surface-0/1/2`,
  `--accent-color`, `--danger`, `--radius-surface`.
- **Playback speed pref**: extend `ChatPreferencesService`
  (localStorage-backed signals; store speed as an enum via the existing
  `readEnum` helper). Spec follows `chat-preferences.service.spec.ts`.
- **i18n**: `chat.tts.*` keys exist in `en.json` + `de-DE.json` (full
  parity); `stop` and `generating` are currently unused — reuse before
  adding keys. New stage keys need both locales.
- **Icons**: Material Symbols via `app-icon` ligatures — `play_arrow`,
  `pause`, `speed`, `replay_10`, `forward_10`, `graphic_eq` all available.
- **Visualizer reuse**: `VoiceAudioProcessor` (takes an AnalyserNode) and
  `TimeBasedBarVisualizer` (takes a level callback) are source-agnostic;
  `createMediaElementSource` is used nowhere yet (new glue), and
  `CanvasRenderer`'s hardcoded blue gradient needs tokenizing.
- **SSE client precedent** (only if backend streaming ever lands): raw
  `EventSource` with `?ngsw-bypass=true` (see `notification.service.ts`).

## Orchestrator integration notes

- **Adapter seam**: branch in `_synthesize_speech` on `params_json.provider`;
  vendor keys can anchor as system keys or endpoints unchanged (the
  credential resolver already returns `(model, base_url, api_key)`).
- **Voice resolution priority** (Phase 3): user `default_tts_voice` →
  catalog `params_json.voice` → per-language default map.
- **Admin flow**: `POST/PATCH /api/admin/providers/models` already persists
  arbitrary `params_json` — `instructions`, `provider`, and per-language
  voice maps need no schema change.
- **Tests**: follow `tests/test_tts.py` (mock `AsyncOpenAI` via patch,
  autouse cache-clear fixture, direct handler calls); adapters mock
  `httpx.AsyncClient` like the reranker tests.
- **Ingress**: no body-size/timeout annotations exist by default;
  `ingress.annotations` in values.yaml is the injection point if streaming
  responses ever need tuning.

## Open questions

1. **First adapter** (Phase 3): if `gpt-4o-mini-tts` satisfies, do we build
   the ElevenLabs/Hume/MiniMax adapter before Phase 5 needs cloning? (Ear
   bake-off decides.)
2. **Voice picker placement**: user settings only, or also a quick-switch
   on the player itself?
3. **STT ceiling**: do we ever need >25 MB / >25 min dictation, or is a
   hard client cap with a clear message the right answer?
4. **Mini-player scope**: is the global scroll-away mini-bar (Matter
   pattern) v1-adjacent or firmly later?

*Resolved 2026-07-04*: player shape → unified virtual timeline on a single
primed element with section chips (research: MSE not cross-browser for MP3;
Web Audio loses pitch-corrected speed; sentence-boundary seams
imperceptible).
