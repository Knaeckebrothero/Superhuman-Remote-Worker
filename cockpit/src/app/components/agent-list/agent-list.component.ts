import { Component, inject, signal, computed, OnInit, OnDestroy } from '@angular/core';
import { ApiService } from '../../core/services/api.service';
import { Agent, AgentStatus } from '../../core/models/api.model';
import { JobSummary } from '../../core/models/audit.model';

/**
 * Agent List component that displays registered agents.
 * Shows agent status, current job, and last heartbeat with auto-refresh.
 * Supports assigning jobs to ready agents.
 */
@Component({
  selector: 'app-agent-list',
  standalone: true,
  template: `
    <div class="agent-list-container">
      <!-- Header -->
      <div class="header-bar">
        <span class="title">Registered Agents</span>
        <span class="agent-count">{{ agents().length }} agents</span>
        <button class="refresh-btn" (click)="refresh()" [disabled]="isLoading()">
          {{ isLoading() ? 'Loading...' : 'Refresh' }}
        </button>
      </div>

      <!-- Assignment Dialog -->
      @if (showAssignDialog()) {
        <div class="dialog-overlay" (click)="closeAssignDialog()">
          <div class="dialog-content" (click)="$event.stopPropagation()">
            <div class="dialog-header">
              <span class="dialog-title">Assign Job to Agent</span>
              <button class="close-btn" (click)="closeAssignDialog()">&times;</button>
            </div>
            <div class="dialog-body">
              @if (availableJobs().length === 0) {
                <div class="no-jobs">
                  <span>No jobs available for assignment</span>
                  <span class="hint">Create a job first or wait for jobs with "created" status</span>
                </div>
              } @else {
                <div class="job-select-list">
                  @for (job of availableJobs(); track job.id) {
                    <div
                      class="job-option"
                      [class.selected]="selectedJobId() === job.id"
                      (click)="selectJob(job.id)"
                    >
                      <span class="job-prompt">{{ truncatePrompt(job.prompt) }}</span>
                      <span class="job-meta">{{ job.id.slice(0, 8) }}... | {{ formatDate(job.created_at) }}</span>
                    </div>
                  }
                </div>
              }
            </div>
            <div class="dialog-footer">
              <button class="btn btn-secondary" (click)="closeAssignDialog()">Cancel</button>
              <button
                class="btn btn-primary"
                [disabled]="!selectedJobId() || isAssigning()"
                (click)="confirmAssignment()"
              >
                @if (isAssigning()) {
                  Assigning...
                } @else {
                  Assign Job
                }
              </button>
            </div>
          </div>
        </div>
      }

      <!-- Loading State -->
      @if (isLoading() && agents().length === 0) {
        <div class="loading-state">
          <div class="spinner"></div>
          <span>Loading agents...</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && agents().length === 0) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F916;</span>
          <span>No agents registered</span>
          <span class="empty-hint">Agents will appear here when they connect to the orchestrator</span>
        </div>
      }

      <!-- Agent Table -->
      @if (agents().length > 0) {
        <div class="table-container">
          <table class="agent-table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Config</th>
                <th>Hostname</th>
                <th>Current Job</th>
                <th>Last Heartbeat</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              @for (agent of agents(); track agent.id) {
                <tr [class]="'status-' + agent.status">
                  <td>
                    <span class="status-badge" [class]="'status-' + agent.status">
                      {{ getStatusIcon(agent.status) }} {{ agent.status }}
                    </span>
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
                      <button
                        class="action-btn assign"
                        (click)="openAssignDialog(agent.id)"
                        title="Assign a job to this agent"
                      >
                        Assign Job
                      </button>
                    }
                    @if (agent.status === 'offline' || agent.status === 'failed') {
                      <button class="action-btn remove" (click)="removeAgent(agent.id)">
                        Remove
                      </button>
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
            Auto-refresh: {{ autoRefreshEnabled() ? 'ON (30s)' : 'OFF' }}
          </span>
        }
        <button class="toggle-btn" (click)="toggleAutoRefresh()">
          {{ autoRefreshEnabled() ? 'Disable' : 'Enable' }}
        </button>
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

      .refresh-btn {
        padding: 5px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .refresh-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .refresh-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      /* Dialog Overlay */
      .dialog-overlay {
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.6);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 100;
      }

      .dialog-content {
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #45475a);
        border-radius: 8px;
        width: 90%;
        max-width: 400px;
        max-height: 80%;
        display: flex;
        flex-direction: column;
      }

      .dialog-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .dialog-title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .close-btn {
        background: none;
        border: none;
        color: var(--text-muted, #6c7086);
        font-size: 20px;
        cursor: pointer;
        padding: 0;
        line-height: 1;
      }

      .close-btn:hover {
        color: var(--text-primary, #cdd6f4);
      }

      .dialog-body {
        flex: 1;
        overflow: auto;
        padding: 16px;
      }

      .dialog-footer {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
        padding: 12px 16px;
        border-top: 1px solid var(--border-color, #313244);
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

      .btn {
        padding: 8px 16px;
        border: none;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .btn-secondary {
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
      }

      .btn-secondary:hover:not(:disabled) {
        background: var(--panel-header-bg, #1e1e2e);
      }

      .btn-primary {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
      }

      .btn-primary:hover:not(:disabled) {
        filter: brightness(1.1);
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

      .spinner {
        width: 32px;
        height: 32px;
        border: 3px solid var(--surface-0, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
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

      /* Status Badge */
      .status-badge {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 500;
        text-transform: capitalize;
      }

      .status-badge.status-ready {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .status-badge.status-working {
        background: rgba(249, 226, 175, 0.2);
        color: #f9e2af;
      }

      .status-badge.status-booting {
        background: rgba(137, 180, 250, 0.2);
        color: #89b4fa;
      }

      .status-badge.status-completed {
        background: rgba(148, 226, 213, 0.2);
        color: #94e2d5;
      }

      .status-badge.status-failed {
        background: rgba(243, 139, 168, 0.2);
        color: #f38ba8;
      }

      .status-badge.status-offline {
        background: rgba(108, 112, 134, 0.2);
        color: #6c7086;
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
      }

      .action-btn {
        padding: 4px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .action-btn.assign {
        color: #a6e3a1;
        border-color: #a6e3a1;
      }

      .action-btn.remove {
        color: #f38ba8;
        border-color: #f38ba8;
      }

      .action-btn:hover {
        background: rgba(255, 255, 255, 0.1);
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

      .toggle-btn {
        padding: 4px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
      }

      .toggle-btn:hover {
        background: var(--panel-header-bg, #1e1e2e);
      }
    `,
  ],
})
export class AgentListComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);

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
          this.showStatus('Job assigned successfully!', false);
          this.refresh();
        } else {
          this.showStatus('Failed to assign job', true);
        }
      },
      error: () => {
        this.isAssigning.set(false);
        this.showStatus('Error assigning job', true);
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

  getStatusIcon(status: AgentStatus): string {
    const icons: Record<AgentStatus, string> = {
      booting: '\u23F3',
      ready: '\u2705',
      working: '\u26A1',
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
      return `${diffSec}s ago`;
    }
    if (diffSec < 3600) {
      return `${Math.floor(diffSec / 60)}m ago`;
    }
    if (diffSec < 86400) {
      return `${Math.floor(diffSec / 3600)}h ago`;
    }
    return date.toLocaleDateString();
  }

  truncatePrompt(prompt: string, maxLength: number = 60): string {
    if (prompt.length <= maxLength) {
      return prompt;
    }
    return prompt.slice(0, maxLength) + '...';
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-GB', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
    });
  }
}
