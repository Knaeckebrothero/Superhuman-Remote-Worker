import {describe, it, expect} from 'vitest';
import {
  computeModelMismatch,
  defaultModelOptionLabel,
  detectModelFamily,
  resolveEffectiveModels,
} from './agent-settings.types';
import type {EffectiveModels} from '../../core/models/api.model';

describe('detectModelFamily — GLM', () => {
  it('maps GLM-5.2 IDs to the glm family across transports', () => {
    expect(detectModelFamily('openrouter/z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('glm-5.2')).toBe('glm');
  });
});

describe('detectModelFamily — GPT-5.6', () => {
  it('maps GPT-5.6 tiers to the gpt-5.6 family, ahead of gpt-5', () => {
    expect(detectModelFamily('gpt-5.6-sol')).toBe('gpt-5.6');
    expect(detectModelFamily('gpt-5.6-terra')).toBe('gpt-5.6');
    expect(detectModelFamily('openai/gpt-5.6-luna')).toBe('gpt-5.6');
    expect(detectModelFamily('codex/gpt-5.6-sol')).toBe('gpt-5.6');
  });

  it('keeps neighbors unaffected', () => {
    expect(detectModelFamily('gpt-5.5')).toBe('gpt-5');
    expect(detectModelFamily('gpt-5.6-codex')).toBe('codex');
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

describe('resolveEffectiveModels', () => {
  const expert: EffectiveModels = {
    strategic: {model: 'gpt-5.5', source: 'expert'},
    tactical: {model: 'gpt-5.5', source: 'expert'},
    subagent: {model: 'gpt-5.5', source: 'expert'},
    session: {model: 'gpt-5.5', source: 'expert'},
  };
  const framework: EffectiveModels = {
    strategic: {model: 'gemma-4-31b', source: 'system_default'},
    tactical: {model: 'gemma-4-31b', source: 'system_default'},
    subagent: {model: 'gemma-4-31b', source: 'system_default'},
    session: {model: 'gemma-4-31b', source: 'system_default'},
  };

  it('prefers the selected expert resolution when present', () => {
    expect(resolveEffectiveModels(expert, framework)).toBe(expert);
  });

  it('falls back to the framework default resolution when no expert is selected', () => {
    // Regression: the no-expert create path. Without the fallback the picker's
    // "Default" option drops to the config-literal llm.model (the hardcoded YAML
    // placeholder, RedHatAI/gemma-4-31B-it-FP8-Dynamic) instead of the resolved
    // system chat pin. null (no expert) and undefined (older API) both fall back.
    expect(resolveEffectiveModels(null, framework)).toBe(framework);
    expect(resolveEffectiveModels(undefined, framework)).toBe(framework);
  });

  it('returns null when neither expert nor framework resolution is available', () => {
    expect(resolveEffectiveModels(null, null)).toBeNull();
  });
});

describe('defaultModelOptionLabel', () => {
  it('appends the resolved model to the inherit-marker prefix', () => {
    expect(defaultModelOptionLabel('Base default', 'gemma-4-31b')).toBe('Base default · gemma-4-31b');
    expect(defaultModelOptionLabel('Project default', 'gpt-5.5')).toBe('Project default · gpt-5.5');
  });

  it('shows the bare prefix when no model is resolved yet (null/undefined/empty)', () => {
    // The picker hasn't loaded the framework default yet, or there is no catalog
    // row — fall back to the plain marker instead of a dangling separator.
    expect(defaultModelOptionLabel('Base default', null)).toBe('Base default');
    expect(defaultModelOptionLabel('Base default', undefined)).toBe('Base default');
    expect(defaultModelOptionLabel('Project default', '')).toBe('Project default');
  });
});
