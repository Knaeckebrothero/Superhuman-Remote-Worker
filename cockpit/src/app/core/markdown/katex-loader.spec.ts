import {describe, expect, it} from 'vitest';
import {loadKatex} from './katex-loader';

describe('loadKatex', () => {
    it('memoizes — repeated calls return the same promise (imports once)', () => {
        expect(loadKatex()).toBe(loadKatex());
    });

    it('exposes katex + renderMathInElement as globals for ngx-markdown', async () => {
        await loadKatex();
        const g = globalThis as Record<string, unknown>;
        // ngx-markdown checks `typeof katex` / `typeof renderMathInElement`.
        expect(typeof g['katex']).not.toBe('undefined');
        expect(typeof g['renderMathInElement']).toBe('function');
    });
});
