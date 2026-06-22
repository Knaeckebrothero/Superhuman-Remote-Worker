import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {AdminUsageService} from '../../../core/services/admin-usage.service';

/**
 * Admin → Usage & Cost (Slice 4). Read-only surface over the usage_events
 * ledger via `GET /api/usage`: LLM tokens + workspace compute, aggregated by
 * (category, unit) over a selectable window. Costs are $0 until rates are
 * configured — the value today is the measured quantities + attribution.
 */
@Component({
  selector: 'app-admin-usage',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SidebarToggleComponent],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">Usage &amp; Cost</h1>
        </div>
        <p class="page-desc">
          LLM tokens and workspace compute, metered from the usage ledger and
          scoped to your visibility (admins see the fleet). Costs read $0 until
          rates are configured — quantities are measured now.
        </p>

        <section class="admin-section">
          <div class="usage-toolbar">
            <div class="filters">
              @for (d of windows; track d) {
                <button
                  type="button"
                  class="filter-chip"
                  [class.active]="windowDays() === d"
                  (click)="setWindow(d)"
                >
                  {{ d }}d
                </button>
              }
            </div>
            <span class="total"
              >Total: {{ fmtCost(summary()?.total_cost_usd ?? 0) }}</span
            >
          </div>

          @if (usage.loading()) {
            <p class="muted">Loading…</p>
          } @else if (summary() && !summary()!.available) {
            <p class="empty-state">
              Usage metering is not enabled on this deployment.
            </p>
          } @else if (!hasData()) {
            <p class="empty-state">No usage recorded in this window.</p>
          } @else {
            <div class="usage-table">
              <div class="usage-header">
                <span class="col-cat">Category</span>
                <span class="col-unit">Unit</span>
                <span class="col-num">Quantity</span>
                <span class="col-num">Events</span>
                <span class="col-num">Cost</span>
              </div>
              @for (r of rows(); track r.category + ':' + r.unit) {
                <div class="usage-row">
                  <span class="col-cat">{{ catLabel(r.category) }}</span>
                  <span class="col-unit mono">{{ r.unit }}</span>
                  <span class="col-num">{{ fmtQty(r.quantity) }}</span>
                  <span class="col-num">{{ r.events }}</span>
                  <span class="col-num">{{ fmtCost(r.cost_usd) }}</span>
                </div>
              }
            </div>
          }
        </section>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: auto;
      }
      .admin-page {
        padding: 32px;
        max-width: var(--content-max-width);
        margin: 0 auto;
        color: var(--text-primary);
      }
      .page-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 8px;
      }
      .page-title {
        font-size: 24px;
        font-weight: 700;
        margin: 0;
      }
      .page-desc {
        font-size: 13px;
        color: var(--text-muted);
        margin: 0 0 32px 0;
      }
      .admin-section {
        background: var(--panel-bg);
        border: 1px solid var(--border-color);
        border-radius: var(--radius-lg);
        padding: 24px;
        margin-bottom: 24px;
      }
      .usage-toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 16px;
      }
      .filters {
        display: inline-flex;
        gap: 6px;
      }
      .filter-chip {
        border: 1px solid var(--border-color);
        background: var(--surface-0);
        color: var(--text-muted);
        border-radius: var(--radius-surface);
        padding: 4px 12px;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
      }
      .filter-chip.active {
        background: var(--accent-color);
        color: var(--on-accent);
        border-color: var(--accent-color);
      }
      .total {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
      }
      .usage-table {
        border: 1px solid var(--border-color);
        border-radius: var(--radius-surface);
        overflow: hidden;
      }
      .usage-header,
      .usage-row {
        display: grid;
        grid-template-columns: 110px 1.4fr 1fr 90px 100px;
        gap: 8px;
        align-items: center;
        padding: 10px 14px;
        font-size: 13px;
      }
      .col-num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .usage-header {
        background: var(--surface-0);
        font-weight: 600;
        font-size: 12px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .usage-row {
        border-top: 1px solid var(--border-color);
      }
      .mono {
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        font-size: 12px;
        color: var(--text-muted);
      }
      .muted {
        color: var(--text-muted);
      }
      .empty-state {
        text-align: center;
        color: var(--text-muted);
        padding: 20px;
        font-size: 13px;
      }
    `,
  ],
})
export class AdminUsageComponent implements OnInit {
  protected readonly usage = inject(AdminUsageService);

  readonly windows = [7, 30, 90] as const;
  readonly windowDays = signal<number>(30);

  readonly summary = computed(() => this.usage.usage());
  readonly rows = computed(() => this.summary()?.by_category ?? []);
  readonly hasData = computed(() => this.rows().length > 0);

  ngOnInit(): void {
    this.usage.loadUsage(this.windowDays());
  }

  setWindow(days: number): void {
    this.windowDays.set(days);
    this.usage.loadUsage(days);
  }

  fmtQty(n: number): string {
    return (n ?? 0).toLocaleString(undefined, {maximumFractionDigits: 2});
  }

  fmtCost(n: number): string {
    return '$' + (n ?? 0).toFixed(2);
  }

  catLabel(c: string): string {
    if (c === 'llm') return 'LLM';
    if (c === 'compute') return 'Compute';
    return c;
  }
}
