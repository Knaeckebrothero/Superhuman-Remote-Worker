import {describe, expect, it} from 'vitest';
import {
  KOKORO_VOICES,
  OPENAI_TTS_VOICES,
  voicesForModelId,
  voiceLanguageTag,
  ttsBackendForModelId,
} from './tts-voices';

describe('voicesForModelId', () => {
  it('returns the Kokoro catalog for kokoro model ids (case-insensitive)', () => {
    expect(voicesForModelId('kokoro-strix')).toBe(KOKORO_VOICES);
    expect(voicesForModelId('kokoro')).toBe(KOKORO_VOICES);
    expect(voicesForModelId('KOKORO')).toBe(KOKORO_VOICES);
    expect(KOKORO_VOICES).toContain('af_heart');
    expect(KOKORO_VOICES.length).toBe(67); // live /v1/audio/voices snapshot
  });

  it('returns the OpenAI catalog for OpenAI tts model ids', () => {
    expect(voicesForModelId('tts-1')).toBe(OPENAI_TTS_VOICES);
    expect(voicesForModelId('tts-1-hd')).toBe(OPENAI_TTS_VOICES);
    expect(voicesForModelId('gpt-4o-mini-tts')).toBe(OPENAI_TTS_VOICES);
    expect(OPENAI_TTS_VOICES).toContain('alloy');
  });

  it('returns [] for ElevenLabs (voices are server-fed, not a static catalog)', () => {
    expect(voicesForModelId('eleven_multilingual_v2')).toEqual([]);
    expect(voicesForModelId('eleven_v3')).toEqual([]);
  });

  it('returns [] for unrecognized or empty ids (free-text fallback)', () => {
    expect(voicesForModelId('claude-opus-4-8')).toEqual([]);
    expect(voicesForModelId('gemma-4-moe-strix')).toEqual([]);
    expect(voicesForModelId('')).toEqual([]);
    expect(voicesForModelId(null)).toEqual([]);
    expect(voicesForModelId(undefined)).toEqual([]);
  });
});

describe('ttsBackendForModelId', () => {
  it('detects kokoro / openai / elevenlabs / unknown', () => {
    expect(ttsBackendForModelId('kokoro-strix')).toBe('kokoro');
    expect(ttsBackendForModelId('KOKORO')).toBe('kokoro');
    expect(ttsBackendForModelId('tts-1')).toBe('openai');
    expect(ttsBackendForModelId('gpt-4o-mini-tts')).toBe('openai');
    expect(ttsBackendForModelId('eleven_multilingual_v2')).toBe('elevenlabs');
    expect(ttsBackendForModelId('eleven_v3')).toBe('elevenlabs');
    expect(ttsBackendForModelId('some-other-model')).toBeNull();
    expect(ttsBackendForModelId('')).toBeNull();
    expect(ttsBackendForModelId(null)).toBeNull();
  });
});

describe('voiceLanguageTag', () => {
  it('decodes the Kokoro id prefix into a language tag', () => {
    const cases: Record<string, string> = {
      af_bella: 'EN-US', // a = American English
      am_adam: 'EN-US',
      bf_emma: 'EN-GB', // b = British English
      bm_george: 'EN-GB',
      ef_dora: 'ES', // e = Spanish
      ff_siwis: 'FR', // f = French
      hf_alpha: 'HI', // h = Hindi
      if_sara: 'IT', // i = Italian
      jf_alpha: 'JA', // j = Japanese
      pf_dora: 'PT', // p = Portuguese
      zf_xiaobei: 'ZH', // z = Chinese
    };
    for (const [voice, tag] of Object.entries(cases)) {
      expect(voiceLanguageTag('kokoro', voice)).toBe(tag);
    }
  });

  it('tags every OpenAI voice as multilingual', () => {
    expect(voiceLanguageTag('tts-1', 'alloy')).toBe('multi');
    expect(voiceLanguageTag('gpt-4o-mini-tts', 'nova')).toBe('multi');
  });

  it('returns null for ElevenLabs (accent labels come from the server, not the id)', () => {
    expect(voiceLanguageTag('eleven_multilingual_v2', 'v_sarah')).toBeNull();
  });

  it('returns null for unknown backends, unknown prefixes, or empty input', () => {
    expect(voiceLanguageTag('some-tts-x', 'rachel')).toBeNull(); // unknown backend
    expect(voiceLanguageTag('kokoro', 'qf_unknown')).toBeNull(); // no such prefix
    expect(voiceLanguageTag('kokoro', '')).toBeNull();
    expect(voiceLanguageTag(null, 'af_bella')).toBeNull();
  });
});
