/**
 * Curated TTS voice catalogs for the Admin → Providers "Add model" voice
 * dropdown.
 *
 * There is no standard "list voices" endpoint in the OpenAI-compatible spec,
 * and our model-orchestrator gateway doesn't proxy Kokoro's
 * `/v1/audio/voices` — so, exactly like the hardcoded Whisper language list,
 * these are snapshots of what each backend actually offers, keyed by a backend
 * detected from the model id. Unrecognized backends fall back to free-text.
 *
 * Kokoro list snapshotted 2026-06-06 from the live kokoro-fastapi
 * `/v1/audio/voices` (67 voices). OpenAI list is the documented set for
 * `tts-1` / `gpt-4o-mini-tts` (OpenAI exposes no list endpoint).
 */

/** Sentinel select value meaning "let me type a voice id by hand". */
export const TTS_VOICE_CUSTOM = '__custom__';

/**
 * Kokoro-82M voices (kokoro-fastapi). Prefix = language + gender:
 * `a*`=American EN, `b*`=British EN, `e*`=Spanish, `f*`=French, `h*`=Hindi,
 * `i*`=Italian, `j*`=Japanese, `p*`=Portuguese, `z*`=Chinese; `*f`=female,
 * `*m`=male; `*_v0*` are legacy variants. (No German voice exists.)
 */
export const KOKORO_VOICES: readonly string[] = [
  'af_alloy', 'af_aoede', 'af_bella', 'af_heart', 'af_jadzia', 'af_jessica',
  'af_kore', 'af_nicole', 'af_nova', 'af_river', 'af_sarah', 'af_sky',
  'af_v0', 'af_v0bella', 'af_v0irulan', 'af_v0nicole', 'af_v0sarah', 'af_v0sky',
  'am_adam', 'am_echo', 'am_eric', 'am_fenrir', 'am_liam', 'am_michael',
  'am_onyx', 'am_puck', 'am_santa', 'am_v0adam', 'am_v0gurney', 'am_v0michael',
  'bf_alice', 'bf_emma', 'bf_lily', 'bf_v0emma', 'bf_v0isabella',
  'bm_daniel', 'bm_fable', 'bm_george', 'bm_lewis', 'bm_v0george', 'bm_v0lewis',
  'ef_dora', 'em_alex', 'em_santa', 'ff_siwis',
  'hf_alpha', 'hf_beta', 'hm_omega', 'hm_psi', 'if_sara', 'im_nicola',
  'jf_alpha', 'jf_gongitsune', 'jf_nezumi', 'jf_tebukuro', 'jm_kumo',
  'pf_dora', 'pm_alex', 'pm_santa',
  'zf_xiaobei', 'zf_xiaoni', 'zf_xiaoxiao', 'zf_xiaoyi',
  'zm_yunjian', 'zm_yunxi', 'zm_yunxia', 'zm_yunyang',
];

/**
 * OpenAI TTS voices (`tts-1`, `tts-1-hd`, `gpt-4o-mini-tts`). Documented
 * constants — OpenAI has no voices endpoint.
 */
export const OPENAI_TTS_VOICES: readonly string[] = [
  'alloy', 'ash', 'ballad', 'coral', 'echo', 'fable',
  'nova', 'onyx', 'sage', 'shimmer', 'verse',
];

/**
 * Voices offered by the backend behind `modelId`, detected from the id.
 * Returns `[]` for unrecognized backends — the caller then shows a free-text
 * field instead of a dropdown.
 */
export function voicesForModelId(
  modelId: string | null | undefined,
): readonly string[] {
  const id = (modelId ?? '').toLowerCase();
  if (!id) return [];
  if (id.includes('kokoro')) return KOKORO_VOICES;
  if (id.includes('tts-1') || id.includes('gpt-4o-mini-tts')) {
    return OPENAI_TTS_VOICES;
  }
  return [];
}
