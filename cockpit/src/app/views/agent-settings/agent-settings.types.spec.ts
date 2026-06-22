import {describe, it, expect} from 'vitest';
import {computeModelMismatch, detectModelFamily} from './agent-settings.types';

describe('detectModelFamily — GLM', () => {
  it('maps GLM-5.2 IDs to the glm family across transports', () => {
    expect(detectModelFamily('openrouter/z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('glm-5.2')).toBe('glm');
  });
});

describe('detectModelFamily — Mistral', () => {
  it('maps Mistral 3 family + specialists across transports', () => {
    expect(detectModelFamily('mistral-large-latest')).toBe('mistral');
    expect(detectModelFamily('mistral-medium-latest')).toBe('mistral');
    expect(detectModelFamily('mistral-small-latest')).toBe('mistral');
    expect(detectModelFamily('codestral-latest')).toBe('mistral');
    expect(detectModelFamily('openrouter/mistralai/mistral-large')).toBe('mistral');
  });
});

describe('computeModelMismatch', () => {
  // Flattened settings-matrix shape (family → resolved settings), exactly what
  // the client receives from the backend's _load_settings_matrix output.
  const M = {
    default: {model_max_context_tokens: 128000, multimodal: false},
    'gpt-5': {model_max_context_tokens: 1050000, multimodal: true},
    gemma: {model_max_context_tokens: 131072, multimodal: true},
    'gpt-oss': {model_max_context_tokens: 131072, multimodal: false},
    gemini: {model_max_context_tokens: 1000000, multimodal: true},
    'minimax-m3': {model_max_context_tokens: 1000000, multimodal: true},
  };

  it('returns null for same-family models (incident pair) — backend min/AND is a no-op', () => {
    // gpt-5.5 + gpt-5.4-mini: same family, same 1.05M window, same multimodal.
    expect(computeModelMismatch(M, 'gpt-5.5', 'gpt-5.4-mini')).toBeNull();
  });

  it('stays silent on DIFFERENT family when window + multimodal agree', () => {
    // gemini vs minimax-m3: different families, both 1M + multimodal → no consequence.
    expect(computeModelMismatch(M, 'gemini-3.5-flash', 'minimax/minimax-m3')).toBeNull();
  });

  it('flags a window gap and marks it prominent when >2x', () => {
    // gpt-5.5 (1.05M, multimodal) + gemma (131072, multimodal): pure window gap.
    const mm = computeModelMismatch(M, 'gpt-5.5', 'RedHatAI/gemma-4-31B-it-FP8-Dynamic');
    expect(mm).not.toBeNull();
    expect(mm!.window).toEqual({min: 131072, strategicWindow: 1050000, tacticalWindow: 131072});
    expect(mm!.prominent).toBe(true); // 1.05M > 2 × 131072
    expect(mm!.multimodal).toBe(false); // both multimodal → no mm warning
  });

  it('does NOT mark prominent for a sub-2x window gap', () => {
    // gemini (1M) + gpt-5.5 (1.05M): differs, but 1.05M ≤ 2 × 1M.
    const mm = computeModelMismatch(M, 'gemini-3.5-flash', 'gpt-5.5');
    expect(mm).not.toBeNull();
    expect(mm!.window!.min).toBe(1000000);
    expect(mm!.prominent).toBe(false);
    expect(mm!.multimodal).toBe(false);
  });

  it('flags a multimodal mismatch (and the accompanying window gap)', () => {
    // gpt-5.5 (multimodal, 1.05M) + gpt-oss-120b (text-only, 131072).
    const mm = computeModelMismatch(M, 'gpt-5.5', 'openai/gpt-oss-120b');
    expect(mm).not.toBeNull();
    expect(mm!.multimodal).toBe(true);
    expect(mm!.window!.min).toBe(131072);
    expect(mm!.prominent).toBe(true);
  });

  it('returns null for identical models, missing models, or empty matrix', () => {
    expect(computeModelMismatch(M, 'gpt-5.5', 'gpt-5.5')).toBeNull();
    expect(computeModelMismatch(M, null, 'gpt-5.5')).toBeNull();
    expect(computeModelMismatch(M, 'gpt-5.5', null)).toBeNull();
    expect(computeModelMismatch({}, 'gpt-5.5', 'gemma')).toBeNull();
  });
});
