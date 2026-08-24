import { describe, expect, it } from 'vitest';
import { reasoningStarveWarning } from './admin-catalog.component';

/**
 * The Admin → Models low-context-window warning. The threshold + backstop math
 * must mirror the agent's loader.py _resolve_max_output_tokens
 * (backstop = ctx - floor(0.80*ctx) - 4096, floored at 4096; warn under 16384).
 */
describe('reasoningStarveWarning', () => {
  it('returns null when the window is unset or non-positive', () => {
    expect(reasoningStarveWarning(null)).toBeNull();
    expect(reasoningStarveWarning(0)).toBeNull();
    expect(reasoningStarveWarning(-1)).toBeNull();
  });

  it('warns when a low window starves output below ~16k', () => {
    // ctx 32000 (the k3d minimax setting) → backstop 4096 → warn.
    const w = reasoningStarveWarning(32000);
    expect(w).not.toBeNull();
    expect(w).toContain('4,096'); // toLocaleString grouping
    expect(w).toContain('Reasoning shares this budget');
  });

  it('warns just under the 16384 threshold and not at/above it', () => {
    // backstop(100000) = 100000 - 80000 - 4096 = 15904  → warn
    expect(reasoningStarveWarning(100000)).not.toBeNull();
    // backstop(102400) = 102400 - 81920 - 4096 = 16384  → no warn (boundary)
    expect(reasoningStarveWarning(102400)).toBeNull();
  });

  it('does not warn for healthy windows (backstop is the binding cap here)', () => {
    expect(reasoningStarveWarning(131072)).toBeNull(); // 128k → backstop 22119
    expect(reasoningStarveWarning(262144)).toBeNull(); // 256k → backstop 48333
    expect(reasoningStarveWarning(1000000)).toBeNull(); // 1M  → backstop 195904
  });

  it('reports the computed backstop, not the raw window', () => {
    // backstop(80000) = 80000 - 64000 - 4096 = 11904
    expect(reasoningStarveWarning(80000)).toContain('11,904');
  });
});
