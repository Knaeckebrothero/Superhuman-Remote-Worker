import {ChangeDetectionStrategy, Component, computed, input} from '@angular/core';
import {TranslocoModule} from '@jsverse/transloco';
import {
  Job,
  JobProgress,
  JobUsage,
  WorkspaceContractProjection,
} from '../../core/models/api.model';
import {JobSummary} from '../../core/models/audit.model';
import {AppBadgeComponent} from '../../ui/badge';
import {AppSpinnerComponent} from '../../ui/spinner';

/** Everything the panel loads lazily for one job, plus its load state. */
export interface JobDetailState {
  loading: boolean;
  /** Set only when every lazy call failed — a partial load still renders. */
  error: boolean;
  detail: Job | null;
  usage: JobUsage | null;
  progress: JobProgress | null;
}

/**
 * The model a job actually ran on, or null when the client cannot know it.
 *
 * `config_override.llm.model` is the only reliable source: `resolved_config`
 * comes back empty for ordinary jobs (checked against a real job on k3d), and
 * the list payload carries neither. When no override is set the model is
 * whatever the expert's config resolves to server-side, which the client cannot
 * see — so this returns null and the panel says "expert default" rather than
 * inventing a name.
 */
export function jobModelLabel(detail: Job | null): string | null {
  const raw = detail?.config_override;
  const override = typeof raw === 'string' ? safeParse(raw) : raw;
  const llm = (override as Record<string, unknown> | null)?.['llm'];
  const model = (llm as Record<string, unknown> | null)?.['model'];
  return typeof model === 'string' && model.trim() ? model.trim() : null;
}

function safeParse(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

/** Thousands-separated integer, or an em dash when there is nothing to show. */
export function formatCount(value: number | null | undefined): string {
  return value == null ? '—' : value.toLocaleString();
}

export function formatDurationSeconds(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/**
 * How a cost figure should read, given that "unknown" and "zero" are different
 * claims and the endpoint distinguishes four states.
 *
 * `amount` is null whenever no number may be shown; the caller renders the
 * `reasonKey` message instead. `isFloor` marks a partially-priced job, whose
 * real cost is strictly above what is displayed — rendering that bare would
 * understate it silently.
 */
export interface CostDisplay {
  amount: number | null;
  isFloor: boolean;
  reasonKey: string | null;
}

export function costDisplay(usage: JobUsage | null): CostDisplay {
  if (!usage) return {amount: null, isFloor: false, reasonKey: 'jobs.detail.costUnknown'};
  switch (usage.state) {
    case 'unavailable':
      return {amount: null, isFloor: false, reasonKey: 'jobs.detail.costMeteringOff'};
    case 'predates_ledger':
      return {amount: null, isFloor: false, reasonKey: 'jobs.detail.costPredatesLedger'};
    case 'no_usage':
      return {amount: null, isFloor: false, reasonKey: 'jobs.detail.costNoUsage'};
  }
  if (usage.cost.usd == null) {
    // Metered, but nothing carried a rate — the common case for self-hosted
    // models. Tokens are still real and are shown alongside.
    return {amount: null, isFloor: false, reasonKey: 'jobs.detail.costUnpriced'};
  }
  return {amount: usage.cost.usd, isFloor: !usage.cost.complete, reasonKey: null};
}

/**
 * Which workspace backend the job actually got, from the safe projection the
 * list row already carries. Falls back down the chain because a job that never
 * dispatched has only a requested backend.
 */
export function workspaceBackendLabel(
  contract: WorkspaceContractProjection | null | undefined,
): string | null {
  if (!contract) return null;
  const backend =
    contract.effective_backend || contract.assigned_backend || contract.requested_backend;
  return backend ? String(backend) : null;
}

/** Small costs need more than 2 decimals to avoid rendering as $0.00. */
export function formatUsd(amount: number): string {
  if (amount === 0) return '$0.00';
  if (amount < 0.01) return `$${amount.toPrecision(2)}`;
  return `$${amount.toFixed(2)}`;
}

@Component({
  selector: 'app-job-detail-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoModule, AppBadgeComponent, AppSpinnerComponent],
  template: `
    <div class="detail-panel">
      @if (job().description) {
        <p class="detail-description">{{ job().description }}</p>
      }

      <div class="detail-grid">
        <div class="fact">
          <span class="fact-label">{{ 'jobs.detail.origin' | transloco }}</span>
          <span class="fact-value">
            <app-badge tone="neutral" size="xs">{{ job().origin || 'user' }}</app-badge>
          </span>
        </div>
        <div class="fact">
          <span class="fact-label">{{ 'jobs.detail.expert' | transloco }}</span>
          <span class="fact-value">{{ job().config_name || '—' }}</span>
        </div>
        <div class="fact">
          <span class="fact-label">{{ 'jobs.detail.model' | transloco }}</span>
          <span class="fact-value" [class.muted]="!modelLabel()">
            {{ modelLabel() || ('jobs.detail.modelDefault' | transloco) }}
          </span>
        </div>
        <div class="fact">
          <span class="fact-label">{{ 'jobs.detail.workspace' | transloco }}</span>
          <span class="fact-value" [class.muted]="!workspaceLabel()">
            {{ workspaceLabel() || ('jobs.detail.unknown' | transloco) }}
          </span>
        </div>
        <div class="fact">
          <span class="fact-label">{{ 'jobs.detail.duration' | transloco }}</span>
          <span class="fact-value" [class.muted]="!durationLabel()">
            {{ durationLabel() || ('jobs.detail.unknown' | transloco) }}
          </span>
        </div>
        <div class="fact">
          <span class="fact-label">{{ 'jobs.detail.auditSteps' | transloco }}</span>
          <span class="fact-value">{{ formatCount(job().audit_count) }}</span>
        </div>
      </div>

      @if (data()?.loading) {
        <div class="detail-loading">
          <app-spinner size="sm" />
          <span>{{ 'jobs.detail.loading' | transloco }}</span>
        </div>
      } @else {
        <div class="usage-block">
          <div class="usage-head">
            <span class="usage-title">{{ 'jobs.detail.usage' | transloco }}</span>
            @if (usage()?.freshness?.live) {
              <span class="usage-note">{{ 'jobs.detail.usageLive' | transloco }}</span>
            }
          </div>

          <div class="usage-figures">
            <div class="figure">
              <span class="figure-value">{{ formatCount(usage()?.llm?.total_tokens) }}</span>
              <span class="figure-label">{{ 'jobs.detail.tokens' | transloco }}</span>
            </div>
            <div class="figure">
              @if (cost().amount !== null) {
                <span class="figure-value">
                  {{ formatUsd(cost().amount!) }}
                  @if (cost().isFloor) {
                    <span class="figure-qualifier" [title]="'jobs.detail.costFloorHint' | transloco"
                      >+</span
                    >
                  }
                </span>
              } @else {
                <span class="figure-value muted">{{ 'jobs.detail.unknown' | transloco }}</span>
              }
              <span class="figure-label">{{ 'jobs.detail.cost' | transloco }}</span>
            </div>
          </div>

          @if (cost().reasonKey; as reason) {
            <p class="usage-reason">{{ reason | transloco }}</p>
          } @else if (cost().isFloor) {
            <p class="usage-reason">
              {{
                'jobs.detail.costFloor'
                  | transloco: {priced: usage()!.cost.priced_events, total: usage()!.cost.events}
              }}
            </p>
          }

          @if (perModel().length > 0) {
            <ul class="usage-models">
              @for (row of perModel(); track row.resource) {
                <li>
                  <span class="model-name">{{ row.resource }}</span>
                  <span class="model-tokens">{{ formatCount(row.tokens) }}</span>
                </li>
              }
            </ul>
          }
        </div>
      }

      @if (childCount() > 0) {
        <p class="detail-children">
          {{ 'jobs.detail.children' | transloco: {count: childCount()} }}
        </p>
      }
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .detail-panel {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding: 12px 16px 14px;
        background: var(--surface-1);
        border-radius: var(--radius-control);
      }
      .detail-description {
        margin: 0;
        color: var(--text-primary);
        font-size: 13px;
        line-height: 1.55;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .detail-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 10px 18px;
      }
      .fact {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .fact-label {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        color: var(--text-muted);
      }
      .fact-value {
        font-size: 13px;
        color: var(--text-primary);
        overflow-wrap: anywhere;
      }
      .muted {
        color: var(--text-muted);
      }
      .detail-loading {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        color: var(--text-muted);
      }
      .usage-block {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding-top: 10px;
        border-top: 1px solid var(--border-color);
      }
      .usage-head {
        display: flex;
        align-items: baseline;
        gap: 8px;
      }
      .usage-title {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        color: var(--text-muted);
      }
      .usage-note {
        font-size: 11px;
        color: var(--text-muted);
      }
      .usage-figures {
        display: flex;
        gap: 28px;
      }
      .figure {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .figure-value {
        font-size: 18px;
        font-weight: 600;
        color: var(--text-primary);
        font-variant-numeric: tabular-nums;
      }
      .figure-qualifier {
        font-size: 13px;
        color: var(--text-muted);
        cursor: help;
      }
      .figure-label {
        font-size: 11px;
        color: var(--text-muted);
      }
      .usage-reason {
        margin: 0;
        font-size: 12px;
        color: var(--text-muted);
        line-height: 1.5;
      }
      .usage-models {
        margin: 0;
        padding: 0;
        list-style: none;
        display: flex;
        flex-direction: column;
        gap: 3px;
      }
      .usage-models li {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        font-size: 12px;
        color: var(--text-secondary);
      }
      .model-name {
        font-family: ui-monospace, monospace;
        overflow-wrap: anywhere;
      }
      .model-tokens {
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      .detail-children {
        margin: 0;
        font-size: 12px;
        color: var(--text-muted);
      }
    `,
  ],
})
export class JobDetailPanelComponent {
  readonly job = input.required<JobSummary>();
  /** Null until the lazy load for this job has been kicked off. */
  readonly data = input<JobDetailState | null>(null);
  /**
   * Children the SERVER returned for this root — i.e. the ones matching the
   * current filter, not every child that exists. The copy says so; a bare count
   * here would repeat the exact overstatement the list avoids elsewhere.
   */
  readonly childCount = input(0);

  protected readonly formatCount = formatCount;
  protected readonly formatUsd = formatUsd;

  readonly usage = computed(() => this.data()?.usage ?? null);
  readonly cost = computed<CostDisplay>(() => costDisplay(this.usage()));
  readonly modelLabel = computed(() => jobModelLabel(this.data()?.detail ?? null));

  readonly durationLabel = computed(() =>
    formatDurationSeconds(this.data()?.progress?.elapsed_seconds),
  );

  /**
   * Backend from the row's own safe projection — no extra call, and nothing
   * here can leak a workspace lease identity (acceptance 5). Effective wins
   * over assigned wins over requested: the last one is only an intent.
   */
  readonly workspaceLabel = computed(() =>
    workspaceBackendLabel(this.job().workspace_contract),
  );

  /** Token totals per model, biggest first — the per-resource split, folded. */
  readonly perModel = computed(() => {
    const rows = this.usage()?.rows ?? [];
    const byResource = new Map<string, number>();
    for (const row of rows) {
      if (row.category !== 'llm') continue;
      if (!row.unit.endsWith('-token')) continue;
      byResource.set(row.resource, (byResource.get(row.resource) ?? 0) + row.quantity);
    }
    return [...byResource.entries()]
      .map(([resource, tokens]) => ({resource, tokens}))
      .sort((a, b) => b.tokens - a.tokens);
  });
}
