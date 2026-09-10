import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {describe, expect, it} from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const scss = readFileSync(join(here, '_theme-config.scss'), 'utf8');

describe('_theme-config.scss ramp tokens', () => {
  it('defines cat-1..8 in BOTH theme maps (once each = twice total)', () => {
    for (let i = 1; i <= 8; i++) {
      const occurrences = scss.split(`'cat-${i}':`).length - 1;
      expect(occurrences, `--cat-${i} must appear in both theme maps`).toBe(2);
    }
  });
});

describe('_theme-config.scss derived tokens', () => {
  for (const token of ['ring', 'border-hairline']) {
    it(`defines ${token} in BOTH theme maps`, () => {
      expect(scss.split(`'${token}':`).length - 1, `--${token} must appear in both maps`).toBe(2);
    });
  }
});

// The accent axis. The base maps must not carry accent tokens (a second
// source would silently win or lose on cascade order), every accent must
// exist for both modes, and the default must not be the danger red — that
// collision is the reason the axis exists.
describe('_theme-config.scss accent axis', () => {
  const ACCENT_TOKENS = ['accent-color', 'accent-hover', 'on-accent', 'user-bubble', 'user-bubble-text'];
  const ACCENTS = ['tyrian', 'porphyry', 'graphite'];
  const accentsStart = scss.indexOf('$accents: (');
  const base = scss.slice(0, accentsStart);
  const accents = scss.slice(accentsStart);

  function block(text: string, header: string): string {
    const start = text.indexOf(header);
    expect(start, `${header} not found`).toBeGreaterThanOrEqual(0);
    const end = text.indexOf('\n);', start);
    return text.slice(start, end);
  }
  function hexOf(text: string, token: string): string {
    const m = text.match(new RegExp(`'${token}':\\s*(#[0-9a-fA-F]{3,8})`));
    expect(m, `${token} not found`).not.toBeNull();
    return (m as RegExpMatchArray)[1].toLowerCase();
  }
  function accentBlock(accent: string, mode: string): string {
    const a = accents.indexOf(`'${accent}': (`);
    expect(a, `accent ${accent}`).toBeGreaterThanOrEqual(0);
    const m = accents.indexOf(`'${mode}': (`, a);
    const close = accents.indexOf('),', m);
    return accents.slice(m, close);
  }

  it('base theme maps carry no accent tokens', () => {
    for (const token of ACCENT_TOKENS) {
      expect(base.includes(`'${token}':`), `${token} must live in $accents only`).toBe(false);
    }
  });

  it('defines every accent for both modes with all five tokens', () => {
    for (const accent of ACCENTS) {
      for (const mode of ['travertine', 'senate']) {
        const b = accentBlock(accent, mode);
        for (const token of ACCENT_TOKENS) expect(b.includes(`'${token}':`), `${accent}/${mode}/${token}`).toBe(true);
      }
    }
  });

  it('defaults to tyrian, which differs from the danger red in both modes', () => {
    expect(scss).toMatch(/\$default-accent:\s*'tyrian'/);
    for (const [mode, header] of [['travertine', '$travertine-theme: ('], ['senate', '$senate-theme: (']]) {
      const danger = hexOf(block(base, header), 'danger');
      expect(hexOf(accentBlock('tyrian', mode), 'accent-color'), `${mode} accent equals danger`).not.toBe(danger);
    }
  });

  it('accent-dependent shadows derive from --accent-color instead of a red literal', () => {
    // The danger tints in the same maps are red by design; only the shadow
    // entries must follow the accent.
    for (const header of ['$travertine-theme: (', '$senate-theme: (']) {
      const b = block(base, header);
      expect(b).toMatch(/'shadow-glow':\s*'[^']*var\(--accent-color\)/);
      expect(b).not.toMatch(/'shadow-(glow|md)':\s*'[^']*rgba\(\s*(156, ?40, ?50|204, ?70, ?71)/);
    }
    expect(block(base, '$senate-theme: (')).toMatch(/'shadow-md':\s*'[^']*var\(--accent-color\)/);
  });
});
