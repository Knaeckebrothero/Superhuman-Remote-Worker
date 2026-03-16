import {
  Component,
  inject,
  signal,
  computed,
  OnInit,
  OnDestroy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { SudoService, SudoRequest } from '../../../core/services/sudo.service';

/** Risk level for visual badging. */
function riskLevel(req: SudoRequest): 'low' | 'medium' | 'high' | 'critical' {
  const cmd = req.arguments.join(' ') || req.command;
  if (/rm\s+-rf|chmod\s+777|mkfs|dd\s+if=/.test(cmd)) return 'critical';
  if (/chmod|chown|passwd|useradd|userdel|visudo/.test(cmd)) return 'high';
  if (/apt|dnf|yum|pip|npm|install|systemctl/.test(cmd)) return 'medium';
  return 'low';
}

/** Seconds remaining until expiry. */
function secondsLeft(req: SudoRequest): number {
  if (!req.expires_at) return 0;
  const diff = new Date(req.expires_at).getTime() - Date.now();
  return Math.max(0, Math.floor(diff / 1000));
}

@Component({
  selector: 'app-sudo-page',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="sudo-page">
      <div class="page-header">
        <h2>Sudo Approvals</h2>
        <div class="header-controls">
          <span class="status-dot" [class.connected]="sudo.isConnected()"></span>
          <select class="filter-select" (change)="setFilter($any($event.target).value)">
            <option value="all">All</option>
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="denied">Denied</option>
            <option value="expired">Expired</option>
          </select>
          <button class="btn-refresh" (click)="refresh()">
            <span class="icon">refresh</span>
          </button>
        </div>
      </div>

      <div class="content-area">
        <!-- Pending requests panel -->
        <div class="panel requests-panel">
          <div class="panel-header">
            Requests
            @if (sudo.pendingCount() > 0) {
              <span class="badge">{{ sudo.pendingCount() }}</span>
            }
          </div>

          @if (sudo.isLoading()) {
            <div class="loading">Loading...</div>
          } @else if (filteredRequests().length === 0) {
            <div class="empty">No requests</div>
          } @else {
            <div class="request-list">
              @for (req of filteredRequests(); track req.id) {
                <div class="request-card" [class]="'risk-' + getRisk(req)">
                  <div class="card-header">
                    <span class="risk-badge" [class]="'risk-' + getRisk(req)">
                      {{ getRisk(req) }}
                    </span>
                    <span class="status-badge" [class]="'status-' + req.status">
                      {{ req.status }}
                    </span>
                    @if (req.status === 'pending') {
                      <span class="countdown">{{ getSecondsLeft(req) }}s</span>
                    }
                  </div>

                  <div class="card-command">
                    <code>{{ req.arguments.join(' ') || req.command }}</code>
                  </div>

                  <div class="card-meta">
                    <span>{{ req.requesting_user }} → {{ req.target_user }}</span>
                    <span>{{ req.vm_name }}</span>
                    @if (req.working_directory) {
                      <span class="cwd">{{ req.working_directory }}</span>
                    }
                  </div>

                  @if (req.status === 'pending') {
                    <div class="card-actions">
                      <button
                        class="btn-approve"
                        (click)="approve(req)"
                      >
                        Approve
                      </button>
                      <button
                        class="btn-deny"
                        (click)="startDeny(req)"
                      >
                        Deny
                      </button>
                    </div>
                  }

                  @if (req.decision_reason) {
                    <div class="card-reason">{{ req.decision_reason }}</div>
                  }
                </div>
              }
            </div>
          }
        </div>

        <!-- Rules panel -->
        <div class="panel rules-panel">
          <div class="panel-header">Auto-Approval Rules</div>

          <div class="rule-form">
            <input
              class="input"
              placeholder="Pattern (e.g. apt-get install *)"
              [(ngModel)]="newRulePattern"
            />
            <select class="filter-select" [(ngModel)]="newRuleAction">
              <option value="approve">Approve</option>
              <option value="deny">Deny</option>
              <option value="review">Review</option>
            </select>
            <button class="btn-add" (click)="addRule()">Add</button>
          </div>

          @for (rule of sudo.rules(); track rule.id) {
            <div class="rule-row">
              <span class="rule-action" [class]="'action-' + rule.action">
                {{ rule.action }}
              </span>
              <code class="rule-pattern">{{ rule.pattern }}</code>
              @if (rule.description) {
                <span class="rule-desc">{{ rule.description }}</span>
              }
              <button class="btn-delete" (click)="deleteRule(rule.id)">
                <span class="icon">close</span>
              </button>
            </div>
          }

          @if (sudo.rules().length === 0) {
            <div class="empty">No rules configured</div>
          }
        </div>
      </div>
    </div>

    <!-- Deny dialog -->
    @if (denyTarget()) {
      <div class="dialog-overlay" (click)="cancelDeny()">
        <div class="dialog" (click)="$event.stopPropagation()">
          <div class="dialog-title">Deny: {{ denyTarget()!.command }}</div>
          <textarea
            class="input reason-input"
            placeholder="Reason for denial (required)"
            [(ngModel)]="denyReason"
            rows="3"
          ></textarea>
          <div class="dialog-actions">
            <button class="btn-cancel" (click)="cancelDeny()">Cancel</button>
            <button
              class="btn-deny"
              [disabled]="!denyReason.trim()"
              (click)="confirmDeny()"
            >
              Deny
            </button>
          </div>
        </div>
      </div>
    }
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }

      .sudo-page {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--app-bg, #1e1e2e);
      }

      .page-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .page-header h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .header-controls {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--text-muted, #6c7086);
      }
      .status-dot.connected {
        background: #a6e3a1;
      }

      .filter-select,
      .input {
        padding: 5px 8px;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-family: inherit;
      }

      .btn-refresh,
      .btn-delete {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        background: transparent;
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
      }
      .btn-refresh:hover,
      .btn-delete:hover {
        color: var(--text-primary, #cdd6f4);
        border-color: var(--text-muted, #6c7086);
      }

      .icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .content-area {
        display: flex;
        flex: 1;
        gap: 1px;
        background: var(--border-color, #313244);
        overflow: hidden;
      }

      .panel {
        display: flex;
        flex-direction: column;
        background: var(--panel-bg, #181825);
        overflow-y: auto;
      }

      .requests-panel {
        flex: 2;
      }

      .rules-panel {
        flex: 1;
        min-width: 280px;
      }

      .panel-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted, #6c7086);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .badge {
        background: #f38ba8;
        color: #11111b;
        font-size: 10px;
        font-weight: 700;
        padding: 1px 6px;
        border-radius: 10px;
      }

      .loading,
      .empty {
        padding: 24px;
        text-align: center;
        color: var(--text-muted, #6c7086);
        font-size: 13px;
      }

      .request-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 8px;
      }

      .request-card {
        background: var(--surface-0, #313244);
        border-radius: 6px;
        padding: 10px 12px;
        border-left: 3px solid var(--border-color, #313244);
      }
      .request-card.risk-low {
        border-left-color: #a6e3a1;
      }
      .request-card.risk-medium {
        border-left-color: #f9e2af;
      }
      .request-card.risk-high {
        border-left-color: #fab387;
      }
      .request-card.risk-critical {
        border-left-color: #f38ba8;
      }

      .card-header {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 6px;
      }

      .risk-badge {
        font-size: 9px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        padding: 1px 5px;
        border-radius: 3px;
        color: #11111b;
      }
      .risk-badge.risk-low {
        background: #a6e3a1;
      }
      .risk-badge.risk-medium {
        background: #f9e2af;
      }
      .risk-badge.risk-high {
        background: #fab387;
      }
      .risk-badge.risk-critical {
        background: #f38ba8;
      }

      .status-badge {
        font-size: 9px;
        font-weight: 600;
        text-transform: uppercase;
        padding: 1px 5px;
        border-radius: 3px;
        color: var(--text-muted, #6c7086);
        background: var(--panel-bg, #181825);
      }
      .status-badge.status-pending {
        color: #f9e2af;
      }
      .status-badge.status-approved,
      .status-badge.status-auto_approved {
        color: #a6e3a1;
      }
      .status-badge.status-denied,
      .status-badge.status-auto_denied,
      .status-badge.status-expired {
        color: #f38ba8;
      }

      .countdown {
        margin-left: auto;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        font-variant-numeric: tabular-nums;
      }

      .card-command {
        margin-bottom: 6px;
      }

      .card-command code {
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        word-break: break-all;
      }

      .card-meta {
        display: flex;
        gap: 12px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        margin-bottom: 8px;
      }

      .cwd {
        opacity: 0.7;
      }

      .card-actions {
        display: flex;
        gap: 6px;
      }

      .btn-approve,
      .btn-deny,
      .btn-add,
      .btn-cancel {
        padding: 4px 12px;
        border: none;
        border-radius: 4px;
        font-size: 12px;
        font-family: inherit;
        font-weight: 600;
        cursor: pointer;
      }

      .btn-approve {
        background: #a6e3a1;
        color: #11111b;
      }
      .btn-approve:hover {
        background: #94e2d5;
      }

      .btn-deny {
        background: #f38ba8;
        color: #11111b;
      }
      .btn-deny:hover {
        background: #eba0ac;
      }
      .btn-deny:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      .btn-add {
        background: var(--accent-color, #cba6f7);
        color: #11111b;
      }

      .btn-cancel {
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
      }

      .card-reason {
        margin-top: 6px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        font-style: italic;
      }

      /* Rules */

      .rule-form {
        display: flex;
        gap: 6px;
        padding: 8px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .rule-form .input {
        flex: 1;
      }

      .rule-row {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        border-bottom: 1px solid var(--border-color, #313244);
        font-size: 12px;
      }

      .rule-action {
        font-size: 9px;
        font-weight: 700;
        text-transform: uppercase;
        padding: 1px 5px;
        border-radius: 3px;
        flex-shrink: 0;
      }
      .action-approve {
        background: #a6e3a1;
        color: #11111b;
      }
      .action-deny {
        background: #f38ba8;
        color: #11111b;
      }
      .action-review {
        background: #f9e2af;
        color: #11111b;
      }

      .rule-pattern {
        flex: 1;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
      }

      .rule-desc {
        color: var(--text-muted, #6c7086);
        font-size: 11px;
      }

      /* Deny dialog */

      .dialog-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.6);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
      }

      .dialog {
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #313244);
        border-radius: 8px;
        padding: 16px;
        width: 400px;
        max-width: 90vw;
      }

      .dialog-title {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
        margin-bottom: 12px;
      }

      .reason-input {
        width: 100%;
        resize: vertical;
        min-height: 60px;
        margin-bottom: 12px;
        box-sizing: border-box;
      }

      .dialog-actions {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
      }
    `,
  ],
})
export class SudoPageComponent implements OnInit, OnDestroy {
  readonly sudo = inject(SudoService);

  readonly activeFilter = signal<string>('all');
  readonly denyTarget = signal<SudoRequest | null>(null);
  denyReason = '';
  newRulePattern = '';
  newRuleAction = 'approve';

  private countdownTimer: ReturnType<typeof setInterval> | null = null;

  readonly filteredRequests = computed(() => {
    const filter = this.activeFilter();
    const reqs = this.sudo.requests();
    if (filter === 'all') return reqs;
    return reqs.filter((r) => r.status === filter);
  });

  ngOnInit(): void {
    this.sudo.loadRequests();
    this.sudo.loadRules();
    this.sudo.connectSSE();

    // Refresh countdown every second for pending requests.
    this.countdownTimer = setInterval(() => {
      // Force signal re-evaluation by touching requests.
      // This is a lightweight tick — only pending countdowns need it.
    }, 1000);
  }

  ngOnDestroy(): void {
    this.sudo.disconnectSSE();
    if (this.countdownTimer) clearInterval(this.countdownTimer);
  }

  setFilter(value: string): void {
    this.activeFilter.set(value);
  }

  refresh(): void {
    this.sudo.loadRequests();
    this.sudo.loadRules();
  }

  getRisk(req: SudoRequest): string {
    return riskLevel(req);
  }

  getSecondsLeft(req: SudoRequest): number {
    return secondsLeft(req);
  }

  approve(req: SudoRequest): void {
    this.sudo.approve(req.id);
  }

  startDeny(req: SudoRequest): void {
    this.denyTarget.set(req);
    this.denyReason = '';
  }

  confirmDeny(): void {
    const target = this.denyTarget();
    if (target && this.denyReason.trim()) {
      this.sudo.deny(target.id, this.denyReason.trim());
      this.denyTarget.set(null);
      this.denyReason = '';
    }
  }

  cancelDeny(): void {
    this.denyTarget.set(null);
    this.denyReason = '';
  }

  addRule(): void {
    if (this.newRulePattern.trim()) {
      this.sudo.createRule(
        this.newRulePattern.trim(),
        this.newRuleAction,
      );
      this.newRulePattern = '';
    }
  }

  deleteRule(ruleId: string): void {
    this.sudo.deleteRule(ruleId);
  }
}
