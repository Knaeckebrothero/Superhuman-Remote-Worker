import {ChangeDetectionStrategy, Component, computed, inject, input, output, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../icon';
import {ToolCardView} from '../../core/models/tool-card.model';
import {JobWatchService} from '../../core/services/job-watch.service';
import {isTerminalJobStatus} from '../../core/util/job-status';
import {JobToolCardPanelComponent} from './job-tool-card-panel.component';

/**
 * A fan-out of `create_job` calls, rendered as one card with a row per
 * job instead of N stacked cards.
 *
 * ## What this actually fixes
 *
 * Not verbosity — visibility. Job calls used to be foldable, and
 * `pinnedEventIds()` pins only a turn's *last* tool call, so dispatching three
 * jobs rendered a "2× tool calls" chip plus one inline card: two live,
 * actionable cards were hidden behind a counter that gave no hint they existed.
 * `isFoldable()` now excludes job calls and `groupEvents()` batches contiguous
 * ones (`core/models/turn.model.ts`); this is the surface for that group.
 *
 * ## Deviations from the `Delegate-A` mockup, and why
 *
 * The mockup shows per-agent role, step counts, token counts and elapsed time.
 * None of those exist for a worker job — `GET /api/jobs/{id}` gives status,
 * description, `freeze_data.summary` and `diff_status` — so the row carries what
 * is real rather than inventing metrics. It was also drawn for `delegate_work`,
 * where sub-agents have roles; these are peer jobs.
 *
 * It also shows *collapsed* as the resting state. This defaults to **open**: a
 * fan-out is usually 2–3 jobs, the rows are actionable, and auto-collapsing
 * would re-create the very failure above — hiding a job that is waiting on the
 * user. Collapse is available, just not the default, and it is never automatic:
 * a card that closed itself the moment the last job completed would slam shut
 * under someone mid-read.
 *
 * Design: knowledge-base/knowledge/features/unified_tool_cards.md (slice 4, batch grouping).
 */
@Component({
    selector: 'app-job-batch-card',
    standalone: true,
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [TranslocoPipe, AppIconComponent, JobToolCardPanelComponent],
    template: `
    <div class="jb">
      <button type="button" class="jb__head" [attr.aria-expanded]="open()"
              (click)="open.set(!open())">
        <app-icon size="sm" class="jb__chevron">{{ open() ? 'expand_more' : 'chevron_right' }}</app-icon>
        <app-icon size="sm" class="jb__icon">rocket_launch</app-icon>
        <span class="jb__title">{{ 'toolCard.jobBatch.title' | transloco:{count: total()} }}</span>
        <span class="jb__meta">{{ 'toolCard.jobBatch.finished' | transloco:{done: finishedCount(), total: total()} }}</span>
        @if (reviewCount(); as n) {
          <span class="jb__chip jb__review">{{ 'toolCard.jobBatch.review' | transloco:{count: n} }}</span>
        }
        @if (failedCount(); as n) {
          <span class="jb__chip jb__failedChip">{{ 'toolCard.jobBatch.failed' | transloco:{count: n} }}</span>
        }
      </button>

      @if (open()) {
        <div class="jb__rows">
          @for (v of views(); track $index) {
            <div class="jb__row">
              @if (v.subtitle) {
                <div class="jb__label" [title]="v.subtitle">{{ v.subtitle }}</div>
              }
              @if (v.entity; as entity) {
                <app-job-tool-card-panel [entity]="entity"
                                         (diffRequested)="diffRequested.emit($event)" />
              } @else {
                <!-- The call never returned an id: it failed, or was denied.
                     There is no row to watch, so say so instead of rendering an
                     empty panel that polls nothing. -->
                <div class="jb__notCreated">
                  <app-icon size="xs">error</app-icon>
                  <span>{{ v.error || ('toolCard.jobBatch.notCreated' | transloco) }}</span>
                </div>
              }
            </div>
          }
        </div>
      }
    </div>
  `,
    styles: `
    :host { display: block; }
    .jb {
      border: 1px solid var(--border-color);
      border-radius: var(--radius-surface); overflow: hidden;
    }
    .jb__head {
      display: flex; align-items: center; gap: 6px; width: 100%;
      padding: 6px 8px; background: transparent; border: 0; color: inherit;
      font: inherit; font-size: 12px; text-align: left; cursor: pointer;
    }
    .jb__head:hover { background: var(--hover); }
    .jb__chevron, .jb__icon { opacity: 0.7; flex: none; }
    .jb__title { font-weight: 600; }
    .jb__meta { color: var(--text-secondary); font-size: 11.5px; }
    /* Tinted pills, not bare coloured text. --warning on this surface is 2.7:1 —
       below AA, and it was carrying the one signal in the card that means
       "something needs you". A tint block is the shared badge/button treatment
       and reads as a status, not as decoration. */
    .jb__chip {
      font-size: 11px; line-height: 1.5; padding: 0 6px;
      border-radius: var(--radius-pill); white-space: nowrap;
    }
    .jb__review { background: var(--warning-tint); color: var(--warning); }
    .jb__failedChip { background: var(--danger-tint); color: var(--danger); }
    .jb__rows { display: flex; flex-direction: column; }
    .jb__row {
      padding: 6px 8px 8px;
      border-top: 1px solid var(--border-color);
    }
    .jb__label {
      font-size: 12px; line-height: 1.4; margin-bottom: 2px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .jb__notCreated {
      display: flex; align-items: center; gap: 4px;
      font-size: 11.5px; color: var(--danger);
    }
  `,
})
export class JobBatchCardComponent {
    /** One view per job call in the batch, in dispatch order. */
    readonly views = input.required<ToolCardView[]>();
    /** Bubbles up from a row; the host owns the job-diff drawer. */
    readonly diffRequested = output<string>();

    private readonly watcher = inject(JobWatchService);

    protected readonly open = signal(true);

    /**
     * Live rows for the header counts, aligned with {@link views} by index.
     *
     * Read-only: the per-row panels are what call `watch()`, so the header
     * cannot start a poller for a job no row is rendering. Null means "not
     * polled yet" (or no entity), which counts as neither done nor in review —
     * the header stays honest rather than guessing.
     */
    private readonly jobs = computed(() => {
        this.watcher.snapshot();
        return this.views().map((v) => (v.entity ? this.watcher.job(v.entity.id) : null));
    });

    protected readonly total = computed(() => this.views().length);

    /**
     * "Finished", not "done".
     *
     * This counter includes `failed` and `cancelled` — a job that failed has
     * stopped, but calling it done implies it succeeded. A batch of one
     * completed, one failed and one cancelled job read "2/3 done", which is a
     * lie about the outcome. {@link failedCount} carries the bad news
     * separately so the header states the outcome instead of averaging it away.
     */
    protected readonly finishedCount = computed(
        () => this.jobs().filter((j) => j && isTerminalJobStatus(j.status)).length,
    );
    protected readonly reviewCount = computed(
        () => this.jobs().filter((j) => j?.status === 'pending_review').length,
    );
    protected readonly failedCount = computed(
        () => this.jobs().filter((j) => j?.status === 'failed').length,
    );
}
