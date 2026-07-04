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
agent has the full plan. Nothing implemented yet.

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
- [ ] **Kill the silent 204**: cockpit learns TTS/STT availability up front
      (flag on thread load or `/api/users/me` settings payload) → button
      renders disabled with a "TTS not configured" tooltip instead of a dead
      click. Same for the mic button.
- [ ] **Fix the activation trap** (also the autoplay-block surfacing): prime
      ONE persistent `Audio` element synchronously in the click handler
      (silent play + catch; `touchend` on iOS) and reuse it for every chunk;
      always `.catch()` `play()` and flip to a visible "tap to play" state
      instead of swallowing. This is a prerequisite for Phase 2 but the
      catch-and-show half can ship immediately.
- [ ] **Close the metering gap**: add a usage/audit hook to
      `generate_message_tts` / `plan_tts_chunks` / `transcribe_thread_audio`
      (the direct-SDK path bypasses `usage_events` entirely — synthesis AND
      the aux formulation calls are invisible today). Follows the
      agent-side `audio_helper.py` archiving precedent; feeds rate-limiting
      v2's ledger.
- [ ] Structured failure logging for TTS/STT (which stage failed, which
      model/endpoint) so the next "it didn't work" report is diagnosable.

### Phase 1 — The box (transparency)

One status box appears in the message the moment **Read** is clicked and
lives through the whole lifecycle; the player materializes *inside the same
box* when the first chunk is ready.

- [ ] New standalone component (see §Cockpit integration): instant-ack line
      → **Rewriting text for speech → Preparing audio (M parts) → Generating
      audio (part N of M) → Player**, with expectation-setting copy on the
      slow stage. The near-instant chunk-split step gets **no stage of its
      own** (a 200 ms label reads as churn) — it's the moment M becomes
      known and the indicator turns count-determinate. The client already
      drives every stage, so this is almost pure frontend.
- [ ] Elapsed-seconds counter appears after ~10 s inside any one stage;
      abnormally long → an explained-wait line ("the language model is
      responding slowly…"), never "almost done". One coarse expectation
      line ("usually ~30 s–2 min"), no countdowns.
- [ ] ✕ **cancel at every stage**; cancelling keeps already-synthesized
      parts playable ("Read-aloud cancelled — 2 parts kept").
- [ ] Plan returns `{chunks, language, rewritten}` (annotated `/tts/plan`) —
      language feeds Phase 3 detection; **make chunk 1 short (~500 chars)**
      in the chunking prompt + deterministic splitter to minimize
      time-to-first-audio.
- [ ] Per-stage error states with unit-level retry: earlier parts stay
      playable, "Retry part 3" regenerates one chunk (caches make earlier
      stages free). Bounded visible auto-retry for transient chunk failures
      ("retrying — attempt 2 of 3"), then hard-fail with state preserved.
- [ ] Surface formulation-skipped: with no auxiliary model configured (or an
      aux failure), the plan silently falls back to a deterministic split of
      the **raw markdown**. The box must say "rewriting skipped" instead of
      pretending it cleaned up.
- [ ] Keep the "Spoken version" disclosure, moved inside the box (collapsed;
      don't stream the rewritten text past the user).
- [ ] Generation is non-blocking: the box keeps progressing if the user
      scrolls away or keeps chatting; once playing, the status demotes to a
      quiet single line ("Playing · generating part 3 of 5…"), and on
      completion becomes "Ready · 5 parts · 3:12".

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

Implementation checklist:

- [ ] `ReadAloudPlaybackService` (root-provided, signal-based) owning: the
      primed element(s), chunk blob URLs + durations (read via
      `preload="metadata"` → `loadedmetadata` per arriving blob), prefix-sum
      virtual timeline, seek routing (`offsets[i] + el.currentTime`), and
      the one-playback-at-a-time rule.
- [ ] Player UI inside the Phase-1 box: play/pause, unified seek bar over
      the **known** region + visually-distinct estimated tail, section
      chips (structural prev/next jump), speed control (0.75–2×, persisted
      via `ChatPreferencesService`), elapsed / "~total" time.
- [ ] Estimated total: `remainingChars × sec/char` seeded at ~1/15 s/char,
      refined as an EMA from arrived chunks; display never decreases;
      `~` prefix until the last chunk lands. Seeks into the unsynthesized
      region become pending-seek targets resolved on chunk arrival.
- [ ] `playbackRate` gotchas: set `defaultPlaybackRate` too and re-apply
      after every src swap; `preservesPitch` with `webkitPreservesPitch`
      fallback; drive the scrubber from `requestAnimationFrame`, not
      `timeupdate` (~4 Hz).
- [ ] Media Session API: metadata + play/pause/seek handlers +
      `setPositionState` (feature-detect; Firefox lacks it; clamp
      `position ≤ duration`). Lock-screen/hardware-key control on mobile.
- [ ] Chunk-level **highlight-as-spoken** (stretch, cheap): highlight the
      message segment corresponding to the playing chunk; needs chunk→char
      range metadata from the plan.
- [ ] Waveform/EQ flair (optional, feature-flagged OFF on iOS — WebKit
      `createMediaElementSource` has a breakage history): one
      `createMediaElementSource` per element cached in a WeakMap, analyser
      connected through to destination, reuse `VoiceAudioProcessor` +
      `TimeBasedBarVisualizer` (both source-agnostic) with the
      `CanvasRenderer` gradient tokenized to theme colors first.
- [ ] Keep every blob URL until the player is destroyed (seek-back), then
      revoke.
- [ ] (Stretch, later) Global mini-player when the source message scrolls
      out of view — mount pattern exists (root-mounted toast-container +
      root-provided signal service).

**Acceptance**: no native `<audio>` visible anywhere in chat; scrubbing back
into an already-played section works instantly; speed persists across
sessions; pausing via lock-screen controls works on mobile; audio starts on
Safari even when synthesis took 40 s.

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

- [ ] **Step 1 (config-only)**: add `gpt-4o-mini-tts` in Admin → Providers,
      set as TTS default. Validates the whole UX with zero code.
- [ ] **`instructions` passthrough**: one param in `_synthesize_speech` +
      `params_json.instructions` on the catalog row — unlocks style prompts
      on the OpenAI lane and is the persona hook for Phase 5.
- [ ] **Provider adapter seam in `tts.py`**: dispatch on
      `params_json.provider` (the `provider_kind` column is a transport
      enum — system|endpoint — not a vendor field; `params_json` is the
      idiomatic bag). Adapters follow the `RerankerScorer` httpx pattern
      (`src/services/memory/plugins/reranker.py`: injectable client,
      bearer auth, vendor JSON) — ~50 lines each. First adapter chosen by
      ear: ElevenLabs vs Hume vs MiniMax bake-off on real messages.
- [ ] **Content-language detection**: the plan LLM returns a language tag
      with the rewritten text; voice resolution uses it instead of the UI
      language. Per-language voice mapping becomes config, not hardcoded
      `alloy`/`nova`.
- [ ] **User-facing voice picker (thin v1)**: `default_tts_voice` in user
      settings (`UserSettingsUpdate` + `_resolve_voice` priority above
      `params_json.voice`); dropdown fed by the configured backend's voice
      list. Personas & per-thread override are Phase 5.
- [ ] **(Optional) streaming synthesis**: installed SDK (openai 2.31.0)
      already supports `audio.speech.with_streaming_response`; with a fast
      external provider, per-chunk latency drops to ~1 s and the "first
      chunk" wait nearly vanishes. Also viable: synthesize 2–3 chunks
      concurrently on fast backends (the sequential loop exists for the CPU
      backend's sake). Backend SSE status streaming is possible if ever
      needed (hand-rolled `StreamingResponse` + `: open` kickstart
      precedent at `main.py:8058`), but client-side stage knowledge makes
      it unnecessary for v1.

**Acceptance**: a German message is read in a German-capable voice without
touching settings; the user can pick a distinctly non-Dave voice in
settings and every subsequent Read uses it; at least one
instruction-capable provider is live in dev; time-to-first-audio on the
external lane < 5 s for a typical message.

### Phase 4 — Voice-in hardening (STT)

- [ ] **Long-recording test**: 10+ min dictation end-to-end. Size is fine
      (opus ≈ 0.2–0.5 MB/min vs the 25 MB cap) but backend latency/timeout
      behavior is unverified.
- [ ] Recording UX: live duration display + soft cap warning near the limit
      (client currently has **no** duration cap or display).
- [ ] If long clips are slow: chunked transcription server-side (split on
      silence, transcribe sequentially, concatenate) behind the same
      endpoint.
- [ ] Verify the 204-vs-error split matches Phase 0's honesty rules
      (broken ≠ off).

**Acceptance**: a 12-minute rambling voice memo comes back as text without
the UI looking frozen, or fails with an honest message.

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
