import {AfterViewInit, Directive, ElementRef, OnDestroy, effect, inject, input} from '@angular/core';
import {MarkdownComponent} from 'ngx-markdown';
import {Subscription} from 'rxjs';
import {KATEX_OPTIONS} from './katex-options';
import {loadKatex} from './katex-loader';

/**
 * Typesets KaTeX math in a `<markdown>` element, sourcing KaTeX lazily.
 *
 * We deliberately do NOT use ngx-markdown's built-in `katex` input: KaTeX is
 * lazy-loaded (katex-loader.ts), but that input is read synchronously at render
 * time and throws before the chunk arrives — and re-rendering once it arrives
 * depends on a change-detection tick firing, which it may not on an idle /
 * disconnected session (the content is already painted, nothing re-triggers CD).
 *
 * Instead this runs `renderMathInElement` imperatively, so it never depends on a
 * CD tick:
 *   - after every markdown (re-)render via `ready` — covers initial paint and
 *     streaming, where ngx-markdown resets innerHTML to raw `$$…$$` each cycle;
 *   - once `loadKatex()` resolves — covers content that painted before KaTeX
 *     finished loading.
 *
 * It's a post-render DOM pass, so KaTeX's inline styles survive Angular's HTML
 * sanitizer, and it composes with `CitationRefDirective` (different selectors).
 * Re-running on an already-typeset element is a no-op — the first pass consumes
 * the `$$…$$` text, so a second pass finds no delimiters. `KATEX_OPTIONS` carries
 * the delimiters (incl. inline `$…$`) and `throwOnError: false`.
 */
@Directive({
    selector: 'markdown[appKatex]',
    standalone: true,
})
export class KatexDirective implements AfterViewInit, OnDestroy {
    private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
    private readonly markdown = inject(MarkdownComponent, {self: true});
    private sub?: Subscription;

    /**
     * While true, skip typesetting: the block is still streaming and its math
     * delimiters may be incomplete, so a lone `$$` would re-typeset on every
     * flush (the visible per-delta height jump). Defaults false, so every
     * existing `appKatex` site is unaffected. When it flips true→false (the
     * block finished streaming) we typeset once — the markdown `[data]` didn't
     * change, only the event status did, so `ready` won't re-fire.
     */
    readonly katexDefer = input(false);

    constructor() {
        let wasDeferred = this.katexDefer();
        effect(() => {
            const deferred = this.katexDefer();
            if (wasDeferred && !deferred) this.typeset();
            wasDeferred = deferred;
        });
    }

    ngAfterViewInit(): void {
        this.sub = this.markdown.ready.subscribe(() => this.typeset());
        loadKatex()
            .then(() => this.typeset())
            .catch((err) => console.error('[katex] failed to load', err));
        this.typeset();
    }

    ngOnDestroy(): void {
        this.sub?.unsubscribe();
    }

    private typeset(): void {
        if (this.katexDefer()) return; // still streaming — defer to the flip
        const renderMathInElement = (globalThis as any).renderMathInElement as
            | ((el: HTMLElement, opts: unknown) => void)
            | undefined;
        // Not loaded yet — the loadKatex().then() above re-runs this once it is.
        renderMathInElement?.(this.host.nativeElement, KATEX_OPTIONS);
    }
}
