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
