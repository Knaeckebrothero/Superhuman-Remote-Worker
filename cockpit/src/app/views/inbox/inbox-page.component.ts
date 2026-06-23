import {
    Component,
    computed,
    ElementRef,
    HostListener,
    inject,
    NgZone,
    OnDestroy,
    OnInit,
    signal,
    ViewChild,
} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {JsonPipe} from '@angular/common';
import {MarkdownComponent} from 'ngx-markdown';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ViewportService} from '../../core/services/viewport.service';
import {ActionCenterService} from '../../core/services/action-center.service';
import {SudoRequest, SudoService} from '../../core/services/sudo.service';
import {ApiService} from '../../core/services/api.service';
import {ActionItem, ActionItemType, ThreadDetail,} from '../../core/models/action.model';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {AppChipComponent} from '../../ui/chip';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppDialogComponent} from '../../ui/dialog';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppCheckboxComponent} from '../../ui/checkbox';
import {AppIconComponent} from '../../ui/icon';
import {AppCopyFieldComponent} from '../../ui/copy-field';

interface FrozenJobData {
  freeze_type?: string;
  phase_type?: string;
  summary?: string;
  deliverables?: string[];
  confidence?: number;
  notes?: string;
  phase_number?: number;
  frozen_at?: string;
  command?: string;
  reason?: string;
}

/** Risk level for sudo visual badging. */
function riskLevel(req: SudoRequest): 'low' | 'medium' | 'high' | 'critical' {
  const cmd = req.arguments?.join(' ') || req.command;
  if (/rm\s+-rf|chmod\s+777|mkfs|dd\s+if=/.test(cmd)) return 'critical';
  if (/chmod|chown|passwd|useradd|userdel|visudo/.test(cmd)) return 'high';
  if (/apt|dnf|yum|pip|npm|install|systemctl/.test(cmd)) return 'medium';
  return 'low';
}

function secondsLeft(req: SudoRequest): number {
  if (!req.expires_at) return 0;
  return Math.max(0, Math.floor((new Date(req.expires_at).getTime() - Date.now()) / 1000));
}

function relativeTime(iso: string, nowLabel: string): string {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return nowLabel;
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

@Component({
  selector: 'app-inbox-page',
  standalone: true,
  imports: [
    JsonPipe,
    MarkdownComponent,
    SidebarToggleComponent,
    TranslocoPipe,
    AppChipComponent,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppDialogComponent,
    AppTextareaComponent,
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppCheckboxComponent,
    AppIconComponent,
    AppCopyFieldComponent,
  ],
  template: `
    <div class="inbox" (keydown)="onKeydown($event)">
      <!-- Header -->
      <header class="inbox-header">
        <div class="header-left">
          <app-sidebar-toggle />
          @if (isMobileDetail()) {
            <app-icon-button
              size="sm"
              [ariaLabel]="'inbox.backBtn' | transloco"
              [tooltip]="'inbox.backBtn' | transloco"
              (clicked)="deselect()"
            >
              <app-icon size="sm">arrow_back</app-icon>
            </app-icon-button>
          }
          <h1 class="header-title">{{ 'inbox.title' | transloco }}</h1>
        </div>

        <div class="filter-chips">
          <app-chip
            [selected]="activeFilter() === null"
            (clicked)="setFilter(null)"
          >
            {{ 'inbox.filters.all' | transloco }}
            @if (actionCenter.counts().total > 0) {
              <span class="chip-count">{{ actionCenter.counts().total }}</span>
            }
          </app-chip>
          <app-chip
            [selected]="activeFilter() === 'message'"
            (clicked)="setFilter('message')"
          >
            <app-icon size="sm">mail</app-icon>
            {{ 'inbox.filters.messages' | transloco }}
            @if (actionCenter.counts().messages > 0) {
              <span class="chip-count">{{ actionCenter.counts().messages }}</span>
            }
          </app-chip>
          <app-chip
            [selected]="activeFilter() === 'sudo'"
            (clicked)="setFilter('sudo')"
          >
            <app-icon size="sm">admin_panel_settings</app-icon>
            {{ 'inbox.filters.sudo' | transloco }}
            @if (actionCenter.counts().sudo > 0) {
              <span class="chip-count">{{ actionCenter.counts().sudo }}</span>
            }
          </app-chip>
          <app-chip
            [selected]="activeFilter() === 'review'"
            (clicked)="setFilter('review')"
          >
            <app-icon size="sm">rate_review</app-icon>
            {{ 'inbox.filters.reviews' | transloco }}
            @if (actionCenter.counts().reviews > 0) {
              <span class="chip-count">{{ actionCenter.counts().reviews }}</span>
            }
          </app-chip>
          <app-chip
            [selected]="activeFilter() === 'session'"
            (clicked)="setFilter('session')"
          >
            <app-icon size="sm">smart_toy</app-icon>
            {{ 'inbox.filters.sessions' | transloco }}
            @if (actionCenter.counts().sessions > 0) {
              <span class="chip-count">{{ actionCenter.counts().sessions }}</span>
            }
          </app-chip>
        </div>

        <div class="header-right">
          <span
            class="sse-dot"
            [class.connected]="sudo.isConnected()"
            [title]="(sudo.isConnected() ? 'inbox.sseConnected' : 'inbox.sseDisconnected') | transloco"
          ></span>
          <app-icon-button
            size="sm"
            [ariaLabel]="'inbox.refresh' | transloco"
            [tooltip]="'inbox.refresh' | transloco"
            (clicked)="refresh()"
          >
            <app-icon size="sm">refresh</app-icon>
          </app-icon-button>
        </div>
      </header>

      <!-- Body: list + detail -->
      <div class="inbox-body" [class.mobile-detail]="isMobileDetail()">
        <!-- Left: Item List -->
        <div class="list-panel" role="feed" [attr.aria-label]="'inbox.listAriaLabel' | transloco">
          @if (filteredItems().length === 0) {
            <div class="empty-list">
              <app-icon size="inherit" class="empty-icon">inbox</app-icon>
              <span class="empty-text">
                @if (activeFilter()) {
                  {{ 'inbox.list.empty.' + activeFilter() | transloco }}
                } @else {
                  {{ 'inbox.list.emptyAll' | transloco }}
                }
              </span>
            </div>
          } @else {
            @for (item of filteredItems(); track item.id; let i = $index) {
              <button
                class="list-item"
                [class.selected]="selectedItem()?.id === item.id"
                [class.pending]="item.status === 'pending'"
                [class.resolved]="item.status === 'resolved'"
                [class.urgent-high]="item.type === 'sudo' && item.sudo && item.sudo.status === 'pending' && getSecondsLeft(item.sudo!) < 30"
                [class.urgent-med]="item.type === 'sudo' && item.sudo && item.sudo.status === 'pending' && getSecondsLeft(item.sudo!) >= 30 && getSecondsLeft(item.sudo!) < 120"
                [attr.data-index]="i"
                (click)="selectItem(item)"
              >
                <div class="item-urgency-bar" [class]="'urgency-' + urgencyColor(item)"></div>
                <app-icon size="lg" class="item-type-icon" [class]="'type-' + item.type">
                  @switch (item.type) {
                    @case ('message') { mail }
                    @case ('sudo') { admin_panel_settings }
                    @case ('review') { rate_review }
                    @case ('session') { smart_toy }
                  }
                </app-icon>
                <div class="item-content">
                  <div class="item-title-row">
                    <span class="item-title">{{ item.title }}</span>
                    @if (item.type === 'message' && item.message?.unread) {
                      <span class="unread-dot"></span>
                    }
                    @if (item.type === 'sudo' && item.sudo?.status === 'pending') {
                      <span class="item-countdown" [class]="'ttl-' + ttlColor(item.sudo!)">
                        {{ getSecondsLeft(item.sudo!) }}s
                      </span>
                    }
                    <span class="item-time">{{ relativeTime(item.timestamp) }}</span>
                  </div>
                  <div class="item-subtitle">
                    @if (item.type === 'message' && item.message?.mode === 'blocking') {
                      <app-badge tone="danger" size="xs" [uppercase]="true">
                        {{ 'inbox.list.modeBlocking' | transloco }}
                      </app-badge>
                    }
                    @if (item.status === 'resolved') {
                      <app-badge tone="neutral" size="xs" [uppercase]="true">
                        @if (item.type === 'sudo' && item.sudo) {
                          {{ item.sudo.status }}
                        } @else {
                          {{ 'inbox.list.resolved' | transloco }}
                        }
                      </app-badge>
                    }
                    {{ item.subtitle }}
                  </div>
                </div>
              </button>
            }
          }
        </div>

        <!-- Right: Detail Panel -->
        <div class="detail-panel">
          @if (!selectedItem()) {
            <!-- Empty state -->
            <div class="detail-empty">
              <app-icon size="inherit" class="detail-empty-icon">select_all</app-icon>
              <span class="detail-empty-text">{{ 'inbox.detail.emptyText' | transloco }}</span>
              <span class="detail-empty-hint">
                {{ 'inbox.detail.hintUse' | transloco }} <kbd>j</kbd>/<kbd>k</kbd> {{ 'inbox.detail.hintToNavigate' | transloco }} <kbd>Enter</kbd> {{ 'inbox.detail.hintToSelect' | transloco }}
              </span>
            </div>
          } @else if (selectedItem()!.type === 'sudo' && selectedItem()!.sudo) {
            <!-- ========== SUDO DETAIL ========== -->
            <div class="detail-content sudo-detail">
              <div class="detail-header">
                <span class="detail-type-badge sudo">
                  <app-icon size="sm">{{ selectedItem()!.sudo!.request_type === 'vm_upgrade' ? 'cloud_upload' : 'admin_panel_settings' }}</app-icon>
                  {{ (selectedItem()!.sudo!.request_type === 'vm_upgrade' ? 'inbox.sudoDetail.vmUpgradeRequest' : 'inbox.sudoDetail.sudoRequest') | transloco }}
                </span>
                @if (selectedItem()!.sudo!.request_type !== 'vm_upgrade') {
                  <app-badge
                    [tone]="riskTone(getRisk(selectedItem()!.sudo!))"
                    appearance="solid"
                    size="xs"
                    [uppercase]="true"
                  >
                    {{ getRisk(selectedItem()!.sudo!) }}
                  </app-badge>
                }
                @if (selectedItem()!.sudo!.status === 'pending' && selectedItem()!.sudo!.request_type !== 'vm_upgrade') {
                  <span class="detail-countdown" [class]="'ttl-' + ttlColor(selectedItem()!.sudo!)">
                    {{ 'inbox.sudoDetail.secondsRemaining' | transloco: {seconds: getSecondsLeft(selectedItem()!.sudo!)} }}
                  </span>
                }
                <app-badge [tone]="sudoStatusTone(selectedItem()!.sudo!.status)" size="xs" [uppercase]="true">
                  {{ selectedItem()!.sudo!.status }}
                </app-badge>
              </div>

              <div class="command-block">
                <code>{{ selectedItem()!.sudo!.arguments.join(' ') || selectedItem()!.sudo!.command }}</code>
              </div>

              <div class="meta-grid">
                @if (selectedItem()!.sudo!.request_type === 'vm_upgrade') {
                  <div class="meta-row">
                    <span class="meta-label">{{ 'inbox.sudoDetail.workspace' | transloco }}</span>
                    <span class="meta-value">{{ selectedItem()!.sudo!.vm_name }}</span>
                  </div>
                } @else {
                  <div class="meta-row">
                    <span class="meta-label">{{ 'inbox.sudoDetail.user' | transloco }}</span>
                    <span class="meta-value">{{ selectedItem()!.sudo!.requesting_user }} → {{ selectedItem()!.sudo!.target_user }}</span>
                  </div>
                  <div class="meta-row">
                    <span class="meta-label">{{ 'inbox.sudoDetail.vm' | transloco }}</span>
                    <span class="meta-value">{{ selectedItem()!.sudo!.vm_name }}</span>
                  </div>
                }
                @if (selectedItem()!.sudo!.working_directory) {
                  <div class="meta-row">
                    <span class="meta-label">{{ 'inbox.sudoDetail.cwd' | transloco }}</span>
                    <span class="meta-value mono">{{ selectedItem()!.sudo!.working_directory }}</span>
                  </div>
                }
              </div>

              <div class="detail-ids">
                @if (selectedItem()!.sudo!.job_id) {
                  <app-copy-field [label]="'inbox.detail.jobId' | transloco" [value]="selectedItem()!.sudo!.job_id" />
                }
                <app-copy-field [label]="'inbox.detail.requestId' | transloco" [value]="selectedItem()!.sudo!.id" />
              </div>

              @if (selectedItem()!.sudo!.status === 'pending') {
                @if (selectedItem()!.sudo!.request_type === 'vm_upgrade') {
                  <div class="upgrade-hint">
                    {{ 'inbox.sudoDetail.upgradeHint' | transloco }}
                  </div>
                  <div class="action-bar">
                    <app-button variant="warning" size="sm" (clicked)="approveVmUpgrade()">
                      <app-icon size="sm">cloud_upload</app-icon> {{ 'inbox.sudoDetail.upgradeToVm' | transloco }}
                      <kbd>u</kbd>
                    </app-button>
                    <app-button variant="primary" size="sm" (clicked)="resumeWithoutVmSudo()">
                      <app-icon size="sm">play_arrow</app-icon> {{ 'inbox.sudoDetail.resumeWithoutVm' | transloco }}
                    </app-button>
                    <app-button variant="danger" size="sm" (clicked)="startDenySudo()">
                      <app-icon size="sm">close</app-icon> {{ 'inbox.sudoDetail.deny' | transloco }}
                      <kbd>d</kbd>
                    </app-button>
                  </div>
                } @else {
                  <div class="action-bar">
                    <app-button variant="success" size="sm" (clicked)="approveSudo()">
                      <app-icon size="sm">check</app-icon> {{ 'inbox.sudoDetail.approve' | transloco }}
                      <kbd>a</kbd>
                    </app-button>
                    <app-button variant="danger" size="sm" (clicked)="startDenySudo()">
                      <app-icon size="sm">close</app-icon> {{ 'inbox.sudoDetail.deny' | transloco }}
                      <kbd>d</kbd>
                    </app-button>
                  </div>
                }
              }

              @if (selectedItem()!.sudo!.decision_reason) {
                <div class="decision-info">
                  <span class="meta-label">{{ 'inbox.sudoDetail.reason' | transloco }}</span>
                  <span class="decision-reason">{{ selectedItem()!.sudo!.decision_reason }}</span>
                </div>
              }

              <!-- Rules section -->
              <details class="rules-section">
                <summary class="rules-toggle">
                  <app-icon size="sm">settings</app-icon>
                  {{ 'inbox.sudoDetail.rulesToggle' | transloco: {count: sudo.rules().length} }}
                </summary>
                <div class="rules-body">
                  <div class="rule-form">
                    <app-input
                      size="sm"
                      [(value)]="newRulePattern"
                      [placeholder]="'inbox.sudoDetail.rulePatternPlaceholder' | transloco"
                    />
                    <app-select size="sm" [(value)]="newRuleAction">
                      <option value="approve">{{ 'inbox.sudoDetail.ruleActionApprove' | transloco }}</option>
                      <option value="deny">{{ 'inbox.sudoDetail.ruleActionDeny' | transloco }}</option>
                    </app-select>
                    <app-button variant="primary" size="sm" (clicked)="addRule()">
                      {{ 'inbox.sudoDetail.ruleAdd' | transloco }}
                    </app-button>
                  </div>
                  @for (rule of sudo.rules(); track rule.id) {
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
                        [ariaLabel]="'inbox.sudoDetail.ruleDelete' | transloco"
                        (clicked)="deleteRule(rule.id)"
                      >
                        <app-icon size="sm">close</app-icon>
                      </app-icon-button>
                    </div>
                  }
                </div>
              </details>
            </div>

          } @else if (selectedItem()!.type === 'message' && selectedItem()!.message) {
            <!-- ========== MESSAGE DETAIL ========== -->
            <div class="detail-content message-detail">
              <div class="detail-header">
                <span class="detail-type-badge message">
                  <app-icon size="sm">mail</app-icon>
                  {{ selectedItem()!.message!.subject || ('inbox.messageDetail.messageLabel' | transloco) }}
                </span>
                @if (selectedItem()!.message!.mode === 'blocking') {
                  <app-badge tone="danger" size="xs" [uppercase]="true">
                    {{ 'inbox.messageDetail.modeBlocking' | transloco }}
                  </app-badge>
                } @else {
                  <app-badge tone="neutral" size="xs" [uppercase]="true">
                    {{ 'inbox.messageDetail.modeAsync' | transloco }}
                  </app-badge>
                }
              </div>

              <div class="thread-meta">
                {{ selectedItem()!.message!.configName || ('inbox.messageDetail.defaultAgent' | transloco) }}
                @if (selectedItem()!.message!.jobDescription) {
                  · {{ selectedItem()!.message!.jobDescription }}
                }
              </div>

              <div class="detail-ids">
                @if (selectedItem()!.jobId) {
                  <app-copy-field [label]="'inbox.detail.jobId' | transloco" [value]="selectedItem()!.jobId!" />
                }
                <app-copy-field [label]="'inbox.detail.threadId' | transloco" [value]="selectedItem()!.message!.threadId" />
              </div>

              <!-- Thread messages -->
              <div class="thread-body" #threadBody>
                @if (threadLoading()) {
                  <div class="thread-loading">{{ 'inbox.messageDetail.threadLoading' | transloco }}</div>
                } @else if (threadDetail()) {
                  @for (msg of threadDetail()!.messages; track msg.id) {
                    <div class="msg-bubble" [class]="'msg-' + msg.direction">
                      <div class="msg-header">
                        <span class="msg-sender">
                          @if (msg.direction === 'outbound') {
                            {{ 'inbox.messageDetail.senderAgent' | transloco }}
                          } @else {
                            {{ 'inbox.messageDetail.senderYou' | transloco }}
                          }
                        </span>
                        <span class="msg-time">{{ formatMsgTime(msg.created_at) }}</span>
                      </div>
                      <div class="msg-content">
                        <markdown [data]="msg.message"></markdown>
                      </div>
                    </div>
                  }
                } @else {
                  <div class="thread-loading">
                    @if (selectedItem()!.message!.lastMessage) {
                      <div class="msg-preview">{{ selectedItem()!.message!.lastMessage }}</div>
                    } @else {
                      {{ 'inbox.messageDetail.noMessagesLoaded' | transloco }}
                    }
                  </div>
                }
              </div>

              <!-- Reply form -->
              <div class="reply-form">
                <app-textarea
                  #replyInput
                  [(value)]="replyText"
                  [placeholder]="'inbox.messageDetail.replyPlaceholder' | transloco"
                  [rows]="2"
                  (keydown.control.enter)="sendReply()"
                  (keydown.meta.enter)="sendReply()"
                />
                <div class="reply-actions">
                  <app-checkbox [(checked)]="replyUrgent">
                    {{ 'inbox.messageDetail.markUrgent' | transloco }}
                  </app-checkbox>
                  <app-button
                    variant="info"
                    size="sm"
                    [disabled]="!replyText.trim() || replySending()"
                    (clicked)="sendReply()"
                  >
                    <app-icon size="sm">send</app-icon>
                    {{ 'inbox.messageDetail.send' | transloco }}
                    <kbd>Ctrl+Enter</kbd>
                  </app-button>
                </div>
              </div>
            </div>

          } @else if (selectedItem()!.type === 'review' && selectedItem()!.review) {
            <!-- ========== REVIEW DETAIL ========== -->
            <div class="detail-content review-detail">
              <div class="detail-header">
                <span class="detail-type-badge review">
                  <app-icon size="sm">rate_review</app-icon>
                  {{ 'inbox.reviewDetail.jobReview' | transloco }}
                </span>
                <app-badge [tone]="freezeTone()" appearance="solid" size="xs" [uppercase]="true">
                  {{ (frozenData()?.freeze_type === 'phase_boundary' ? 'inbox.reviewDetail.freezePhase' :
                     frozenData()?.freeze_type === 'vm_upgrade_required' ? 'inbox.reviewDetail.freezeUpgrade' : 'inbox.reviewDetail.freezeComplete') | transloco }}
                </app-badge>
              </div>

              <div class="review-job-info">
                <div class="review-job-desc">{{ selectedItem()!.review!.jobDescription }}</div>
                <div class="review-job-meta">
                  <span>{{ selectedItem()!.review!.configName || ('inbox.reviewDetail.defaultAgent' | transloco) }}</span>
                  @if (frozenData()?.phase_number !== undefined) {
                    <span>{{ 'inbox.reviewDetail.phase' | transloco: {number: frozenData()?.phase_number} }}</span>
                  }
                </div>
                <div class="detail-ids">
                  <app-copy-field [label]="'inbox.detail.jobId' | transloco" [value]="selectedItem()!.review!.jobId" />
                </div>
              </div>

              @if (reviewLoading()) {
                <div class="review-loading">{{ 'inbox.reviewDetail.loadingFrozen' | transloco }}</div>
              } @else if (frozenData()) {
                @if (frozenData()!.summary) {
                  <div class="review-section">
                    <div class="review-section-title">{{ 'inbox.reviewDetail.summary' | transloco }}</div>
                    <div class="review-summary">
                      <markdown [data]="frozenData()!.summary!"></markdown>
                    </div>
                  </div>
                }

                @if (frozenData()!.confidence !== undefined && frozenData()!.confidence !== null) {
                  <div class="review-section">
                    <div class="review-section-title">{{ 'inbox.reviewDetail.confidence' | transloco }}</div>
                    <div class="confidence-bar-wrap">
                      <div class="confidence-bar">
                        <div
                          class="confidence-fill"
                          [style.width.%]="(frozenData()!.confidence || 0) * 100"
                          [class.low]="(frozenData()!.confidence || 0) < 0.5"
                          [class.medium]="(frozenData()!.confidence || 0) >= 0.5 && (frozenData()!.confidence || 0) < 0.8"
                          [class.high]="(frozenData()!.confidence || 0) >= 0.8"
                        ></div>
                      </div>
                      <span class="confidence-label">{{ ((frozenData()!.confidence || 0) * 100).toFixed(0) }}%</span>
                    </div>
                  </div>
                }

                @if (frozenData()!.deliverables?.length) {
                  <div class="review-section">
                    <div class="review-section-title">{{ 'inbox.reviewDetail.deliverables' | transloco }}</div>
                    <ul class="deliverables-list">
                      @for (d of frozenData()!.deliverables; track d) {
                        <li>{{ d }}</li>
                      }
                    </ul>
                  </div>
                }
              }

              <!-- Actions -->
              <div class="action-bar">
                @if (frozenData()?.freeze_type === 'vm_upgrade_required') {
                  <div class="review-action-group upgrade-group">
                    @if (frozenData()!.command) {
                      <code class="upgrade-command">{{ frozenData()!.command }}</code>
                    }
                    <div class="upgrade-hint">
                      {{ 'inbox.reviewDetail.upgradeHint' | transloco }}
                    </div>
                    <div class="upgrade-buttons">
                      <app-button
                        variant="warning"
                        size="sm"
                        [disabled]="reviewActing()"
                        (clicked)="upgradeToVm()"
                      >
                        <app-icon size="sm">cloud_upload</app-icon> {{ 'inbox.reviewDetail.upgradeToVm' | transloco }}
                      </app-button>
                      <app-button
                        variant="primary"
                        size="sm"
                        [disabled]="reviewActing()"
                        (clicked)="resumeWithoutVm()"
                      >
                        <app-icon size="sm">play_arrow</app-icon> {{ 'inbox.reviewDetail.resumeWithoutVm' | transloco }}
                      </app-button>
                    </div>
                  </div>
                } @else if (frozenData()?.freeze_type === 'job_complete') {
                  <div class="review-action-group">
                    <app-textarea
                      [(value)]="reviewNotes"
                      [placeholder]="'inbox.reviewDetail.notesPlaceholder' | transloco"
                      [rows]="2"
                    />
                    <app-button
                      variant="success"
                      size="sm"
                      [disabled]="reviewActing()"
                      (clicked)="approveReview()"
                    >
                      <app-icon size="sm">check_circle</app-icon> {{ 'inbox.reviewDetail.approve' | transloco }}
                      <kbd>a</kbd>
                    </app-button>
                  </div>
                } @else {
                  <div class="review-action-group">
                    <app-textarea
                      [(value)]="reviewFeedback"
                      [placeholder]="'inbox.reviewDetail.feedbackPlaceholder' | transloco"
                      [rows]="2"
                    />
                    <app-button
                      variant="primary"
                      size="sm"
                      [disabled]="reviewActing()"
                      (clicked)="resumeReview()"
                    >
                      <app-icon size="sm">play_arrow</app-icon> {{ 'inbox.reviewDetail.resumeWithFeedback' | transloco }}
                    </app-button>
                  </div>
                }
              </div>
            </div>
          } @else if (selectedItem()!.type === 'session' && selectedItem()!.session) {
            <!-- ========== SESSION DETAIL ========== -->
            <div class="detail-content session-detail">
              <div class="detail-header">
                <span class="detail-type-badge session">
                  <app-icon size="sm">smart_toy</app-icon>
                  {{ 'inbox.sessionDetail.agentSession' | transloco }}
                </span>
                <app-badge tone="warning" size="xs" [uppercase]="true">
                  {{ 'inbox.sessionDetail.needsAttention' | transloco }}
                </app-badge>
              </div>

              <h3 class="session-title">{{ selectedItem()!.title }}</h3>
              <div class="session-subtitle">{{ selectedItem()!.subtitle }}</div>

              <div class="detail-ids">
                <app-copy-field [label]="'inbox.detail.sessionId' | transloco" [value]="selectedItem()!.session!.threadId" />
                <app-copy-field [label]="'inbox.detail.eventId' | transloco" [value]="selectedItem()!.session!.event.event_id" />
              </div>

              @if (selectedItem()!.session!.event.tool) {
                <div class="session-section">
                  <span class="meta-label">{{ 'inbox.sessionDetail.tool' | transloco }}</span>
                  <code class="command-block">{{ selectedItem()!.session!.event.tool }}</code>
                </div>
                @if (selectedItem()!.session!.event.args) {
                  <div class="session-section">
                    <span class="meta-label">{{ 'inbox.sessionDetail.arguments' | transloco }}</span>
                    <pre class="args-block">{{ selectedItem()!.session!.event.args | json }}</pre>
                  </div>
                }
              }
              @if (selectedItem()!.session!.event.command) {
                <div class="session-section">
                  <span class="meta-label">{{ 'inbox.sessionDetail.command' | transloco }}</span>
                  <code class="command-block">{{ selectedItem()!.session!.event.command }}</code>
                </div>
              }
              @if (selectedItem()!.session!.event.reason) {
                <div class="session-section">
                  <span class="meta-label">{{ 'inbox.sessionDetail.reason' | transloco }}</span>
                  <span>{{ selectedItem()!.session!.event.reason }}</span>
                </div>
              }

              <div class="session-actions">
                <app-button variant="primary" size="sm" (clicked)="goToSession(selectedItem()!.session!.threadId)">
                  <app-icon size="sm">open_in_new</app-icon> {{ 'inbox.sessionDetail.goToSession' | transloco }}
                </app-button>
              </div>
            </div>
          }
        </div>
      </div>
    </div>

    <!-- Deny dialog -->
    <app-dialog
      [open]="!!denyTarget()"
      size="md"
      [closeOnBackdrop]="false"
      [title]="('inbox.denyDialog.titlePrefix' | transloco) + ' ' + (denyTarget()?.command ?? '')"
      (closed)="cancelDeny()"
    >
      <app-textarea
        [(value)]="denyReason"
        [placeholder]="'inbox.denyDialog.reasonPlaceholder' | transloco"
        [rows]="3"
      />
      <ng-container appDialogActions>
        <app-button variant="ghost" size="sm" (clicked)="cancelDeny()">
          {{ 'inbox.denyDialog.cancel' | transloco }}
        </app-button>
        <app-button
          variant="danger"
          size="sm"
          [disabled]="!denyReason.trim()"
          (clicked)="confirmDeny()"
        >
          {{ 'inbox.denyDialog.deny' | transloco }}
        </app-button>
      </ng-container>
    </app-dialog>

    <!-- Keyboard shortcuts dialog -->
    <app-dialog
      [open]="showShortcuts()"
      size="md"
      [title]="'inbox.shortcuts.title' | transloco"
      (closed)="showShortcuts.set(false)"
    >
      <div class="shortcuts-grid">
        <kbd>j</kbd><span>{{ 'inbox.shortcuts.nextItem' | transloco }}</span>
        <kbd>k</kbd><span>{{ 'inbox.shortcuts.prevItem' | transloco }}</span>
        <kbd>Enter</kbd><span>{{ 'inbox.shortcuts.selectItem' | transloco }}</span>
        <kbd>Escape</kbd><span>{{ 'inbox.shortcuts.deselect' | transloco }}</span>
        <kbd>a</kbd><span>{{ 'inbox.shortcuts.approveSudoReview' | transloco }}</span>
        <kbd>d</kbd><span>{{ 'inbox.shortcuts.denySudo' | transloco }}</span>
        <kbd>r</kbd><span>{{ 'inbox.shortcuts.focusReply' | transloco }}</span>
        <kbd>Ctrl+Enter</kbd><span>{{ 'inbox.shortcuts.sendReply' | transloco }}</span>
        <kbd>1</kbd><span>{{ 'inbox.shortcuts.filterMessages' | transloco }}</span>
        <kbd>2</kbd><span>{{ 'inbox.shortcuts.filterSudo' | transloco }}</span>
        <kbd>3</kbd><span>{{ 'inbox.shortcuts.filterReviews' | transloco }}</span>
        <kbd>0</kbd><span>{{ 'inbox.shortcuts.clearFilter' | transloco }}</span>
        <kbd>?</kbd><span>{{ 'inbox.shortcuts.toggleOverlay' | transloco }}</span>
      </div>
      <ng-container appDialogActions>
        <app-button variant="ghost" size="sm" (clicked)="showShortcuts.set(false)">
          {{ 'inbox.shortcuts.close' | transloco }}
        </app-button>
      </ng-container>
    </app-dialog>
  `,
  styles: [`
    :host { display: block; height: 100%; }

    .inbox {
      display: flex;
      flex-direction: column;
      height: 100%;
      background: var(--app-bg, #1e1e2e);
    }

    /* ===== HEADER ===== */

    .inbox-header {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 16px;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
      flex-shrink: 0;
      min-height: 44px;
    }

    .header-left {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }

    .header-title {
      margin: 0;
      font-size: 15px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
      white-space: nowrap;
    }

    .filter-chips {
      display: flex;
      gap: 4px;
      flex: 1;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .filter-chips::-webkit-scrollbar { display: none; }

    .chip-count {
      background: var(--accent-color, var(--accent-color));
      color: var(--on-accent, var(--timeline-bg, #11111b));
      font-size: 9px;
      font-weight: 700;
      padding: 0 5px;
      border-radius: var(--radius-pill);
      min-width: 14px;
      text-align: center;
      line-height: 14px;
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }

    .sse-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--text-muted, #6c7086);
      transition: background 0.3s;
    }
    .sse-dot.connected { background: var(--success); }

    /* ===== BODY: TWO-PANEL ===== */

    .inbox-body {
      display: flex;
      flex: 1;
      overflow: hidden;
    }

    .list-panel {
      width: 360px;
      min-width: 280px;
      flex-shrink: 0;
      border-right: 1px solid var(--border-color, var(--surface-0));
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border-color, var(--surface-0)) transparent;
    }

    .detail-panel {
      flex: 1;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border-color, var(--surface-0)) transparent;
    }

    /* ===== LIST ITEMS ===== */

    .list-item {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      width: 100%;
      padding: 10px 12px 10px 0;
      background: transparent;
      border: none;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
      cursor: pointer;
      text-align: left;
      font-family: inherit;
      color: var(--text-primary, var(--text-primary));
      transition: background 0.1s;
      position: relative;
    }
    .list-item:hover { background: rgba(49, 50, 68, 0.4); }
    .list-item.selected { background: var(--surface-0, var(--surface-0)); }
    .list-item.resolved { opacity: 0.55; }

    .item-urgency-bar {
      width: 3px;
      align-self: stretch;
      border-radius: 0 var(--radius-tag) var(--radius-tag) 0;
      flex-shrink: 0;
    }
    .urgency-green { background: var(--success); }
    .urgency-amber { background: var(--warning); }
    .urgency-red { background: var(--danger); }
    .urgency-purple { background: var(--accent-color, var(--accent-color)); }
    .urgency-muted { background: var(--border-color, var(--surface-0)); }

    .item-type-icon {
      font-size: 18px;
      flex-shrink: 0;
      margin-top: 1px;
      color: var(--text-muted, #6c7086);
    }
    .type-message { color: var(--info); }
    .type-sudo { color: var(--alert); }
    .type-review { color: var(--success); }
    .type-session { color: var(--accent-color); }

    .item-content { flex: 1; min-width: 0; }

    .item-title-row {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .item-title {
      font-size: 12px;
      font-weight: 500;
      color: var(--text-primary, var(--text-primary));
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
      min-width: 0;
    }

    .unread-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--info);
      flex-shrink: 0;
    }

    .item-countdown {
      font-size: 10px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      flex-shrink: 0;
    }

    .item-time {
      font-size: 10px;
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
      font-variant-numeric: tabular-nums;
    }

    .item-subtitle {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      margin-top: 2px;
      display: flex;
      align-items: center;
      gap: 4px;
    }

    .ttl-green { color: var(--success); }
    .ttl-amber { color: var(--warning); }
    .ttl-red { color: var(--danger); }

    /* ===== EMPTY STATES ===== */

    .empty-list {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 60px 20px;
      gap: 8px;
    }

    .empty-icon {
      font-size: 40px;
      color: var(--border-color, var(--surface-0));
    }

    .empty-text {
      font-size: 13px;
      color: var(--text-muted, #6c7086);
    }

    .detail-empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100%;
      gap: 8px;
    }

    .detail-empty-icon {
      font-size: 48px;
      color: var(--border-color, var(--surface-0));
    }

    .detail-empty-text {
      font-size: 14px;
      color: var(--text-muted, #6c7086);
    }

    .detail-empty-hint {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      opacity: 0.6;
    }

    .detail-empty-hint kbd {
      display: inline-block;
      padding: 1px 5px;
      background: var(--surface-0, var(--surface-0));
      border-radius: var(--radius-tag);
      font-size: 10px;
      font-family: inherit;
    }

    /* ===== DETAIL SHARED ===== */

    .detail-content {
      padding: 16px 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .detail-header {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .detail-type-badge {
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, var(--text-primary));
    }
    .detail-type-badge .icon { font-size: 18px; }
    .detail-type-badge.sudo .icon { color: var(--alert); }
    .detail-type-badge.message .icon { color: var(--info); }
    .detail-type-badge.review .icon { color: var(--success); }
    .detail-type-badge.session .icon { color: var(--accent-color); }

    .session-detail .session-title {
      font-size: 16px;
      font-weight: 600;
      margin: 16px 0 4px;
    }
    .session-detail .session-subtitle {
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      margin-bottom: 16px;
    }
    .session-detail .session-section {
      margin-bottom: 12px;
    }
    .session-detail .args-block {
      background: var(--surface-0, var(--surface-0));
      border-radius: var(--radius-tag);
      padding: 8px 12px;
      font-size: 12px;
      overflow-x: auto;
      max-height: 200px;
      margin-top: 4px;
    }
    .session-detail .session-actions {
      margin-top: 20px;
      display: flex;
      gap: 8px;
    }
    .detail-countdown {
      font-size: 12px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      margin-left: auto;
    }

    .detail-ids {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-top: 8px;
    }

    /* ===== SUDO DETAIL ===== */

    .command-block {
      background: var(--panel-bg, var(--panel-bg));
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: var(--radius-tag);
      padding: 12px 14px;
    }
    .command-block code {
      font-size: 13px;
      color: var(--text-primary, var(--text-primary));
      word-break: break-all;
      line-height: 1.5;
    }

    .meta-grid {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .meta-row {
      display: flex;
      gap: 12px;
      font-size: 12px;
    }

    .meta-label {
      color: var(--text-muted, #6c7086);
      min-width: 40px;
      flex-shrink: 0;
    }

    .meta-value {
      color: var(--text-secondary, var(--text-secondary));
    }
    .meta-value.mono { font-family: monospace; font-size: 11px; }

    .decision-info {
      background: var(--panel-bg, var(--panel-bg));
      border-radius: var(--radius-surface);
      padding: 10px 12px;
    }

    .decision-reason {
      font-size: 12px;
      color: var(--text-secondary, var(--text-secondary));
      font-style: italic;
      display: block;
      margin-top: 4px;
    }

    /* Rules */

    .rules-section {
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: var(--radius-surface);
      overflow: hidden;
    }

    .rules-toggle {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 8px 12px;
      font-size: 12px;
      color: var(--text-muted, #6c7086);
      cursor: pointer;
      list-style: none;
    }
    .rules-toggle::-webkit-details-marker { display: none; }
    .rules-toggle .icon { font-size: 14px; }

    .rules-body {
      border-top: 1px solid var(--border-color, var(--surface-0));
    }

    .rule-form {
      display: flex;
      gap: 6px;
      padding: 8px;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
      align-items: center;
    }
    .rule-form app-input { flex: 1; min-width: 0; }
    /* Bound the approve/deny <select>: its base-select field is width:100%, so without
       a definite host width it takes the whole flex row and starves the pattern input
       down to ~16px (reproduces at every viewport, not just mobile). */
    .rule-form app-select { flex: 0 0 7.5rem; }

    .rule-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
      font-size: 11px;
    }
    .rule-row:last-child { border-bottom: none; }

    .rule-pattern {
      flex: 1;
      font-size: 11px;
      color: var(--text-primary, var(--text-primary));
    }

    /* ===== MESSAGE DETAIL ===== */

    .thread-meta {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
    }

    .thread-body {
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 120px;
      max-height: 50vh;
      overflow-y: auto;
      padding: 4px 0;
      scrollbar-width: thin;
      scrollbar-color: var(--border-color, var(--surface-0)) transparent;
    }

    .thread-loading {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      color: var(--text-muted, #6c7086);
      font-size: 12px;
    }

    .msg-preview {
      color: var(--text-secondary, var(--text-secondary));
      font-size: 12px;
      line-height: 1.5;
    }

    .msg-bubble {
      border-radius: var(--radius-surface);
      padding: 10px 14px;
      max-width: 85%;
    }
    .msg-outbound {
      background: var(--panel-bg, var(--panel-bg));
      border: 1px solid var(--border-color, var(--surface-0));
      align-self: flex-start;
    }
    .msg-inbound {
      background: color-mix(in srgb, var(--accent-color) 20%, transparent);
      border: 1px solid color-mix(in srgb, var(--accent-color) 20%, transparent);
      align-self: flex-end;
    }

    .msg-header {
      display: flex;
      justify-content: space-between;
      margin-bottom: 6px;
      gap: 12px;
    }

    .msg-sender {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-secondary, var(--text-secondary));
    }
    .msg-outbound .msg-sender { color: var(--alert); }
    .msg-inbound .msg-sender { color: var(--accent-color, var(--accent-color)); }

    .msg-time {
      font-size: 10px;
      color: var(--text-muted, #6c7086);
    }

    .msg-content {
      font-size: 13px;
      line-height: 1.55;
      color: var(--text-primary, var(--text-primary));
    }

    .msg-content ::ng-deep {
      p { margin: 0 0 8px; }
      p:last-child { margin-bottom: 0; }
      code {
        background: var(--surface-0, var(--surface-0));
        padding: 1px 4px;
        border-radius: var(--radius-tag);
        font-size: 12px;
      }
      pre {
        background: var(--surface-0, var(--surface-0));
        border-radius: var(--radius-tag);
        padding: 8px 10px;
        overflow-x: auto;
        margin: 6px 0;
      }
      pre code { background: none; padding: 0; }
    }

    .reply-form {
      border-top: 1px solid var(--border-color, var(--surface-0));
      padding-top: 12px;
    }

    .reply-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 8px;
    }

    /* ===== REVIEW DETAIL ===== */

    .upgrade-command {
      display: block;
      padding: 6px 10px;
      background: rgba(0, 0, 0, 0.3);
      border-radius: var(--radius-tag);
      font-family: monospace;
      font-size: 0.85em;
      color: var(--text-primary, var(--text-primary));
      word-break: break-all;
    }

    .upgrade-hint {
      font-size: 0.8em;
      color: var(--text-secondary, var(--text-secondary));
      font-style: italic;
    }

    .upgrade-buttons {
      display: flex;
      gap: 8px;
    }

    .review-job-info {
      background: var(--panel-bg, var(--panel-bg));
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: var(--radius-surface);
      padding: 12px;
    }

    .review-job-desc {
      font-size: 13px;
      color: var(--text-primary, var(--text-primary));
      line-height: 1.5;
    }

    .review-job-meta {
      display: flex;
      gap: 12px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 6px;
    }

    .review-loading {
      text-align: center;
      padding: 20px;
      color: var(--text-muted, #6c7086);
      font-size: 12px;
    }

    .review-section {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .review-section-title {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted, #6c7086);
    }

    .review-summary {
      font-size: 13px;
      color: var(--text-secondary, var(--text-secondary));
      line-height: 1.55;
    }
    .review-summary ::ng-deep p { margin: 0 0 6px; }
    .review-summary ::ng-deep p:last-child { margin-bottom: 0; }

    .confidence-bar-wrap {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .confidence-bar {
      flex: 1;
      height: 6px;
      background: var(--surface-0, var(--surface-0));
      border-radius: var(--radius-pill);
      overflow: hidden;
    }

    .confidence-fill {
      height: 100%;
      border-radius: var(--radius-pill);
      transition: width 0.3s ease;
    }
    .confidence-fill.low { background: var(--danger); }
    .confidence-fill.medium { background: var(--warning); }
    .confidence-fill.high { background: var(--success); }

    .confidence-label {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-secondary, var(--text-secondary));
      font-variant-numeric: tabular-nums;
      min-width: 30px;
    }

    .deliverables-list {
      margin: 0;
      padding: 0 0 0 18px;
      font-size: 12px;
      color: var(--text-secondary, var(--text-secondary));
      line-height: 1.6;
    }

    .review-action-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
      width: 100%;
    }

    /* ===== SHARED: ACTION BAR LAYOUT + KBD HINTS ===== */

    .upgrade-hint {
      font-size: 12px;
      color: var(--text-secondary);
      padding: 8px 12px;
      background: var(--warning-tint);
      border-left: 2px solid var(--warning);
      border-radius: var(--radius-tag);
      margin-bottom: 8px;
      line-height: 1.5;
    }

    .action-bar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    /* Kbd shortcut hints projected into <app-button>. Scoped to inbox view. */
    app-button kbd {
      font-size: 9px;
      padding: 1px 4px;
      border-radius: var(--radius-tag);
      background: rgba(0, 0, 0, 0.2);
      font-family: inherit;
      margin-left: 2px;
    }

    /* ===== SHORTCUTS DIALOG BODY ===== */

    .shortcuts-grid {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 6px 14px;
    }

    .shortcuts-grid kbd {
      display: inline-block;
      padding: 2px 7px;
      background: var(--surface-0, var(--surface-0));
      border-radius: var(--radius-tag);
      font-size: 11px;
      font-family: inherit;
      color: var(--accent-color, var(--accent-color));
      text-align: center;
      min-width: 24px;
    }

    .shortcuts-grid span {
      font-size: 12px;
      color: var(--text-secondary, var(--text-secondary));
      padding-top: 2px;
    }

    /* ===== RESPONSIVE ===== */

    @media (max-width: 768px) {
      .list-panel {
        width: 100%;
        min-width: unset;
        border-right: none;
      }
      .detail-panel { display: none; }
      .inbox-body.mobile-detail .list-panel { display: none; }
      .inbox-body.mobile-detail .detail-panel { display: block; }

      /* Give the filter chips their own full-width row instead of a ~138px sliver
         squeezed between the title and the SSE/refresh cluster (which hid 3 of the 5
         filters behind a scrollbar-less swipe). Title + status stay on row 1; the
         chips wrap to row 2 and scroll there with ~4 visible at rest. */
      .inbox-header { flex-wrap: wrap; row-gap: 6px; }
      .header-right { margin-left: auto; }
      .filter-chips { gap: 3px; order: 3; flex-basis: 100%; }
    }
  `],
})
export class InboxPageComponent implements OnInit, OnDestroy {
  readonly actionCenter = inject(ActionCenterService);
  readonly sudo = inject(SudoService);
  private readonly api = inject(ApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly zone = inject(NgZone);
  private readonly transloco = inject(TranslocoService);

  @ViewChild('threadBody') threadBodyRef?: ElementRef<HTMLElement>;
  @ViewChild('replyInput') replyInputRef?: AppTextareaComponent;

  // --- State ---
  readonly activeFilter = signal<ActionItemType | null>(null);
  readonly selectedItem = signal<ActionItem | null>(null);
  readonly showShortcuts = signal(false);

  // Countdown tick
  private countdownTimer: ReturnType<typeof setInterval> | null = null;
  readonly tick = signal(0);

  // Message detail
  readonly threadDetail = signal<ThreadDetail | null>(null);
  readonly threadLoading = signal(false);
  replyText = '';
  replyUrgent = false;
  readonly replySending = signal(false);

  // Review detail
  readonly frozenData = signal<FrozenJobData | null>(null);
  readonly reviewLoading = signal(false);
  readonly reviewActing = signal(false);
  reviewNotes = '';
  reviewFeedback = '';

  // Sudo deny
  readonly denyTarget = signal<SudoRequest | null>(null);
  denyReason = '';

  // Sudo rules
  newRulePattern = '';
  newRuleAction = 'approve';

  // Navigation
  private focusIndex = -1;

  readonly filteredItems = computed(() => {
    // Touch tick to force re-evaluation for countdowns
    this.tick();
    const filter = this.activeFilter();
    const items = this.actionCenter.items();
    if (!filter) return items;
    return items.filter((i) => i.type === filter);
  });

  private readonly viewport = inject(ViewportService);
  readonly isMobile = this.viewport.isMobile;
  readonly isMobileDetail = computed(() => {
    return !!this.selectedItem() && this.isMobile();
  });

  ngOnInit(): void {
    // SSE is opened at app-shell init (App constructor effect) so it's
    // already up by the time the user navigates to Inbox. initSSE() is
    // idempotent — this call is removed; keep the rest of the bring-up.
    this.actionCenter.refreshAll();
    this.sudo.loadRules();

    // Countdown tick every second — run outside Angular zone to avoid
    // ExpressionChangedAfterItHasBeenChecked errors, then re-enter zone
    this.zone.runOutsideAngular(() => {
      this.countdownTimer = setInterval(() => {
        this.zone.run(() => this.tick.update((t) => t + 1));
      }, 1000);
    });

    // Deep-link support
    this.route.queryParams.subscribe((params) => {
      const jobId = params['job'];
      const threadId = params['thread'];
      const sudoId = params['sudo'];
      const review = params['review'];

      if (sudoId) {
        this.trySelectById(`sudo:${sudoId}`);
      } else if (jobId && threadId) {
        this.trySelectById(`msg:${jobId}:${threadId}`);
      } else if (jobId && review) {
        this.trySelectById(`rev:${jobId}`);
      }
    });
  }

  ngOnDestroy(): void {
    if (this.countdownTimer) clearInterval(this.countdownTimer);
  }

  // --- Deep-link ---
  private trySelectById(id: string): void {
    const item = this.actionCenter.items().find((i) => i.id === id);
    if (item) {
      this.selectItem(item);
    } else {
      // Retry once after data loads
      setTimeout(() => {
        const retryItem = this.actionCenter.items().find((i) => i.id === id);
        if (retryItem) this.selectItem(retryItem);
      }, 2000);
    }
  }

  // --- Filters ---
  setFilter(type: ActionItemType | null): void {
    this.activeFilter.set(type);
  }

  refresh(): void {
    this.actionCenter.refreshAll();
    this.sudo.loadRules();
  }

  // --- Selection ---
  selectItem(item: ActionItem): void {
    this.selectedItem.set(item);
    this.focusIndex = this.filteredItems().findIndex((i) => i.id === item.id);

    // Load detail data based on type
    if (item.type === 'message' && item.message && item.jobId) {
      this.loadThreadDetail(item.jobId, item.message.threadId);
    } else if (item.type === 'review' && item.review) {
      this.loadFrozenData(item.review.jobId);
    }
  }

  deselect(): void {
    this.selectedItem.set(null);
    this.threadDetail.set(null);
    this.frozenData.set(null);
  }

  // --- Message detail ---
  private loadThreadDetail(jobId: string, threadId: string): void {
    this.threadLoading.set(true);
    this.threadDetail.set(null);
    this.actionCenter.loadThread(jobId, threadId).subscribe({
      next: (detail) => {
        this.threadDetail.set(detail);
        this.threadLoading.set(false);
        // Auto-scroll to bottom
        setTimeout(() => {
          const el = this.threadBodyRef?.nativeElement;
          if (el) el.scrollTop = el.scrollHeight;
        }, 50);
      },
      error: () => this.threadLoading.set(false),
    });
  }

  sendReply(): void {
    const item = this.selectedItem();
    if (!item?.message || !item.jobId || !this.replyText.trim()) return;

    this.replySending.set(true);
    this.actionCenter
      .reply(item.jobId, item.message.threadId, this.replyText.trim(), this.replyUrgent)
      .subscribe({
        next: () => {
          this.replyText = '';
          this.replyUrgent = false;
          this.replySending.set(false);
          // Reload thread
          if (item.jobId && item.message) {
            this.loadThreadDetail(item.jobId, item.message.threadId);
          }
        },
        error: () => this.replySending.set(false),
      });
  }

  // --- Review detail ---
  private loadFrozenData(jobId: string): void {
    this.reviewLoading.set(true);
    this.frozenData.set(null);
    this.api.getFrozenJobData(jobId).subscribe({
      next: (data) => {
        this.frozenData.set(data as FrozenJobData | null);
        this.reviewLoading.set(false);
      },
      error: () => this.reviewLoading.set(false),
    });
  }

  approveReview(): void {
    const item = this.selectedItem();
    if (!item?.review) return;
    this.reviewActing.set(true);
    this.actionCenter.approveJob(item.review.jobId, this.reviewNotes || undefined).subscribe({
      next: () => {
        this.reviewActing.set(false);
        this.reviewNotes = '';
        this.actionCenter.loadReviewJobs();
      },
      error: () => this.reviewActing.set(false),
    });
  }

  resumeReview(): void {
    const item = this.selectedItem();
    if (!item?.review || !this.reviewFeedback.trim()) return;
    this.reviewActing.set(true);
    this.actionCenter.resumeJob(item.review.jobId, this.reviewFeedback.trim()).subscribe({
      next: () => {
        this.reviewActing.set(false);
        this.reviewFeedback = '';
        this.actionCenter.loadReviewJobs();
      },
      error: () => this.reviewActing.set(false),
    });
  }

  upgradeToVm(): void {
    const item = this.selectedItem();
    if (!item?.review) return;
    this.reviewActing.set(true);
    this.actionCenter.upgradeJobToVm(item.review.jobId).subscribe({
      next: () => {
        this.reviewActing.set(false);
        this.actionCenter.loadReviewJobs();
      },
      error: () => this.reviewActing.set(false),
    });
  }

  resumeWithoutVm(): void {
    const item = this.selectedItem();
    if (!item?.review) return;
    this.reviewActing.set(true);
    this.actionCenter.approveJob(item.review.jobId).subscribe({
      next: () => {
        this.reviewActing.set(false);
        this.actionCenter.loadReviewJobs();
      },
      error: () => this.reviewActing.set(false),
    });
  }

  // --- Session actions ---
  goToSession(threadId: string): void {
    this.router.navigate(['/sessions', threadId]);
  }

  // --- Sudo actions ---
  approveSudo(): void {
    const item = this.selectedItem();
    if (!item?.sudo) return;
    this.sudo.approve(item.sudo.id);
  }

  approveVmUpgrade(): void {
    const item = this.selectedItem();
    if (!item?.sudo) return;
    this.sudo.approveVmUpgrade(item.sudo.id);
  }

  resumeWithoutVmSudo(): void {
    const item = this.selectedItem();
    if (!item?.sudo) return;
    this.sudo.resumeWithoutVm(item.sudo.id);
  }

  startDenySudo(): void {
    const item = this.selectedItem();
    if (!item?.sudo) return;
    this.denyTarget.set(item.sudo);
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
      this.sudo.createRule(this.newRulePattern.trim(), this.newRuleAction);
      this.newRulePattern = '';
    }
  }

  deleteRule(ruleId: string): void {
    this.sudo.deleteRule(ruleId);
  }

  // --- Helpers ---
  getRisk(req: SudoRequest): string { return riskLevel(req); }
  getSecondsLeft(req: SudoRequest): number { return secondsLeft(req); }
  relativeTime(iso: string): string {
    return relativeTime(iso, this.transloco.translate('inbox.time.now'));
  }

  ttlColor(req: SudoRequest): string {
    const s = secondsLeft(req);
    if (s <= 0) return 'red';
    if (s < 30) return 'red';
    if (s < 120) return 'amber';
    return 'green';
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

  riskTone(risk: string): BadgeTone {
    switch (risk) {
      case 'low': return 'success';
      case 'medium': return 'warning';
      case 'high': return 'alert';
      case 'critical': return 'danger';
      default: return 'neutral';
    }
  }

  freezeTone(): BadgeTone {
    const t = this.frozenData()?.freeze_type;
    if (t === 'phase_boundary') return 'accent';
    if (t === 'vm_upgrade_required') return 'warning';
    return 'success';
  }

  urgencyColor(item: ActionItem): string {
    if (item.status === 'resolved') return 'muted';
    if (item.type === 'sudo' && item.sudo?.status === 'pending') {
      return this.ttlColor(item.sudo!);
    }
    if (item.type === 'message') {
      if (item.message?.mode === 'blocking') return 'red';
      if (item.message?.unread) return 'purple';
      return 'muted';
    }
    if (item.type === 'review') return 'green';
    return 'muted';
  }

  formatMsgTime(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleTimeString(this.transloco.getActiveLang(), { hour: '2-digit', minute: '2-digit' });
  }

  // --- Keyboard navigation ---
  @HostListener('document:keydown', ['$event'])
  onKeydown(e: KeyboardEvent): void {
    // Ignore when typing in inputs
    const tag = (e.target as HTMLElement).tagName;
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

    if (e.key === '?' && !isInput) {
      e.preventDefault();
      this.showShortcuts.update((v) => !v);
      return;
    }

    if (e.key === 'Escape') {
      if (this.showShortcuts()) {
        this.showShortcuts.set(false);
      } else if (this.denyTarget()) {
        this.cancelDeny();
      } else {
        this.deselect();
      }
      return;
    }

    if (isInput) return;

    const items = this.filteredItems();
    switch (e.key) {
      case 'j':
      case 'ArrowDown':
        e.preventDefault();
        this.focusIndex = Math.min(this.focusIndex + 1, items.length - 1);
        if (items[this.focusIndex]) this.selectItem(items[this.focusIndex]);
        break;
      case 'k':
      case 'ArrowUp':
        e.preventDefault();
        this.focusIndex = Math.max(this.focusIndex - 1, 0);
        if (items[this.focusIndex]) this.selectItem(items[this.focusIndex]);
        break;
      case 'Enter':
        if (this.focusIndex >= 0 && items[this.focusIndex]) {
          this.selectItem(items[this.focusIndex]);
        }
        break;
      case 'a':
        if (this.selectedItem()?.type === 'sudo' && this.selectedItem()?.sudo?.status === 'pending'
            && this.selectedItem()?.sudo?.request_type !== 'vm_upgrade') {
          this.approveSudo();
        } else if (this.selectedItem()?.type === 'review') {
          this.approveReview();
        }
        break;
      case 'u':
        if (this.selectedItem()?.type === 'sudo' && this.selectedItem()?.sudo?.status === 'pending'
            && this.selectedItem()?.sudo?.request_type === 'vm_upgrade') {
          this.approveVmUpgrade();
        }
        break;
      case 'd':
        if (this.selectedItem()?.type === 'sudo' && this.selectedItem()?.sudo?.status === 'pending') {
          this.startDenySudo();
        }
        break;
      case 'r':
        if (this.selectedItem()?.type === 'message') {
          this.replyInputRef?.focus();
        }
        break;
      case '0': this.setFilter(null); break;
      case '1': this.setFilter('message'); break;
      case '2': this.setFilter('sudo'); break;
      case '3': this.setFilter('review'); break;
      case '4':
        this.setFilter('session');
        break;
    }
  }
}
