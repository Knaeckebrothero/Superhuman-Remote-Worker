import {describe, expect, it} from 'vitest';
import {
  allToolCategoriesSelected,
  delegationOverride,
  disabledToolCategoriesFromConfig,
} from './tools-group.component';

/**
 * Select-all state for the tool toggles. A category is "selected" (enabled)
 * unless it appears in the disabled set; grant-blocked categories are filtered
 * out by the caller before reaching here, so only toggleable keys are passed.
 */
describe('tools-group select-all state', () => {
  it('is true when nothing is disabled', () => {
    expect(allToolCategoriesSelected(['research', 'shell', 'delegation'], new Set())).toBe(true);
  });

  it('is false when one category is disabled', () => {
    expect(
      allToolCategoriesSelected(['research', 'shell', 'delegation'], new Set(['shell'])),
    ).toBe(false);
  });

  it('is false when all categories are disabled', () => {
    const keys = ['research', 'shell'];
    expect(allToolCategoriesSelected(keys, new Set(keys))).toBe(false);
  });

  it('is false when there are no selectable categories', () => {
    expect(allToolCategoriesSelected([], new Set())).toBe(false);
  });

  it('ignores disabled keys that are not in the selectable set', () => {
    // A grant-blocked category lingering in the disabled set must not flip the
    // state false, since it is excluded from the selectable keys.
    expect(allToolCategoriesSelected(['research'], new Set(['browser_direct']))).toBe(true);
  });
});

describe('resolved tool config', () => {
  it('recognizes categories disabled by persistent session defaults', () => {
    const disabled = disabledToolCategoriesFromConfig({
      tools: {
        communication: [],
        delegation: [],
        agent_catalog: [],
        workflows: [],
      },
    }, ['communication', 'delegation', 'agent_catalog', 'workflows', 'research']);

    expect(disabled).toEqual(new Set([
      'communication',
      'delegation',
      'agent_catalog',
      'workflows',
    ]));
  });
});

describe('delegation override', () => {
  it('emits only max_concurrent for an inline parameter edit', () => {
    expect(delegationOverride(true, true, 6)).toEqual({max_concurrent: 6});
  });

  it('keeps enabled synchronization and omits the retired keys', () => {
    const override = delegationOverride(false, true, 3);
    expect(override).toEqual({enabled: false, max_concurrent: 3});
    expect(override).not.toHaveProperty('max_depth');
    expect(override).not.toHaveProperty('default_timeout');
  });

  it('emits nothing when delegation still matches its baseline', () => {
    expect(delegationOverride(true, true, null)).toBeNull();
  });
});
