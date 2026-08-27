import {AfterViewInit, Directive, ElementRef, OnDestroy, effect, inject} from '@angular/core';
import {MarkdownComponent} from 'ngx-markdown';
import {Subscription} from 'rxjs';
import {PersistentChatService} from '../services/persistent-chat.service';

/**
 * Resolves the inline citation markers produced by `citationExtension` into
 * live, source-linked references.
 *
 * The marked stage emits `<sup class="citation-ref cid-N">N</sup>` for each
 * `[N]` (N == citation id). Applied to every `<markdown>` that renders agent
 * text, this directive runs after render — and again when the thread's
 * citations arrive (they're fetched async) — and, per marker:
 *
 *   - resolved id  → renumbers it 1..k in order of appearance, attaches a source
 *                    popover (native title: name / url / claim / verification),
 *                    and makes web sources click-to-open;
 *   - unresolved id (once citations are loaded) → rendered as the literal `[N]`
 *                    text it was, so a stray `[5]` in prose is never shown as a
 *                    citation. Non-destructive (the `cid-N` class is kept) so it
 *                    still upgrades to a real citation if one loads later.
 *
 * The id is read from the `cid-N` class (survives the HTML sanitizer) rather
 * than the visible text, which the renumber step overwrites.
 */
@Directive({
    selector: 'markdown[appCitationRef]',
    standalone: true,
})
export class CitationRefDirective implements AfterViewInit, OnDestroy {
    private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
    private readonly markdown = inject(MarkdownComponent, {self: true});
    private readonly chat = inject(PersistentChatService);
    private sub?: Subscription;

    constructor() {
        // Re-resolve when the thread's citations load/refresh after render.
        effect(() => {
            this.chat.citationsByCid();
            this.chat.citationsLoaded();
            this.resolve();
        });
    }

    ngAfterViewInit(): void {
        this.resolve();
        this.sub = this.markdown.ready.subscribe(() => this.resolve());
        this.host.nativeElement.addEventListener('click', this.onClick);
    }

    ngOnDestroy(): void {
        this.sub?.unsubscribe();
        this.host.nativeElement.removeEventListener('click', this.onClick);
    }

    private readonly onClick = (ev: Event): void => {
        const target = ev.target as HTMLElement | null;
        const ref = target?.closest('sup.citation-ref') as HTMLElement | null;
        const url = ref?.getAttribute('data-url');
        if (url) {
            ev.preventDefault();
            window.open(url, '_blank', 'noopener');
        }
    };

    private cidOf(el: HTMLElement): number {
        const m = el.className.match(/\bcid-(\d+)\b/);
        return m ? Number(m[1]) : NaN;
    }

    private resolve(): void {
        const refs =
            this.host.nativeElement.querySelectorAll<HTMLElement>('sup.citation-ref');
        if (refs.length === 0) {
            return;
        }
        const byCid = this.chat.citationsByCid();
        const loaded = this.chat.citationsLoaded();
        const display = new Map<number, number>();
        let next = 1;
        refs.forEach((sup) => {
            const cid = this.cidOf(sup);
            if (!Number.isFinite(cid)) {
                return;
            }
            const cit = byCid.get(cid);
            if (cit) {
                let n = display.get(cid);
                if (n === undefined) {
                    n = next++;
                    display.set(cid, n);
                }
                sup.textContent = String(n);
                sup.classList.remove('unresolved');
                sup.classList.add('resolved');
                const lines = [cit.source_name || `source #${cit.source_id}`];
                if (cit.source_identifier) {
                    lines.push(cit.source_identifier);
                }
                if (cit.claim) {
                    lines.push(`“${cit.claim}”`);
                }
                lines.push(`[${cit.verification_status}]`);
                sup.setAttribute('title', lines.join('\n'));
                // Clickable when the source is reachable by URL (web citations);
                // document citations have a path identifier, not a link.
                if (cit.source_identifier && /^https?:\/\//i.test(cit.source_identifier)) {
                    sup.setAttribute('data-url', cit.source_identifier);
                    sup.classList.add('clickable');
                } else {
                    sup.removeAttribute('data-url');
                    sup.classList.remove('clickable');
                }
            } else if (loaded) {
                // Citations are loaded and this id isn't one of them — it was
                // ordinary text. Render it as the literal `[N]` (kept as a <sup>
                // styled flat so it can still upgrade if a citation loads later).
                sup.textContent = `[${cid}]`;
                sup.classList.remove('resolved', 'clickable');
                sup.classList.add('unresolved');
                sup.removeAttribute('data-url');
                sup.removeAttribute('title');
            }
            // else: not loaded yet — leave as-is; the effect re-runs on load.
        });
    }
}
