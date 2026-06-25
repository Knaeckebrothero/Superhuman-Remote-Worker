import {describe, it, expect} from 'vitest';
import {getReasoningOptions, reasoningOptionsForModel} from './reasoning-options';

describe('getReasoningOptions (capability-driven)', () => {
  it('returns Default-only for method=none', () => {
    const opts = getReasoningOptions({method: 'none', default: null, options: []});
    expect(opts).toEqual([{value: null, label: 'Default'}]);
  });

  it('returns Default-only for a missing/null capability', () => {
    expect(getReasoningOptions(null)).toEqual([{value: null, label: 'Default'}]);
    expect(getReasoningOptions(undefined)).toEqual([{value: null, label: 'Default'}]);
  });

  it('maps effort_enum options to labelled entries after Default', () => {
    const opts = getReasoningOptions({
      method: 'effort_enum',
      default: 'high',
      options: ['low', 'medium', 'high'],
    });
    expect(opts).toEqual([
      {value: null, label: 'Default'},
      {value: 'low', label: 'Low'},
      {value: 'medium', label: 'Medium'},
      {value: 'high', label: 'High'},
    ]);
  });

  it('renders binary_toggle as On/Off (gemma)', () => {
    const opts = getReasoningOptions({
      method: 'binary_toggle',
      default: 'on',
      options: ['on', 'off'],
    });
    expect(opts).toEqual([
      {value: null, label: 'Default'},
      {value: 'on', label: 'On'},
      {value: 'off', label: 'Off'},
    ]);
  });

  it('labels the OpenRouter superset incl. xhigh', () => {
    const opts = getReasoningOptions({
      method: 'effort_enum',
      default: 'high',
      options: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'],
    });
    expect(opts.map((o) => o.label)).toEqual([
      'Default',
      'None',
      'Minimal',
      'Low',
      'Medium',
      'High',
      'X-High',
    ]);
  });

  it('shows a non-selectable "Always on" for always_on models', () => {
    const opts = getReasoningOptions({method: 'always_on', default: 'on', options: []});
    expect(opts).toEqual([{value: null, label: 'Always on'}]);
  });
});

describe('reasoningOptionsForModel', () => {
  const byModel = {
    'gemma-4-moe': {method: 'binary_toggle', default: 'on', options: ['on', 'off']},
    'gpt-5.4': {method: 'effort_enum', default: 'high', options: ['low', 'medium', 'high']},
  };

  it('looks up the capability by model id', () => {
    expect(reasoningOptionsForModel('gemma-4-moe', byModel).map((o) => o.label)).toEqual([
      'Default',
      'On',
      'Off',
    ]);
  });

  it('falls back to Default-only for unknown / null models', () => {
    expect(reasoningOptionsForModel('mystery-model', byModel)).toEqual([
      {value: null, label: 'Default'},
    ]);
    expect(reasoningOptionsForModel(null, byModel)).toEqual([
      {value: null, label: 'Default'},
    ]);
  });
});
