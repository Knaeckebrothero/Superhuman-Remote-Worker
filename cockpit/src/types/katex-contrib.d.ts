// KaTeX's `./contrib/auto-render` subpath ships no type declarations (its export
// map omits `types`), so declare the one function we use — otherwise the dynamic
// import in katex-loader.ts fails to resolve under tsc.
declare module 'katex/contrib/auto-render' {
    const renderMathInElement: (element: HTMLElement, options?: unknown) => void;
    export default renderMathInElement;
}
