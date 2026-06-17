import {describe, expect, it} from 'vitest';
import {parseConfigText, slugify} from './expert-editor.component';

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
