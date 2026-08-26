import {Component, computed, effect, inject, input, signal, untracked} from '@angular/core';
import {Router} from '@angular/router';
import {MarkdownComponent} from 'ngx-markdown';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ActionItem} from '../../core/models/action.model';
import {
  Notification,
  NotificationSource,
  NotificationStep,
  SourceAutomation,
  SourceJob,
  SourceLoop,
  SourceMessageThread,
  SourcePermissionRequest,
  SourceSudoRequest,
  SourceThread,
  SourceUser,
  SudoRequestRow,
} from '../../core/models/notification.model';
import {ActionCenterService} from '../../core/services/action-center.service';
import {NotificationService} from '../../core/services/notification.service';
import {SudoService} from '../../core/services/sudo.service';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppButtonComponent} from '../../ui/button';
import {AppIconComponent} from '../../ui/icon';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppCopyFieldComponent} from '../../ui/copy-field';
import {ExternalImageDirective} from '../../ui/external-image';
import {NotificationActEvent, NotificationActionsComponent} from './notification-actions.component';

/** Icon per category; unknown categories fall back to a generic bell. */
const CATEGORY_ICON: Record<string, string> = {
  review_queue: 'rate_review',
  vm_upgrade: 'cloud_upload',
  budget_exceeded: 'account_balance_wallet',
  incident: 'error',
  officer_question: 'campaign',
  officer_runtime: 'military_tech',
  agent_message: 'mail',
  session_wake: 'smart_toy',
  loop_event: 'all_inclusive',
  automation_disabled: 'schedule',
  user_registered: 'person_add',
  session_permission: 'key',
  sudo_request: 'admin_panel_settings',
};

export function categoryIcon(category: string): string {
  return CATEGORY_ICON[category] ?? 'notifications';
}

/** Risk level for sudo visual badging (presentation only). */
export function sudoRiskLevel(req: SudoRequestRow): 'low' | 'medium' | 'high' | 'critical' {
  const cmd = req.arguments?.join(' ') || req.command || '';
  if (/rm\s+-rf|chmod\s+777|mkfs|dd\s+if=/.test(cmd)) return 'critical';
  if (/chmod|chown|passwd|useradd|userdel|visudo/.test(cmd)) return 'high';
  if (/apt|dnf|yum|pip|npm|install|systemctl/.test(cmd)) return 'medium';
  return 'low';
}

export function sudoSecondsLeft(req: SudoRequestRow, now = Date.now()): number {
  if (!req.expires_at) return 0;
  return Math.max(0, Math.floor((new Date(req.expires_at).getTime() - now) / 1000));
}

/**
 * Detail pane for one feed row (unified notification system).
 *
 * Presentation only: subject, body, the generic action bar, the source's
 * payload (fetched from `GET /api/notifications/{id}`, rendered per
 * `source.kind`), the row's deferred channel steps, and the engagement
 * timeline. Category-specific *meaning* stays on the server — this pane
 * renders whatever the row declares and whatever the source loader returns.
 */
@Component({
  selector: 'app-notification-detail',
  standalone: true,
  imports: [
    MarkdownComponent,
    ExternalImageDirective,
    TranslocoPipe,
    AppBadgeComponent,
    AppButtonComponent,
    AppIconComponent,
    AppIconButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppCopyFieldComponent,
    NotificationActionsComponent,
  ],
  template: `
    <div class="detail-content notification-detail">
      <div class="detail-header">
        <span class="detail-type-badge notification">
          <app-icon size="sm">{{ icon() }}</app-icon>
          {{ ('notifications.category.' + n().category) | transloco }}
        </span>
        <app-badge [tone]="severityTone()" appearance="solid" size="xs" [uppercase]="true">
          {{ ('notifications.severity.' + n().severity) | transloco }}
        </app-badge>
        @if (n().resolved_at) {
          <app-badge tone="neutral" size="xs" [uppercase]="true">
            {{ 'notifications.status.resolved' | transloco }}
          </app-badge>
        } @else if (!n().seen_at) {
          <app-badge tone="accent" size="xs" [uppercase]="true">
            {{ 'notifications.status.unseen' | transloco }}
          </app-badge>
        }
        @if (sudo(); as req) {
          @if (req.request_type !== 'vm_upgrade') {
            <app-badge [tone]="riskTone(req)" appearance="solid" size="xs" [uppercase]="true">
              {{ ('notifications.risk.' + risk(req)) | transloco }}
            </app-badge>
          }
          @if (req.status === 'pending' && req.expires_at) {
            <span class="detail-countdown" [class]="'ttl-' + ttlColor(req)">
              {{ 'notifications.detail.secondsRemaining' | transloco: {seconds: secondsLeft(req)} }}
            </span>
          }
        }
      </div>

      <h3 class="notification-subject">{{ n().subject }}</h3>

      @if (n().body) {
        <div class="notification-body">
          <markdown [data]="n().body"></markdown>
        </div>
      }

      @if (error()) {
        <div class="action-error">{{ error() }}</div>
      }

      <div class="action-bar">
        <app-notification-actions
          [actions]="n().actions"
          [resolved]="!!n().resolved_at"
          [busy]="acting()"
          (act)="onAct($event)"
        />
      </div>

      @if (n().source_ref) {
        <div class="source-section">
          <div class="section-title">{{ 'notifications.detail.source' | transloco }}</div>

          @if (loadingSource()) {
            <div class="source-loading">{{ 'notifications.detail.loadingSource' | transloco }}</div>
          } @else if (job(); as src) {
            <!-- ===== job ===== -->
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.jobStatus' | transloco }}</span>
              <span class="meta-value">{{ src.job.status }}</span>
              @if (src.job.description) {
                <span class="meta-label">{{ 'notifications.detail.jobDescription' | transloco }}</span>
                <span class="meta-value">{{ src.job.description }}</span>
              }
              @if (src.job.config_name) {
                <span class="meta-label">{{ 'notifications.detail.configName' | transloco }}</span>
                <span class="meta-value">{{ src.job.config_name }}</span>
              }
              @if (src.freeze_data?.['freeze_type']) {
                <span class="meta-label">{{ 'notifications.detail.freezeType' | transloco }}</span>
                <span class="meta-value">{{ src.freeze_data!['freeze_type'] }}</span>
              }
              @if (src.freeze_data?.['phase_number'] !== undefined && src.freeze_data?.['phase_number'] !== null) {
                <span class="meta-label">{{ 'notifications.detail.phase' | transloco }}</span>
                <span class="meta-value">{{ src.freeze_data!['phase_number'] }}</span>
              }
              @if (src.job.error_message) {
                <span class="meta-label">{{ 'notifications.detail.error' | transloco }}</span>
                <span class="meta-value">{{ src.job.error_message }}</span>
              }
            </div>
            @if (freezeSummary(); as summary) {
              <div class="sub-section">
                <div class="section-title">{{ 'notifications.detail.summary' | transloco }}</div>
                <div class="markdown-block"><markdown [data]="summary"></markdown></div>
              </div>
            }
            @if (freezeCommand(); as command) {
              <div class="command-block"><code>{{ command }}</code></div>
            }
          } @else if (sudo(); as req) {
            <!-- ===== sudo_request ===== -->
            <div class="command-block">
              <code>{{ req.arguments?.join(' ') || req.command }}</code>
            </div>
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.requestType' | transloco }}</span>
              <span class="meta-value">{{ req.request_type }}</span>
              <span class="meta-label">{{ 'notifications.detail.requestStatus' | transloco }}</span>
              <span class="meta-value">{{ req.status }}</span>
              @if (req.vm_name) {
                <span class="meta-label">{{ 'notifications.detail.vm' | transloco }}</span>
                <span class="meta-value">{{ req.vm_name }}</span>
              }
              @if (req.requesting_user || req.target_user) {
                <span class="meta-label">{{ 'notifications.detail.user' | transloco }}</span>
                <span class="meta-value">{{ req.requesting_user }} → {{ req.target_user }}</span>
              }
              @if (req.working_directory) {
                <span class="meta-label">{{ 'notifications.detail.cwd' | transloco }}</span>
                <span class="meta-value mono">{{ req.working_directory }}</span>
              }
              @if (req.decision_reason) {
                <span class="meta-label">{{ 'notifications.detail.reason' | transloco }}</span>
                <span class="meta-value">{{ req.decision_reason }}</span>
              }
            </div>

            <details class="rules-section">
              <summary class="rules-toggle">
                <app-icon size="sm">settings</app-icon>
                {{ 'notifications.sudo.rulesToggle' | transloco: {count: sudoRules.rules().length} }}
              </summary>
              <div class="rules-body">
                <div class="rule-form">
                  <app-input
                    size="sm"
                    [(value)]="newRulePattern"
                    [placeholder]="'notifications.sudo.rulePatternPlaceholder' | transloco"
                  />
                  <app-select size="sm" [(value)]="newRuleAction">
                    <option value="approve">{{ 'notifications.sudo.ruleActionApprove' | transloco }}</option>
                    <option value="deny">{{ 'notifications.sudo.ruleActionDeny' | transloco }}</option>
                  </app-select>
                  <app-button variant="primary" size="sm" (clicked)="addRule()">
                    {{ 'notifications.sudo.ruleAdd' | transloco }}
                  </app-button>
                </div>
                @for (rule of sudoRules.rules(); track rule.id) {
                  <div class="rule-row">
                    <app-badge
                      [tone]="rule.action === 'approve' ? 'success' : 'danger'"
                      appearance="solid"
                      size="xs"
                      [uppercase]="true"
                    >{{ rule.action }}</app-badge>
                    <code class="rule-pattern">{{ rule.pattern }}</code>
                    <app-icon-button
                      size="sm"
                      variant="danger"
                      [ariaLabel]="'notifications.sudo.ruleDelete' | transloco"
                      (clicked)="sudoRules.deleteRule(rule.id)"
                    >
                      <app-icon size="sm">close</app-icon>
                    </app-icon-button>
                  </div>
                }
              </div>
            </details>
          } @else if (messageThread(); as src) {
            <!-- ===== message_thread ===== -->
            <div class="thread-body">
              @for (msg of src.messages; track msg.id) {
                <div class="msg-bubble" [class]="'msg-' + msg.direction">
                  <div class="msg-header">
                    <span class="msg-sender">
                      {{ (msg.direction === 'outbound' ? 'notifications.detail.senderAgent' : 'notifications.detail.senderYou') | transloco }}
                    </span>
                    @if (msg.mode === 'blocking') {
                      <app-badge tone="danger" size="xs" [uppercase]="true">
                        {{ 'notifications.detail.modeBlocking' | transloco }}
                      </app-badge>
                    }
                    <span class="msg-time">{{ fmt(msg.created_at) }}</span>
                  </div>
                  <div class="msg-content"><markdown [data]="msg.message"></markdown></div>
                </div>
              } @empty {
                <div class="source-loading">{{ 'notifications.detail.noMessages' | transloco }}</div>
              }
            </div>
          } @else if (thread(); as src) {
            <!-- ===== thread (session) ===== -->
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.session' | transloco }}</span>
              <span class="meta-value">{{ src.thread.title || src.thread.id }}</span>
              @if (src.thread.config_name) {
                <span class="meta-label">{{ 'notifications.detail.configName' | transloco }}</span>
                <span class="meta-value">{{ src.thread.config_name }}</span>
              }
              @if (src.thread.status) {
                <span class="meta-label">{{ 'notifications.detail.status' | transloco }}</span>
                <span class="meta-value">{{ src.thread.status }}</span>
              }
            </div>
          } @else if (permission(); as src) {
            <!-- ===== permission_request ===== -->
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.tool' | transloco }}</span>
              <code class="meta-value mono">{{ src.request.tool_name }}</code>
              <span class="meta-label">{{ 'notifications.detail.requestStatus' | transloco }}</span>
              <span class="meta-value">{{ src.request.status }}</span>
              @if (src.request.requested_at) {
                <span class="meta-label">{{ 'notifications.detail.requestedAt' | transloco }}</span>
                <span class="meta-value">{{ fmt(src.request.requested_at) }}</span>
              }
              @if (src.request.decided_by) {
                <span class="meta-label">{{ 'notifications.detail.decidedBy' | transloco }}</span>
                <span class="meta-value">{{ src.request.decided_by }}</span>
              }
            </div>
            @if (src.request.tool_args) {
              <div class="sub-section">
                <div class="section-title">{{ 'notifications.detail.arguments' | transloco }}</div>
                <pre class="args-block">{{ toolArgs(src) }}</pre>
              </div>
            }
          } @else if (loop(); as src) {
            <!-- ===== loop ===== -->
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.loop' | transloco }}</span>
              <span class="meta-value">{{ src.loop.title || src.loop.name || src.loop.id }}</span>
              @if (src.loop.status) {
                <span class="meta-label">{{ 'notifications.detail.status' | transloco }}</span>
                <span class="meta-value">{{ src.loop.status }}</span>
              }
            </div>
          } @else if (automation(); as src) {
            <!-- ===== automation ===== -->
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.automation' | transloco }}</span>
              <span class="meta-value">{{ src.automation.name || src.automation.id }}</span>
              <span class="meta-label">{{ 'notifications.detail.status' | transloco }}</span>
              <span class="meta-value">
                {{ (src.automation.enabled ? 'notifications.detail.enabled' : 'notifications.detail.disabled') | transloco }}
              </span>
              @if (src.automation.disabled_reason) {
                <span class="meta-label">{{ 'notifications.detail.reason' | transloco }}</span>
                <span class="meta-value">{{ src.automation.disabled_reason }}</span>
              }
            </div>
          } @else if (user(); as src) {
            <!-- ===== user ===== -->
            <div class="source-summary">
              <span class="meta-label">{{ 'notifications.detail.userName' | transloco }}</span>
              <span class="meta-value">{{ src.user.display_name || src.user.id }}</span>
              @if (src.user.email) {
                <span class="meta-label">{{ 'notifications.detail.email' | transloco }}</span>
                <span class="meta-value">{{ src.user.email }}</span>
              }
              <span class="meta-label">{{ 'notifications.detail.status' | transloco }}</span>
              <span class="meta-value">
                {{ (src.user.is_approved ? 'notifications.detail.approved' : 'notifications.detail.pendingApproval') | transloco }}
              </span>
            </div>
          } @else if (sourceMissing()) {
            <div class="source-loading">{{ 'notifications.detail.sourceMissing' | transloco }}</div>
          }

          <div class="detail-ids">
            @if (jobId(); as id) {
              <app-copy-field [label]="'notifications.detail.jobId' | transloco" [value]="id" />
            }
            <app-copy-field
              [label]="('notifications.source.' + n().source_ref!.kind) | transloco"
              [value]="n().source_ref!.id"
            />
          </div>
        </div>
      }

      @if (steps().length) {
        <div class="steps-section">
          <div class="section-title">{{ 'notifications.detail.steps' | transloco }}</div>
          <div class="steps-grid">
            @for (step of steps(); track step.id) {
              <span class="meta-value">{{ ('notifications.channel.' + step.channel) | transloco }}</span>
              <span class="meta-value">{{ fmt(step.due_at) }}</span>
              <app-badge [tone]="stepTone(step)" size="xs" [uppercase]="true">
                {{ ('notifications.stepState.' + step.state) | transloco }}
              </app-badge>
              <span class="meta-label">
                @if (step.state === 'pending' && step.conditions.length) {
                  {{ 'notifications.detail.stepUnless' | transloco: {conditions: conditionsLabel(step)} }}
                } @else if (step.detail) {
                  {{ step.detail }}
                }
              </span>
            }
          </div>
        </div>
      }

      <div class="timeline">
        <div class="section-title">{{ 'notifications.detail.timeline' | transloco }}</div>
        <div class="timeline-grid">
          <span class="meta-label">{{ 'notifications.detail.created' | transloco }}</span>
          <span class="meta-value">{{ fmt(n().created_at) }}</span>
          <span class="meta-label">{{ 'notifications.detail.seen' | transloco }}</span>
          <span class="meta-value">{{ fmt(n().seen_at) }}</span>
          <span class="meta-label">{{ 'notifications.detail.read' | transloco }}</span>
          <span class="meta-value">{{ fmt(n().read_at) }}</span>
          <span class="meta-label">{{ 'notifications.detail.resolved' | transloco }}</span>
          <span class="meta-value">
            {{ fmt(n().resolved_at) }}
            @if (n().resolved_by) {
              <span class="resolved-by">· {{ n().resolved_by }}</span>
            }
          </span>
        </div>
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; }
    .detail-header {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .detail-type-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
    }
    .detail-countdown {
      font-size: 12px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      margin-left: auto;
    }
    .ttl-green { color: var(--success); }
    .ttl-amber { color: var(--warning); }
    .ttl-red { color: var(--danger); }
    .notification-subject {
      margin: 0 0 12px 0;
      font-size: 16px;
      font-weight: 600;
      color: var(--text-primary);
    }
    .notification-body {
      font-size: 14px;
      line-height: 1.55;
      color: var(--text-primary);
      margin-bottom: 16px;
    }
    .action-bar { margin-bottom: 20px; }
    .action-error {
      color: var(--danger);
      font-size: 12px;
      margin-bottom: 8px;
    }
    .section-title {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-secondary);
      margin-bottom: 6px;
    }
    .source-section, .steps-section { margin-bottom: 16px; }
    .sub-section { margin-top: 10px; }
    .detail-ids {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-top: 10px;
    }
    .source-summary, .timeline-grid {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 4px 12px;
      font-size: 13px;
    }
    .steps-grid {
      display: grid;
      grid-template-columns: max-content max-content max-content 1fr;
      gap: 4px 12px;
      align-items: center;
      font-size: 13px;
    }
    .meta-label { color: var(--text-secondary); }
    .meta-value { color: var(--text-primary); overflow-wrap: anywhere; }
    .mono { font-family: var(--font-mono, monospace); }
    .source-loading { font-size: 12px; color: var(--text-secondary); }
    .resolved-by { color: var(--text-secondary); font-size: 12px; }
    .markdown-block {
      font-size: 13px;
      line-height: 1.55;
      color: var(--text-primary);
    }
    .command-block {
      background: var(--surface-1);
      border: 1px solid var(--surface-0);
      border-radius: var(--radius-tag);
      padding: 12px 14px;
      margin: 8px 0;
    }
    .command-block code {
      font-size: 13px;
      color: var(--text-primary);
      word-break: break-all;
      line-height: 1.5;
    }
    .args-block {
      background: var(--surface-0);
      border-radius: var(--radius-tag);
      padding: 8px 12px;
      font-size: 12px;
      overflow-x: auto;
      max-height: 240px;
      margin: 0;
    }

    /* message thread */
    .thread-body {
      display: flex;
      flex-direction: column;
      gap: 12px;
      max-height: 50vh;
      overflow-y: auto;
      padding: 4px 0;
      scrollbar-width: thin;
    }
    .msg-bubble {
      border-radius: var(--radius-surface);
      padding: 10px 14px;
      max-width: 85%;
    }
    .msg-outbound {
      background: var(--surface-1);
      border: 1px solid var(--surface-0);
      align-self: flex-start;
    }
    .msg-inbound {
      background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      border: 1px solid color-mix(in srgb, var(--accent-color) 20%, transparent);
      align-self: flex-end;
    }
    .msg-header {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
    }
    .msg-sender {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-secondary);
      flex: 1;
    }
    .msg-time { font-size: 10px; color: var(--text-secondary); }
    .msg-content {
      font-size: 13px;
      line-height: 1.55;
      color: var(--text-primary);
    }
    .msg-content ::ng-deep {
      p { margin: 0 0 8px; }
      p:last-child { margin-bottom: 0; }
      code {
        background: var(--surface-0);
        padding: 1px 4px;
        border-radius: var(--radius-tag);
        font-size: 12px;
      }
      pre {
        background: var(--surface-0);
        border-radius: var(--radius-tag);
        padding: 8px 10px;
        overflow-x: auto;
        margin: 6px 0;
      }
      pre code { background: none; padding: 0; }
    }

    /* sudo rules */
    .rules-section {
      border: 1px solid var(--surface-0);
      border-radius: var(--radius-surface);
      overflow: hidden;
      margin-top: 10px;
    }
    .rules-toggle {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 8px 12px;
      font-size: 12px;
      color: var(--text-secondary);
      cursor: pointer;
      list-style: none;
    }
    .rules-toggle::-webkit-details-marker { display: none; }
    .rules-body { border-top: 1px solid var(--surface-0); }
    .rule-form {
      display: flex;
      gap: 6px;
      padding: 8px;
      border-bottom: 1px solid var(--surface-0);
      align-items: center;
    }
    .rule-form app-input { flex: 1; min-width: 0; }
    /* Bound the approve/deny <select>: its base-select field is width:100%,
       so without a definite host width it starves the pattern input. */
    .rule-form app-select { flex: 0 0 7.5rem; }
    .rule-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border-bottom: 1px solid var(--surface-0);
      font-size: 11px;
    }
    .rule-row:last-child { border-bottom: none; }
    .rule-pattern { flex: 1; font-size: 11px; color: var(--text-primary); }
  `],
})
export class NotificationDetailComponent {
  readonly item = input.required<ActionItem>();

  private readonly actionCenter = inject(ActionCenterService);
  private readonly notifications = inject(NotificationService);
  readonly sudoRules = inject(SudoService);
  private readonly router = inject(Router);
  private readonly transloco = inject(TranslocoService);

  readonly n = computed<Notification>(() => this.item().notification);
  readonly icon = computed(() => categoryIcon(this.n().category));

  readonly source = signal<NotificationSource | null>(null);
  readonly steps = signal<NotificationStep[]>([]);
  readonly loadingSource = signal(false);
  readonly sourceMissing = signal(false);
  readonly acting = signal(false);
  readonly error = signal<string | null>(null);

  // Sudo auto-approval rule form (the sudo pane only).
  newRulePattern = '';
  newRuleAction = 'approve';

  readonly job = computed(() => this.ofKind<SourceJob>('job'));
  readonly sudo = computed(() => this.ofKind<SourceSudoRequest>('sudo_request')?.request ?? null);
  readonly thread = computed(() => this.ofKind<SourceThread>('thread'));
  readonly messageThread = computed(() => this.ofKind<SourceMessageThread>('message_thread'));
  readonly loop = computed(() => this.ofKind<SourceLoop>('loop'));
  readonly automation = computed(() => this.ofKind<SourceAutomation>('automation'));
  readonly user = computed(() => this.ofKind<SourceUser>('user'));
  readonly permission = computed(() => this.ofKind<SourcePermissionRequest>('permission_request'));

  readonly freezeSummary = computed(() => {
    const v = this.job()?.freeze_data?.['summary'];
    return typeof v === 'string' && v ? v : null;
  });
  readonly freezeCommand = computed(() => {
    const v = this.job()?.freeze_data?.['command'];
    return typeof v === 'string' && v ? v : null;
  });
  /** The job behind the row, from whichever side names it. */
  readonly jobId = computed<string | null>(() => {
    const ref = this.n().source_ref;
    if (ref?.kind === 'job') return null; // the source field already shows it
    return (
      this.item().jobId ??
      this.sudo()?.job_id ??
      this.messageThread()?.job_id ??
      null
    );
  });

  constructor() {
    // Re-fetch the source payload whenever a different row is selected.
    effect(() => {
      const id = this.n().id;
      untracked(() => this.loadSource(id));
    });
    effect(() => {
      // The rules editor only lives in the sudo pane; load once it appears.
      if (this.sudo()) untracked(() => this.sudoRules.loadRules());
    });
  }

  private ofKind<T extends NotificationSource>(kind: T['kind']): T | null {
    const src = this.source();
    return src && src.kind === kind ? (src as T) : null;
  }

  private loadSource(id: string): void {
    this.error.set(null);
    this.sourceMissing.set(false);
    this.steps.set([]);
    this.source.set(null);
    if (!this.n().source_ref) {
      // No source — the steps still matter ("email at 21:45 unless…").
      this.notifications.getNotification(id).subscribe((detail) => {
        this.steps.set(detail?.steps ?? []);
      });
      return;
    }
    this.loadingSource.set(true);
    this.notifications.getNotification(id).subscribe((detail) => {
      this.source.set(detail?.source ?? null);
      this.steps.set(detail?.steps ?? []);
      this.sourceMissing.set(!!detail && !detail.source);
      this.loadingSource.set(false);
    });
  }

  onAct(event: NotificationActEvent): void {
    this.acting.set(true);
    this.error.set(null);
    this.actionCenter.act(this.n().id, event.type, event.params).subscribe({
      next: (res) => {
        this.acting.set(false);
        const nav = res.result?.['navigate'];
        if (typeof nav === 'string' && nav) this.router.navigateByUrl(nav);
        else if (this.messageThread()) this.loadSource(this.n().id); // a reply extends the thread
      },
      error: (err: {error?: {detail?: unknown}}) => {
        this.acting.set(false);
        const detail = err?.error?.detail;
        this.error.set(
          typeof detail === 'string'
            ? detail
            : this.transloco.translate('notifications.detail.actionFailed'),
        );
      },
    });
  }

  addRule(): void {
    const pattern = this.newRulePattern.trim();
    if (!pattern) return;
    this.sudoRules.createRule(pattern, this.newRuleAction);
    this.newRulePattern = '';
  }

  severityTone(): BadgeTone {
    switch (this.n().severity) {
      case 'critical': return 'danger';
      case 'high': return 'warning';
      case 'normal': return 'accent';
      default: return 'neutral';
    }
  }

  risk(req: SudoRequestRow): string { return sudoRiskLevel(req); }
  secondsLeft(req: SudoRequestRow): number { return sudoSecondsLeft(req); }

  riskTone(req: SudoRequestRow): BadgeTone {
    switch (sudoRiskLevel(req)) {
      case 'low': return 'success';
      case 'medium': return 'warning';
      case 'high': return 'alert';
      case 'critical': return 'danger';
      default: return 'neutral';
    }
  }

  ttlColor(req: SudoRequestRow): string {
    const s = sudoSecondsLeft(req);
    if (s < 30) return 'red';
    if (s < 120) return 'amber';
    return 'green';
  }

  stepTone(step: NotificationStep): BadgeTone {
    switch (step.state) {
      case 'pending': return 'accent';
      case 'done': return 'success';
      case 'failed': return 'danger';
      default: return 'neutral';
    }
  }

  conditionsLabel(step: NotificationStep): string {
    return step.conditions
      .map((c) => this.transloco.translate(`notifications.condition.${c}`))
      .join(', ');
  }

  toolArgs(src: SourcePermissionRequest): string {
    const args = src.request.tool_args;
    if (typeof args === 'string') return args;
    return JSON.stringify(args, null, 2);
  }

  fmt(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(this.transloco.getActiveLang(), {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  }
}
