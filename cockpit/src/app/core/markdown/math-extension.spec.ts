import {describe, expect, it} from 'vitest';
import {Marked} from 'marked';
import {mathExtension} from './math-extension';

/** Parse with the same gfm + breaks config the app uses (app.config.ts), so the
 *  markdown-mangling these tests guard against is actually in play. */
function render(md: string): string {
    const m = new Marked({gfm: true, breaks: true});
    m.use(mathExtension());
    return m.parse(md) as string;
}

describe('mathExtension', () => {
    it('normalizes \\[…\\] to $$…$$ and keeps the backslash markdown would strip', () => {
        const html = render('\\[ E = \\frac{a}{b} \\]');
        expect(html).toContain('$$E = \\frac{a}{b}$$');
    });

    it('normalizes \\(…\\) to inline $…$', () => {
        const html = render('Euler: \\(e^{i\\pi} + 1 = 0\\).');
        expect(html).toContain('$e^{i\\pi} + 1 = 0$');
    });

    it('collapses a multi-line $$ block to one line — no <br>, & escaped, \\\\ kept', () => {
        const md = [
            '$$',
            '\\begin{aligned}',
            '(a+b)^2 &= a^2 + 2ab + b^2 \\\\',
            '\\frac{d}{dx}\\left[ x^n \\right] &= n x^{n-1}',
            '\\end{aligned}',
            '$$',
        ].join('\n');
        const html = render(md);
        expect(html).toContain('$$\\begin{aligned}');
        expect(html).toContain('\\end{aligned}$$');
        expect(html).toContain('&amp;= a^2'); // alignment char escaped, not eaten
        expect(html).toContain('\\\\ \\frac'); // LaTeX line break survived
        expect(html).not.toContain('<br>'); // delimiters not fragmented
    });

    it('protects the inline body from markdown emphasis', () => {
        const html = render('$a*b*c$');
        expect(html).toContain('$a*b*c$');
        expect(html).not.toContain('<em>');
    });

    it('protects backslash-escaped punctuation inside a formula', () => {
        const html = render('$$\\{ x \\mid x > 0 \\}$$');
        expect(html).toContain('$$\\{ x \\mid x &gt; 0 \\}$$');
    });

    it('leaves math inside inline code alone', () => {
        const html = render('use `$x$` literally');
        expect(html).toContain('<code>$x$</code>');
    });

    it('passes already-correct $$…$$ through unchanged', () => {
        expect(render('$$x = 1$$')).toContain('$$x = 1$$');
    });
});
