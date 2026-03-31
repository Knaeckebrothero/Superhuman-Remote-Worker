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
import {FormsModule} from '@angular/forms';
import {MarkdownComponent} from 'ngx-markdown';
import {ActionCenterService} from '../../../core/services/action-center.service';
import {SudoRequest, SudoService} from '../../../core/services/sudo.service';
import {ApiService} from '../../../core/services/api.service';
import {ActionItem, ActionItemType, ThreadDetail,} from '../../../core/models/action.model';

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

function relativeTime(iso: string): string {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'now';
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

@Component({
  selector: 'app-inbox-page',
  standalone: true,
  imports: [FormsModule, JsonPipe, MarkdownComponent],
  template: `
    <div class="inbox" (keydown)="onKeydown($event)">
      <!-- Header -->
      <header class="inbox-header">
        <div class="header-left">
          @if (isMobileDetail()) {
            <button class="back-btn" (click)="deselect()">
              <span class="icon">arrow_back</span>
            </button>
          }
          <h1 class="header-title">Action Center</h1>
        </div>

        <div class="filter-chips">
          <button
            class="chip"
            [class.active]="activeFilter() === null"
            (click)="setFilter(null)"
          >
            All
            @if (actionCenter.counts().total > 0) {
              <span class="chip-count">{{ actionCenter.counts().total }}</span>
            }
          </button>
          <button
            class="chip"
            [class.active]="activeFilter() === 'message'"
            (click)="setFilter('message')"
          >
            <span class="chip-icon">mail</span>
            Messages
            @if (actionCenter.counts().messages > 0) {
              <span class="chip-count">{{ actionCenter.counts().messages }}</span>
            }
          </button>
          <button
            class="chip"
            [class.active]="activeFilter() === 'sudo'"
            (click)="setFilter('sudo')"
          >
            <span class="chip-icon">admin_panel_settings</span>
            Sudo
            @if (actionCenter.counts().sudo > 0) {
              <span class="chip-count">{{ actionCenter.counts().sudo }}</span>
            }
          </button>
          <button
            class="chip"
            [class.active]="activeFilter() === 'review'"
            (click)="setFilter('review')"
          >
            <span class="chip-icon">rate_review</span>
            Reviews
            @if (actionCenter.counts().reviews > 0) {
              <span class="chip-count">{{ actionCenter.counts().reviews }}</span>
            }
          </button>
          <button
            class="chip"
            [class.active]="activeFilter() === 'session'"
            (click)="setFilter('session')"
          >
            <span class="chip-icon">smart_toy</span>
            Sessions
            @if (actionCenter.counts().sessions > 0) {
              <span class="chip-count">{{ actionCenter.counts().sessions }}</span>
            }
          </button>
        </div>

        <div class="header-right">
          <span
            class="sse-dot"
            [class.connected]="sudo.isConnected()"
            [title]="sudo.isConnected() ? 'SSE connected' : 'SSE disconnected'"
          ></span>
          <button class="icon-btn" (click)="refresh()" title="Refresh">
            <span class="icon">refresh</span>
          </button>
        </div>
      </header>

      <!-- Body: list + detail -->
      <div class="inbox-body" [class.mobile-detail]="isMobileDetail()">
        <!-- Left: Item List -->
        <div class="list-panel" role="feed" aria-label="Action center items">
          @if (filteredItems().length === 0) {
            <div class="empty-list">
              <span class="empty-icon">inbox</span>
              <span class="empty-text">
                @if (activeFilter()) {
                  No {{ activeFilter() }} items
                } @else {
                  All clear — no items need your attention
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
                <span class="item-type-icon" [class]="'type-' + item.type">
                  @switch (item.type) {
                    @case ('message') { mail }
                    @case ('sudo') { admin_panel_settings }
                    @case ('review') { rate_review }
                    @case ('session') { smart_toy }
                  }
                </span>
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
                      <span class="mode-badge blocking">blocking</span>
                    }
                    @if (item.status === 'resolved') {
                      <span class="resolved-badge">
                        @if (item.type === 'sudo' && item.sudo) {
                          {{ item.sudo.status }}
                        } @else {
                          resolved
                        }
                      </span>
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
              <span class="detail-empty-icon">select_all</span>
              <span class="detail-empty-text">Select an item to view details</span>
              <span class="detail-empty-hint">
                Use <kbd>j</kbd>/<kbd>k</kbd> to navigate, <kbd>Enter</kbd> to select
              </span>
            </div>
          } @else if (selectedItem()!.type === 'sudo' && selectedItem()!.sudo) {
            <!-- ========== SUDO DETAIL ========== -->
            <div class="detail-content sudo-detail">
              <div class="detail-header">
                <span class="detail-type-badge sudo">
                  <span class="icon">admin_panel_settings</span>
                  Sudo Request
                </span>
                <span class="risk-badge" [class]="'risk-' + getRisk(selectedItem()!.sudo!)">
                  {{ getRisk(selectedItem()!.sudo!) }}
                </span>
                @if (selectedItem()!.sudo!.status === 'pending') {
                  <span class="detail-countdown" [class]="'ttl-' + ttlColor(selectedItem()!.sudo!)">
                    {{ getSecondsLeft(selectedItem()!.sudo!) }}s remaining
                  </span>
                }
                <span class="status-badge" [class]="'status-' + selectedItem()!.sudo!.status">
                  {{ selectedItem()!.sudo!.status }}
                </span>
              </div>

              <div class="command-block">
                <code>{{ selectedItem()!.sudo!.arguments.join(' ') || selectedItem()!.sudo!.command }}</code>
              </div>

              <div class="meta-grid">
                <div class="meta-row">
                  <span class="meta-label">User</span>
                  <span class="meta-value">{{ selectedItem()!.sudo!.requesting_user }} → {{ selectedItem()!.sudo!.target_user }}</span>
                </div>
                <div class="meta-row">
                  <span class="meta-label">VM</span>
                  <span class="meta-value">{{ selectedItem()!.sudo!.vm_name }}</span>
                </div>
                @if (selectedItem()!.sudo!.working_directory) {
                  <div class="meta-row">
                    <span class="meta-label">CWD</span>
                    <span class="meta-value mono">{{ selectedItem()!.sudo!.working_directory }}</span>
                  </div>
                }
              </div>

              @if (selectedItem()!.sudo!.status === 'pending') {
                <div class="action-bar">
                  <button class="btn btn-approve" (click)="approveSudo()">
                    <span class="icon">check</span> Approve
                    <kbd>a</kbd>
                  </button>
                  <button class="btn btn-deny" (click)="startDenySudo()">
                    <span class="icon">close</span> Deny
                    <kbd>d</kbd>
                  </button>
                </div>
              }

              @if (selectedItem()!.sudo!.decision_reason) {
                <div class="decision-info">
                  <span class="meta-label">Reason</span>
                  <span class="decision-reason">{{ selectedItem()!.sudo!.decision_reason }}</span>
                </div>
              }

              <!-- Rules section -->
              <details class="rules-section">
                <summary class="rules-toggle">
                  <span class="icon">settings</span>
                  Auto-Approval Rules ({{ sudo.rules().length }})
                </summary>
                <div class="rules-body">
                  <div class="rule-form">
                    <input
                      class="input"
                      placeholder="Pattern (e.g. apt-get install *)"
                      [(ngModel)]="newRulePattern"
                    />
                    <select class="select" [(ngModel)]="newRuleAction">
                      <option value="approve">Approve</option>
                      <option value="deny">Deny</option>
                    </select>
                    <button class="btn btn-sm" (click)="addRule()">Add</button>
                  </div>
                  @for (rule of sudo.rules(); track rule.id) {
                    <div class="rule-row">
                      <span class="rule-action-badge" [class]="'action-' + rule.action">{{ rule.action }}</span>
                      <code class="rule-pattern">{{ rule.pattern }}</code>
                      <button class="icon-btn-sm" (click)="deleteRule(rule.id)">
                        <span class="icon">close</span>
                      </button>
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
                  <span class="icon">mail</span>
                  {{ selectedItem()!.message!.subject || 'Message' }}
                </span>
                @if (selectedItem()!.message!.mode === 'blocking') {
                  <span class="mode-badge blocking">blocking</span>
                } @else {
                  <span class="mode-badge async">async</span>
                }
              </div>

              <div class="thread-meta">
                {{ selectedItem()!.message!.configName || 'agent' }}
                @if (selectedItem()!.message!.jobDescription) {
                  · {{ selectedItem()!.message!.jobDescription }}
                }
                @if (selectedItem()!.jobId) {
                  · job {{ selectedItem()!.jobId!.slice(0, 8) }}
                }
              </div>

              <!-- Thread messages -->
              <div class="thread-body" #threadBody>
                @if (threadLoading()) {
                  <div class="thread-loading">Loading thread...</div>
                } @else if (threadDetail()) {
                  @for (msg of threadDetail()!.messages; track msg.id) {
                    <div class="msg-bubble" [class]="'msg-' + msg.direction">
                      <div class="msg-header">
                        <span class="msg-sender">
                          @if (msg.direction === 'outbound') {
                            Agent
                          } @else {
                            You
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
                      No messages loaded
                    }
                  </div>
                }
              </div>

              <!-- Reply form -->
              <div class="reply-form">
                <textarea
                  class="reply-input"
                  placeholder="Type a reply..."
                  [(ngModel)]="replyText"
                  (keydown.control.enter)="sendReply()"
                  (keydown.meta.enter)="sendReply()"
                  rows="2"
                  #replyInput
                ></textarea>
                <div class="reply-actions">
                  <label class="urgent-check">
                    <input type="checkbox" [(ngModel)]="replyUrgent" />
                    Mark as urgent
                  </label>
                  <button
                    class="btn btn-send"
                    [disabled]="!replyText.trim() || replySending()"
                    (click)="sendReply()"
                  >
                    <span class="icon">send</span>
                    Send
                    <kbd>Ctrl+Enter</kbd>
                  </button>
                </div>
              </div>
            </div>

          } @else if (selectedItem()!.type === 'review' && selectedItem()!.review) {
            <!-- ========== REVIEW DETAIL ========== -->
            <div class="detail-content review-detail">
              <div class="detail-header">
                <span class="detail-type-badge review">
                  <span class="icon">rate_review</span>
                  Job Review
                </span>
                <span class="freeze-badge" [class]="
                  frozenData()?.freeze_type === 'phase_boundary' ? 'phase' :
                  frozenData()?.freeze_type === 'vm_upgrade_required' ? 'upgrade' : 'complete'
                ">
                  {{ frozenData()?.freeze_type === 'phase_boundary' ? 'Phase Boundary' :
                     frozenData()?.freeze_type === 'vm_upgrade_required' ? 'Sudo Required' : 'Job Complete' }}
                </span>
              </div>

              <div class="review-job-info">
                <div class="review-job-desc">{{ selectedItem()!.review!.jobDescription }}</div>
                <div class="review-job-meta">
                  <span>{{ selectedItem()!.review!.configName || 'agent' }}</span>
                  <span>ID: {{ selectedItem()!.review!.jobId.slice(0, 8) }}</span>
                  @if (frozenData()?.phase_number !== undefined) {
                    <span>Phase {{ frozenData()?.phase_number }}</span>
                  }
                </div>
              </div>

              @if (reviewLoading()) {
                <div class="review-loading">Loading frozen data...</div>
              } @else if (frozenData()) {
                @if (frozenData()!.summary) {
                  <div class="review-section">
                    <div class="review-section-title">Summary</div>
                    <div class="review-summary">
                      <markdown [data]="frozenData()!.summary!"></markdown>
                    </div>
                  </div>
                }

                @if (frozenData()!.confidence !== undefined && frozenData()!.confidence !== null) {
                  <div class="review-section">
                    <div class="review-section-title">Confidence</div>
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
                    <div class="review-section-title">Deliverables</div>
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
                      Upgrade to a VM for sudo access, or resume without a VM.
                    </div>
                    <div class="upgrade-buttons">
                      <button
                        class="btn btn-upgrade"
                        [disabled]="reviewActing()"
                        (click)="upgradeToVm()"
                      >
                        <span class="icon">cloud_upload</span> Upgrade to VM
                      </button>
                      <button
                        class="btn btn-resume"
                        [disabled]="reviewActing()"
                        (click)="resumeWithoutVm()"
                      >
                        <span class="icon">play_arrow</span> Resume without VM
                      </button>
                    </div>
                  </div>
                } @else if (frozenData()?.freeze_type === 'job_complete') {
                  <div class="review-action-group">
                    <textarea
                      class="input review-notes"
                      placeholder="Optional notes..."
                      [(ngModel)]="reviewNotes"
                      rows="2"
                    ></textarea>
                    <button
                      class="btn btn-approve"
                      [disabled]="reviewActing()"
                      (click)="approveReview()"
                    >
                      <span class="icon">check_circle</span> Approve
                      <kbd>a</kbd>
                    </button>
                  </div>
                } @else {
                  <div class="review-action-group">
                    <textarea
                      class="input review-notes"
                      placeholder="Feedback for next phase..."
                      [(ngModel)]="reviewFeedback"
                      rows="2"
                    ></textarea>
                    <button
                      class="btn btn-resume"
                      [disabled]="reviewActing()"
                      (click)="resumeReview()"
                    >
                      <span class="icon">play_arrow</span> Resume with Feedback
                    </button>
                  </div>
                }
              </div>
            </div>
          } @else if (selectedItem()!.type === 'session' && selectedItem()!.session) {
            <!-- ========== SESSION DETAIL ========== -->
            <div class="detail-content session-detail">
              <div class="detail-header">
                <span class="detail-type-badge session">
                  <span class="icon">smart_toy</span>
                  Agent Session
                </span>
                <span class="status-badge status-pending">needs attention</span>
              </div>

              <h3 class="session-title">{{ selectedItem()!.title }}</h3>
              <div class="session-subtitle">{{ selectedItem()!.subtitle }}</div>

              @if (selectedItem()!.session!.event.tool) {
                <div class="session-section">
                  <span class="meta-label">Tool</span>
                  <code class="command-block">{{ selectedItem()!.session!.event.tool }}</code>
                </div>
                @if (selectedItem()!.session!.event.args) {
                  <div class="session-section">
                    <span class="meta-label">Arguments</span>
                    <pre class="args-block">{{ selectedItem()!.session!.event.args | json }}</pre>
                  </div>
                }
              }
              @if (selectedItem()!.session!.event.command) {
                <div class="session-section">
                  <span class="meta-label">Command</span>
                  <code class="command-block">{{ selectedItem()!.session!.event.command }}</code>
                </div>
              }
              @if (selectedItem()!.session!.event.reason) {
                <div class="session-section">
                  <span class="meta-label">Reason</span>
                  <span>{{ selectedItem()!.session!.event.reason }}</span>
                </div>
              }

              <div class="session-actions">
                <button class="btn btn-primary" (click)="goToSession(selectedItem()!.session!.threadId)">
                  <span class="icon">open_in_new</span> Go to Session
                </button>
              </div>
            </div>
          }
        </div>
      </div>
    </div>

    <!-- Deny dialog overlay -->
    @if (denyTarget()) {
      <div class="dialog-overlay" (click)="cancelDeny()">
        <div class="dialog" (click)="$event.stopPropagation()">
          <div class="dialog-title">
            <span class="icon">block</span>
            Deny: {{ denyTarget()!.command }}
          </div>
          <textarea
            class="input deny-reason"
            placeholder="Reason for denial (required)"
            [(ngModel)]="denyReason"
            rows="3"
          ></textarea>
          <div class="dialog-actions">
            <button class="btn btn-ghost" (click)="cancelDeny()">Cancel</button>
            <button
              class="btn btn-deny"
              [disabled]="!denyReason.trim()"
              (click)="confirmDeny()"
            >
              Deny
            </button>
          </div>
        </div>
      </div>
    }

    <!-- Keyboard shortcuts overlay -->
    @if (showShortcuts()) {
      <div class="dialog-overlay" (click)="showShortcuts.set(false)">
        <div class="shortcuts-dialog" (click)="$event.stopPropagation()">
          <div class="shortcuts-title">Keyboard Shortcuts</div>
          <div class="shortcuts-grid">
            <kbd>j</kbd><span>Next item</span>
            <kbd>k</kbd><span>Previous item</span>
            <kbd>Enter</kbd><span>Select item</span>
            <kbd>Escape</kbd><span>Deselect / close</span>
            <kbd>a</kbd><span>Approve (sudo / review)</span>
            <kbd>d</kbd><span>Deny (sudo)</span>
            <kbd>r</kbd><span>Focus reply (message)</span>
            <kbd>Ctrl+Enter</kbd><span>Send reply</span>
            <kbd>1</kbd><span>Filter: Messages</span>
            <kbd>2</kbd><span>Filter: Sudo</span>
            <kbd>3</kbd><span>Filter: Reviews</span>
            <kbd>0</kbd><span>Clear filter</span>
            <kbd>?</kbd><span>Toggle this overlay</span>
          </div>
          <button class="btn btn-ghost shortcuts-close" (click)="showShortcuts.set(false)">Close</button>
        </div>
      </div>
    }
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
      border-bottom: 1px solid var(--border-color, #313244);
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
      color: var(--text-primary, #cdd6f4);
      white-space: nowrap;
    }

    .back-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      background: transparent;
      border: none;
      border-radius: 6px;
      color: var(--text-secondary, #a6adc8);
      cursor: pointer;
    }
    .back-btn:hover { background: var(--surface-0, #313244); }

    .filter-chips {
      display: flex;
      gap: 4px;
      flex: 1;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .filter-chips::-webkit-scrollbar { display: none; }

    .chip {
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      background: transparent;
      border: 1px solid var(--border-color, #313244);
      border-radius: 14px;
      color: var(--text-muted, #6c7086);
      font-size: 11px;
      font-family: inherit;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.12s ease;
    }
    .chip:hover {
      border-color: var(--text-muted, #6c7086);
      color: var(--text-secondary, #a6adc8);
    }
    .chip.active {
      background: var(--surface-0, #313244);
      border-color: var(--accent-color, #cba6f7);
      color: var(--accent-color, #cba6f7);
    }

    .chip-icon {
      font-family: 'Material Symbols Outlined';
      font-size: 14px;
    }

    .chip-count {
      background: var(--accent-color, #cba6f7);
      color: #11111b;
      font-size: 9px;
      font-weight: 700;
      padding: 0 5px;
      border-radius: 8px;
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
    .sse-dot.connected { background: #a6e3a1; }

    .icon-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      background: transparent;
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-muted, #6c7086);
      cursor: pointer;
      transition: all 0.12s;
    }
    .icon-btn:hover {
      color: var(--text-primary, #cdd6f4);
      border-color: var(--text-muted, #6c7086);
    }

    .icon {
      font-family: 'Material Symbols Outlined';
      font-size: 16px;
    }

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
      border-right: 1px solid var(--border-color, #313244);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border-color, #313244) transparent;
    }

    .detail-panel {
      flex: 1;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border-color, #313244) transparent;
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
      border-bottom: 1px solid var(--border-color, #313244);
      cursor: pointer;
      text-align: left;
      font-family: inherit;
      color: var(--text-primary, #cdd6f4);
      transition: background 0.1s;
      position: relative;
    }
    .list-item:hover { background: rgba(49, 50, 68, 0.4); }
    .list-item.selected { background: var(--surface-0, #313244); }
    .list-item.resolved { opacity: 0.55; }

    .item-urgency-bar {
      width: 3px;
      align-self: stretch;
      border-radius: 0 2px 2px 0;
      flex-shrink: 0;
    }
    .urgency-green { background: #a6e3a1; }
    .urgency-amber { background: #f9e2af; }
    .urgency-red { background: #f38ba8; }
    .urgency-purple { background: var(--accent-color, #cba6f7); }
    .urgency-muted { background: var(--border-color, #313244); }

    .item-type-icon {
      font-family: 'Material Symbols Outlined';
      font-size: 18px;
      flex-shrink: 0;
      margin-top: 1px;
      color: var(--text-muted, #6c7086);
    }
    .type-message { color: #89b4fa; }
    .type-sudo { color: #fab387; }
    .type-review { color: #a6e3a1; }
    .type-session { color: #cba6f7; }

    .item-content { flex: 1; min-width: 0; }

    .item-title-row {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .item-title {
      font-size: 12px;
      font-weight: 500;
      color: var(--text-primary, #cdd6f4);
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
      background: #89b4fa;
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

    .mode-badge {
      font-size: 9px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.3px;
      padding: 0 4px;
      border-radius: 3px;
      flex-shrink: 0;
    }
    .mode-badge.blocking { background: rgba(243, 139, 168, 0.2); color: #f38ba8; }
    .mode-badge.async { background: rgba(108, 112, 134, 0.2); color: var(--text-muted, #6c7086); }

    .resolved-badge {
      font-size: 9px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.3px;
      padding: 0 4px;
      border-radius: 3px;
      background: rgba(108, 112, 134, 0.2);
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
    }

    .ttl-green { color: #a6e3a1; }
    .ttl-amber { color: #f9e2af; }
    .ttl-red { color: #f38ba8; }

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
      font-family: 'Material Symbols Outlined';
      font-size: 40px;
      color: var(--border-color, #313244);
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
      font-family: 'Material Symbols Outlined';
      font-size: 48px;
      color: var(--border-color, #313244);
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
      background: var(--surface-0, #313244);
      border-radius: 3px;
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
      color: var(--text-primary, #cdd6f4);
    }
    .detail-type-badge .icon { font-size: 18px; }
    .detail-type-badge.sudo .icon { color: #fab387; }
    .detail-type-badge.message .icon { color: #89b4fa; }
    .detail-type-badge.review .icon { color: #a6e3a1; }
    .detail-type-badge.session .icon { color: #cba6f7; }

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
      background: var(--surface-0, #313244);
      border-radius: 6px;
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
    .session-detail .btn-primary {
      background: var(--accent-color, #cba6f7);
      color: var(--panel-bg, #1e1e2e);
      border: none;
      padding: 8px 16px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .session-detail .btn-primary:hover {
      opacity: 0.9;
    }

    .detail-countdown {
      font-size: 12px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      margin-left: auto;
    }

    .risk-badge {
      font-size: 9px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 2px 6px;
      border-radius: 3px;
      color: #11111b;
    }
    .risk-low { background: #a6e3a1; }
    .risk-medium { background: #f9e2af; }
    .risk-high { background: #fab387; }
    .risk-critical { background: #f38ba8; }

    .status-badge {
      font-size: 9px;
      font-weight: 600;
      text-transform: uppercase;
      padding: 2px 6px;
      border-radius: 3px;
      background: rgba(108, 112, 134, 0.15);
      color: var(--text-muted, #6c7086);
    }
    .status-pending { color: #f9e2af; }
    .status-approved, .status-auto_approved { color: #a6e3a1; }
    .status-denied, .status-auto_denied, .status-expired { color: #f38ba8; }

    /* ===== SUDO DETAIL ===== */

    .command-block {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      padding: 12px 14px;
    }
    .command-block code {
      font-size: 13px;
      color: var(--text-primary, #cdd6f4);
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
      color: var(--text-secondary, #a6adc8);
    }
    .meta-value.mono { font-family: monospace; font-size: 11px; }

    .decision-info {
      background: var(--panel-bg, #181825);
      border-radius: 6px;
      padding: 10px 12px;
    }

    .decision-reason {
      font-size: 12px;
      color: var(--text-secondary, #a6adc8);
      font-style: italic;
      display: block;
      margin-top: 4px;
    }

    /* Rules */

    .rules-section {
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
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
      border-top: 1px solid var(--border-color, #313244);
    }

    .rule-form {
      display: flex;
      gap: 6px;
      padding: 8px;
      border-bottom: 1px solid var(--border-color, #313244);
    }
    .rule-form .input { flex: 1; }

    .rule-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border-bottom: 1px solid var(--border-color, #313244);
      font-size: 11px;
    }
    .rule-row:last-child { border-bottom: none; }

    .rule-action-badge {
      font-size: 8px;
      font-weight: 700;
      text-transform: uppercase;
      padding: 1px 4px;
      border-radius: 2px;
      color: #11111b;
    }
    .action-approve { background: #a6e3a1; }
    .action-deny { background: #f38ba8; }

    .rule-pattern {
      flex: 1;
      font-size: 11px;
      color: var(--text-primary, #cdd6f4);
    }

    .icon-btn-sm {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      background: transparent;
      border: none;
      border-radius: 3px;
      color: var(--text-muted, #6c7086);
      cursor: pointer;
    }
    .icon-btn-sm:hover { color: #f38ba8; }
    .icon-btn-sm .icon { font-size: 14px; }

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
      scrollbar-color: var(--border-color, #313244) transparent;
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
      color: var(--text-secondary, #a6adc8);
      font-size: 12px;
      line-height: 1.5;
    }

    .msg-bubble {
      border-radius: 8px;
      padding: 10px 14px;
      max-width: 85%;
    }
    .msg-outbound {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      align-self: flex-start;
    }
    .msg-inbound {
      background: rgba(203, 166, 247, 0.08);
      border: 1px solid rgba(203, 166, 247, 0.15);
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
      color: var(--text-secondary, #a6adc8);
    }
    .msg-outbound .msg-sender { color: #fab387; }
    .msg-inbound .msg-sender { color: var(--accent-color, #cba6f7); }

    .msg-time {
      font-size: 10px;
      color: var(--text-muted, #6c7086);
    }

    .msg-content {
      font-size: 13px;
      line-height: 1.55;
      color: var(--text-primary, #cdd6f4);
    }

    .msg-content ::ng-deep {
      p { margin: 0 0 8px; }
      p:last-child { margin-bottom: 0; }
      code {
        background: var(--surface-0, #313244);
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 12px;
      }
      pre {
        background: var(--surface-0, #313244);
        border-radius: 4px;
        padding: 8px 10px;
        overflow-x: auto;
        margin: 6px 0;
      }
      pre code { background: none; padding: 0; }
    }

    .reply-form {
      border-top: 1px solid var(--border-color, #313244);
      padding-top: 12px;
    }

    .reply-input {
      width: 100%;
      padding: 8px 10px;
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      color: var(--text-primary, #cdd6f4);
      font-size: 13px;
      font-family: inherit;
      resize: vertical;
      min-height: 48px;
      box-sizing: border-box;
    }
    .reply-input:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }

    .reply-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 8px;
    }

    .urgent-check {
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      cursor: pointer;
    }
    .urgent-check input { accent-color: var(--accent-color, #cba6f7); }

    /* ===== REVIEW DETAIL ===== */

    .freeze-badge {
      font-size: 9px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.3px;
      padding: 2px 6px;
      border-radius: 3px;
      color: #11111b;
    }
    .freeze-badge.complete { background: #a6e3a1; }
    .freeze-badge.phase { background: var(--accent-color, #cba6f7); }
    .freeze-badge.upgrade { background: #f9e2af; }

    .upgrade-command {
      display: block;
      padding: 6px 10px;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 4px;
      font-family: monospace;
      font-size: 0.85em;
      color: var(--text-primary, #cdd6f4);
      word-break: break-all;
    }

    .upgrade-hint {
      font-size: 0.8em;
      color: var(--text-secondary, #a6adc8);
      font-style: italic;
    }

    .upgrade-buttons {
      display: flex;
      gap: 8px;
    }

    .btn-upgrade {
      background: rgba(249, 226, 175, 0.2);
      color: #f9e2af;
    }

    .btn-upgrade:hover:not(:disabled) {
      background: rgba(249, 226, 175, 0.3);
    }

    .review-job-info {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      padding: 12px;
    }

    .review-job-desc {
      font-size: 13px;
      color: var(--text-primary, #cdd6f4);
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
      color: var(--text-secondary, #a6adc8);
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
      background: var(--surface-0, #313244);
      border-radius: 3px;
      overflow: hidden;
    }

    .confidence-fill {
      height: 100%;
      border-radius: 3px;
      transition: width 0.3s ease;
    }
    .confidence-fill.low { background: #f38ba8; }
    .confidence-fill.medium { background: #f9e2af; }
    .confidence-fill.high { background: #a6e3a1; }

    .confidence-label {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-secondary, #a6adc8);
      font-variant-numeric: tabular-nums;
      min-width: 30px;
    }

    .deliverables-list {
      margin: 0;
      padding: 0 0 0 18px;
      font-size: 12px;
      color: var(--text-secondary, #a6adc8);
      line-height: 1.6;
    }

    .review-action-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
      width: 100%;
    }

    .review-notes {
      width: 100%;
      resize: vertical;
      min-height: 40px;
      box-sizing: border-box;
    }

    /* ===== SHARED: BUTTONS & INPUTS ===== */

    .action-bar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    .btn {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 6px 14px;
      border: none;
      border-radius: 5px;
      font-size: 12px;
      font-family: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.12s;
    }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn .icon { font-size: 15px; }
    .btn kbd {
      font-size: 9px;
      padding: 1px 4px;
      border-radius: 2px;
      background: rgba(0,0,0,0.2);
      font-family: inherit;
      margin-left: 2px;
    }

    .btn-approve { background: #a6e3a1; color: #11111b; }
    .btn-approve:hover:not(:disabled) { background: #94e2d5; }

    .btn-deny { background: #f38ba8; color: #11111b; }
    .btn-deny:hover:not(:disabled) { background: #eba0ac; }

    .btn-resume { background: var(--accent-color, #cba6f7); color: #11111b; }
    .btn-resume:hover:not(:disabled) { background: #b4befe; }

    .btn-send { background: #89b4fa; color: #11111b; }
    .btn-send:hover:not(:disabled) { background: #74c7ec; }

    .btn-ghost {
      background: transparent;
      color: var(--text-secondary, #a6adc8);
      border: 1px solid var(--border-color, #313244);
    }
    .btn-ghost:hover { border-color: var(--text-muted, #6c7086); }

    .btn-sm { padding: 3px 8px; font-size: 11px; background: var(--accent-color, #cba6f7); color: #11111b; }

    .input, .select {
      padding: 5px 8px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 4px;
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      font-family: inherit;
    }
    .input:focus, .select:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }

    /* ===== DIALOG ===== */

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
      border-radius: 10px;
      padding: 20px;
      width: 420px;
      max-width: 90vw;
    }

    .dialog-title {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 14px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      margin-bottom: 12px;
    }
    .dialog-title .icon { color: #f38ba8; }

    .deny-reason {
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

    /* Shortcuts dialog */

    .shortcuts-dialog {
      background: var(--panel-bg, #181825);
      border: 1px solid var(--border-color, #313244);
      border-radius: 10px;
      padding: 24px;
      width: 380px;
      max-width: 90vw;
    }

    .shortcuts-title {
      font-size: 15px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      margin-bottom: 16px;
    }

    .shortcuts-grid {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 6px 14px;
      margin-bottom: 16px;
    }

    .shortcuts-grid kbd {
      display: inline-block;
      padding: 2px 7px;
      background: var(--surface-0, #313244);
      border-radius: 4px;
      font-size: 11px;
      font-family: inherit;
      color: var(--accent-color, #cba6f7);
      text-align: center;
      min-width: 24px;
    }

    .shortcuts-grid span {
      font-size: 12px;
      color: var(--text-secondary, #a6adc8);
      padding-top: 2px;
    }

    .shortcuts-close { width: 100%; justify-content: center; }

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
      .filter-chips { gap: 3px; }
      .chip { padding: 3px 7px; font-size: 10px; }
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

  @ViewChild('threadBody') threadBodyRef?: ElementRef<HTMLElement>;
  @ViewChild('replyInput') replyInputRef?: ElementRef<HTMLTextAreaElement>;

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

  readonly isMobile = signal(window.innerWidth <= 768);
  readonly isMobileDetail = computed(() => {
    return !!this.selectedItem() && this.isMobile();
  });

  ngOnInit(): void {
    this.actionCenter.initSSE();
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
  relativeTime(iso: string): string { return relativeTime(iso); }

  ttlColor(req: SudoRequest): string {
    const s = secondsLeft(req);
    if (s <= 0) return 'red';
    if (s < 30) return 'red';
    if (s < 120) return 'amber';
    return 'green';
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
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
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
        if (this.selectedItem()?.type === 'sudo' && this.selectedItem()?.sudo?.status === 'pending') {
          this.approveSudo();
        } else if (this.selectedItem()?.type === 'review') {
          this.approveReview();
        }
        break;
      case 'd':
        if (this.selectedItem()?.type === 'sudo' && this.selectedItem()?.sudo?.status === 'pending') {
          this.startDenySudo();
        }
        break;
      case 'r':
        if (this.selectedItem()?.type === 'message') {
          this.replyInputRef?.nativeElement?.focus();
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
