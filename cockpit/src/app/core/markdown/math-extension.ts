import {MarkedExtension} from 'marked';

/**
 * Marked extension that protects LaTeX math from the markdown layer.
 *
 * KaTeX runs as a post-render DOM pass (ngx-markdown's `renderMathInElement`,
 * configured in katex-options.ts), so by the time it sees the text, markdown has
 * already mangled the source:
 *   - `\[`, `\]`, `\(`, `\)` are treated as escaped punctuation and lose their
 *     backslash → KaTeX's `\[…\]` / `\(…\)` delimiters never match;
 *   - `breaks: true` turns the newlines inside a multi-line `$$…$$` block into
 *     `<br>`, fragmenting the delimiters across text nodes so auto-render can't
 *     pair them; and `\\`, `\{`, `\}` inside a formula lose their backslash too.
 *
 * This extension tokenizes math *during* the marked parse — marked runs custom
 * inline tokenizers before its built-in escape/emphasis/break rules (the same
 * priority `citationExtension` relies on), so the body is captured verbatim. It
 * then re-emits the math with normalized dollar delimiters (`\[`/`\(` → `$$`/`$`)
 * on a single line, HTML-escaping the body — notably `&`, which `aligned` uses
 * for column alignment. The downstream `renderMathInElement` pass then renders
 * the clean `$$…$$` / `$…$`.
 *
 * Why hand off as text instead of calling `katex.renderToString()` here: KaTeX's
 * markup leans on inline `style` attributes that Angular's HTML sanitizer strips
 * from ngx-markdown's output, which would break alignment. The DOM-based
 * auto-render pass runs *after* sanitization, so it is safe — and that is the
 * pass we feed.
 *
 * Inline level is sufficient: with `breaks: true` every block ends up inside a
 * paragraph, so block display math (`$$` on its own line) is reached by the
 * inline lexer anyway. The display tokenizers span newlines (`[\s\S]`) to capture
 * multi-line blocks. Math inside code is untouched — marked does not inline-parse
 * code spans/fences, and KaTeX's default `ignoredTags` skips `code`/`pre` too.
 *
 * `KATEX_OPTIONS` still lists `\[`/`\(` as delimiters, but those are now dead
 * (this extension normalizes them to `$$`/`$` first); the live delimiters are the
 * dollar forms.
 */
export function mathExtension(): MarkedExtension {
    return {
        extensions: [
            {
                name: 'math',
                level: 'inline',
                start(src: string) {
                    const m = src.match(/\$\$|\\\[|\\\(|\$/);
                    return m ? m.index : undefined;
                },
                tokenizer(src: string) {
                    // Display first so `$$` wins over `$`. `\[…\]` and `\(…\)`
                    // span newlines; inline `$…$` stays on one line.
                    const display =
                        /^\$\$([\s\S]+?)\$\$/.exec(src) ?? /^\\\[([\s\S]+?)\\\]/.exec(src);
                    if (display) {
                        return {type: 'math', raw: display[0], body: display[1], display: true};
                    }
                    const inline =
                        /^\\\(([\s\S]+?)\\\)/.exec(src) ?? /^\$([^$\n]+?)\$/.exec(src);
                    if (inline) {
                        return {type: 'math', raw: inline[0], body: inline[1], display: false};
                    }
                    return undefined;
                },
                renderer(token: any) {
                    // Collapse newlines so `breaks` can't fragment the delimiters,
                    // and escape HTML so the re-emitted text is valid (and `&`
                    // alignment survives as `&amp;` → `&` in textContent).
                    const body = escapeHtml(token.body).replace(/[\r\n]+/g, ' ').trim();
                    const fence = token.display ? '$$' : '$';
                    return `${fence}${body}${fence}`;
                },
            },
        ],
    };
}

function escapeHtml(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
