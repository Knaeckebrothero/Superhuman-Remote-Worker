import {ChangeDetectionStrategy, Component, OnDestroy, computed, inject, input, signal} from '@angular/core';
import {toSignal} from '@angular/core/rxjs-interop';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {MarkdownComponent} from 'ngx-markdown';
import {AppIconComponent} from '../icon';
import {AppIconButtonComponent} from '../icon-button';
import {copyText} from '../copy-field';
import {DiffLine} from '../../core/util/line-diff';
import {ToolCardStatus, ToolCardView, ToolResult} from '../../core/models/tool-card.model';

/** Lines of a text/code/terminal result shown before "show N more". */
const RESULT_LINE_CAP = 200;
const COPIED_RESET_MS = 2000;

/**
 * Source-agnostic presentational card for one agent tool call. Renders a
 * {@link ToolCardView} (built by the descriptor registry from either the live
 * session stream or the debug audit trail) with a fixed body order: raw tool
 * name → Parameters → Result → Details → Error. Owns its own `<details>`;
 * auto-opens on error/denied. Design: `docs/features/unified_tool_cards.md`.
 */
@Component({
    selector: 'app-tool-card',
    standalone: true,
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [TranslocoPipe, MarkdownComponent, AppIconComponent, AppIconButtonComponent],
    template: `
    <details class="tc" [class.tc--error]="status() === 'error'" [class.tc--denied]="status() === 'denied'"
             [attr.open]="open() ? '' : null">
      <summary class="tc__head">
        <app-icon size="sm" class="tc__icon">{{ view().icon }}</app-icon>
        <span class="tc__title">{{ title() }}</span>
        @if (view().subtitle) {
          <span class="tc__hint" [title]="view().subtitle">{{ view().subtitle }}</span>
        }
        <span class="tc__status" [class]="'tc__status--' + status()">
          <app-icon size="xs" class="tc__status-icon">{{ statusIcon() }}</app-icon>
          {{ statusLabel() }}
        </span>
      </summary>

      <div class="tc__body">
        <div class="tc__toolname">{{ view().tool }}</div>

        @for (p of view().params; track $index) {
          <section class="tc__section">
            <div class="tc__label">{{ p.label }}</div>
            <pre class="tc__code" [class.tc__code--path]="p.kind === 'path'">{{ p.value }}</pre>
          </section>
        }

        @if (view().result; as r) {
          <section class="tc__section">
            <div class="tc__label">
              <span>{{ 'toolCard.sections.result' | transloco }}</span>
              @if (r.language) { <span class="tc__tag">{{ r.language }}</span> }
              @else if (r.diffMode) { <span class="tc__tag">{{ 'toolCard.diff.' + r.diffMode | transloco }}</span> }
              @if (canCopy(r)) {
                <app-icon-button class="tc__copy" variant="ghost" size="sm"
                                 [ariaLabel]="(copied() ? 'common.copied' : 'common.copy') | transloco"
                                 [tooltip]="(copied() ? 'common.copied' : 'common.copy') | transloco"
                                 (clicked)="copy(r)">
                  <app-icon size="sm">{{ copied() ? 'check' : 'content_copy' }}</app-icon>
                </app-icon-button>
              }
            </div>

            @if (r.kind === 'diff') {
              <div class="tc__diff">
                @for (ln of r.diffLines; track $index) {
                  <div class="tc__diff-line" [class.add]="ln.type === 'add'" [class.del]="ln.type === 'del'">
                    <span class="tc__diff-sign">{{ sign(ln.type) }}</span><span class="tc__diff-text">{{ ln.text }}</span>
                  </div>
                }
                @if (r.truncatedLines) {
                  <div class="tc__more-note">{{ 'toolCard.diff.truncated' | transloco:{count: r.truncatedLines} }}</div>
                }
              </div>
            } @else if (r.kind === 'markdown') {
              <div class="tc__md"><markdown [data]="r.content || ''"></markdown></div>
            } @else {
              <pre class="tc__result" [class.tc__result--terminal]="r.kind === 'terminal'">{{ visibleContent(r) }}</pre>
            }

            @if (hiddenLineCount(r) > 0) {
              <button type="button" class="tc__more" (click)="showFull.set(true)">
                {{ 'toolCard.showMore' | transloco:{count: hiddenLineCount(r)} }}
              </button>
            } @else if (showFull() && isCapped(r)) {
              <button type="button" class="tc__more" (click)="showFull.set(false)">
                {{ 'toolCard.showLess' | transloco }}
              </button>
            }
            @if (r.bytesTotal) {
              <div class="tc__more-note">{{ 'toolCard.previewNote' | transloco:{size: formatBytes(r.bytesTotal)} }}</div>
            }
          </section>
        }

        @if (view().details.length) {
          <dl class="tc__details">
            @for (d of view().details; track $index) {
              <div class="tc__detail">
                <dt>{{ d.label }}</dt>
                <dd [class]="'tone-' + (d.tone || 'default')">{{ d.value }}</dd>
              </div>
            }
          </dl>
        }

        @if (view().error) {
          <section class="tc__section">
            <div class="tc__label tc__label--error">{{ 'toolCard.sections.error' | transloco }}</div>
            <pre class="tc__result tc__result--error">{{ view().error }}</pre>
          </section>
        }
      </div>
    </details>
  `,
    styleUrl: './tool-card.component.scss',
})
export class AppToolCardComponent implements OnDestroy {
    readonly view = input.required<ToolCardView>();
    /** Force the card open regardless of status (e.g. the debug surface). */
    readonly defaultOpen = input<boolean>(false);

    private readonly transloco = inject(TranslocoService);
    private readonly lang = toSignal(this.transloco.langChanges$, {
        initialValue: this.transloco.getActiveLang(),
    });

    protected readonly showFull = signal(false);
    protected readonly copied = signal(false);
    private copiedTimer?: ReturnType<typeof setTimeout>;

    protected readonly status = computed<ToolCardStatus>(() => this.view().status);

    /** Auto-open on a problem; otherwise honour `defaultOpen`. */
    protected readonly open = computed(
        () => this.defaultOpen() || this.status() === 'error' || this.status() === 'denied',
    );

    /** Title via `toolCard.titles.<tool>` with the descriptor literal as fallback. */
    protected readonly title = computed(() => {
        this.lang();
        return this.tr(`toolCard.titles.${this.view().tool}`, this.view().title);
    });

    protected readonly statusLabel = computed(() => {
        this.lang();
        return this.tr(`toolCard.status.${this.status()}`, this.status());
    });

    protected readonly statusIcon = computed(() => {
        switch (this.status()) {
            case 'ok':
                return 'check_circle';
            case 'running':
                return 'progress_activity';
            case 'denied':
                return 'block';
            case 'error':
                return 'error';
            default:
                return 'radio_button_unchecked';
        }
    });

    private tr(key: string, fallback: string): string {
        const t = this.transloco.translate(key);
        return t === key ? fallback : t;
    }

    sign(type: DiffLine['type']): string {
        return type === 'add' ? '+' : type === 'del' ? '-' : ' ';
    }

    canCopy(r: ToolResult): boolean {
        return r.kind !== 'diff' && !!r.content;
    }

    /** Result content trimmed to the line cap unless expanded or markdown. */
    visibleContent(r: ToolResult): string {
        const content = r.content ?? '';
        if (this.showFull() || r.kind === 'markdown') return content;
        const nl = this.lineCount(content);
        if (nl <= RESULT_LINE_CAP) return content;
        return content.split('\n').slice(0, RESULT_LINE_CAP).join('\n');
    }

    hiddenLineCount(r: ToolResult): number {
        if (this.showFull() || r.kind === 'diff' || r.kind === 'markdown') return 0;
        return Math.max(0, this.lineCount(r.content ?? '') - RESULT_LINE_CAP);
    }

    isCapped(r: ToolResult): boolean {
        return r.kind !== 'diff' && r.kind !== 'markdown' && this.lineCount(r.content ?? '') > RESULT_LINE_CAP;
    }

    private lineCount(s: string): number {
        if (!s) return 0;
        let n = 1;
        for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) === 10) n++;
        return n;
    }

    formatBytes(n: number): string {
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    }

    async copy(r: ToolResult): Promise<void> {
        const ok = await copyText(r.content ?? '');
        if (!ok) return;
        this.copied.set(true);
        clearTimeout(this.copiedTimer);
        this.copiedTimer = setTimeout(() => this.copied.set(false), COPIED_RESET_MS);
    }

    ngOnDestroy(): void {
        clearTimeout(this.copiedTimer);
    }
}
