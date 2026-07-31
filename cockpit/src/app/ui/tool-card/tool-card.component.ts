import {ChangeDetectionStrategy, Component, OnDestroy, computed, inject, input, output, signal} from '@angular/core';
import {toSignal} from '@angular/core/rxjs-interop';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {MarkdownComponent} from 'ngx-markdown';
import {AppIconComponent} from '../icon';
import {AppIconButtonComponent} from '../icon-button';
import {copyText} from '../copy-field';
import {DiffLine} from '../../core/util/line-diff';
import {ToolCardAction, ToolCardStatus, ToolCardView, ToolResult} from '../../core/models/tool-card.model';
import {CanvasService} from '../../core/services/canvas.service';
import {CanvasState} from '../../core/models/canvas.model';
import {CanvasToolCardPresentationComponent} from './canvas-tool-card-presentation.component';
import {JobToolCardPanelComponent} from './job-tool-card-panel.component';
import {ExternalImageDirective} from '../external-image';

/** Lines of a text/code/terminal result shown before "show N more". */
const RESULT_LINE_CAP = 200;
const COPIED_RESET_MS = 2000;

export type CanvasToolCardContext = 'current' | 'currentStage' | 'replaced' | 'unavailable';

/** Historical set_canvas cards can open only the current logical stage. */
export function canvasToolCardContext(
    action: ToolCardAction | undefined,
    current: CanvasState | null,
): CanvasToolCardContext {
    if (!current?.source || current.status === 'cleared') return 'unavailable';
    // Content identity first: the revision advances for reasons that leave the
    // stage unchanged — re-binding a Canvas to a rebuilt workspace bumps it
    // while presenting byte-identical content — and calling that "replaced"
    // would be wrong. Fall back to the revision when either side lacks a hash.
    if (action?.sourceVersion && current.source_version) {
        return action.sourceVersion === current.source_version ? 'current' : 'replaced';
    }
    if (action?.presentationRevision === undefined) return 'currentStage';
    return action.presentationRevision === current.presentation_revision ? 'current' : 'replaced';
}

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
    imports: [
        TranslocoPipe,
        MarkdownComponent,
        ExternalImageDirective,
        AppIconComponent,
        AppIconButtonComponent,
        CanvasToolCardPresentationComponent,
        JobToolCardPanelComponent,
    ],
    template: `
    @if (view().notify; as n) {
      <!-- Officer→user message: a first-class chat bubble, not a collapsed
           call record. Mirrors the user's own speech bubble (the officer is
           *addressing* the user); the receipt line is the delivery record. -->
      <div class="tc-notify" [class.tc-notify--failed]="notifyFailed()">
        <div class="tc-notify__meta">
          <app-icon size="sm" class="tc-notify__icon">campaign</app-icon>
          <span class="tc-notify__from">{{ 'toolCard.notify.from' | transloco }}</span>
          <span [class]="'tc-notify__chip tc-notify__chip--' + n.urgency">
            {{ 'toolCard.notify.urgency.' + n.urgency | transloco }}
          </span>
        </div>
        <div class="tc-notify__bubble">
          @if (n.subject) {
            <div class="tc-notify__subject">{{ n.subject }}</div>
          }
          <div class="tc-notify__body"><markdown [data]="n.body"></markdown></div>
        </div>
        @if (notifyReceipt(); as receipt) {
          <div class="tc-notify__receipt" [class.tc-notify__receipt--error]="notifyFailed()">{{ receipt }}</div>
        }
      </div>
    } @else {
    <details class="tc" [class.tc--error]="status() === 'error'" [class.tc--denied]="status() === 'denied'"
             [attr.open]="open() ? '' : null">
      <summary class="tc__head">
        <app-icon size="sm" class="tc__icon">{{ view().icon }}</app-icon>
        <span class="tc__title">{{ title() }}</span>
        @if (!view().canvasPresentation && view().subtitle) {
          <span class="tc__hint" [title]="view().subtitle">{{ view().subtitle }}</span>
        }
        <span class="tc__status" [class]="'tc__status--' + status()">
          <app-icon size="xs" class="tc__status-icon">{{ statusIcon() }}</app-icon>
          {{ statusLabel() }}
        </span>
        @if (view().action; as action) {
          <app-canvas-tool-card-presentation [presentation]="view().canvasPresentation"
                                             [action]="action"
                                             [contextLabel]="canvasContextLabel()"
                                             [available]="canvasActionAvailable()"
                                             (requested)="requestAction($event)" />
        }
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

    @if (view().entity; as entity) {
      <!-- Outside <details> on purpose. Every other card is a record of a call
           that finished, so hiding its body behind a disclosure is right. A job
           card is a handle on something still running: its status has to be
           readable, and its review actions reachable, without expanding. -->
      <app-job-tool-card-panel [entity]="entity"
                               (diffRequested)="jobDiffRequested.emit($event)" />
    }
    }
  `,
    styleUrl: './tool-card.component.scss',
})
export class AppToolCardComponent implements OnDestroy {
    readonly view = input.required<ToolCardView>();
    /** Force the card open regardless of status (e.g. the debug surface). */
    readonly defaultOpen = input<boolean>(false);
    readonly actionRequested = output<ToolCardAction>();
    /** Job id whose diff the host should open in the drawer it already owns. */
    readonly jobDiffRequested = output<string>();

    private readonly transloco = inject(TranslocoService);
    private readonly canvas = inject(CanvasService);
    private readonly lang = toSignal(this.transloco.langChanges$, {
        initialValue: this.transloco.getActiveLang(),
    });

    protected readonly showFull = signal(false);
    protected readonly copied = signal(false);
    private copiedTimer?: ReturnType<typeof setTimeout>;

    protected readonly status = computed<ToolCardStatus>(() => this.view().status);
    protected readonly canvasActionAvailable = computed(() => {
        const state = this.canvas.state();
        return !!state?.source && state.status !== 'cleared';
    });
    protected readonly canvasContextLabel = computed(() => {
        const context = canvasToolCardContext(this.view().action, this.canvas.state());
        return this.transloco.translate(`toolCard.canvas.${context}`);
    });

    /** Auto-open on a problem; otherwise honour `defaultOpen`. */
    protected readonly open = computed(
        () => this.defaultOpen() || this.status() === 'error' || this.status() === 'denied',
    );

    /** The notify bubble's delivery receipt line: result, error, or "sending". */
    protected readonly notifyReceipt = computed(() => {
        this.lang();
        const receipt = this.view().notify?.receipt || this.view().error || '';
        if (receipt) return receipt;
        const s = this.status();
        if (s === 'pending' || s === 'running') {
            return this.transloco.translate('toolCard.notify.sending');
        }
        // denied without an error text: surface the status rather than nothing
        return s === 'denied' ? this.statusLabel() : '';
    });

    protected readonly notifyFailed = computed(
        () => this.status() === 'error' || this.status() === 'denied',
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

    requestAction(action: ToolCardAction): void {
        if (this.canvasActionAvailable()) this.actionRequested.emit(action);
    }

    ngOnDestroy(): void {
        clearTimeout(this.copiedTimer);
    }
}
