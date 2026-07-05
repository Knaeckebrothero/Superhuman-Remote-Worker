# TTS Vendor Providers: OpenAI Drop-in + ElevenLabs Adapter

**Status**: PROPOSED
**Date**: 2026-07-05
**Depends on**: `voice_experience_roadmap.md` (P0–P4 shipped; this doc concretizes P5)

## Motivation

Kokoro (the self-hosted default) has good voices but a hard ceiling: its
voices are monolingual (a French voice speaks French, not French-accented
English), there is no German voice at all, and there is no way to get
*character* — an accented, expressive voice that makes read-aloud fun rather
than corporate. Two vendors close the gap:

1. **OpenAI** (`gpt-4o-mini-tts`, `tts-1`) — multilingual voices that handle
   German today, instruction-steerable style, and the provider self-hosters
   most likely already have a key for. Rides our existing OpenAI-compatible
   seam with **zero adapter code**.
2. **ElevenLabs** — the quality/expressiveness ceiling (Eleven v3, 70+
   languages) and the only vendor with a real answer to "a voice with a
   French accent speaking English/German": a 10k+ community Voice Library
   filterable by language × accent × gender × age, plus Voice Design
   (generate a voice from a text description). Needs a native adapter — its
   API is not OpenAI-compatible.

Kokoro stays exactly as it is (free, local, no external dependency).

## Current State (the seams this builds on)

- **Synthesis choke point**: `_synthesize_speech()` in
  `orchestrator/services/tts.py` — the single place audio is produced.
  Contract: `(text, model, voice, base_url, api_key, instructions) → mp3
  bytes | None`. Everything upstream — audio cache, usage metering, aux
  rewrite, size-based chunking, SSE plan/stream, `/tts/preview` — is
  provider-agnostic and consumes only this contract.
- **Model/credential resolution**: TTS models are rows in the `models`
  registry (`capabilities ∋ 'tts'`, Admin → Models).
  `resolve_capability_credentials(capability="tts")` resolves
  `user_settings.default_tts_model` → system default → `(model, base_url,
  api_key)`. The user-level preference **already exists server-side**; what's
  missing is a Settings picker for it (see Phase 1).
- **Per-model TTS params**: the row's `params_json` carries `voice` /
  per-language `voices` / `instructions` (read by `_resolve_tts_params`,
  consumed by `_pick_voice`).
- **Cockpit backend abstraction**: `cockpit/src/app/core/models/tts-voices.ts`
  — `ttsBackendForModelId()` (single detector) + `voicesForModelId()` +
  `voiceLanguageTag()`. Built precisely so a vendor is one new `case`, not a
  rewrite. Settings renders the voice picker + language tags off these.
- **Preview**: `POST /api/settings/tts/preview` synthesizes a canned phrase
  for the selected voice (no aux rewrite, cached, metered).
- **Deployment-level vendor key precedent**: `TAVILY_API_KEY` — a
  `secrets.values` entry in the Helm values (Vault/ESO in prod,
  `values-local.yaml` in dev) → shared `srw` Secret → orchestrator env via
  `secretKeyRef` with `optional: true`
  (`helm/templates/orchestrator/deployment.yaml`).

## Design

### One concept, not two: provider = registry model row

We do **not** introduce a separate "voice provider" entity. A provider is an
enabled TTS-capable row in the `models` registry (`kokoro`,
`gpt-4o-mini-tts`, `eleven_multilingual_v2`, …). "The user selects which
voice provider to use" therefore means: a **user-facing picker for
`default_tts_model`** in Settings — the preference and its entire resolution
chain already work; only the UI is missing. The Read-aloud section then
renders a per-backend config UI switched on `ttsBackendForModelId()`:

| Backend | Settings UI |
| --- | --- |
| `kokoro` | As today: static voice list + prefix language tags + note. Unchanged. |
| `openai` | Reuses the same simple UI (static list, `[multi]` tags). Confirmed: OpenAI offers ~11 fixed voices with no per-voice customization — the Kokoro-style picker is exactly right. (Its one extra knob, free-text `instructions` on `gpt-4o-mini-tts`, already rides `params_json` as admin config; a user-level style field is a possible later nicety, not in scope.) |
| `elevenlabs` | New, richer UI: async voice list from the account (names + accent/gender labels + hosted preview), Voice Library search/add, Voice Design. Phases 5–7. |

### ElevenLabs account model: one per deployment

As proposed: a single ElevenLabs account per deployment, keyed by
**`ELEVENLABS_API_KEY`** following the Tavily pattern exactly —
`secrets.values.ELEVENLABS_API_KEY` in Helm values (Vault/ESO in prod, local
overlay in dev) → shared `srw` Secret → **orchestrator env only**
(`optional: true`). TTS is orchestrator-only, so unlike Tavily the key is
*not* fanned out to agent pods, and per the internal-creds guardrail it must
never reach workspace pods or the browser. Every ElevenLabs API call is
proxied through the orchestrator; the `xi-api-key` header is attached
server-side.

Key resolution in the adapter: the credential triple's `api_key` (if the
registry row is endpoint-anchored with a key) → else
`os.environ["ELEVENLABS_API_KEY"]`. The env var is the expected path; the
endpoint-row option just falls out of the existing machinery for free.

Consequence to be explicit about: **all users of a deployment share one
voice list and one character quota**. Voices added from the Library and
created via Voice Design consume account voice slots (plan-limited, e.g.
~30 on Creator tier). See Open Questions.

### Backend detection (server side)

Mirror the cockpit seam: `params_json.provider` on the registry row
(explicit, e.g. `"elevenlabs"`) with a fallback sniff on the model id
(`"eleven" in model_id`). `_synthesize_speech` branches on this; the
OpenAI-compatible path remains the default branch, so Kokoro/OpenAI/Groq
behavior is untouched.

### ElevenLabs API surface (verified 2026-07-05)

All calls: header `xi-api-key`, base `https://api.elevenlabs.io`.

| Purpose | Endpoint |
| --- | --- |
| Synthesize | `POST /v1/text-to-speech/{voice_id}?output_format=mp3_44100_128` — body `{text, model_id, language_code?, voice_settings?}`. Default model `eleven_multilingual_v2`; `eleven_flash_v2_5` (cheap/fast), `eleven_v3` (most expressive) also selectable — the registry row's `model_id` is passed through, so each is just another registry row. |
| Account voices | `GET /v2/voices` — returns `voice_id`, `name`, `labels` (accent, gender, age, description), `preview_url` |
| Library search | `GET /v1/shared-voices?search=&language=&accent=&gender=&age=&page_size=` (≤100/page) — returns `voice_id`, `public_owner_id`, `name`, `accent`, `preview_url`, … |
| Add library voice | `POST /v1/voices/add/{public_user_id}/{voice_id}` — body `{new_name}` → `{voice_id}` (account-scoped id) |
| Voice Design (generate) | `POST /v1/text-to-voice/design` — body `{voice_description, text?, model_id?}` → previews `[{generated_voice_id, audio_base_64, …}]` |
| Voice Design (save) | `POST /v1/text-to-voice` — body `{voice_name, voice_description, generated_voice_id}` |

### Adapter mechanics

New `_synthesize_speech_elevenlabs()` (or an inline branch) in `tts.py`:
`httpx.AsyncClient` POST to `/v1/text-to-speech/{voice}` with
`model_id=<registry model_id>`, `output_format=mp3_44100_128`, same 120 s
timeout / no-retry posture as the OpenAI path, same `→ mp3 bytes | None`
contract. `instructions` is OpenAI-only and is not sent. Chunk streaming,
caching (`_hash_key(model, voice, "", text)`), and metering
(`tts-character` UsageEvents — ElevenLabs bills per character, same unit)
work unchanged because they sit above the seam.

Voice resolution: `_pick_voice` already returns the user's
`default_tts_voice` first — for ElevenLabs that value is an account
`voice_id` (opaque hash; the picker displays names, stores ids). The
built-in `alloy`/`nova` fallbacks are meaningless here, so the **registry
row must set `params_json.voice`** (or a per-language `voices` map) to a
default account voice_id; the adapter fails loudly (clear log + `None`) if
the resolved voice is empty.

### New orchestrator endpoints (all `require_approved_user`, all proxy server-side)

| Route | Phase | Notes |
| --- | --- | --- |
| `GET /api/settings/tts/voices` | 5 | Account voices for the resolved TTS model. `{backend, voices: [{id, name, labels, preview_url}]}`. ElevenLabs → live `GET /v2/voices`, cached in-process ~5 min; other backends → static/empty (cockpit keeps its local lists). |
| `POST /api/settings/tts/preview` | 2 | **Extended**, not new: optional `text` field, ≤500 chars, any provider. Flows through the existing cache/metering naturally. |
| `GET /api/settings/tts/library` | 6 | Proxy of `/v1/shared-voices` with `search/language/accent/gender/age/page` passthrough. |
| `POST /api/settings/tts/library/add` | 6 | `{public_owner_id, voice_id, new_name}` → proxy add → invalidate voices cache. |
| `POST /api/settings/tts/design` | 7 | `{voice_description, text?}` → previews (base64 audio straight to client for playback). |
| `POST /api/settings/tts/design/save` | 7 | `{generated_voice_id, name, description}` → saves to account → invalidate voices cache. |

### Cockpit

- `tts-voices.ts`: add `'elevenlabs'` to `TtsBackend` + detection
  (`id.includes('eleven')`). `voicesForModelId` returns `[]` for it (options
  are async, server-fed); option labels are composed from server `labels`
  (`Rachel [EN · calm]`-style) rather than `voiceLanguageTag`, which returns
  `null` for this backend by design — the tags come from ElevenLabs' own
  metadata, which is strictly better than prefix-decoding.
- Settings: TTS model picker (mirror the aux-model picker pattern,
  filtered to `capabilities ∋ tts`); `@switch (ttsBackend())` block for the
  per-backend section; ElevenLabs voice `<select>` populated from
  `/api/settings/tts/voices` with a hotlinked `preview_url` play button
  (public CDN mp3s — no characters spent) alongside our synth preview.
- Custom preview text: a 500-char `<textarea>` above the existing Preview
  button, provider-independent; empty → canned phrase as today.
- Library browser (Phase 6): search box + accent/language/gender filters →
  result cards (name, accent, preview play, "Add to deployment") — a thin
  skin over the proxy.
- Voice Design (Phase 7): description textarea → "Generate previews" → 3
  playable candidates → "Save voice".

## Phases

Each phase ships and verifies independently (Tilt inner loop; pytest +
vitest + live smoke on k3d).

**Phase 1 — user TTS model picker (the "provider selector")**
Settings picker writing `default_tts_model`, listing enabled TTS-capable
registry models, "(default)" annotation like the aux picker. Voice section
reacts live (backend switch swaps the voice list).
*Accept*: user can flip Kokoro ↔ OpenAI and voice list + tags follow; clearing
falls back to system default.

**Phase 2 — custom preview text**
`text` field on `/tts/preview` (≤500 chars, server-enforced) + textarea in
Settings.
*Accept*: custom text is spoken by the selected voice on any provider;
overlength → 422; empty → canned phrase; usage metered.

**Phase 3 — OpenAI enablement (ops + verify, ~zero code)**
Register `gpt-4o-mini-tts` (Admin → Models, `capabilities: [tts]`,
OpenAI key). Expected to ride the existing seam untouched.
*Accept*: German read-aloud works end-to-end; picker shows `[multi]` tags;
preview works; `instructions` param honored from `params_json`.

**Phase 4 — ElevenLabs synthesis adapter** — ✅ CODE DONE (2026-07-05)
Helm: `ELEVENLABS_API_KEY` (values-local example + orchestrator env,
`optional: true`; ESO needs **no** change — `dataFrom: extract` pulls the whole
Vault bundle, same as Tavily). Backend: `_synthesize_elevenlabs` (httpx POST
`/v1/text-to-speech/{voice_id}`, `mp3_44100_128`, 120 s / no-retry) +
`_resolve_tts_provider` (explicit `params_json.provider` → model-id sniff
`eleven_*` → default `openai`) + a fork at the top of `_synthesize_speech`
(before the api-key guard, since ElevenLabs supplies its key from env);
`provider` threaded from both call sites. 10 unit tests mock the HTTP layer.
*Accept*: read-aloud + preview + SSE chunk-streaming produce ElevenLabs audio;
missing key → clean "not configured" (204 on preview), never a crash; usage
metered per character.

Two implementation facts learned during the build (both feed Phase 5):

- **Key wiring — either path works, adapter is agnostic.** The adapter uses the
  resolved credential's `api_key` if present, else `os.environ["ELEVENLABS_API_
  KEY"]`. So a deployment can either (a) set the raw env (Tavily style — what the
  Helm plumbing above does), or (b) seed an `elevenlabs` row in `systemApiKeys`
  (idiomatic: it flows through `resolve_capability_credentials` **and** lets a
  `systemModels` entry auto-seed the model row, since `_seed_system_models` skips
  providers with no key). Recommend (a) for simplicity; (b) if you want the row
  to self-seed.
- **The registry row's default voice can't be seeded.** `_seed_system_models`
  calls `create_model()` without `params_json`, so a seeded `eleven_*` row has
  no default `voice_id`. `_pick_voice` would then fall through to the OpenAI
  `alloy`/`nova` default, which ElevenLabs 404s on. Resolution: the voice comes
  from the **user's `default_tts_voice`** (the Phase 5 account picker sets a real
  `voice_id`) or an admin `params_json.voice` edit in Admin → Models. Until
  Phase 5 ships, set a default via admin edit (a known-public voice id such as
  Rachel `21m00Tcm4TlvDq8ikWAM` works on every account). The adapter fails loud
  (logged `None` → 502) if no usable voice resolves — never a silent 404.

Live end-to-end synthesis needs a real key + a registered row + a voice_id
(user/admin actions, like Phase 3); everything above is unit- and
import-verified.

**Phase 5 — account voice picker**
`GET /api/settings/tts/voices` proxy (+5 min cache) + async cockpit picker
with server-fed labels + hosted preview playback.
*Accept*: ElevenLabs backend shows real account voices by name with
accent labels; selection persists as `voice_id`; synth preview + hosted
preview both play.

**Phase 6 — Voice Library browser**
Search/filter proxy + add-to-account + cockpit browser UI.
*Accept*: searching "french english" surfaces French-accented English voices;
preview plays from `preview_url`; Add makes the voice appear in the Phase 5
picker (cache invalidated); slot-limit errors from ElevenLabs surface as a
readable message, not a 500.

**Phase 7 — Voice Design (stretch)**
Design/save proxies + generate-preview-save UI.
*Accept*: a text description yields playable candidates; saving one makes it
selectable; failures surface readable errors.

## Open Questions

1. **Shared voice slots**: library adds + designed voices consume the
   deployment account's plan-limited slots. **DECIDED (2026-07-05)**: the
   Phase 6/7 mutating surfaces (Library add, Voice Design save) ship behind
   an admin enable flag in `system_settings`
   (`tts.elevenlabs_library_enabled`, default off). Browsing/searching the
   library and previewing are read-only and can stay ungated; only the
   account-mutating actions require the flag. Read-only picker + synth
   (Phases 1–5) are always available.
2. **Shared character quota / cost**: all users burn one ElevenLabs balance;
   long read-alouds are expensive there. Existing per-user `tts-character`
   UsageEvents give visibility; per-user caps would ride the rate-limiting-v2
   design rather than anything bespoke here.
3. **Default ElevenLabs model tier**: `eleven_multilingual_v2` (quality
   default) vs `eleven_flash_v2_5` (~½ cost, low latency) vs `eleven_v3`
   (max expressiveness). Registry rows make this admin-selectable; seed
   `multilingual_v2` first, evaluate v3 by ear.
4. **`language_code` passthrough**: we know the message language at synth
   time; passing it may improve normalization on flash models. Cheap to add
   in the adapter; verify against v2/v3 model support at implementation time.
5. **Hosted `preview_url` hotlinking**: fine for the browser (public CDN);
   proxy only if CSP or privacy concerns appear in practice.
