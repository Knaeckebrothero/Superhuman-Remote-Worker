import {
  Component,
  inject,
  signal,
  computed,
  OnInit,
  OnDestroy,
} from '@angular/core';
import { TranslocoPipe } from '@jsverse/transloco';
import { SudoService, SudoRequest } from '../../../core/services/sudo.service';
import { AppButtonComponent } from '../../../ui/button';
import { AppIconButtonComponent } from '../../../ui/icon-button';
import { AppInputComponent } from '../../../ui/input';
import { AppSelectComponent } from '../../../ui/select';
import { AppTextareaComponent } from '../../../ui/textarea';
import { AppDialogComponent } from '../../../ui/dialog';
import { AppBadgeComponent, type BadgeTone } from '../../../ui/badge';
import { AppIconComponent } from '../../../ui/icon';

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
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppDialogComponent,
    AppBadgeComponent,
    AppIconComponent,
  ],
  template: `
    <div class="sudo-page">
      <div class="page-header">
        <h2>{{ 'sudo.title' | transloco }}</h2>
        <div class="header-controls">
          <span class="status-dot" [class.connected]="sudo.isConnected()"></span>
          <app-select
            size="sm"
            [value]="activeFilter()"
            (changed)="setFilter($event ?? 'all')"
          >
            <option value="all">{{ 'sudo.filter.all' | transloco }}</option>
            <option value="pending">{{ 'sudo.filter.pending' | transloco }}</option>
            <option value="approved">{{ 'sudo.filter.approved' | transloco }}</option>
            <option value="denied">{{ 'sudo.filter.denied' | transloco }}</option>
            <option value="expired">{{ 'sudo.filter.expired' | transloco }}</option>
          </app-select>
          <app-icon-button
            size="sm"
            [ariaLabel]="'sudo.refresh' | transloco"
            (clicked)="refresh()"
          >
            <app-icon size="sm">refresh</app-icon>
          </app-icon-button>
        </div>
      </div>

      <div class="content-area">
        <!-- Pending requests panel -->
        <div class="panel requests-panel">
          <div class="panel-header">
            {{ 'sudo.panels.requests' | transloco }}
            @if (sudo.pendingCount() > 0) {
              <app-badge tone="danger" appearance="solid" size="xs">
                {{ sudo.pendingCount() }}
              </app-badge>
            }
          </div>

          @if (sudo.isLoading()) {
            <div class="loading">{{ 'sudo.loading' | transloco }}</div>
          } @else if (filteredRequests().length === 0) {
            <div class="empty">{{ 'sudo.emptyRequests' | transloco }}</div>
          } @else {
            <div class="request-list">
              @for (req of filteredRequests(); track req.id) {
                <div class="request-card" [class]="'risk-' + getRisk(req)">
                  <div class="card-header">
                    <app-badge
                      [tone]="riskTone(getRisk(req))"
                      appearance="solid"
                      size="xs"
                      [uppercase]="true"
                    >
                      {{ 'sudo.risk.' + getRisk(req) | transloco }}
                    </app-badge>
                    <app-badge
                      [tone]="sudoStatusTone(req.status)"
                      size="xs"
                      [uppercase]="true"
                    >
                      {{ 'sudo.status.' + req.status | transloco }}
                    </app-badge>
                    @if (req.status === 'pending') {
                      <span class="countdown">{{ 'sudo.secondsLeft' | transloco: {n: getSecondsLeft(req)} }}</span>
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
                      <app-button variant="success" size="sm" (clicked)="approve(req)">
                        {{ 'sudo.actions.approve' | transloco }}
                      </app-button>
                      <app-button variant="danger" size="sm" (clicked)="startDeny(req)">
                        {{ 'sudo.actions.deny' | transloco }}
                      </app-button>
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
          <div class="panel-header">{{ 'sudo.panels.rules' | transloco }}</div>

          <div class="rule-form">
            <app-input
              size="sm"
              [(value)]="newRulePattern"
              [placeholder]="'sudo.rule.patternPlaceholder' | transloco"
            />
            <app-select size="sm" [(value)]="newRuleAction">
              <option value="approve">{{ 'sudo.rule.actionApprove' | transloco }}</option>
              <option value="deny">{{ 'sudo.rule.actionDeny' | transloco }}</option>
              <option value="review">{{ 'sudo.rule.actionReview' | transloco }}</option>
            </app-select>
            <app-button variant="primary" size="sm" (clicked)="addRule()">
              {{ 'sudo.rule.add' | transloco }}
            </app-button>
          </div>

          @for (rule of sudo.rules(); track rule.id) {
            <div class="rule-row">
              <app-badge
                [tone]="ruleActionTone(rule.action)"
                appearance="solid"
                size="xs"
                [uppercase]="true"
              >
                {{ 'sudo.rule.badge.' + rule.action | transloco }}
              </app-badge>
              <code class="rule-pattern">{{ rule.pattern }}</code>
              @if (rule.description) {
                <span class="rule-desc">{{ rule.description }}</span>
              }
              <app-icon-button
                size="sm"
                variant="danger"
                [ariaLabel]="'sudo.rule.delete' | transloco"
                (clicked)="deleteRule(rule.id)"
              >
                <app-icon size="sm">close</app-icon>
              </app-icon-button>
            </div>
          }

          @if (sudo.rules().length === 0) {
            <div class="empty">{{ 'sudo.emptyRules' | transloco }}</div>
          }
        </div>
      </div>
    </div>

    <!-- Deny dialog -->
    <app-dialog
      [open]="!!denyTarget()"
      size="md"
      [closeOnBackdrop]="false"
      [title]="'sudo.denyDialog.title' | transloco: {command: denyTarget()?.command ?? ''}"
      (closed)="cancelDeny()"
    >
      <app-textarea
        [(value)]="denyReason"
        [placeholder]="'sudo.denyDialog.reasonPlaceholder' | transloco"
        [rows]="3"
      />
      <ng-container appDialogActions>
        <app-button variant="ghost" size="sm" (clicked)="cancelDeny()">
          {{ 'sudo.denyDialog.cancel' | transloco }}
        </app-button>
        <app-button
          variant="danger"
          size="sm"
          [disabled]="!denyReason.trim()"
          (clicked)="confirmDeny()"
        >
          {{ 'sudo.denyDialog.confirm' | transloco }}
        </app-button>
      </ng-container>
    </app-dialog>
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

      .rule-form app-input {
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

      .rule-pattern {
        flex: 1;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
      }

      .rule-desc {
        color: var(--text-muted, #6c7086);
        font-size: 11px;
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

  sudoStatusTone(status: string | undefined): BadgeTone {
    switch (status) {
      case 'pending': return 'warning';
      case 'approved':
      case 'auto_approved': return 'success';
      case 'denied':
      case 'auto_denied':
      case 'expired': return 'danger';
      default: return 'neutral';
    }
  }

  ruleActionTone(action: string): BadgeTone {
    switch (action) {
      case 'approve': return 'success';
      case 'deny': return 'danger';
      case 'review': return 'warning';
      default: return 'neutral';
    }
  }

  riskTone(risk: string): BadgeTone {
    switch (risk) {
      case 'low': return 'success';
      case 'medium': return 'warning';
      case 'high': return 'alert';
      case 'critical': return 'danger';
      default: return 'neutral';
    }
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
