import {ChangeDetectionStrategy, Component, computed, input} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import cronstrue from 'cronstrue';
import parser from 'cron-parser';

export interface CronSummary {
  valid: boolean;
  humanized: string;
  nextRuns: string[];
  error: string | null;
}

/** Build the cron preview state for a given expression / timezone.
 *
 *  Exported as a pure function so unit tests can exercise it without
 *  TestBed + template compilation. The component's `summary` computed
 *  is a one-line wrapper that feeds the input signals into this. */
export function computeCronSummary(
  cronExpr: string,
  timezone: string,
  previewCount: number,
  now: Date = new Date(),
): CronSummary {
  const expr = cronExpr.trim();
  const tz = timezone || 'UTC';
  if (!expr) {
    return {valid: false, humanized: '', nextRuns: [], error: null};
  }
  let humanized: string;
  try {
    humanized = cronstrue.toString(expr, {use24HourTimeFormat: true});
  } catch (err) {
    return {
      valid: false,
      humanized: '',
      nextRuns: [],
      error: err instanceof Error ? err.message : String(err),
    };
  }

  const runs: string[] = [];
  try {
    const it = parser.parseExpression(expr, {tz, currentDate: now});
    for (let i = 0; i < previewCount; i++) {
      runs.push(it.next().toDate().toLocaleString(undefined, {timeZone: tz}));
    }
  } catch (err) {
    return {
      valid: false,
      humanized,
      nextRuns: [],
      error: err instanceof Error ? err.message : String(err),
    };
  }

  return {valid: true, humanized, nextRuns: runs, error: null};
}

/**
 * Renders a humanized cron description plus the next N firing times in
 * the user's chosen timezone.
 *
 * Pure presentational — no I/O. Reactivity comes from the `cronExpr` and
 * `timezone` inputs; preview recomputes via computed signals.
 */
@Component({
  selector: 'app-cron-preview',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe],
  template: `
    <div class="cron-preview" [class.cron-preview--invalid]="!summary().valid">
      @if (summary().valid) {
        <p class="summary">{{ summary().humanized }}</p>
        <p class="tz-note">
          {{ 'automations.preview.timezoneNote' | transloco: {tz: timezone()} }}
        </p>
        <ol class="next-runs">
          @for (run of summary().nextRuns; track run) {
            <li class="run">{{ run }}</li>
          }
        </ol>
      } @else {
        <p class="error">{{ 'automations.preview.invalid' | transloco }}</p>
        @if (summary().error) {
          <p class="error-detail">{{ summary().error }}</p>
        }
      }
    </div>
  `,
  styles: [
    `
      .cron-preview {
        display: flex;
        flex-direction: column;
        gap: var(--space-2, 0.5rem);
        padding: var(--space-3, 0.75rem);
        background: var(--surface-2, rgba(127, 127, 127, 0.08));
        border-radius: var(--radius-surface, 8px);
        font-size: 0.9rem;
      }
      .cron-preview--invalid {
        background: var(--surface-danger, rgba(180, 60, 60, 0.08));
      }
      .summary {
        margin: 0;
        font-weight: 600;
      }
      .tz-note {
        margin: 0;
        opacity: 0.75;
        font-size: 0.85rem;
      }
      .next-runs {
        margin: 0;
        padding-left: 1.25rem;
        list-style: decimal;
        opacity: 0.85;
      }
      .run {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 0.85rem;
      }
      .error {
        margin: 0;
        font-weight: 600;
        color: var(--text-danger, #b00020);
      }
      .error-detail {
        margin: 0;
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 0.8rem;
        opacity: 0.75;
      }
    `,
  ],
})
export class CronPreviewComponent {
  /** The cron expression to humanize and preview. */
  readonly cronExpr = input<string>('');
  /** IANA timezone name; used both for cron-parser and the user-visible note. */
  readonly timezone = input<string>('UTC');
  /** How many future fires to show. */
  readonly previewCount = input<number>(5);

  readonly summary = computed<CronSummary>(() =>
    computeCronSummary(this.cronExpr(), this.timezone(), this.previewCount()),
  );
}
