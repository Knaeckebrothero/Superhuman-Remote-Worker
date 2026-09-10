import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {describe, expect, it} from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const overrides = readFileSync(join(here, '_roman-accents.scss'), 'utf8');
const semantic = readFileSync(join(here, '..', '_semantic-tokens.scss'), 'utf8');

// Strip line comments so prose about the retired sharp-corner pass can't trip
// the guard — only live declarations count.
const code = (s: string) => s.replace(/\/\/.*$/gm, '');

describe('shape language — rounded corners (2026-09-10)', () => {
  it('the theme layer no longer rebinds the primitive radius scale', () => {
    expect(code(overrides)).not.toMatch(/--radius-(sm|md|lg|xl)\s*:/);
  });

  it('the theme layer no longer hard-codes border-radius: 0', () => {
    expect(code(overrides)).not.toMatch(/border-radius\s*:\s*0\b/);
  });

  it('surfaces bind to the lg primitive, controls to md', () => {
    expect(code(semantic)).toMatch(/--radius-surface\s*:\s*var\(--radius-lg\)/);
    expect(code(semantic)).toMatch(/--radius-control\s*:\s*var\(--radius-md\)/);
  });
});
