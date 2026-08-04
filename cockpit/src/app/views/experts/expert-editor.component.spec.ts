import {describe, expect, it} from 'vitest';
import {
  buildPromptsPayload,
  expertBaseConfigName,
  expertEditorMode,
  expertToolPreviewRequest,
  parseConfigText,
  slugify,
} from './expert-editor.component';

const FIELDS = {
  persona: 'P',
  instructions: 'I',
  strategic: 'S',
  tactical: 'T',
  summarization: 'Z',
};

describe('slugify', () => {
  it('lowercases and dashes', () => expect(slugify('My Cool Expert!')).toBe('my-cool-expert'));
  it('prefixes when not starting with a letter', () =>
    expect(slugify('123 go')).toMatch(/^[a-z][a-z0-9_-]*$/));
  it('strips non-ascii', () => expect(slugify('Über Helper')).toMatch(/^[a-z][a-z0-9_-]*$/));
  it('empty falls back to expert', () => expect(slugify('   ')).toBe('expert'));
});

describe('parseConfigText', () => {
  it('blank yields empty object', () => expect(parseConfigText('  ')).toEqual({config: {}}));
  it('valid object parses', () =>
    expect(parseConfigText('{"llm":{"model":"x"}}')).toEqual({config: {llm: {model: 'x'}}}));
  it('array is rejected', () => expect(parseConfigText('[]').error).toBeTruthy());
  it('non-json is rejected', () => expect(parseConfigText('{nope').error).toBeTruthy());
});

describe('expert type base', () => {
  it('maps each immutable expert type to its matching mode and base', () => {
    expect(expertBaseConfigName('worker')).toBe('worker_base');
    expect(expertEditorMode('worker')).toBe('job');
    expect(expertBaseConfigName('session')).toBe('session_base');
    expect(expertEditorMode('session')).toBe('session');
  });
});

describe('expertToolPreviewRequest', () => {
  it('resolves against the base the edited type inherits from', () => {
    expect(expertToolPreviewRequest('worker', {}).config_name).toBe('worker_base');
    expect(expertToolPreviewRequest('session', {}).config_name).toBe('session_base');
  });

  it('forwards the type, so the server defaults nothing on our behalf', () => {
    // The endpoint's own default is `session`; omitting this predicts
    // session_base for a worker expert.
    expect(expertToolPreviewRequest('worker', {}).expert_type).toBe('worker');
    expect(expertToolPreviewRequest('session', {}).expert_type).toBe('session');
  });

  it('sends the fragment as the override layer', () => {
    const fragment = {tools: {shell: ['run_command']}};
    expect(expertToolPreviewRequest('worker', fragment).config_override).toEqual(fragment);
  });

  it('sends null rather than an empty layer when there is no fragment', () => {
    expect(expertToolPreviewRequest('worker', {}).config_override).toBeNull();
  });

  it('never identifies the expert by id', () => {
    // The whole point: base ⊕ fragment. An `expert_id` layer underneath cannot
    // express a key the author DELETED, so the pane would keep showing a
    // category the expert is about to lose. Asserted structurally because the
    // failure mode is an extra field, not a wrong one.
    expect(Object.keys(expertToolPreviewRequest('worker', {tools: {}})).sort()).toEqual([
      'config_name',
      'config_override',
      'expert_type',
    ]);
  });
});

describe('buildPromptsPayload', () => {
  it('worker mode emits all five segments', () => {
    expect(buildPromptsPayload(FIELDS, 'job')).toEqual({
      persona: 'P',
      instructions: 'I',
      strategic: 'S',
      tactical: 'T',
      summarization: 'Z',
    });
  });

  it('session mode drops the worker-only phase prompts', () => {
    const out = buildPromptsPayload(FIELDS, 'session');
    expect(out).toEqual({persona: 'P', instructions: 'I', summarization: 'Z'});
    expect(out['strategic']).toBeUndefined();
    expect(out['tactical']).toBeUndefined();
  });

  it('omits empty/whitespace segments (empty ⇒ inherit / clear)', () => {
    const out = buildPromptsPayload(
      {persona: 'P', instructions: '', strategic: '   ', tactical: 'T', summarization: ''},
      'job',
    );
    expect(out).toEqual({persona: 'P', tactical: 'T'});
  });
});
