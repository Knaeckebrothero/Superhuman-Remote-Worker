import {Component, inject, OnDestroy, OnInit, signal} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {Agent, AgentStatus} from '../../core/models/api.model';
import {JobSummary} from '../../core/models/audit.model';
import {AppButtonComponent} from '../../ui/button';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppDialogComponent} from '../../ui/dialog';
import {AppSpinnerComponent} from '../../ui/spinner';

/**
 * Agent List component that displays registered agents.
 * Shows agent status, current job, and last heartbeat with auto-refresh.
 * Supports assigning jobs to ready agents.
 */
@Component({
  selector: 'app-agent-list',
  standalone: true,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppBadgeComponent,
    AppDialogComponent,
    AppSpinnerComponent,
  ],
  template: `
    <div class="agent-list-container">
      <!-- Header -->
      <div class="header-bar">
        <span class="title">{{ 'agentList.title' | transloco }}</span>
        <span class="agent-count">{{ 'agentList.count' | transloco: {n: agents().length} }}</span>
        <app-button variant="secondary" size="sm" [disabled]="isLoading()" (clicked)="refresh()">
          {{ (isLoading() ? 'agentList.loading' : 'agentList.refresh') | transloco }}
        </app-button>
      </div>

      <!-- Assignment Dialog -->
      <app-dialog
        [open]="showAssignDialog()"
        size="md"
        [title]="'agentList.assign.title' | transloco"
        (closed)="closeAssignDialog()"
      >
        @if (availableJobs().length === 0) {
          <div class="no-jobs">
            <span>{{ 'agentList.assign.noJobs' | transloco }}</span>
            <span class="hint">{{ 'agentList.assign.noJobsHint' | transloco }}</span>
          </div>
        } @else {
          <div class="job-select-list">
            @for (job of availableJobs(); track job.id) {
              <div
                class="job-option"
                [class.selected]="selectedJobId() === job.id"
                (click)="selectJob(job.id)"
              >
                <span class="job-prompt">{{ truncatePrompt(job.description) }}</span>
                <span class="job-meta">{{ job.id.slice(0, 8) }}... | {{ formatDate(job.created_at) }}</span>
              </div>
            }
          </div>
        }
        <ng-container appDialogActions>
          <app-button variant="secondary" size="sm" (clicked)="closeAssignDialog()">
            {{ 'agentList.assign.cancel' | transloco }}
          </app-button>
          <app-button
            variant="primary"
            size="sm"
            [disabled]="!selectedJobId() || isAssigning()"
            [loading]="isAssigning()"
            (clicked)="confirmAssignment()"
          >
            {{ (isAssigning() ? 'agentList.assign.assigning' : 'agentList.assign.confirm') | transloco }}
          </app-button>
        </ng-container>
      </app-dialog>

      <!-- Loading State -->
      @if (isLoading() && agents().length === 0) {
        <div class="loading-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'agentList.loadingAgents' | transloco }}</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && agents().length === 0) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F916;</span>
          <span>{{ 'agentList.empty' | transloco }}</span>
          <span class="empty-hint">{{ 'agentList.emptyHint' | transloco }}</span>
        </div>
      }

      <!-- Agent Table -->
      @if (agents().length > 0) {
        <div class="table-container">
          <table class="agent-table">
            <thead>
              <tr>
                <th>{{ 'agentList.table.status' | transloco }}</th>
                <th>{{ 'agentList.table.config' | transloco }}</th>
                <th>{{ 'agentList.table.hostname' | transloco }}</th>
                <th>{{ 'agentList.table.currentJob' | transloco }}</th>
                <th>{{ 'agentList.table.lastHeartbeat' | transloco }}</th>
                <th>{{ 'agentList.table.actions' | transloco }}</th>
              </tr>
            </thead>
            <tbody>
              @for (agent of agents(); track agent.id) {
                <tr [class]="'status-' + agent.status">
                  <td>
                    <app-badge [tone]="agentStatusTone(agent.status)" size="sm">
                      {{ getStatusIcon(agent.status) }} {{ 'agentList.status.' + agent.status | transloco }}
                    </app-badge>
                  </td>
                  <td class="config-name">{{ agent.config_name }}</td>
                  <td class="hostname">{{ agent.hostname || agent.pod_ip || '-' }}</td>
                  <td class="job-id">
                    @if (agent.current_job_id) {
                      <span class="job-link" title="{{ agent.current_job_id }}">
                        {{ agent.current_job_id.slice(0, 8) }}...
                      </span>
                    } @else {
                      <span class="no-job">-</span>
                    }
                  </td>
                  <td class="heartbeat">{{ formatTimestamp(agent.last_heartbeat) }}</td>
                  <td class="actions">
                    @if (agent.status === 'ready') {
                      <app-button
                        variant="success"
                        size="sm"
                        [ariaLabel]="'agentList.actions.assignTitle' | transloco"
                        (clicked)="openAssignDialog(agent.id)"
                      >
                        {{ 'agentList.actions.assign' | transloco }}
                      </app-button>
                    }
                    @if (agent.status === 'offline' || agent.status === 'failed') {
                      <app-button variant="danger" size="sm" (clicked)="removeAgent(agent.id)">
                        {{ 'agentList.actions.remove' | transloco }}
                      </app-button>
                    }
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }

      <!-- Footer with status message -->
      <div class="footer-bar">
        @if (statusMessage()) {
          <span class="status-message" [class.error]="statusIsError()">
            {{ statusMessage() }}
          </span>
        } @else {
          <span class="auto-refresh">
            {{ (autoRefreshEnabled() ? 'agentList.footer.autoRefreshOn' : 'agentList.footer.autoRefreshOff') | transloco }}
          </span>
        }
        <app-button variant="secondary" size="sm" (clicked)="toggleAutoRefresh()">
          {{ (autoRefreshEnabled() ? 'agentList.footer.disable' : 'agentList.footer.enable') | transloco }}
        </app-button>
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

      .agent-list-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        position: relative;
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .agent-count {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
        margin-left: auto;
      }

      .no-jobs {
        text-align: center;
        padding: 20px;
        color: var(--text-muted, #6c7086);
      }

      .no-jobs .hint {
        display: block;
        font-size: 11px;
        margin-top: 8px;
        opacity: 0.7;
      }

      .job-select-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .job-option {
        padding: 10px 12px;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #45475a);
        border-radius: 6px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .job-option:hover {
        background: var(--panel-header-bg, #1e1e2e);
      }

      .job-option.selected {
        border-color: var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.1);
      }

      .job-prompt {
        display: block;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        margin-bottom: 4px;
      }

      .job-meta {
        display: block;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      /* Loading State */
      .loading-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        flex: 1;
      }

      /* Empty State */
      .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        color: var(--text-muted, #6c7086);
        flex: 1;
      }

      .empty-icon {
        font-size: 48px;
        opacity: 0.5;
      }

      .empty-hint {
        font-size: 11px;
        opacity: 0.6;
      }

      /* Table */
      .table-container {
        flex: 1;
        overflow: auto;
        padding: 8px;
      }

      .agent-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }

      .agent-table th {
        text-align: left;
        padding: 8px 10px;
        background: var(--surface-0, #313244);
        color: var(--text-muted, #6c7086);
        font-weight: 500;
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.5px;
        border-bottom: 1px solid var(--border-color, #45475a);
      }

      .agent-table td {
        padding: 10px;
        border-bottom: 1px solid var(--border-color, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .agent-table tbody tr:hover {
        background: var(--surface-0, #313244);
      }

      .config-name {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
      }

      .hostname {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-secondary, #a6adc8);
      }

      .job-link {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #89b4fa;
        cursor: pointer;
      }

      .job-link:hover {
        text-decoration: underline;
      }

      .no-job {
        color: var(--text-muted, #6c7086);
      }

      .heartbeat {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      /* Action Buttons */
      .actions {
        white-space: nowrap;
        display: flex;
        gap: 6px;
      }

      /* Footer */
      .footer-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: var(--surface-0, #313244);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .auto-refresh {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .status-message {
        font-size: 11px;
        color: #a6e3a1;
      }

      .status-message.error {
        color: #f38ba8;
      }

    `,
  ],
})
export class AgentListComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);
  private readonly transloco = inject(TranslocoService);

  readonly agents = signal<Agent[]>([]);
  readonly availableJobs = signal<JobSummary[]>([]);
  readonly isLoading = signal(false);
  readonly autoRefreshEnabled = signal(true);

  // Assignment dialog state
  readonly showAssignDialog = signal(false);
  readonly selectedAgentId = signal<string | null>(null);
  readonly selectedJobId = signal<string | null>(null);
  readonly isAssigning = signal(false);

  // Status message
  readonly statusMessage = signal<string | null>(null);
  readonly statusIsError = signal(false);

  private refreshInterval: ReturnType<typeof setInterval> | null = null;
  private statusTimeout: ReturnType<typeof setTimeout> | null = null;

  ngOnInit(): void {
    this.refresh();
    this.startAutoRefresh();
  }

  ngOnDestroy(): void {
    this.stopAutoRefresh();
    if (this.statusTimeout) {
      clearTimeout(this.statusTimeout);
    }
  }

  refresh(): void {
    this.isLoading.set(true);
    this.api.getAgents().subscribe((agents) => {
      this.agents.set(agents);
      this.isLoading.set(false);
    });
  }

  loadAvailableJobs(): void {
    // Load jobs with 'created' status that can be assigned
    this.api.getJobs('created', 50).subscribe((jobs) => {
      this.availableJobs.set(jobs);
    });
  }

  openAssignDialog(agentId: string): void {
    this.selectedAgentId.set(agentId);
    this.selectedJobId.set(null);
    this.loadAvailableJobs();
    this.showAssignDialog.set(true);
  }

  closeAssignDialog(): void {
    this.showAssignDialog.set(false);
    this.selectedAgentId.set(null);
    this.selectedJobId.set(null);
  }

  selectJob(jobId: string): void {
    this.selectedJobId.set(jobId);
  }

  confirmAssignment(): void {
    const agentId = this.selectedAgentId();
    const jobId = this.selectedJobId();

    if (!agentId || !jobId) {
      return;
    }

    this.isAssigning.set(true);

    this.api.assignJob(jobId, agentId).subscribe({
      next: (result) => {
        this.isAssigning.set(false);
        this.closeAssignDialog();

        if (result) {
          this.showStatus(this.transloco.translate('agentList.messages.assigned'), false);
          this.refresh();
        } else {
          this.showStatus(this.transloco.translate('agentList.messages.assignFailed'), true);
        }
      },
      error: () => {
        this.isAssigning.set(false);
        this.showStatus(this.transloco.translate('agentList.messages.assignError'), true);
      },
    });
  }

  private showStatus(message: string, isError: boolean): void {
    this.statusMessage.set(message);
    this.statusIsError.set(isError);

    if (this.statusTimeout) {
      clearTimeout(this.statusTimeout);
    }

    this.statusTimeout = setTimeout(() => {
      this.statusMessage.set(null);
    }, 5000);
  }

  toggleAutoRefresh(): void {
    if (this.autoRefreshEnabled()) {
      this.stopAutoRefresh();
      this.autoRefreshEnabled.set(false);
    } else {
      this.startAutoRefresh();
      this.autoRefreshEnabled.set(true);
    }
  }

  private startAutoRefresh(): void {
    this.stopAutoRefresh();
    this.refreshInterval = setInterval(() => {
      if (!this.isLoading()) {
        this.refresh();
      }
    }, 30000);
  }

  private stopAutoRefresh(): void {
    if (this.refreshInterval) {
      clearInterval(this.refreshInterval);
      this.refreshInterval = null;
    }
  }

  removeAgent(agentId: string): void {
    this.api.deleteAgent(agentId).subscribe((result) => {
      if (result) {
        this.refresh();
      }
    });
  }

  agentStatusTone(status: AgentStatus): BadgeTone {
    switch (status) {
      case 'ready': return 'success';
      case 'completed': return 'success';
      case 'working':
      case 'draining': return 'warning';
      case 'booting':
      case 'session': return 'info';
      case 'failed': return 'danger';
      case 'offline': return 'neutral';
      default: return 'neutral';
    }
  }

  getStatusIcon(status: AgentStatus): string {
    const icons: Record<AgentStatus, string> = {
      booting: '\u23F3',
      ready: '\u2705',
      working: '\u26A1',
      session: '\uD83D\uDCAC',
      draining: '\u23F3',
      completed: '\u2714',
      failed: '\u274C',
      offline: '\u26AA',
    };
    return icons[status] || '\u2753';
  }

  formatTimestamp(timestamp: string): string {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffSec = Math.floor(diffMs / 1000);

    if (diffSec < 60) {
      return this.transloco.translate('agentList.time.secondsAgo', {n: diffSec});
    }
    if (diffSec < 3600) {
      return this.transloco.translate('agentList.time.minutesAgo', {n: Math.floor(diffSec / 60)});
    }
    if (diffSec < 86400) {
      return this.transloco.translate('agentList.time.hoursAgo', {n: Math.floor(diffSec / 3600)});
    }
    return date.toLocaleDateString(this.transloco.getActiveLang());
  }

  truncatePrompt(prompt: string | undefined, maxLength: number = 60): string {
    if (!prompt) {
      return '';
    }
    if (prompt.length <= maxLength) {
      return prompt;
    }
    return prompt.slice(0, maxLength) + '...';
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString(this.transloco.getActiveLang(), {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
}
