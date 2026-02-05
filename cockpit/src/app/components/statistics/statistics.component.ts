import { Component, inject, signal, OnInit, OnDestroy } from '@angular/core';
import { ApiService } from '../../core/services/api.service';
import {
  JobStatistics,
  DailyStatistics,
  AgentStatistics,
  StuckJob,
} from '../../core/models/api.model';

/**
 * Statistics component showing job and agent metrics.
 */
@Component({
  selector: 'app-statistics',
  standalone: true,
  template: `
    <div class="statistics-container">
      <div class="header-bar">
        <span class="title">System Statistics</span>
        <button class="refresh-btn" (click)="refresh()" [disabled]="isLoading()">
          {{ isLoading() ? 'Loading...' : 'Refresh' }}
        </button>
      </div>

      <div class="stats-content">
        <!-- Job Statistics -->
        <div class="stats-section">
          <h3 class="section-title">Job Queue</h3>
          @if (jobStats()) {
            <div class="metrics-grid">
              <div class="metric-card">
                <span class="metric-value">{{ jobStats()!.total_jobs }}</span>
                <span class="metric-label">Total Jobs</span>
              </div>
              <div class="metric-card status-created">
                <span class="metric-value">{{ jobStats()!.created }}</span>
                <span class="metric-label">Created</span>
              </div>
              <div class="metric-card status-processing">
                <span class="metric-value">{{ jobStats()!.processing }}</span>
                <span class="metric-label">Processing</span>
              </div>
              <div class="metric-card status-completed">
                <span class="metric-value">{{ jobStats()!.completed }}</span>
                <span class="metric-label">Completed</span>
              </div>
              <div class="metric-card status-failed">
                <span class="metric-value">{{ jobStats()!.failed }}</span>
                <span class="metric-label">Failed</span>
              </div>
              <div class="metric-card status-cancelled">
                <span class="metric-value">{{ jobStats()!.cancelled }}</span>
                <span class="metric-label">Cancelled</span>
              </div>
            </div>
          } @else {
            <div class="loading-placeholder">Loading job statistics...</div>
          }
        </div>

        <!-- Agent Statistics -->
        <div class="stats-section">
          <h3 class="section-title">Agent Workforce</h3>
          @if (agentStats()) {
            <div class="metrics-grid">
              <div class="metric-card">
                <span class="metric-value">{{ agentStats()!.total }}</span>
                <span class="metric-label">Total Agents</span>
              </div>
              <div class="metric-card agent-ready">
                <span class="metric-value">{{ agentStats()!.ready }}</span>
                <span class="metric-label">Ready</span>
              </div>
              <div class="metric-card agent-working">
                <span class="metric-value">{{ agentStats()!.working }}</span>
                <span class="metric-label">Working</span>
              </div>
              <div class="metric-card agent-offline">
                <span class="metric-value">{{ agentStats()!.offline }}</span>
                <span class="metric-label">Offline</span>
              </div>
            </div>
          } @else {
            <div class="loading-placeholder">Loading agent statistics...</div>
          }
        </div>

        <!-- Daily Statistics -->
        <div class="stats-section">
          <h3 class="section-title">Daily Activity (7 Days)</h3>
          @if (dailyStats().length > 0) {
            <div class="daily-table">
              <div class="daily-header">
                <span class="col-date">Date</span>
                <span class="col-num">Created</span>
                <span class="col-num">Completed</span>
                <span class="col-num">Failed</span>
              </div>
              @for (day of dailyStats(); track day.date) {
                <div class="daily-row">
                  <span class="col-date">{{ formatDate(day.date) }}</span>
                  <span class="col-num created">{{ day.jobs_created }}</span>
                  <span class="col-num completed">{{ day.jobs_completed }}</span>
                  <span class="col-num failed">{{ day.jobs_failed }}</span>
                </div>
              }
            </div>
          } @else {
            <div class="loading-placeholder">No activity data available</div>
          }
        </div>

        <!-- Stuck Jobs Alert -->
        <div class="stats-section">
          <h3 class="section-title">
            Stuck Jobs
            @if (stuckJobs().length > 0) {
              <span class="alert-badge">{{ stuckJobs().length }}</span>
            }
          </h3>
          @if (stuckJobs().length > 0) {
            <div class="stuck-list">
              @for (job of stuckJobs(); track job.id) {
                <div class="stuck-item">
                  <div class="stuck-header">
                    <span class="stuck-component" [class]="'component-' + job.stuck_component">
                      {{ job.stuck_component.toUpperCase() }}
                    </span>
                    <span class="stuck-id">{{ job.id.slice(0, 8) }}...</span>
                  </div>
                  <div class="stuck-reason">{{ job.stuck_reason }}</div>
                  <div class="stuck-meta">
                    Last update: {{ formatTimestamp(job.updated_at) }}
                  </div>
                </div>
              }
            </div>
          } @else {
            <div class="no-stuck">
              <span class="check-icon">&#x2705;</span>
              No stuck jobs detected
            </div>
          }
        </div>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: hidden;
      }

      .statistics-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .refresh-btn {
        padding: 5px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
      }

      .refresh-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
      }

      /* Content */
      .stats-content {
        flex: 1;
        overflow: auto;
        padding: 12px;
      }

      /* Sections */
      .stats-section {
        margin-bottom: 20px;
        padding: 12px;
        background: var(--surface-0, #313244);
        border-radius: 8px;
      }

      .section-title {
        margin: 0 0 12px 0;
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .alert-badge {
        padding: 2px 8px;
        border-radius: 10px;
        background: #f38ba8;
        color: var(--timeline-bg, #11111b);
        font-size: 11px;
        font-weight: 600;
      }

      .loading-placeholder {
        padding: 20px;
        text-align: center;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
      }

      /* Metrics Grid */
      .metrics-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
        gap: 10px;
      }

      .metric-card {
        padding: 12px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: 6px;
        text-align: center;
      }

      .metric-value {
        display: block;
        font-size: 24px;
        font-weight: 700;
        color: var(--text-primary, #cdd6f4);
        font-family: 'JetBrains Mono', monospace;
      }

      .metric-label {
        display: block;
        margin-top: 4px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        text-transform: uppercase;
      }

      /* Status Colors */
      .metric-card.status-created .metric-value { color: #89b4fa; }
      .metric-card.status-processing .metric-value { color: #f9e2af; }
      .metric-card.status-completed .metric-value { color: #a6e3a1; }
      .metric-card.status-failed .metric-value { color: #f38ba8; }
      .metric-card.status-cancelled .metric-value { color: #6c7086; }

      .metric-card.agent-ready .metric-value { color: #a6e3a1; }
      .metric-card.agent-working .metric-value { color: #f9e2af; }
      .metric-card.agent-offline .metric-value { color: #6c7086; }

      /* Daily Table */
      .daily-table {
        font-size: 12px;
      }

      .daily-header {
        display: flex;
        padding: 8px 0;
        border-bottom: 1px solid var(--border-color, #45475a);
        font-weight: 500;
        color: var(--text-muted, #6c7086);
        text-transform: uppercase;
        font-size: 10px;
      }

      .daily-row {
        display: flex;
        padding: 8px 0;
        border-bottom: 1px solid var(--border-color, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .daily-row:last-child {
        border-bottom: none;
      }

      .col-date {
        flex: 1;
      }

      .col-num {
        width: 80px;
        text-align: right;
        font-family: 'JetBrains Mono', monospace;
      }

      .col-num.created { color: #89b4fa; }
      .col-num.completed { color: #a6e3a1; }
      .col-num.failed { color: #f38ba8; }

      /* Stuck Jobs */
      .stuck-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .stuck-item {
        padding: 10px;
        background: rgba(243, 139, 168, 0.1);
        border: 1px solid rgba(243, 139, 168, 0.2);
        border-radius: 6px;
      }

      .stuck-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 6px;
      }

      .stuck-component {
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        font-weight: 600;
      }

      .component-creator {
        background: rgba(148, 226, 213, 0.2);
        color: #94e2d5;
      }

      .component-validator {
        background: rgba(249, 226, 175, 0.2);
        color: #f9e2af;
      }

      .component-unknown {
        background: rgba(108, 112, 134, 0.2);
        color: #6c7086;
      }

      .stuck-id {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .stuck-reason {
        font-size: 12px;
        color: #f38ba8;
        margin-bottom: 4px;
      }

      .stuck-meta {
        font-size: 10px;
        color: var(--text-muted, #6c7086);
      }

      .no-stuck {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 16px;
        color: #a6e3a1;
        font-size: 13px;
      }

      .check-icon {
        font-size: 18px;
      }
    `,
  ],
})
export class StatisticsComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);

  readonly jobStats = signal<JobStatistics | null>(null);
  readonly agentStats = signal<AgentStatistics | null>(null);
  readonly dailyStats = signal<DailyStatistics[]>([]);
  readonly stuckJobs = signal<StuckJob[]>([]);
  readonly isLoading = signal(false);

  private refreshInterval: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.refresh();
    // Auto-refresh every 60 seconds
    this.refreshInterval = setInterval(() => {
      this.refresh();
    }, 60000);
  }

  ngOnDestroy(): void {
    if (this.refreshInterval) {
      clearInterval(this.refreshInterval);
    }
  }

  refresh(): void {
    this.isLoading.set(true);

    // Fetch all statistics in parallel
    this.api.getJobStatistics().subscribe((stats) => {
      this.jobStats.set(stats);
    });

    this.api.getAgentStatistics().subscribe((stats) => {
      this.agentStats.set(stats);
    });

    this.api.getDailyStatistics(7).subscribe((stats) => {
      this.dailyStats.set(stats);
    });

    this.api.getStuckJobs(60).subscribe((jobs) => {
      this.stuckJobs.set(jobs);
      this.isLoading.set(false);
    });
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    });
  }

  formatTimestamp(timestamp: string): string {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);

    if (diffMin < 60) {
      return `${diffMin} min ago`;
    }
    if (diffMin < 1440) {
      return `${Math.floor(diffMin / 60)} hours ago`;
    }
    return date.toLocaleDateString();
  }
}
