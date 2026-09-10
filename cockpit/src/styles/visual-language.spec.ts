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

describe('typography — Cinzel only on the brand mark and hero', () => {
  const overrides = code(read('./themes/_roman-accents.scss'));

  it('themes no longer rebind the primary font or its display casing', () => {
    expect(overrides).not.toMatch(/--font-primary\s*:/);
    expect(overrides).not.toMatch(/--letter-spacing-display\s*:/);
    expect(overrides).not.toMatch(/--text-transform-display\s*:/);
  });

  it('legacy component selectors are gone from the theme layer', () => {
    for (const sel of ['.btn', '.filter-chip', '.tab-button', '.session-message', '.chat-message', 'h1.panel-title', '.session-title', '.nav-section-label', '.section-title']) {
      expect(overrides, sel).not.toContain(sel);
    }
  });

  it('the approval-card left rule survives', () => {
    expect(overrides).toMatch(/\.approval-card,\s*\n\s*\.tool-approval\s*\{[^}]*border-left:\s*3px solid var\(--accent-color\)/);
  });
});

describe('typography — recipes and scale', () => {
  const recipes = code(read('./_typography-recipes.scss'));
  const variables = code(read('./_variables.scss'));
  const styles = code(read('../styles.scss'));

  it('eyebrow tracking is modest and the display recipe is medium weight', () => {
    expect(recipes).toMatch(/@mixin eyebrow\s*\{[^}]*letter-spacing:\s*0\.06em/);
    expect(recipes).not.toMatch(/letter-spacing:\s*0\.2[0-9]em/);
    expect(recipes).toMatch(/@mixin display\s*\{[^}]*font-weight:\s*500/);
    expect(recipes).toMatch(/@mixin heading\(\$level\)/);
  });

  it('the scale has a 13px control size and a 12px spacing step', () => {
    expect(variables).toMatch(/\$font-size-control:\s*0\.8125rem/);
    expect(variables).toMatch(/\$space-12:\s*0\.75rem/);
  });

  it('headings are styled globally, sentence case, tight tracking', () => {
    expect(styles).toMatch(/\nh1\s*\{[^}]*letter-spacing:\s*-0\.02em/);
    expect(styles).not.toMatch(/\nh[1-4][^{]*\{[^}]*text-transform:\s*uppercase/);
  });
});

describe('typography — --font-display readers', () => {
  it('only the rail brand block and the chat hero read the Cinzel token', () => {
    const sites = scanSources(join(here, '../app'), /var\(--font-display/);
    const allowed = /sidebar\.component\.ts|chat-empty-state\.component\.scss/;
    const offenders = sites.filter((l) => !allowed.test(l));
    expect(offenders, offenders.join('\n')).toEqual([]);
    expect(sites.length).toBe(3);
  });
});

describe('controls — no stamp, compact sizes', () => {
  it('nothing includes shape.stamp and the recipe is gone', () => {
    const hits = scanSources(join(here, '..'), /shape\.stamp|--stamp-/);
    expect(hits, hits.join('\n')).toEqual([]);
  });

  it('md buttons are 32px', () => {
    expect(code(read('../app/ui/button/button.component.scss'))).toMatch(/data-size='md'\]\s*\{[^}]*height:\s*32px/);
  });
});

describe('surfaces — data tables', () => {
  it('every <table> in a view carries the app-table hook', () => {
    const tables = [
      ...scanSources(join(here, '../app/views'), /<table\b/),
      ...scanSources(join(here, '../app/workbench'), /<table\b/),
    ];
    const bare = tables.filter((hit) => {
      const [file, line] = hit.split(':');
      const dir = hit.includes('workbench/') || file.startsWith('components/') ? '../app/workbench' : '../app/views';
      const text = read(join(dir, file)).split('\n')[Number(line) - 1];
      return !/app-table/.test(text);
    });
    expect(bare, bare.join('\n')).toEqual([]);
  });
});

describe('templates — no raw legacy buttons', () => {
  it('every button in a view is a primitive', () => {
    // Legacy variants only: class hooks such as btn-add-env on a primitive are fine.
    const hits = scanSources(join(here, '../app'), /class="btn(?:[\s"]|-primary|-ghost|-text)|queued-action parked-retry/);
    expect(hits, hits.join('\n')).toEqual([]);
  });
});

describe('debt — no px radii anywhere', () => {
  it('inline styles obey the stylelint rule too', () => {
    const hits = scanSources(join(here, '../app'), /border-radius:\s*[1-9][0-9]*px/);
    expect(hits, hits.join('\n')).toEqual([]);
  });
});
