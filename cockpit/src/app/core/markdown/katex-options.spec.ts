import {describe, expect, it} from 'vitest';
import {KATEX_OPTIONS} from './katex-options';

describe('KATEX_OPTIONS', () => {
    it('renders half-arrived formulas instead of throwing (streaming-safe)', () => {
        // A formula that is still being streamed (e.g. `$$\frac{` so far) must not
        // throw and blank the message — KaTeX should emit a partial and recover.
        expect(KATEX_OPTIONS.throwOnError).toBe(false);
    });

    it('enables inline $…$, which KaTeX auto-render omits by default', () => {
        const inline = KATEX_OPTIONS.delimiters?.find(d => d.left === '$' && d.right === '$');
        expect(inline).toBeDefined();
        expect(inline?.display).toBe(false);
    });

    it('matches $$ display math before $ inline so the longer delimiter wins', () => {
        const lefts = (KATEX_OPTIONS.delimiters ?? []).map(d => d.left);
        expect(lefts).toContain('$$');
        expect(lefts.indexOf('$$')).toBeLessThan(lefts.indexOf('$'));
    });

    it('covers both display ($$, \\[) and inline (\\(, $) delimiters', () => {
        const lefts = new Set((KATEX_OPTIONS.delimiters ?? []).map(d => d.left));
        expect(lefts).toEqual(new Set(['$$', '\\[', '\\(', '$']));
    });

    it('flags block delimiters as display and inline delimiters as inline', () => {
        const display = new Map((KATEX_OPTIONS.delimiters ?? []).map(d => [d.left, d.display]));
        expect(display.get('$$')).toBe(true);
        expect(display.get('\\[')).toBe(true);
        expect(display.get('\\(')).toBe(false);
        expect(display.get('$')).toBe(false);
    });
});
