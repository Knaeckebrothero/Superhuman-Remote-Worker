import {describe, expect, it} from 'vitest';
import {resolveGraphColors, buildCytoscapeStyles} from './graph-colors';

// Fake reader: returns the token name back so we can assert mapping without a real DOM.
const echo = (name: string) => `RESOLVED${name}`;

describe('resolveGraphColors', () => {
  it('maps node types to ramp tokens', () => {
    const c = resolveGraphColors(echo);
    expect(c.nodeType['Rule']).toBe('RESOLVED--cat-1');       // terracotta (was red)
    expect(c.nodeType['Requirement']).toBe('RESOLVED--cat-6'); // lapis (was blue)
    expect(c.nodeDefault).toBe('RESOLVED--text-muted');
  });
  it('keeps change states on the Okabe-Ito palette (NOT themed)', () => {
    const c = resolveGraphColors(echo);
    expect(c.changeCreated).toBe('#0072B2');
    expect(c.changeModified).toBe('#E69F00');
    expect(c.changeDeleted).toBe('#D55E00');
  });
});

describe('buildCytoscapeStyles', () => {
  it('produces concrete colors — no var() and no Catppuccin hex', () => {
    const styles = buildCytoscapeStyles(resolveGraphColors(echo));
    const json = JSON.stringify(styles);
    expect(json).not.toContain('var(');
    expect(json).not.toMatch(/#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#6c7086|#1e1e2e|#45475a|#f5c2e7|#7f849c|#cdd6f4/);
  });
  it('applies the resolved node-type color to its selector', () => {
    const c = resolveGraphColors(echo);
    const styles = buildCytoscapeStyles(c);
    const rule = styles.find((s) => s.selector === 'node[label="Rule"]');
    expect(rule?.style['background-color']).toBe(c.nodeType['Rule']);
  });
});
