import {
    ChangeDetectionStrategy,
    Component,
    computed,
    effect,
    inject,
    input,
    output,
    signal,
} from '@angular/core';
import {Observable, firstValueFrom} from 'rxjs';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppBadgeComponent} from '../badge/badge.component';
import {AppIconComponent} from '../icon';
import {ToolCardEntity} from '../../core/models/tool-card.model';
import {JobWatchService} from '../../core/services/job-watch.service';
import {ApiService} from '../../core/services/api.service';
import {
    isRunningJobStatus,
    isTerminalJobStatus,
    jobStatusTone,
} from '../../core/util/job-status';

/**
 * Live status + review actions for the job a `create_worker_job` card points at.
 *
 * Kept out of `<app-tool-card>` so that component stays what its docstring
 * promises — source-agnostic and presentational — and so the job dependency
 * (polling, ApiService) is confined to the one card that needs it. Same shape
 * as `<app-canvas-tool-card-presentation>`.
 *
 * ## Why an actionable card in history is defensible here
 *
 * This codebase has a documented rule against it. SSE replay once resurrected a
 * dead approve button and the click 409'd
 * (`docs/issues/session_silent_failure_audit.md`); the resulting rule, stated in
 * `persistent-chat.service.ts`, is that such cards are **live-only** and the
 * durable transcript gets a text system message instead, "because the reason is
 * stale."
 *
 * The job card is the exception, and the asymmetry is real: **a permission
 * request is a moment with no durable addressable state; a job is a row with a
 * stable id that can be re-fetched.** So this follows the *canvas* precedent,
 * not the permission one — `canvasToolCardContext()` compares a historical
 * card's recorded state against current live state and disables the button when
 * stale. Concretely, and non-negotiably:
 *
 * - **Actions render from a fresh `getJob()`, never from the transcript.** The
 *   card shows nothing actionable until the watcher has a current row, and each
 *   button is gated on the *current* status — so the resurrected-dead-button
 *   failure is prevented structurally rather than caught after the fact.
 * - **The remaining race settles itself.** If the agent approves a job in the
 *   second before the user clicks, the post-action refresh corrects the card.
 *
 * Design: docs/features/unified_tool_cards.md (slice 4).
 */
@Component({
    selector: 'app-job-tool-card-panel',
    standalone: true,
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [TranslocoPipe, AppBadgeComponent, AppIconComponent],
    template: `
    @if (job()) {
      <div class="jc">
        <div class="jc__row">
          <app-badge [tone]="tone()" size="xs">{{ statusLabel() }}</app-badge>
          <span class="jc__id" [title]="entity().id">{{ shortId() }}</span>
          @if (running()) {
            <app-icon size="xs" class="jc__spin">progress_activity</app-icon>
          }
        </div>

        @if (summary(); as s) {
          <p class="jc__summary">{{ s }}</p>
        }

        <div class="jc__actions">
          @if (canApprove()) {
            <button type="button" class="jc__btn jc__btn--primary"
                    [disabled]="busy()" (click)="approve()">
              {{ 'toolCard.job.approve' | transloco }}
            </button>
          }
          @if (canOpenDiff()) {
            <button type="button" class="jc__btn" [disabled]="busy()"
                    (click)="diffRequested.emit(entity().id)">
              {{ 'toolCard.job.openDiff' | transloco }}
            </button>
          }
          @if (canCancel()) {
            <button type="button" class="jc__btn jc__btn--danger"
                    [disabled]="busy()" (click)="cancel()">
              {{ 'toolCard.job.cancel' | transloco }}
            </button>
          }
        </div>
      </div>
    }
  `,
    styles: `
    :host { display: block; }
    .jc { display: flex; flex-direction: column; gap: 6px; padding: 6px 0 2px; }
    .jc__row { display: flex; align-items: center; gap: 6px; }
    .jc__id { font-family: var(--font-mono, monospace); font-size: 10.5px; opacity: 0.6; }
    .jc__spin { animation: jc-spin 1.4s linear infinite; opacity: 0.7; }
    @keyframes jc-spin { to { transform: rotate(360deg); } }
    .jc__summary {
      margin: 0; font-size: 12px; line-height: 1.45; opacity: 0.85;
      white-space: pre-wrap; overflow-wrap: anywhere;
    }
    .jc__actions { display: flex; flex-wrap: wrap; gap: 6px; }
    .jc__btn {
      padding: 3px 10px; font-size: 11.5px; border-radius: 5px; cursor: pointer;
      border: 1px solid var(--border-color, rgba(127,127,127,0.3));
      background: transparent; color: inherit;
    }
    .jc__btn:hover:not(:disabled) { background: rgba(127,127,127,0.12); }
    .jc__btn:disabled { opacity: 0.5; cursor: default; }
    .jc__btn--primary { border-color: var(--accent-color); color: var(--accent-color); }
    .jc__btn--danger { border-color: var(--danger-color, #e5534b); color: var(--danger-color, #e5534b); }
  `,
})
export class JobToolCardPanelComponent {
    readonly entity = input.required<ToolCardEntity>();
    /** Asks the host to open the job diff drawer it already owns. */
    readonly diffRequested = output<string>();

    private readonly watcher = inject(JobWatchService);
    private readonly api = inject(ApiService);

    protected readonly busy = signal(false);

    constructor() {
        // Subscribe on first render and whenever the card is pointed at a
        // different job. watch() is idempotent per id, so several cards sharing
        // a job share one poller.
        effect(() => this.watcher.watch(this.entity().id));
    }

    protected readonly job = computed(() => {
        // Reading the map signal is what makes this recompute on every poll.
        this.watcher.snapshot();
        return this.watcher.job(this.entity().id);
    });

    protected readonly shortId = computed(() => this.entity().id.slice(0, 8));
    protected readonly tone = computed(() => jobStatusTone(this.job()?.status ?? ''));
    protected readonly statusLabel = computed(() => this.job()?.status ?? '');
    protected readonly running = computed(() => isRunningJobStatus(this.job()?.status));

    /** Frozen-summary text for a job awaiting review; absent while it runs. */
    protected readonly summary = computed(() => {
        const ctx = this.job()?.context as Record<string, unknown> | null | undefined;
        const raw = ctx?.['summary'];
        return typeof raw === 'string' && raw.trim() ? raw.trim() : null;
    });

    // Gating reads the LIVE row only — never the transcript. A card reloaded
    // from history shows no actions until its first poll lands, which is the
    // point: the button state must describe the job now, not when the call ran.
    protected readonly canApprove = computed(
        () => this.job()?.status === 'pending_review',
    );
    protected readonly canCancel = computed(() => !isTerminalJobStatus(this.job()?.status));
    protected readonly canOpenDiff = computed(() => {
        const s = this.job()?.diff_status;
        return s === 'pending' || this.job()?.status === 'pending_review';
    });

    async approve(): Promise<void> {
        await this.run(this.api.approveJob(this.entity().id));
    }

    async cancel(): Promise<void> {
        await this.run(this.api.cancelJob(this.entity().id));
    }

    /**
     * Run one action, then force a refresh so the card settles on the job's real
     * state rather than an optimistic guess.
     *
     * No bespoke error handling on purpose. `ApiService` already catches, logs
     * and raises a toast, returning null — so a second inline error message here
     * would double-report. The audit's "treat the stale click as benign"
     * requirement is met **structurally instead of defensively**: Approve only
     * renders while the *freshly fetched* status is `pending_review`, so the
     * dead-button-resurrected-by-history failure the permission path hit cannot
     * arise. The refresh below closes the remaining window — if the agent
     * approved the job a second before the user did, the card corrects itself.
     */
    private async run(call: Observable<unknown>): Promise<void> {
        this.busy.set(true);
        try {
            await firstValueFrom(call).catch(() => null);
            await this.watcher.refresh(this.entity().id);
        } finally {
            this.busy.set(false);
        }
    }
}
