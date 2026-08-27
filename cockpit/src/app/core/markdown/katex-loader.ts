/**
 * Lazily loads KaTeX (the renderer + its auto-render contrib) and exposes them as
 * the globals ngx-markdown expects: it looks up `katex` / `renderMathInElement`
 * synchronously at render time and throws if they're missing (`renderKatex` in
 * ngx-markdown). That's why the chat gates `<markdown [katex]>` on this promise —
 * math stays off until the globals exist.
 *
 * KaTeX is ~266 KB of JS — too heavy to ship in the initial bundle for an
 * occasional feature — so it's pulled out of angular.json `scripts` and imported
 * on demand here (esbuild splits the dynamic import into a lazy chunk). Memoized:
 * the import runs once per app load.
 *
 * The KaTeX *stylesheet* stays eager (angular.json `styles`, ~23 KB) so typeset
 * math is styled the instant the JS lands — no unstyled-math flash — and the
 * woff2 fonts still download only when a formula actually renders.
 */
let katexPromise: Promise<void> | null = null;

export function loadKatex(): Promise<void> {
    return (katexPromise ??= Promise.all([
        import('katex'),
        import('katex/contrib/auto-render'),
    ]).then(([katexMod, autoRenderMod]) => {
        const g = globalThis as any;
        // `??=` so we never clobber a global another loader already set; the
        // `.default ?? mod` covers both `export default` and `export =` interop.
        g.katex ??= (katexMod as any).default ?? katexMod;
        g.renderMathInElement ??= (autoRenderMod as any).default ?? autoRenderMod;
    }));
}
