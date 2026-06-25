import {describe, expect, it} from 'vitest';
import {buildPromptsPayload, parseConfigText, slugify} from './expert-editor.component';

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
