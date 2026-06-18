import {describe, expect, it} from 'vitest';
import {assembleExpertConfig, MANAGED_CONFIG_KEYS, splitExpertConfig} from './expert-config';

describe('MANAGED_CONFIG_KEYS', () => {
  it('covers the dimensions owned by the structured controls', () => {
    for (const k of ['tools', 'llm', 'autonomy', 'workspace', 'limits', 'interactive']) {
      expect(MANAGED_CONFIG_KEYS.has(k)).toBe(true);
    }
    expect(MANAGED_CONFIG_KEYS.has('instruction_files')).toBe(false);
  });
});

describe('splitExpertConfig', () => {
  it('separates managed keys from the raw remainder', () => {
    const {managed, rawRemainderText} = splitExpertConfig({
      tools: {shell: []},
      llm: {model: 'm'},
      instruction_files: [{file: 'x.md'}],
      agent_id: 'scholar',
    });
    expect(managed).toEqual({tools: {shell: []}, llm: {model: 'm'}});
    expect(JSON.parse(rawRemainderText)).toEqual({
      instruction_files: [{file: 'x.md'}],
      agent_id: 'scholar',
    });
  });

  it('empty remainder yields empty string', () => {
    expect(splitExpertConfig({tools: {}}).rawRemainderText).toBe('');
  });

  it('empty fragment yields empty managed + empty string', () => {
    expect(splitExpertConfig({})).toEqual({managed: {}, rawRemainderText: ''});
  });
});

describe('assembleExpertConfig', () => {
  it('preserves a managed value the control did not re-emit (model survives)', () => {
    // The highest-risk case: model-select / model-group emits no override for a
    // pinned-but-untouched model; managedBase must carry it through.
    const out = assembleExpertConfig({llm: {strategic: {model: 'X'}}}, {}, {});
    expect(out).toEqual({llm: {strategic: {model: 'X'}}});
  });

  it('structured edit beats the stale managed base', () => {
    const out = assembleExpertConfig({autonomy: 'review'}, {autonomy: 'full'}, {});
    expect(out['autonomy']).toBe('full');
  });

  it('preserves an unowned subkey when only a sibling is overridden', () => {
    const out = assembleExpertConfig(
      {workspace: {structure: ['repo/'], backend: 'sandbox'}},
      {workspace: {backend: 'vm'}},
      {},
    );
    expect(out['workspace']).toEqual({structure: ['repo/'], backend: 'vm'});
  });

  it('raw flap wins last (power-user override)', () => {
    const out = assembleExpertConfig({}, {autonomy: 'full'}, {autonomy: 'guided'});
    expect(out['autonomy']).toBe('guided');
  });

  it('merges structured llm reasoning with the model-select model', () => {
    const out = assembleExpertConfig(
      {},
      {llm: {strategic: {model: 'X', reasoning_level: 'high'}}},
      {},
    );
    expect(out['llm']).toEqual({strategic: {model: 'X', reasoning_level: 'high'}});
  });

  it('does not mutate the source fragment', () => {
    const frag = {tools: {shell: []}};
    assembleExpertConfig(frag, {tools: {shell: ['run_command']}}, {});
    expect(frag).toEqual({tools: {shell: []}});
  });
});
