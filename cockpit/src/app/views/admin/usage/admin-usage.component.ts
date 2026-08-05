import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  OnDestroy,
  OnInit,
  signal,
} from '@angular/core';
import {SidebarToggleComponent} from '../../../shell/sidebar-toggle/sidebar-toggle.component';
import {
  AdminUsageService,
  BreakdownDim,
  UsageRowV2,
  UsageTsPoint,
  UsageWindow,
} from '../../../core/services/admin-usage.service';
import {ApiService} from '../../../core/services/api.service';
import {UserService} from '../../../core/services/user.service';
import {AgentStatistics, DailyStatistics, JobStatistics} from '../../../core/models/api.model';
import {TranslocoPipe} from '@jsverse/transloco';

/**
 * Admin → Usage & Cost (Slice 4). Read-only surface over the usage_events
 * ledger via `GET /api/usage`: LLM tokens plus dimensionally separate workspace
 * CPU and memory, aggregated by (category, unit) over a selectable window.
 * Costs are $0 until rates are configured — the value today is the measured
 * quantities + attribution.
 */
@Component({
  selector: 'app-admin-usage',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SidebarToggleComponent, TranslocoPipe],
  template: `
    <div class="admin-page">
      <div class="admin-container">
        <div class="page-header">
          <app-sidebar-toggle />
          <h1 class="page-title">Usage &amp; Cost</h1>
          <div class="page-controls">
            <div class="seg">
              @for (w of windows; track w.hours) {
                <button type="button" class="seg-btn"
                  [class.active]="windowHours() === w.hours" (click)="setWindow(w.hours)">{{ w.label }}</button>
              }
            </div>
            <div class="seg">
              @for (o of refreshOptions; track o.ms) {
                <button type="button" class="seg-btn"
                  [class.active]="refreshIntervalMs() === o.ms" (click)="setRefresh(o.ms)">{{ o.label }}</button>
              }
            </div>
            @if (isAdmin()) {
              <label class="scope-switch" [class.on]="viewAllData()" [title]="scopeHint()">
                <input type="checkbox" [checked]="viewAllData()" (change)="setViewAllData($event)" />
                <span class="switch-track"><span class="switch-thumb"></span></span>
                <span class="switch-label">All data</span>
              </label>
            }
          </div>
        </div>
        <p class="page-desc">
          {{ 'admin.usage.pageDescCloud' | transloco }}
        </p>

        <section class="kpi-row">
          <div class="kpi-card"><span class="kpi-label">Tokens</span>
            <span class="kpi-value">{{ fmtQty(tokensTotal()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">Cache hit</span>
            <span class="kpi-value">{{ fmtPct(cacheHitRatio()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">{{ 'admin.usage.vcpuHours' | transloco }}</span>
            <span class="kpi-value">{{ fmtQty(vcpuHours()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">{{ 'admin.usage.memoryGibHours' | transloco }}</span>
            <span class="kpi-value">{{ fmtQty(memoryGibHours()) }}</span></div>
          @if (hasClaimStorage()) {
            <div class="kpi-card"><span class="kpi-label">{{ 'admin.usage.claimGibHours' | transloco }}</span>
              <span class="kpi-value">{{ fmtQty(claimGibHours()) }}</span></div>
            <div class="kpi-card"><span class="kpi-label">{{ 'admin.usage.claimHours' | transloco }}</span>
              <span class="kpi-value">{{ fmtQty(claimHours()) }}</span></div>
          }
          @if (hasVolumeStorage()) {
            <div class="kpi-card"><span class="kpi-label">{{ 'admin.usage.volumeGibHours' | transloco }}</span>
              <span class="kpi-value">{{ fmtQty(volumeGibHours()) }}</span></div>
            <div class="kpi-card"><span class="kpi-label">{{ 'admin.usage.volumeHours' | transloco }}</span>
              <span class="kpi-value">{{ fmtQty(volumeHours()) }}</span></div>
          }
          <div class="kpi-card"><span class="kpi-label">Events</span>
            <span class="kpi-value">{{ fmtQty(eventsTotal()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">Jobs completed</span>
            <span class="kpi-value">{{ fmtQty(jobStats()?.completed ?? 0) }}</span></div>
          @if (isAdmin()) {
            <div class="kpi-card"><span class="kpi-label">Agents in-field</span>
              <span class="kpi-value">{{ agentStats()?.working ?? 0 }}</span></div>
          }
        </section>

        @if (cloudEstimates().length > 0) {
          <section class="admin-section cloud-section">
            <div class="section-head cloud-head">
              <div>
                <h2 class="section-title">{{ 'admin.usage.cloudEstimate.title' | transloco }}</h2>
                <p class="section-note">
                  {{ 'admin.usage.cloudEstimate.subtitle' | transloco }}
                </p>
              </div>
              <span class="estimate-badge">{{ 'admin.usage.cloudEstimate.computeOnly' | transloco }}</span>
            </div>
            <div class="cloud-grid">
              @for (card of cloudEstimates(); track card.id) {
                <article class="cloud-card">
                  <div class="cloud-card-head">
                    <div>
                      <span class="cloud-provider">{{ card.provider }}</span>
                      <h3>{{ card.display_name }}</h3>
                    </div>
                    <span class="cloud-value">{{ fmtCurrency(card.estimate, card.currency) }}</span>
                  </div>
                  <p class="cloud-region">{{ card.region }}</p>
                  <p class="cloud-description">{{ card.description }}</p>
                  <div class="cloud-components">
                    @for (component of card.components; track component.unit) {
                      <div class="cloud-component">
                        <span>{{ fmtQty(component.quantity) }} {{ component.unit }}</span>
                        <span>{{ fmtCurrency(component.amount, card.currency) }}</span>
                      </div>
                    }
                  </div>
                  <p class="cloud-formula">
                    {{ (card.aggregation === 'max'
                      ? 'admin.usage.cloudEstimate.formulaMax'
                      : 'admin.usage.cloudEstimate.formulaSum') | transloco }}
                  </p>
                  <p class="cloud-exclusions">{{ card.exclusions }}</p>
                  <a class="cloud-source" [href]="card.source_url" target="_blank" rel="noopener noreferrer">
                    {{ card.source_label }}
                  </a>
                </article>
              }
            </div>
          </section>
        }

        <section class="admin-section">
          <div class="explorer-head">
            <h2 class="section-title">Usage over time</h2>
            <div class="explorer-toggles">
              <div class="seg">
                @for (d of tsDims; track d.key) {
                  <button type="button" class="seg-btn" [class.active]="tsDim() === d.key"
                    (click)="tsDim.set(d.key)">{{ d.label }}</button>
                }
              </div>
              <div class="seg">
                @for (m of tsMetrics; track m.key) {
                  <button type="button" class="seg-btn" [class.active]="tsMetric() === m.key"
                    (click)="tsMetric.set(m.key)">{{ m.label }}</button>
                }
              </div>
            </div>
          </div>

          @if (chart(); as c) {
            <div class="explorer-body">
              <div class="ts-chart">
                <svg class="ts-svg" viewBox="0 0 720 180" preserveAspectRatio="none">
                  @for (g of c.grid; track g) {
                    <line class="grid-line" x1="0" [attr.y1]="g" x2="720" [attr.y2]="g" />
                  }
                  @for (b of c.bars; track $index) {
                    <rect [attr.x]="b.x" [attr.y]="b.y" [attr.width]="b.w" [attr.height]="b.h"
                      [attr.fill]="b.color"><title>{{ b.title }}</title></rect>
                  }
                </svg>
                <div class="ts-xaxis">
                  @for (l of c.xLabels; track l.text) {
                    <span class="ts-xlabel" [style.left.%]="l.pct">{{ l.text }}</span>
                  }
                </div>
              </div>
              <div class="ts-side">
                <div class="donut-wrap">
                  <svg class="donut-svg" viewBox="0 0 120 120">
                    @for (s of donut(); track s.key) {
                      <circle cx="60" cy="60" r="50" fill="none" [attr.stroke]="s.color"
                        stroke-width="16" [attr.stroke-dasharray]="s.dash"
                        [attr.stroke-dashoffset]="s.offset"
                        transform="rotate(-90 60 60)"><title>{{ s.title }}</title></circle>
                    }
                  </svg>
                  <div class="donut-center">
                    <span class="donut-total">{{ c.grandLabel }}</span>
                    <span class="donut-cap">{{ metricLabel() }}</span>
                  </div>
                </div>
                <ul class="legend">
                  @for (l of c.legend; track l.key) {
                    <li class="legend-item">
                      <span class="swatch" [style.background]="l.color"></span>
                      <span class="lg-label">{{ l.label }}</span>
                      <span class="lg-val">{{ fmtMetric(l.total) }}</span>
                    </li>
                  }
                </ul>
              </div>
            </div>
          } @else {
            <p class="empty-state">No time-series usage in this window.</p>
          }
        </section>

        <section class="admin-section">
          <div class="section-head">
            <h2 class="section-title">By category</h2>
            <span class="total">Total: {{ fmtCost(summary()?.total_cost_usd ?? 0) }}</span>
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

        @if (userRows().length > 0) {
          <section class="admin-section">
            <h2 class="section-title">{{ isAdmin() ? 'Consumption by user' : 'My consumption' }}</h2>
            <div class="breakdown-table">
              <div class="breakdown-header">
                <span class="col-wide">User</span>
                <span class="col-role">Role</span>
                <span class="col-num">Prompt tok.</span>
                <span class="col-num">Compl. tok.</span>
                <span class="col-num">{{ 'admin.usage.vcpuHoursShort' | transloco }}</span>
                <span class="col-num">{{ 'admin.usage.memoryGibHoursShort' | transloco }}</span>
                <span class="col-num">Events</span>
                <span class="col-share">Share</span>
                <span class="col-num">Cost</span>
              </div>
              @for (r of userRows(); track r.label) {
                <div class="breakdown-row">
                  <span class="col-wide">{{ r.label }}</span>
                  <span class="col-role">{{ r.role }}</span>
                  <span class="col-num">{{ fmtQty(r.prompt) }}</span>
                  <span class="col-num">{{ fmtQty(r.completion) }}</span>
                  <span class="col-num">{{ fmtQty(r.vcpu) }}</span>
                  <span class="col-num"
                    [title]="r.memory === null ? ('admin.usage.memoryBreakdownUnavailable' | transloco) : ''">
                    {{ r.memory === null ? '—' : fmtQty(r.memory) }}
                  </span>
                  <span class="col-num">{{ r.events }}</span>
                  <span class="col-share"><span class="share-bar" [style.width.%]="r.share * 100"></span></span>
                  <span class="col-num">{{ r.cost ? fmtCost(r.cost) : '—' }}</span>
                </div>
              }
            </div>
          </section>
        }

        @if (modelRows().length > 0) {
          <section class="admin-section">
            <h2 class="section-title">By model</h2>
            <div class="breakdown-table">
              <div class="breakdown-header model-grid">
                <span class="col-wide">Model</span>
                <span class="col-num">Prompt tok.</span>
                <span class="col-num">Cached tok.</span>
                <span class="col-num">Cache hit</span>
                <span class="col-num">Compl. tok.</span>
                <span class="col-num">Events</span>
                <span class="col-num">Cost</span>
              </div>
              @for (r of modelRows(); track r.label) {
                <div class="breakdown-row model-grid">
                  <span class="col-wide mono">{{ r.label }}</span>
                  <span class="col-num">{{ fmtQty(r.prompt) }}</span>
                  <span class="col-num">{{ fmtQty(r.cached) }}</span>
                  <span class="col-num">{{ fmtPct(r.cacheHit) }}</span>
                  <span class="col-num">{{ fmtQty(r.completion) }}</span>
                  <span class="col-num">{{ r.events }}</span>
                  <span class="col-num">{{ r.cost ? fmtCost(r.cost) : '—' }}</span>
                </div>
              }
            </div>
          </section>
        }

        @if (projectRows().length > 0) {
          <section class="admin-section">
            <h2 class="section-title">By project</h2>
            <div class="breakdown-table">
              <div class="breakdown-header project-grid">
                <span class="col-wide">Project</span>
                <span class="col-num">Tokens</span>
                <span class="col-num">{{ 'admin.usage.vcpuHoursShort' | transloco }}</span>
                <span class="col-num">{{ 'admin.usage.memoryGibHoursShort' | transloco }}</span>
                <span class="col-num">Events</span>
                <span class="col-num">Cost</span>
              </div>
              @for (r of projectRows(); track r.label) {
                <div class="breakdown-row project-grid">
                  <span class="col-wide">{{ r.label }}</span>
                  <span class="col-num">{{ fmtQty(r.tokens) }}</span>
                  <span class="col-num">{{ fmtQty(r.vcpu) }}</span>
                  <span class="col-num"
                    [title]="r.memory === null ? ('admin.usage.memoryBreakdownUnavailable' | transloco) : ''">
                    {{ r.memory === null ? '—' : fmtQty(r.memory) }}
                  </span>
                  <span class="col-num">{{ r.events }}</span>
                  <span class="col-num">{{ r.cost ? fmtCost(r.cost) : '—' }}</span>
                </div>
              }
            </div>
          </section>
        }

        @if (dailyBars().length > 0) {
          <section class="admin-section throughput">
            <h2 class="section-title">Job throughput</h2>
            <div class="bar-chart">
              @for (b of dailyBars(); track b.date) {
                <div class="bar-col">
                  <div class="bar-fill" [style.height.%]="b.height" [title]="b.date + ': ' + b.completed + ' completed'"></div>
                  <span class="bar-label">{{ b.date.slice(5) }}</span>
                </div>
              }
            </div>
          </section>
        }

        @if (isAdmin()) {
          <section class="admin-section fleet-status">
            <h2 class="section-title">Fleet status</h2>
            <div class="fleet-list">
              <div class="fleet-item"><span class="fleet-label">In-field</span><span class="fleet-count">{{ agentStats()?.working ?? 0 }}</span></div>
              <div class="fleet-item"><span class="fleet-label">Idle</span><span class="fleet-count">{{ agentStats()?.ready ?? 0 }}</span></div>
              <div class="fleet-item"><span class="fleet-label">Standing by</span><span class="fleet-count">{{ agentStats()?.booting ?? 0 }}</span></div>
              <div class="fleet-item"><span class="fleet-label">Signal lost</span><span class="fleet-count">{{ (agentStats()?.offline ?? 0) + (agentStats()?.failed ?? 0) }}</span></div>
            </div>
          </section>
        }
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
        flex-wrap: wrap;
        margin-bottom: 8px;
      }
      .page-controls {
        margin-left: auto;
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
        justify-content: flex-end;
      }
      .scope-switch {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        cursor: pointer;
        user-select: none;
        font-size: 12px;
        font-weight: 600;
        color: var(--text-muted);
      }
      .scope-switch input {
        position: absolute;
        opacity: 0;
        width: 0;
        height: 0;
      }
      .switch-track {
        position: relative;
        width: 34px;
        height: 18px;
        border-radius: 999px;
        background: var(--surface-0);
        border: 1px solid var(--border-color);
        transition: background 0.15s, border-color 0.15s;
        flex: 0 0 auto;
      }
      .switch-thumb {
        position: absolute;
        top: 1px;
        left: 1px;
        width: 14px;
        height: 14px;
        border-radius: 50%;
        background: var(--text-muted);
        transition: transform 0.15s, background 0.15s;
      }
      .scope-switch.on .switch-track {
        background: var(--accent-color);
        border-color: var(--accent-color);
      }
      .scope-switch.on .switch-thumb {
        transform: translateX(16px);
        background: var(--on-accent);
      }
      .scope-switch.on .switch-label {
        color: var(--text-primary);
      }
      .section-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
      }
      .section-head .section-title {
        margin: 0;
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
      .cloud-head {
        align-items: flex-start;
      }
      .section-note {
        margin: 4px 0 0;
        color: var(--text-muted);
        font-size: 12px;
        line-height: 1.5;
      }
      .estimate-badge {
        flex: 0 0 auto;
        padding: 4px 8px;
        border: 1px solid var(--border-color);
        border-radius: 999px;
        color: var(--text-muted);
        background: var(--surface-0);
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.6px;
        text-transform: uppercase;
      }
      .cloud-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
        gap: 12px;
      }
      .cloud-card {
        min-width: 0;
        padding: 16px;
        border: 1px solid var(--border-color);
        border-radius: var(--radius-surface);
        background: var(--surface-0);
      }
      .cloud-card-head {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 12px;
      }
      .cloud-provider {
        color: var(--text-muted);
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.7px;
        text-transform: uppercase;
      }
      .cloud-card h3 {
        margin: 3px 0 0;
        color: var(--text-primary);
        font-size: 13px;
        font-weight: 650;
      }
      .cloud-value {
        flex: 0 0 auto;
        color: var(--text-primary);
        font-size: 20px;
        font-weight: 750;
        font-variant-numeric: tabular-nums;
      }
      .cloud-region,
      .cloud-description,
      .cloud-formula,
      .cloud-exclusions {
        color: var(--text-muted);
        font-size: 11px;
        line-height: 1.45;
      }
      .cloud-region {
        margin: 5px 0 12px;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
      }
      .cloud-description {
        min-height: 32px;
        margin: 0 0 12px;
      }
      .cloud-components {
        padding: 8px 0;
        border-top: 1px solid var(--border-color);
        border-bottom: 1px solid var(--border-color);
      }
      .cloud-component {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 3px 0;
        color: var(--text-secondary);
        font-size: 11px;
        font-variant-numeric: tabular-nums;
      }
      .cloud-formula {
        margin: 10px 0 0;
      }
      .cloud-exclusions {
        margin: 6px 0 10px;
      }
      .cloud-source {
        color: var(--accent-color);
        font-size: 11px;
        text-decoration: none;
      }
      .cloud-source:hover {
        text-decoration: underline;
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
      .kpi-row {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 24px;
      }
      .kpi-card {
        flex: 1 1 140px;
        background: var(--panel-bg);
        border: 1px solid var(--border-color);
        border-radius: var(--radius-lg);
        padding: 16px 20px;
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .kpi-label {
        font-size: 11px;
        font-weight: 600;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .kpi-value {
        font-size: 22px;
        font-weight: 700;
        color: var(--text-primary);
        font-variant-numeric: tabular-nums;
      }
      .section-title {
        font-size: 14px;
        font-weight: 600;
        margin: 0 0 12px 0;
        color: var(--text-primary);
      }
      .breakdown-table {
        border: 1px solid var(--border-color);
        border-radius: var(--radius-surface);
        overflow: hidden;
      }
      .breakdown-header,
      .breakdown-row {
        display: grid;
        grid-template-columns: 1.4fr 70px 100px 100px 86px 100px 70px 80px 80px;
        gap: 8px;
        align-items: center;
        padding: 8px 14px;
        font-size: 13px;
      }
      .breakdown-header {
        background: var(--surface-0);
        font-weight: 600;
        font-size: 12px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .breakdown-row {
        border-top: 1px solid var(--border-color);
      }
      .col-wide { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .col-role { color: var(--text-muted); font-size: 12px; }
      .breakdown-row .col-share { position: relative; height: 6px; background: var(--surface-0); border-radius: 3px; overflow: hidden; }
      .share-bar { display: block; height: 100%; background: var(--accent-color); border-radius: 3px; }
      .model-grid { grid-template-columns: 1.6fr 96px 96px 78px 96px 70px 80px; }
      .project-grid { grid-template-columns: 1.6fr 100px 86px 100px 70px 80px; }
      .bar-chart {
        display: flex;
        align-items: flex-end;
        gap: 4px;
        height: 120px;
        padding-top: 8px;
      }
      .bar-col {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        height: 100%;
        justify-content: flex-end;
        gap: 4px;
      }
      .bar-fill {
        width: 100%;
        min-height: 2px;
        background: var(--accent-color);
        border-radius: 2px 2px 0 0;
        transition: height 0.2s;
      }
      .bar-label {
        font-size: 10px;
        color: var(--text-muted);
        white-space: nowrap;
      }
      .fleet-list {
        display: flex;
        gap: 16px;
        flex-wrap: wrap;
      }
      .fleet-item {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 80px;
      }
      .fleet-label {
        font-size: 11px;
        color: var(--text-muted);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.4px;
      }
      .fleet-count {
        font-size: 20px;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
        color: var(--text-primary);
      }
      .explorer-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 16px;
      }
      .explorer-toggles {
        display: inline-flex;
        gap: 8px;
      }
      .seg {
        display: inline-flex;
        border: 1px solid var(--border-color);
        border-radius: var(--radius-surface);
        overflow: hidden;
      }
      .seg-btn {
        border: 0;
        border-right: 1px solid var(--border-color);
        background: var(--surface-0);
        color: var(--text-muted);
        padding: 4px 12px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
      }
      .seg-btn:last-child {
        border-right: 0;
      }
      .seg-btn.active {
        background: var(--accent-color);
        color: var(--on-accent);
      }
      .explorer-body {
        display: flex;
        gap: 24px;
        align-items: stretch;
        flex-wrap: wrap;
      }
      .ts-chart {
        flex: 1 1 360px;
        min-width: 0;
        display: flex;
        flex-direction: column;
      }
      .ts-svg {
        width: 100%;
        height: 200px;
        display: block;
      }
      .grid-line {
        stroke: var(--border-color);
        stroke-width: 1;
        opacity: 0.5;
      }
      .ts-svg rect {
        transition: opacity 0.15s;
      }
      .ts-svg rect:hover {
        opacity: 0.82;
      }
      .ts-xaxis {
        position: relative;
        height: 14px;
        margin-top: 6px;
      }
      .ts-xlabel {
        position: absolute;
        transform: translateX(-50%);
        font-size: 10px;
        color: var(--text-muted);
        white-space: nowrap;
      }
      .ts-side {
        flex: 0 0 200px;
        /* Long unbreakable model slugs in the legend must not set this
           column's min-content — let the label ellipsis do its job. */
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      .donut-wrap {
        position: relative;
        width: 140px;
        height: 140px;
        align-self: center;
      }
      .donut-svg {
        width: 140px;
        height: 140px;
        display: block;
      }
      .donut-center {
        position: absolute;
        inset: 0;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        pointer-events: none;
      }
      .donut-total {
        font-size: 18px;
        font-weight: 700;
        color: var(--text-primary);
        font-variant-numeric: tabular-nums;
      }
      .donut-cap {
        font-size: 10px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.4px;
      }
      .legend {
        list-style: none;
        margin: 0;
        padding: 0;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .legend-item {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        min-width: 0;
      }
      .swatch {
        width: 10px;
        height: 10px;
        border-radius: 2px;
        flex: 0 0 auto;
      }
      .lg-label {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--text-primary);
      }
      .lg-val {
        color: var(--text-muted);
        font-variant-numeric: tabular-nums;
      }

      @media (max-width: 768px) {
        .admin-page {
          padding: 16px;
        }
        .page-desc {
          margin-bottom: 20px;
        }
        .admin-section {
          padding: 14px;
          margin-bottom: 16px;
        }
        /* Controls flow under the title at their natural size instead of
           right-aligning into overlap. */
        .page-controls {
          margin-left: 0;
          width: 100%;
          justify-content: flex-start;
        }
        .explorer-toggles {
          flex-wrap: wrap;
        }
        .kpi-card {
          padding: 12px 14px;
        }
        .kpi-value {
          font-size: 18px;
        }
        /* Grid tables keep their column widths and scroll horizontally inside
           their own border instead of crushing at phone width. */
        .usage-table,
        .breakdown-table {
          overflow-x: auto;
        }
        .usage-header,
        .usage-row {
          min-width: 520px;
        }
        .breakdown-header,
        .breakdown-row {
          min-width: 760px;
        }
        .ts-side {
          flex: 1 1 100%;
        }
        /* The nowrap date labels set each bar's min width, so two weeks of
           bars can't fit — scroll the chart like the tables. */
        .bar-chart {
          overflow-x: auto;
        }
        .bar-col {
          min-width: 34px;
        }
      }
    `,
  ],
})
export class AdminUsageComponent implements OnInit, OnDestroy {
  protected readonly usage = inject(AdminUsageService);
  private readonly api = inject(ApiService);
  private readonly users = inject(UserService);
  readonly isAdmin = computed(() => this.users.currentUser()?.is_admin === true);

  readonly windows = [
    {label: '8h', hours: 8, statsDays: 1},
    {label: '24h', hours: 24, statsDays: 1},
    {label: '7d', hours: 24 * 7, statsDays: 7},
    {label: '30d', hours: 24 * 30, statsDays: 30},
    {label: '90d', hours: 24 * 90, statsDays: 90},
  ] as const;
  readonly windowHours = signal<number>(24 * 30);
  readonly windowDays = computed(() => Math.max(1, Math.ceil(this.windowHours() / 24)));

  readonly summary = computed(() => this.usage.usage());
  readonly rows = computed(() => this.summary()?.by_category ?? []);
  readonly cloudEstimates = computed(() => this.summary()?.cloud_estimates ?? []);
  readonly hasData = computed(() => this.rows().length > 0);

  readonly jobStats = signal<JobStatistics | null>(null);
  readonly agentStats = signal<AgentStatistics | null>(null);

  private qty(category: string, unit: string): number {
    return (this.summary()?.by_category ?? [])
      .filter((r) => r.category === category && r.unit === unit)
      .reduce((s, r) => s + r.quantity, 0);
  }
  private typedRows(
    category: string,
    measurementBasis: UsageRowV2['measurement_basis'],
    unit: string,
  ): UsageRowV2[] | null {
    const summary = this.usage.usageV2();
    if (!summary) return null;
    return summary.rows.filter((r) =>
      r.category === category
      && r.measurement_basis === measurementBasis
      && r.unit === unit);
  }
  private typedQty(
    category: string,
    measurementBasis: UsageRowV2['measurement_basis'],
    unit: string,
  ): number | null {
    const rows = this.typedRows(category, measurementBasis, unit);
    if (rows === null || rows.length === 0) return null;
    return rows.reduce((sum, row) => sum + Number(row.quantity), 0);
  }
  /** The legacy breakdown response is keyed by unit only. It can represent a
   * GiB-hour as compute memory only while no other category emits that unit. */
  private breakdownUnitBelongsTo(category: string, unit: string): boolean {
    return (this.summary()?.by_category ?? [])
      .filter((r) => r.unit === unit)
      .every((r) => r.category === category);
  }
  private unitQty(
    units: Record<string, {quantity: number}> | undefined,
    unit: string,
  ): number {
    return units?.[unit]?.quantity ?? 0;
  }
  private cacheRatio(uncachedPrompt: number, cachedPrompt: number): number {
    const total = uncachedPrompt + cachedPrompt;
    return total > 0 ? cachedPrompt / total : 0;
  }
  readonly promptTokens = computed(() =>
    this.qty('llm', 'prompt-token') + this.qty('llm', 'cached-prompt-token'));
  readonly cachedPromptTokens = computed(() => this.qty('llm', 'cached-prompt-token'));
  readonly completionTokens = computed(() => this.qty('llm', 'completion-token'));
  readonly tokensTotal = computed(() => this.promptTokens() + this.completionTokens());
  readonly cacheHitRatio = computed(() =>
    this.summary()?.cache_hit_ratio
      ?? this.cacheRatio(this.qty('llm', 'prompt-token'), this.cachedPromptTokens()));
  readonly vcpuHours = computed(() =>
    this.typedQty('compute', 'scheduler-request', 'vcpu-hour')
      ?? this.qty('compute', 'vcpu-hour'));
  readonly memoryGibHours = computed(() =>
    this.typedQty('compute', 'scheduler-request', 'gib-hour')
      ?? this.qty('compute', 'gib-hour'));
  readonly hasClaimStorage = computed(() => {
    const gib = this.typedRows('storage', 'claim-requested', 'gib-hour');
    const instances = this.typedRows('storage', 'claim-requested', 'claim-hour');
    return gib !== null && (gib.length > 0 || (instances?.length ?? 0) > 0);
  });
  readonly claimGibHours = computed(() =>
    this.typedQty('storage', 'claim-requested', 'gib-hour') ?? 0);
  readonly claimHours = computed(() =>
    this.typedQty('storage', 'claim-requested', 'claim-hour') ?? 0);
  readonly hasVolumeStorage = computed(() => {
    const gib = this.typedRows('storage', 'volume-provisioned', 'gib-hour');
    const instances = this.typedRows('storage', 'volume-provisioned', 'volume-hour');
    return gib !== null && (gib.length > 0 || (instances?.length ?? 0) > 0);
  });
  readonly volumeGibHours = computed(() =>
    this.typedQty('storage', 'volume-provisioned', 'gib-hour') ?? 0);
  readonly volumeHours = computed(() =>
    this.typedQty('storage', 'volume-provisioned', 'volume-hour') ?? 0);
  readonly eventsTotal = computed(() =>
    (this.summary()?.by_category ?? []).reduce((s, r) => s + r.events, 0));

  readonly daily = signal<DailyStatistics[]>([]);
  readonly dailyBars = computed(() => {
    const d = this.daily();
    const max = Math.max(1, ...d.map((x) => x.jobs_completed));
    return d.map((x) => ({date: x.date, completed: x.jobs_completed,
      height: (x.jobs_completed / max) * 100}));
  });

  readonly modelRows = computed(() => (this.usage.breakdown('model')?.rows ?? []).map((r) => ({
    label: r.label,
    prompt: this.unitQty(r.units, 'prompt-token')
      + this.unitQty(r.units, 'cached-prompt-token'),
    cached: this.unitQty(r.units, 'cached-prompt-token'),
    cacheHit: r.cache_hit_ratio ?? this.cacheRatio(
      this.unitQty(r.units, 'prompt-token'),
      this.unitQty(r.units, 'cached-prompt-token')),
    completion: this.unitQty(r.units, 'completion-token'),
    events: r.events, cost: r.cost_usd,
  })));
  readonly projectRows = computed(() => (this.usage.breakdown('project')?.rows ?? []).map((r) => ({
    label: r.label,
    tokens: this.unitQty(r.units, 'prompt-token')
      + this.unitQty(r.units, 'cached-prompt-token')
      + this.unitQty(r.units, 'completion-token'),
    vcpu: this.unitQty(r.units, 'vcpu-hour'),
    memory: this.breakdownUnitBelongsTo('compute', 'gib-hour')
      ? this.unitQty(r.units, 'gib-hour') : null,
    events: r.events, cost: r.cost_usd,
  })));

  readonly userRows = computed(() => {
    const rows = this.usage.breakdown('user')?.rows ?? [];
    const max = Math.max(1, ...rows.map((r) => r.events));
    return rows.map((r) => ({
      label: r.label,
      role: r.is_admin ? 'Admin' : 'User',
      prompt: this.unitQty(r.units, 'prompt-token')
        + this.unitQty(r.units, 'cached-prompt-token'),
      completion: this.unitQty(r.units, 'completion-token'),
      vcpu: this.unitQty(r.units, 'vcpu-hour'),
      memory: this.breakdownUnitBelongsTo('compute', 'gib-hour')
        ? this.unitQty(r.units, 'gib-hour') : null,
      events: r.events,
      cost: r.cost_usd,
      share: r.events / max,
    }));
  });

  // ---- Usage-over-time explorer (stacked timeline + composition donut) ----
  readonly tsDims = [
    {key: 'model', label: 'Model'},
    {key: 'user', label: 'User'},
    {key: 'project', label: 'Project'},
  ] as const;
  readonly tsMetrics = [
    {key: 'tokens', label: 'Tokens'},
    {key: 'cost', label: 'Cost'},
    {key: 'events', label: 'Events'},
  ] as const;
  readonly tsDim = signal<BreakdownDim>('model');
  readonly tsMetric = signal<'tokens' | 'cost' | 'events'>('tokens');
  private readonly PALETTE = ['#6366f1', '#22d3ee', '#f59e0b', '#ef4444', '#10b981', '#a855f7'];
  private readonly OTHER_COLOR = '#64748b';
  private readonly TOP_N = 6;

  /** Builds stacked-bar geometry (viewBox 720×180), legend and totals for the
   * selected dimension + metric. Top-N series keep distinct colors; the rest roll
   * into a single "Other" band. Returns null when the window has no series. */
  readonly chart = computed(() => {
    const ts = this.usage.timeseries(this.tsDim());
    const days = ts?.days ?? [];
    const rawSeries = ts?.series ?? [];
    if (!days.length || !rawSeries.length) return null;

    const metric = this.tsMetric();
    const val = (p: UsageTsPoint) =>
      metric === 'tokens' ? p.tokens : metric === 'cost' ? p.cost_usd : p.events;

    const totals = rawSeries
      .map((s) => ({s, total: s.points.reduce((a, p) => a + val(p), 0)}))
      .sort((a, b) => b.total - a.total);
    const top = totals.slice(0, this.TOP_N);
    const rest = totals.slice(this.TOP_N);

    const legend = top.map((t, i) => ({
      key: t.s.key,
      label: t.s.label,
      color: this.PALETTE[i % this.PALETTE.length],
      total: t.total,
    }));
    if (rest.length) {
      legend.push({
        key: '__other__',
        label: `Other (${rest.length})`,
        color: this.OTHER_COLOR,
        total: rest.reduce((a, t) => a + t.total, 0),
      });
    }
    const grandTotal = legend.reduce((a, l) => a + l.total, 0);

    const colorOf = new Map(legend.map((l) => [l.key, l.color]));
    const labelOf = new Map(legend.map((l) => [l.key, l.label]));
    const topByDay = top.map((t) => {
      const m = new Map<string, number>();
      for (const p of t.s.points) m.set(p.day, val(p));
      return {key: t.s.key, m};
    });
    const restByDay = new Map<string, number>();
    for (const t of rest) {
      for (const p of t.s.points) restByDay.set(p.day, (restByDay.get(p.day) ?? 0) + val(p));
    }

    const columns = days.map((day) => {
      const segs: {key: string; value: number}[] = [];
      let total = 0;
      for (const tb of topByDay) {
        const v = tb.m.get(day) ?? 0;
        if (v > 0) {
          segs.push({key: tb.key, value: v});
          total += v;
        }
      }
      const ov = restByDay.get(day) ?? 0;
      if (ov > 0) {
        segs.push({key: '__other__', value: ov});
        total += ov;
      }
      return {day, segs, total};
    });
    const maxTotal = Math.max(1, ...columns.map((c) => c.total));

    const W = 720;
    const H = 180;
    const colW = W / days.length;
    const gap = days.length > 45 ? 0.5 : days.length > 20 ? 1.5 : 3;
    const barW = Math.max(1, colW - gap);
    const bars: {x: number; y: number; w: number; h: number; color: string; title: string}[] = [];
    columns.forEach((c, ci) => {
      const x = ci * colW + (colW - barW) / 2;
      let yTop = H;
      for (const seg of c.segs) {
        const h = (seg.value / maxTotal) * H;
        yTop -= h;
        bars.push({
          x,
          y: yTop,
          w: barW,
          h,
          color: colorOf.get(seg.key) ?? this.OTHER_COLOR,
          title: `${c.day} · ${labelOf.get(seg.key) ?? seg.key}: ${this.fmtMetric(seg.value)}`,
        });
      }
    });

    const want = Math.min(6, days.length);
    const step = (days.length - 1) / Math.max(1, want - 1);
    const xLabels: {pct: number; text: string}[] = [];
    const seen = new Set<number>();
    for (let i = 0; i < want; i++) {
      const idx = Math.round(i * step);
      if (seen.has(idx)) continue;
      seen.add(idx);
      xLabels.push({pct: ((idx + 0.5) / days.length) * 100, text: days[idx].slice(5)});
    }
    const grid = [0.25, 0.5, 0.75].map((f) => f * H);

    return {bars, xLabels, grid, legend, grandTotal, grandLabel: this.fmtShort(grandTotal)};
  });

  /** Composition donut segments (stroke-dasharray technique) sharing chart()'s legend. */
  readonly donut = computed(() => {
    const c = this.chart();
    if (!c || c.grandTotal <= 0) return [];
    const C = 2 * Math.PI * 50;
    let off = 0;
    return c.legend
      .filter((l) => l.total > 0)
      .map((l) => {
        const frac = l.total / c.grandTotal;
        const seg = {
          key: l.key,
          color: l.color,
          dash: `${frac * C} ${C - frac * C}`,
          offset: off ? -off : 0,
          title: `${l.label}: ${(frac * 100).toFixed(1)}%`,
        };
        off += frac * C;
        return seg;
      });
  });

  metricLabel(): string {
    return this.tsMetrics.find((m) => m.key === this.tsMetric())?.label ?? '';
  }

  fmtMetric(n: number): string {
    return this.tsMetric() === 'cost' ? this.fmtCost(n) : this.fmtQty(n);
  }

  fmtShort(n: number): string {
    if (this.tsMetric() === 'cost') return this.fmtCost(n);
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (a >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  }

  // ---- Page-level scope override (admin "All data" switch) ----
  // Reuses the global view-as header (see view-as.interceptor): 'all' suppresses
  // it (fleet view), 'mine' forces it (self) — so this page can override the
  // admin's global "view as me" mode WITHOUT a backend change. Persisted per-user
  // in localStorage so the choice sticks. Non-admins are always self-scoped → null.
  private readonly SCOPE_KEY = 'srw.usageViewAll';
  readonly viewAllData = signal<boolean>(this.loadViewAllPref());
  readonly scopeOverride = computed<'all' | 'mine' | null>(() =>
    this.isAdmin() ? (this.viewAllData() ? 'all' : 'mine') : null,
  );

  private scopeStorageKey(): string {
    const uid = this.users.currentUserId();
    return uid ? `${this.SCOPE_KEY}.${uid}` : this.SCOPE_KEY;
  }
  private loadViewAllPref(): boolean {
    try {
      const v = localStorage.getItem(this.scopeStorageKey());
      return v === null ? true : v === 'true'; // default: view all (fleet)
    } catch {
      return true;
    }
  }
  setViewAllData(ev: Event): void {
    const on = (ev.target as HTMLInputElement).checked;
    this.viewAllData.set(on);
    try {
      localStorage.setItem(this.scopeStorageKey(), String(on));
    } catch {
      /* localStorage unavailable — choice holds for this session only */
    }
    this.reloadAll();
  }
  scopeHint(): string {
    return this.viewAllData()
      ? 'Showing all usage — overrides your global view-as-me mode on this page'
      : 'Showing only your own usage on this page';
  }

  readonly refreshOptions = [
    {label: 'Off', ms: 0}, {label: '10s', ms: 10000},
    {label: '30s', ms: 30000}, {label: '1m', ms: 60000},
  ] as const;
  readonly refreshIntervalMs = signal<number>(0);
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    effect(() => {
      const ms = this.refreshIntervalMs();
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
      if (ms > 0) this.timer = setInterval(() => this.reloadAll(), ms);
    });
  }

  setRefresh(ms: number): void { this.refreshIntervalMs.set(ms); }

  /** Single funnel every panel's loader goes through (used by auto-refresh + window change). */
  reloadAll(): void {
    const usageWindow = this.currentUsageWindow();
    const statsDays = this.currentStatsDays();
    const scope = this.scopeOverride();
    this.usage.loadUsage(usageWindow, scope);
    this.usage.loadUsageV2(usageWindow, scope);
    this.api.getJobStatistics().subscribe((s) => this.jobStats.set(s));
    if (this.isAdmin()) this.api.getAgentStatistics().subscribe((s) => this.agentStats.set(s));
    this.usage.loadBreakdown('user', usageWindow, scope);
    this.usage.loadBreakdown('model', usageWindow, scope);
    this.usage.loadBreakdown('project', usageWindow, scope);
    this.usage.loadTimeseries('user', usageWindow, scope);
    this.usage.loadTimeseries('model', usageWindow, scope);
    this.usage.loadTimeseries('project', usageWindow, scope);
    this.api.getDailyStatistics(statsDays).subscribe((stats) => this.daily.set(stats));
  }

  ngOnDestroy(): void { if (this.timer) clearInterval(this.timer); }

  ngOnInit(): void {
    this.reloadAll();
  }

  setWindow(hours: number): void {
    this.windowHours.set(hours);
    this.reloadAll();
  }

  private currentUsageWindow(): UsageWindow {
    const to = new Date();
    const from = new Date(to.getTime() - this.windowHours() * 60 * 60 * 1000);
    return {
      days: this.windowDays(),
      fromIso: from.toISOString(),
      toIso: to.toISOString(),
    };
  }

  private currentStatsDays(): number {
    return this.windows.find((w) => w.hours === this.windowHours())?.statsDays ?? this.windowDays();
  }

  fmtQty(n: number): string {
    return (n ?? 0).toLocaleString(undefined, {maximumFractionDigits: 2});
  }

  fmtCost(n: number): string {
    return '$' + (n ?? 0).toFixed(2);
  }

  fmtCurrency(n: number, currency: string): string {
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      }).format(n ?? 0);
    } catch {
      return `${currency} ${(n ?? 0).toFixed(2)}`;
    }
  }

  fmtPct(n: number): string {
    return ((n ?? 0) * 100).toFixed(1) + '%';
  }

  catLabel(c: string): string {
    if (c === 'llm') return 'LLM';
    if (c === 'compute') return 'Compute';
    return c;
  }
}
