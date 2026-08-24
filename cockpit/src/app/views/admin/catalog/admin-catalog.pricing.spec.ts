import {describe, expect, it} from 'vitest';
import {
  mergePricingId,
  pricingIdOf,
  pricingLabelOf,
  pricingModeOf,
} from './admin-catalog.component';

/**
 * `params_json.pricing_id` decides whether a catalog model gets a $/token rate
 * at all, and it had no UI until now — which is why self-hosted models sat
 * unpriced and their usage metered with `cost_usd` NULL.
 *
 * The pure helpers are tested here rather than through the component because
 * vitest runs Angular JIT: signal inputs cannot be property-bound and signal
 * `viewChild` never resolves, so a rendered test of this dialog would assert
 * the harness rather than the logic. Same split as admin-catalog.warning.spec.ts.
 */
describe('pricing mode', () => {
  it('treats an absent key as auto-detect', () => {
    expect(pricingModeOf(null)).toBe('auto');
    expect(pricingModeOf({})).toBe('auto');
    expect(pricingModeOf({voice: 'af_heart'})).toBe('auto');
  });

  it('treats an empty string as an explicit never-price', () => {
    // The backend's own sentinel: "" marks a model deliberately unpriced,
    // which is a different statement from "nobody has mapped this yet".
    expect(pricingModeOf({pricing_id: ''})).toBe('never');
    expect(pricingModeOf({pricing_id: '   '})).toBe('never');
  });

  it('treats a non-empty string as an explicit mapping', () => {
    expect(pricingModeOf({pricing_id: 'google/gemma-4-26b-a4b-it'})).toBe('map');
  });

  it('ignores a non-string value rather than guessing', () => {
    expect(pricingModeOf({pricing_id: 42})).toBe('auto');
    expect(pricingModeOf({pricing_id: null})).toBe('auto');
  });

  it('reads back the trimmed id', () => {
    expect(pricingIdOf({pricing_id: '  openai/gpt-5.5 '})).toBe('openai/gpt-5.5');
    expect(pricingIdOf(null)).toBe('');
  });

  it('labels each state distinctly', () => {
    expect(pricingLabelOf(null)).toBe('Auto');
    expect(pricingLabelOf({pricing_id: ''})).toBe('Never price');
    expect(pricingLabelOf({pricing_id: 'minimax/minimax-m3'})).toBe('minimax/minimax-m3');
  });
});

describe('mergePricingId', () => {
  it('preserves every other params_json key', () => {
    // The trap this helper exists for: PATCH assigns params_json wholesale
    // (`params_json = $n` in PostgresDB.update_model — no jsonb merge), so
    // sending {pricing_id} alone would silently drop a TTS voice.
    const merged = mergePricingId(
      {voice: 'af_heart', temperature: 0},
      'google/gemma-4-26b-a4b-it',
    );
    expect(merged).toEqual({
      voice: 'af_heart',
      temperature: 0,
      pricing_id: 'google/gemma-4-26b-a4b-it',
    });
  });

  it('does not mutate the row it was handed', () => {
    const original = {voice: 'af_heart'};
    mergePricingId(original, 'openai/gpt-5.5');
    expect(original).toEqual({voice: 'af_heart'});
  });

  it('clears the key on undefined without touching the rest', () => {
    expect(mergePricingId({voice: 'af_heart', pricing_id: 'x'}, undefined)).toEqual({
      voice: 'af_heart',
    });
  });

  it('writes the empty-string sentinel for never-price', () => {
    expect(mergePricingId(null, '')).toEqual({pricing_id: ''});
  });

  it('returns null when nothing is left, so the column resets to SQL NULL', () => {
    expect(mergePricingId({pricing_id: 'x'}, undefined)).toBeNull();
    expect(mergePricingId(null, undefined)).toBeNull();
  });

  it('round-trips through the reader', () => {
    const merged = mergePricingId(null, 'google/gemma-4-31b-it');
    expect(pricingModeOf(merged)).toBe('map');
    expect(pricingIdOf(merged)).toBe('google/gemma-4-31b-it');
  });

  it('falsy-but-real values survive the merge', () => {
    // 0 and false are legitimate inference overrides; a truthiness-based merge
    // would drop them.
    const merged = mergePricingId({temperature: 0, stream: false}, 'openai/gpt-5.5');
    expect(merged).toMatchObject({temperature: 0, stream: false});
  });
});
