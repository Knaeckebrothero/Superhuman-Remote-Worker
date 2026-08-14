---
tags:
  - feature
  - design
  - voice
  - realtime
  - officer
  - steering
  - orchestrator
aliases:
  - live talk mode
  - realtime voice mode
  - supervisor control plane
  - talk to the agent while it works
related:
  - "[[voice_experience_roadmap]]"
  - "[[worker_runtime_strategy]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[centurion]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
  - "[[agent_lifecycle]]"
---

# Supervisor Control Plane & Live Talk Mode

**Status:** design capture, nothing built. Filed 2026-08-02 from a design
session. No code changes accompany this doc.

**Citation convention:** this doc cites `file::symbol`, not `file:line`.
Line numbers in `src/graph.py` drifted by ~28 lines *between two greps in
the same session* while writing this — see [Doc hygiene](#doc-hygiene).

---

## 1. What this is

Two things that turned out to be one thing.

The starting question was a product one: today's voice support is
turn-based (record → transcribe → agent runs → TTS the result). Could we
add a **live talk mode** where you hold a conversation with a realtime
voice model *while* a job is building something — asking what it's doing,
feeding in ideas, changing direction mid-build — instead of waiting for it
to finish?

Partway through it became clear this is not a voice feature. The steering
substrate it needs is the same substrate the **officer** (Centurion) needs
to supervise workers, and most of it already shipped. So the real design
is a **supervisor control plane with several clients**, of which voice is
the newest and smallest.

---

## 2. Verified external facts (OpenAI Realtime, as of 2026-08-02)

Recorded so this doesn't get re-researched. Model naming is moving fast —
**pin an exact snapshot ID and re-verify limits at build time.**

### Modalities — the enabling fact

The Realtime API is **not** audio-in/audio-out. It is **audio in → audio
*and* text out**, with text available for the input side too:

- **Model's own speech → text**: `response.output_audio_transcript.delta`
  and `.done` stream a text transcript of what the model is saying, in
  parallel with the audio. This arrives whether or not `"text"` is in
  `output_modalities`; `output_modalities: ["audio"]` still yields the
  transcript.
- **User's speech → text**: enable `input_audio_transcription` in the
  session config to get `conversation.item.input_audio_transcription.*`
  events. Now backed by the dedicated `gpt-realtime-whisper` streaming STT
  model.
- `output_modalities` may include both `"audio"` and `"text"`. Note these
  are different things: the audio transcript is *text of the speech*,
  while adding `"text"` produces a separate text response.

This is what makes a two-model design cheap: the text lane is free and
already time-aligned with the audio. No separate STT/TTS round trip is
needed to know what was said in either direction.

### Models

- **`gpt-realtime`** (GA, default snapshot `gpt-realtime-2025-08-28`):
  32K context, 4,096 max output tokens. In: text/audio/image. Out:
  text/audio. Function calling supported.
- **`gpt-realtime-2`** (announced May 2026): first voice model with
  GPT-5-class reasoning; five configurable reasoning levels.
  **Context window is disputed** — OpenAI's announcement says 128K
  (4× the prior 32K), third-party trackers still report 32K. *Verify
  before designing around it.*
- **`gpt-realtime-translate`**: 70+ input languages → 13 output languages.
- **`gpt-realtime-whisper`**: streaming speech-to-text.
- A **mini** tier exists and is materially cheaper.

### Pricing and token density

- Audio: **$32 / 1M input**, **$64 / 1M output**.
- **Cached audio input: $0.40 / 1M** (~99% off) — system prompt and tool
  definitions re-send every turn, so cache hits are consistent. This is
  the single biggest cost lever.
- Text: $4 / 1M input, $24 / 1M output. Image input $5 / 1M.
- Density: ~**600 tokens per minute of input audio**, ~**1,200 tokens per
  minute of generated speech** (output tokenizes ~2× denser).
- All-in: ~**$0.05/min flagship**, ~**$0.016/min mini** — roughly $3/hr
  and $1/hr.
- **An open microphone bills during silence.** VAD-gate or push-to-talk;
  dead air is a real cost leak.

### Two hard limits that shape scope

1. **~4,096 max output tokens ≈ 3.5 minutes of continuous speech** per
   response. Fine for conversation, useless for long-form narration.
2. **Context fills fast.** At ~1,800 tokens/min combined, an hour of
   continuous talk is ~100K tokens on the voice lane alone. On a 32K
   window that's ~18 minutes. The voice lane needs its own context
   management regardless of what the agent is doing.

### Connection topology

[WebRTC with ephemeral client secrets](https://developers.openai.com/api/docs/guides/realtime-webrtc)
is the recommended browser path:

1. Orchestrator calls `POST /v1/realtime/client_secrets` with the real
   key, sets `OpenAI-Safety-Identifier` server-side.
2. Browser receives a short-lived `ek_`-prefixed token.
3. Browser negotiates SDP directly with OpenAI; tool calls return over the
   same data channel.

This keeps the real key server-side (consistent with the ElevenLabs
decision in [[voice_experience_roadmap]] — vendor keys never leave the
orchestrator) **and** keeps realtime audio out of the orchestrator, which
matters because that pod already bounces on Reloader cascades.

**Sources:** [gpt-realtime model page](https://developers.openai.com/api/docs/models/gpt-realtime) ·
[Advancing voice intelligence (May 2026)](https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/) ·
[Realtime with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc) ·
[Realtime pricing per-minute math](https://www.layer3labs.io/guides/openai-realtime-api-pricing)

---

## 3. Architecture: give the voice model tools, do not make it a relay

The intuitive design is "voice model talks, pipes transcripts to the real
chat model, pipes answers back." **Reject this.** A relay is the version
that gets expensive and goes wrong:

- Streaming agent output into the voice context blows a 32K (or even
  128K) window in minutes, at audio-token prices.
- The model's audio transcript is *what it said*, not what you meant. You
  do not want `"Sure, I'll let it know!"` forwarded to the worker.

Instead, the voice model is a **conversational front-end whose tools are
the supervisor API**:

| Tool | Backed by |
|---|---|
| `get_current_activity()` | job progress / todos / current phase |
| `read_recent_output(n)` | job files, recent messages |
| `steer(message, urgent)` | the existing guidance lane (§4) |

The model *pulls* state when asked and *calls* steer when directed.
Nothing streams in continuously. Notable agent events (phase boundary,
decision point, contract bounce) get pushed into the session as
`conversation.item.create` so it can proactively volunteer
*"it just finished the auth module."*

Barge-in comes free: the Realtime API handles interruption natively via
server VAD + `response.cancel`. The existing read-aloud player does not.

**Scope note:** this *adds a lane, it does not replace the TTS stack.*
Because of the ~3.5-minute output ceiling, long-form narration stays on
the existing streaming rewrite → chunk → Kokoro/OpenAI/ElevenLabs
pipeline. Realtime owns conversation; the existing pipeline owns reading
things aloud.

---

## 4. The reframe: one control plane, several clients

The steering work is not voice-specific and is not officer-specific.
There is one interface with (at least) three consumers:

| Client | Principal | Status |
|---|---|---|
| **Officer** (Centurion) | synthetic supervisor | **shipped**, live-acceptance open |
| **Cockpit** (human, typed) | human supervisor | **shipped** |
| **Voice** (human, spoken) | human supervisor | not built |

The realtime model's tool schema *is* the officer's tool schema pointed at
the same endpoints, with an ephemeral WebRTC token instead of an MCP one.
That is the argument for building it: a second consumer proves the
interface is actually general, rather than accidentally shaped around its
only caller.

### Why this got more load-bearing on 2026-08-03

[[worker_runtime_strategy]] §7 decided to loosen the phased worker in
place, and its safety argument rests directly on this control plane:

> **Compensating controls make loosening safe**: each hard guardrail
> removed is replaced by soft supervision that didn't exist when the
> guardrails were designed — officer sitreps + steering, `request_replan`
> [...] **Structure traded for oversight.**

So as the strategic/tactical scaffolding erodes, *steering is what
replaces it*. The supervisor control plane stops being a supervision
convenience and becomes the thing holding up the loosening path. Live talk
mode is then not a side quest — it is a second, higher-bandwidth channel
into the control that the runtime strategy is now leaning on.

### What already exists (verified in-code 2026-08-02)

- **`src/tools/orchestrator/jobs.py::steer_worker_job`** — the officer's
  steer tool. `urgent=True` lands in the worker's next LLM turn (≤ ~1
  heartbeat, currently 60s); `urgent=False` delivers at the next phase
  boundary. Non-destructive either way — worker keeps context, todos, plan.
- **`src/core/guidance_injection.py`** — renders pending guidance as a
  synthetic `supervisor_guidance` tool-call result. Re-derived fresh on
  **every** `execute()` call (two `_inject_transient_messages` sites in
  `src/graph.py`), so it is immune to context compaction. At-least-once
  with an ack round-trip.
- **Two lanes**, documented at `src/graph.py::_deliver_queued_replies`
  ("Steering has two lanes"): Lane A `pending_guidance` (urgent, next
  turn), Lane B `queued_replies` (natural break / phase boundary).
- **Routes**: `POST /api/jobs/{job_id}/messages/{thread_id}/reply` — the
  officer uses the reserved literal `thread_id="officer"`, so the tool's
  URL `/api/jobs/{id}/messages/officer/reply` binds against the generic
  route. Ack via `POST /api/jobs/{job_id}/guidance/ack`.
- Workspace side-effect path `messages/officer/NNN_received.md`, covered
  by `tests/test_supervisor_guidance.py`.

This is P1-A (`dc33a86b`, 2026-07-30) from
[[officer_blind_reads_and_worker_bureaucracy]]. **The mid-turn steering
tool is not a thing to build. It is a thing to prove.**

---

## 5. Gaps and open decisions

### G1 — `persistent_graph.py` has no steering drain (verified, but *not* a blocker)

Grep `src/persistent_graph.py` for guidance / steer / queued_replies:
**zero hits.** The entire P1-A lane lives in `src/graph.py` +
`src/api/dual_app.py`.

**This was initially filed as a blocker on the assumption that jobs would
migrate to the persistent loop. That assumption is dead** — see G3;
`JobDriver` is shelved and worker jobs stay on `graph.py`, which already
carries the lane. So:

- **Steering a worker job** → `graph.py`, lane already exists. Fine.
- **Talking to a session** → sessions are inherently interactive and
  already accept live input at
  `POST /api/persistent/threads/{thread_id}/input` (the "front door" used
  to nudge the officer on 07-30). No drain needed.
- **Steering the officer itself by voice** → the officer runs *on*
  `persistent_graph`, so this is the one path G1 actually blocks. Whether
  that matters depends on whether voice mode should be able to redirect an
  officer mid-turn, or only converse with it between turns.

Net: G1 narrows from "blocks the whole design" to "blocks one optional
path." Good news for scope.

### G2 — P1-A was never live-accepted

As of the last note (2026-07-31 evening): "steer a real worker mid-phase,
watch a gate bounce" was still open, and the **seal gate had still never
fired**. Building a voice client on an unexercised lane means the first
bug will be ambiguous between the two layers.

**Recommended gate: one confirmed mid-phase steer before any voice work
starts.** If night-3's field observation harvested evidence here, fold it
in and this resolves.

### G3 — ~~is the runtime decision being reversed?~~ **CLOSED 2026-08-03**

> **Resolved while this doc was being written.** A parallel design session
> (2026-08-02/03) took this exact question to ground. Decision record:
> **`docs/features/worker_runtime_strategy.md`** ([[worker_runtime_strategy]]),
> uncommitted at filing. The section below is kept because it is the
> reasoning the decision confirmed — but it is history now, not an open
> question.
>
> **Decided: no new worker runtime.** Evolve `graph.py` by subtraction
> (small-job completion floor → merge phase-gated toolsets → todos from
> gate to recitation) until the strategic/tactical switch is vestigial.
> *ReAct arrives by erosion; nothing new gets built.* The reasoning: a
> from-scratch ReAct worker re-grows the same scaffolding (the industry
> equilibrium is ReAct + externalized *recited* plan + verification —
> Manus's todo.md, Claude Code's TodoWrite, Devin's planner) **while also**
> migrating every integration hanging off the old path.
>
> - **`JobDriver` SHELVED** (reopen conditions in that doc §10) — this is
>   the option §7 of *this* doc originally sequenced as step 3. It is no
>   longer the plan.
> - **Foreign harnesses REJECTED** as the general worker (no remote-FS
>   seam; only viable topology is harness co-resident in the workspace
>   pod). One narrow homelab-only exception for subscription-funded bulk
>   coding.
> - **Gate: "slice 0" — the re-measurement owed since 07-31** (token +
>   wall-clock per phase on post-`99c9aba0` jobs). Everything else gates
>   on it. The 07-31 measurement gate **has not fired.**
>
> **Consequence for this doc:** voice mode is *less* blocked than
> originally written. Worker jobs stay where the steering lane already
> is. See revised G1 and §7.

The original framing is preserved below.

The 2026-07-31 decision (recorded in [[phase_model_overhead_amnesia_loop]])
was explicit:

- **Do not tear down the strategic/tactical split.** Make tactical phases
  much larger so a job is ~3 phases (plan → execute → review+submit).
  Mostly a prompt change.
- **A ReAct runtime does not fix the real problem.** The ~15k/turn
  injection floor and pinned-first memory assembly are *shared* code
  (`session_base.yaml` carries the identical 10k budget). Porting jobs to
  the persistent loop inherits the same floor minus the boundary that
  bounded it.
- Sequence: P-1 (drop `force_summarize`) → P-2 (conditional review) →
  P-3 (cap pinned tier) → P-4 (trim floor), **then re-measure, then**
  decide the runtime split.
- **Four ReAct runtimes already exist — do not build a fifth**:
  `src/graph.py` (the only real `StateGraph`),
  `src/persistent_graph.py::run_persistent_loop` (plain `while True:`,
  not a graph despite the name — runs sessions *and* the officer),
  `src/services/auxiliary.py::AuxiliaryLLM.agent()`, and
  `src/tools/delegation/light_runner.py::run_light_subagent`.
  Consolidation survivor = `persistent_graph`.

So "we need a ReAct agent anyway" is better stated as: **jobs should
eventually run on the runtime that already runs the officer.** The delta
is a `JobDriver` implementing `PersistentLoopCallbacks` plus a bootstrap
turn — not a new runtime.

*(Resolved — see the box at the top of G3. The deferral was reaffirmed and
extended into a full decision record on 08-03.)*

### G4 — where does the realtime session terminate?

WebRTC-direct (§2) is recommended, but it is a genuine fork:
browser↔OpenAI direct (lowest latency, key stays server-side, audio
bypasses the orchestrator) vs. proxied through the orchestrator (uniform
with existing TTS/STT, but a third long-lived connection through a pod
that already bounces on deploys). Not decided.

### G5 — shared-timeline drift (the part that will actually be hard)

Not the audio. The voice model and the agent hold **separate contexts that
drift.** You say *"use Postgres"*; the voice model confirms instantly, but
the worker will not see the steer until its next turn boundary. For the
next ~60s the voice model must know it already promised something the
agent has not acted on, or it will contradict itself on the follow-up.

Mitigations: `steer()` returns a real acknowledgment the model can hold;
`get_current_activity()` must distinguish **"delivered, not yet
consumed"** from **"in effect"** — which the existing
`pending_guidance` → `consumed_replies` transition already models.

---

## 6. Incidental findings from this session

- **P-1 has shipped.** `src/graph.py` no longer contains
  `force_summarize = is_strategic`; it now reads `force_summarize = False`.
  The unconditional strategic→tactical compaction — the root cause in
  [[phase_model_overhead_amnesia_loop]] — is gone.
- **`src/graph.py` was modified in the working tree during this session**
  (it was clean at session start, and line numbers shifted ~28 lines
  mid-session). Likely a parallel session. Worth reconciling before
  committing anything that touches it.

### Doc hygiene

[[phase_model_overhead_amnesia_loop]] records that line citations in the
companion doc "rotted 3× in one session." That reproduced here **within a
single session**: two `_inject_transient_messages` call sites moved
1241/1295 → 1265/1319, and the "Steering has two lanes" docstring moved
3225 → 3253, while this doc was being written. Two other cited line
numbers were wrong outright. **Cite `file::symbol`. Verify before
claiming doc work done.**

---

## 7. Suggested sequencing

Nothing here is committed to — this is the shape, not a plan of record.
**Revised after G3 closed**: the JobDriver step is gone, and voice mode is
substantially less blocked than the first draft assumed.

1. **Prove the lane (G2).** One confirmed mid-phase steer on a real
   worker. Blocks everything else; costs nothing to run. Independent of
   the runtime work.
2. **Formalize the supervisor API** as a named surface with several
   clients, rather than the officer's private tool set. This is the real
   deliverable, and [[worker_runtime_strategy]] §7 raised its priority by
   making steering the compensating control for the loosening path.
   [[unified_orchestrator_tool_surface]] is the shared implementation;
   [[officer_supervision_surface]] defines truthful reads and the background
   officer's no-object boundary; [[officer_message_routing]] defines the
   durable worker/officer/user thread and escalation lane. Voice consumes
   those APIs and does not invent a parallel control plane.
3. **Voice client**: ephemeral-token endpoint + WebRTC in cockpit + the
   three-tool schema. Near-zero new backend — jobs use the existing P1-A
   lane on `graph.py`, sessions use the existing `/input` front door.
4. **Optional, only if wanted**: steering drain on `persistent_graph`
   (G1), which unlocks redirecting *the officer* mid-turn by voice.

Note the dependency that is **not** here: none of this waits on slice 0 or
the loosening slices. The control plane and the runtime path are
independent tracks that happen to reinforce each other.

Deliberately out of scope: replacing the read-aloud pipeline (§3), and
custom voices (P5 in [[voice_experience_roadmap]]).
