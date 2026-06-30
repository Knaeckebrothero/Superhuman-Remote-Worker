import type {KatexOptions} from 'ngx-markdown';

/**
 * Shared KaTeX (auto-render) options for the chat's `<markdown>` components.
 *
 * ngx-markdown forwards this straight to KaTeX's `renderMathInElement` *after* it
 * has rendered + sanitized the markdown (see `renderKatex` in ngx-markdown), so
 * math is typeset as a post-render DOM pass. That ordering is why KaTeX's inline
 * styles survive Angular's HTML sanitizer, and why it coexists with
 * `CitationRefDirective` — both act on the already-rendered element (different
 * selectors), and KaTeX skips `<code>`/`<pre>` via its default `ignoredTags`.
 *
 * Two deliberate choices:
 *   - `delimiters` lists inline `$…$` explicitly. KaTeX auto-render's defaults
 *     cover `$$…$$`, `\[…\]`, and `\(…\)` but NOT inline `$…$`, which LLMs use
 *     constantly (and which markdown would otherwise mangle into emphasis).
 *     `$$` is listed before `$` so the longer display delimiter matches first.
 *   - `throwOnError: false` so a half-arrived formula mid-stream renders as a red
 *     partial instead of throwing, then settles once the token completes.
 */
export const KATEX_OPTIONS: KatexOptions = {
    throwOnError: false,
    delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '\\[', right: '\\]', display: true},
        {left: '\\(', right: '\\)', display: false},
        {left: '$', right: '$', display: false},
    ],
};
