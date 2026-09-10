import {readFileSync, readdirSync, statSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join, relative} from 'node:path';
import {describe, expect, it} from 'vitest';

// Text-level guards for the 2026-09-10 visual refresh
// (knowledge-base/knowledge/features/cockpit_modern_visual_refresh.md).
// Each retired pattern gets an assertion here so it cannot creep back through
// a stray edit. Styles are read as text: jsdom computes no custom properties,
// so the browser walk (e2e/visual-walk) remains the only visual gate.

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(join(here, rel), 'utf8');
// Strip // comments: prose about retired patterns must not trip the guard.
const code = (s: string) => s.replace(/\/\/.*$/gm, '');

/** Every .ts/.scss under `root` (recursively) whose text matches `re`, as
 *  `relative/path:line` strings. Spec files are skipped. */
export function scanSources(root: string, re: RegExp, skip: RegExp = /\.spec\.ts$/): string[] {
  const hits: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) {
        walk(full);
      } else if (/\.(ts|scss)$/.test(name) && !skip.test(name)) {
        const lines = readFileSync(full, 'utf8').split('\n');
        lines.forEach((line, i) => {
          if (re.test(line)) hits.push(`${relative(root, full)}:${i + 1}`);
        });
      }
    }
  };
  walk(root);
  return hits;
}

describe('root scale', () => {
  const styles = code(read('../styles.scss'));

  it('html sets the rem base to 100% and the shared html,body rule no longer sizes text', () => {
    expect(styles).toMatch(/\nhtml\s*\{[^}]*font-size:\s*100%;/);
    expect(styles).not.toMatch(/\nhtml,\s*\nbody\s*\{[^}]*font-size:/);
  });

  it('body carries the app text size', () => {
    expect(styles).toMatch(/\nbody\s*\{[^}]*font-size:\s*v\.\$font-size-sm;/);
  });
});
