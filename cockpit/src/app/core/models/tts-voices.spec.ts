import {describe, expect, it} from 'vitest';
import {KOKORO_VOICES, OPENAI_TTS_VOICES, voicesForModelId} from './tts-voices';

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

  it('returns [] for unrecognized or empty ids (free-text fallback)', () => {
    expect(voicesForModelId('claude-opus-4-8')).toEqual([]);
    expect(voicesForModelId('gemma-4-moe-strix')).toEqual([]);
    expect(voicesForModelId('')).toEqual([]);
    expect(voicesForModelId(null)).toEqual([]);
    expect(voicesForModelId(undefined)).toEqual([]);
  });
});
