import {describe, expect, it} from 'vitest';
import {resolveAppearance} from './button-appearance';

describe('resolveAppearance', () => {
  it('tinted variants default to subtle', () => {
    for (const v of ['success', 'warning', 'info', 'danger'] as const) {
      expect(resolveAppearance(v, undefined)).toBe('subtle');
    }
  });

  it('tinted variants honour an explicit solid', () => {
    expect(resolveAppearance('danger', 'solid')).toBe('solid');
  });

  it('primary, secondary and ghost have no appearance axis', () => {
    for (const v of ['primary', 'secondary', 'ghost'] as const) {
      expect(resolveAppearance(v, 'solid')).toBeNull();
    }
  });
});
