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
    asRecord,
    canResumeJob,
    effectiveJobStatus,
    jobStatusLabelKey,
    isRunningJobStatus,
    isTerminalJobStatus,
    jobStatusTone,
} from '../../core/util/job-status';

/**
 * Live status + review actions for the job a `create_job` card points at.
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
 * (`knowledge-base/knowledge/issues/session_silent_failure_audit.md`); the resulting rule, stated in
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
 * Design: knowledge-base/knowledge/features/unified_tool_cards.md (slice 4).
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
          <!-- The status in the product's words ("Pending Review"), not the
               database enum. jobs.status.* already existed and is what the Jobs
               page shows; this card was printing the raw value for the same row.
               Unknown statuses fall back to the raw value rather than to a bare
               i18n key. -->
          <app-badge [tone]="tone()" size="xs">
            {{ statusKey() ? (statusKey()! | transloco) : rawStatus() }}
          </app-badge>
          <span class="jc__id" [title]="entity().id">{{ shortId() }}</span>
          @if (running()) {
            <app-icon size="xs" class="jc__spin">progress_activity</app-icon>
          }
        </div>

        @if (summary(); as s) {
          <p class="jc__summary">{{ s }}</p>
        }

        <!-- Composing replaces the action row rather than sitting under it, so
             the only "cancel" on screen means "cancel writing" — next to a
             "Cancel job" button it would be a one-click accident. -->
        @if (composing()) {
          <div class="jc__compose">
            <textarea class="jc__input" rows="3" [value]="feedback()"
                      [disabled]="busy()"
                      [placeholder]="'toolCard.job.feedbackPlaceholder' | transloco"
                      (input)="feedback.set($any($event.target).value)"
                      (keydown.control.enter)="resumeWithFeedback()"
                      (keydown.meta.enter)="resumeWithFeedback()"></textarea>
            <div class="jc__actions">
              <!-- canResume() again, not just at open time: a poll can land
                   mid-typing and take the job somewhere unresumable (the agent
                   approves it). Same rule as every other button here — gated on
                   the status *now*. The draft stays on screen rather than being
                   yanked; only the dead action is unclickable. -->
              <button type="button" class="jc__btn jc__btn--primary"
                      [disabled]="busy() || !feedback().trim() || !canResume()"
                      (click)="resumeWithFeedback()">
                {{ 'toolCard.job.feedbackSubmit' | transloco }}
              </button>
              <button type="button" class="jc__btn" [disabled]="busy()"
                      (click)="composing.set(false)">
                {{ 'toolCard.job.feedbackDismiss' | transloco }}
              </button>
            </div>
          </div>
        } @else {
          <div class="jc__actions">
            @if (canApprove()) {
              <button type="button" class="jc__btn jc__btn--primary"
                      [disabled]="busy()" (click)="approve()">
                {{ 'toolCard.job.approve' | transloco }}
              </button>
            }
            @if (canResume()) {
              <button type="button" class="jc__btn" [disabled]="busy()"
                      (click)="composing.set(true)">
                {{ 'toolCard.job.resumeWithFeedback' | transloco }}
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
        }
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
    .jc__compose { display: flex; flex-direction: column; gap: 6px; }
    .jc__input {
      width: 100%; box-sizing: border-box; resize: vertical;
      padding: 6px 8px; font: inherit; font-size: 12px; line-height: 1.45;
      border-radius: var(--radius-control); color: inherit;
      border: 1px solid var(--border-color);
      background: var(--surface-0);
    }
    .jc__input:focus { outline: 1px solid var(--accent-color); outline-offset: -1px; }
    .jc__input:disabled { opacity: 0.5; }
    .jc__btn {
      padding: 4px 10px; min-height: 24px; font-size: 11.5px;
      border-radius: var(--radius-control); cursor: pointer;
      border: 1px solid var(--border-color);
      background: transparent; color: inherit;
    }
    .jc__btn:hover:not(:disabled) { background: var(--hover); }
    .jc__btn:disabled { opacity: 0.5; cursor: default; }
    /* Primary is FILLED and destructive is TINTED, rather than both being
       outlines in different hues. Two reasons: it gives the row an actual
       hierarchy (the recommended action reads as the recommended action), and
       --danger === --accent-color in the Roman themes, so hue alone cannot tell
       Approve from Cancel job. Weight can. Mirrors the shared button component's
       'primary' and 'warning' variants. */
    .jc__btn--primary {
      background: var(--accent-color); color: var(--on-accent);
      border-color: var(--accent-color);
    }
    .jc__btn--danger {
      background: var(--danger-tint); color: var(--danger); border-color: transparent;
    }
    /* Filled/tinted buttons keep their own background on hover — the generic
       .jc__btn:hover above would repaint them with the neutral --hover. */
    .jc__btn--primary:hover:not(:disabled) { background: var(--accent-color); filter: brightness(1.08); }
    .jc__btn--danger:hover:not(:disabled) { background: var(--danger-tint); filter: brightness(1.08); }
  `,
})
export class JobToolCardPanelComponent {
    readonly entity = input.required<ToolCardEntity>();
    /** Asks the host to open the job diff drawer it already owns. */
    readonly diffRequested = output<string>();

    private readonly watcher = inject(JobWatchService);
    private readonly api = inject(ApiService);

    protected readonly busy = signal(false);
    /** Whether the feedback composer is open. Collapsed by default — the card
     *  sits inline in a transcript and a permanently open textarea would make
     *  every historical job call three lines taller. */
    protected readonly composing = signal(false);
    protected readonly feedback = signal('');

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
    protected readonly effectiveStatus = computed(() => effectiveJobStatus(this.job()));
    protected readonly tone = computed(() => jobStatusTone(this.effectiveStatus()));

    /**
     * The status in the product's own words — "Pending Review", not
     * `pending_review`.
     *
     * `jobs.status.*` already exists in both locales and is what the Jobs page
     * shows; this card was rendering the raw database enum, underscore and all,
     * for the same row. Falls back to the raw value when a status has no
     * translation — `waiting_for_reply` is in the DB CHECK constraint but not in
     * the locale files, so that gap is real and shows up as `waiting_for_reply`
     * rather than as a bare i18n key.
     */
    protected readonly statusKey = computed(() => jobStatusLabelKey(this.effectiveStatus()));
    protected readonly rawStatus = computed(() => this.effectiveStatus());
    protected readonly running = computed(() => isRunningJobStatus(this.job()?.status));

    /**
     * Summary of what the job produced, shown once it has one.
     *
     * Reads `freeze_data.summary` — the same source the session-wake payload
     * formatter uses. Two things the dev live gate (2026-07-29) caught here,
     * both of which failed *silently*:
     *
     * 1. **It is not in `context`.** An earlier draft read `context.summary`,
     *    which simply does not exist; the freeze blob is where a job records
     *    what it did.
     * 2. **JSONB comes back as a STRING, not an object.** `GET /api/jobs/{id}`
     *    returns `context` (and `freeze_data`) as raw JSON text — the
     *    orchestrator-wide asyncpg behaviour — while the cockpit `Job` model
     *    types them as `Record<string, any>`. So indexing straight into the
     *    field type-checks, compiles, and always yields `undefined` at runtime.
     *    Hence {@link asRecord}.
     *
     * Absent while the job runs, and absent again after approval (which clears
     * `freeze_data`) — in both cases the card just shows status, which is
     * correct.
     */
    protected readonly summary = computed(() => {
        const raw = asRecord(this.job()?.freeze_data)?.['summary'];
        return typeof raw === 'string' && raw.trim() ? raw.trim() : null;
    });

    // Gating reads the LIVE row only — never the transcript. A card reloaded
    // from history shows no actions until its first poll lands, which is the
    // point: the button state must describe the job now, not when the call ran.
    protected readonly canApprove = computed(
        () => this.job()?.status === 'pending_review',
    );
    protected readonly canCancel = computed(() => !isTerminalJobStatus(this.job()?.status));
    protected readonly canResume = computed(() => canResumeJob(this.job()));
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
     * Hand the job back with guidance — the third review outcome.
     *
     * Approve and Cancel are "ship it" and "kill it"; the common answer to a
     * job that stopped for review is neither, it is "close, but do X". Without
     * this the user has to leave the transcript for the Jobs page, which is the
     * trip this card exists to remove (knowledge-base/knowledge/features/unified_tool_cards.md).
     *
     * The draft survives a failed send. `run()` reports the API's result rather
     * than swallowing it, so a resume rejected by the resume PEP (403 on a
     * grant denial, 409 on an unresolvable stored config — main.py:13597) leaves
     * the composer open with the text still in it. Clearing on failure would
     * discard what the user wrote and show only a toast.
     */
    async resumeWithFeedback(): Promise<void> {
        const text = this.feedback().trim();
        if (!text || this.busy() || !this.canResume()) return;
        const result = await this.run(this.api.resumeJob(this.entity().id, text));
        if (result === null) return;
        this.feedback.set('');
        this.composing.set(false);
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
     *
     * Returns the call's result (null on failure, since `ApiService` maps errors
     * to null) so a caller that owns user-entered state can decide whether to
     * clear it — see {@link resumeWithFeedback}. Buttons that own nothing ignore
     * it, as before.
     */
    private async run(call: Observable<unknown>): Promise<unknown> {
        this.busy.set(true);
        try {
            const result = await firstValueFrom(call).catch(() => null);
            await this.watcher.refresh(this.entity().id);
            return result;
        } finally {
            this.busy.set(false);
        }
    }
}
