import {
    AfterViewChecked,
    Component,
    computed,
    effect,
    ElementRef,
    inject,
    OnDestroy,
    signal,
    ViewChild,
} from '@angular/core';
import {JsonPipe, TitleCasePipe} from '@angular/common';
import {FormsModule} from '@angular/forms';
import {RouterLink} from '@angular/router';
import {MarkdownComponent} from 'ngx-markdown';
import {PersistentChatService, ToolCallInfo,} from '../../../core/services/persistent-chat.service';
import {ApiService, IdeSessionStatus} from '../../../core/services/api.service';
import {ModelService} from '../../../core/services/model.service';
import {environment} from '../../../core/environment';

interface SlashCommand {
    command: string;
    description: string;
}

const SLASH_COMMANDS: SlashCommand[] = [
    {command: '/compact', description: 'Compress conversation history to free up context'},
    {command: '/done', description: 'End session — extract memories and archive'},
    {command: '/undo', description: 'Undo file changes from the last turn'},
    {command: '/auto', description: 'Switch to auto-accept mode (approve all except shell)'},
    {command: '/supervised', description: 'Switch to supervised mode (approve all writes)'},
    {command: '/autonomous', description: 'Switch to autonomous mode (approve nothing)'},
];

@Component({
    selector: 'app-persistent-chat',
    standalone: true,
    imports: [FormsModule, JsonPipe, TitleCasePipe, RouterLink, MarkdownComponent],
    template: `
    <div class="chat-container">
      <!-- Header -->
      <div class="chat-header">
        <div class="header-left">
          <a class="back-link" routerLink="/sessions">
            <span class="back-icon">arrow_back</span>
          </a>
          <span class="header-icon">smart_toy</span>
          <span class="header-title">{{ chat.sessionTitle() || 'Persistent Agent' }}</span>
          <span class="status-dot" [class]="connectionClass()"></span>
          <span class="status-label">{{ connectionLabel() }}</span>
        </div>
        <div class="header-right">
          @if (chat.isConnected()) {
            <button class="settings-btn" (click)="showSettings.update(v => !v)"
                    [class.active]="showSettings()" title="Session settings">
              <span class="settings-icon">tune</span>
            </button>
          }

          @if (chat.isConnected()) {
            @if (chat.ncSessionFolder()) {
              <button class="ide-btn" (click)="openSessionFiles()" title="Open session files in Nextcloud">
                <span class="ide-icon">cloud</span>
                Files
              </button>
            }
            @if (ideStatus(); as ide) {
              @if (ide.gitea_url) {
                <button class="ide-btn gitea-btn" (click)="openIde(ide.gitea_url!)" title="View workspace in Gitea">
                  <span class="ide-icon">history</span>
                  Git
                </button>
              }
              @if (ide.status === 'active' && ide.code_server_url) {
                <button class="ide-btn" (click)="openIde(ide.code_server_url!)" title="Open workspace in IDE">
                  <span class="ide-icon">code</span>
                  IDE
                </button>
              } @else if (ide.status === 'restoring') {
                <button class="ide-btn ide-loading" disabled title="Workspace starting...">
                  <span class="ide-spinner"></span>
                  IDE
                </button>
              }
            }
            <button class="disconnect-btn" (click)="chat.disconnect()">
              Disconnect
            </button>
          }
        </div>
      </div>

      <!-- Status bar -->
      @if (chat.isConnected()) {
        <div class="status-bar">
          @if (chat.modelName()) {
            <span class="status-chip model-chip">{{ chat.modelName() }}</span>
          }
          @if (chat.temperature()) {
            <span class="status-chip">temp {{ chat.temperature() }}</span>
          }
          <span class="status-chip turn-chip">Turn {{ chat.turnCount() }}</span>
          <span class="status-chip mode-chip">{{ chat.permissionMode() | titlecase }}</span>
        </div>
      }

      <!-- Settings panel -->
      @if (showSettings()) {
        <div class="settings-panel">
          <div class="settings-row">
            <label class="settings-label">Mode</label>
            <select class="settings-select"
                    [value]="chat.permissionMode()"
                    (change)="onModeChange($event)">
              <option value="supervised">Supervised</option>
              <option value="auto_accept">Auto-accept</option>
              <option value="autonomous">Autonomous</option>
            </select>
          </div>
          <div class="settings-row">
            <label class="settings-label">Model</label>
            <select class="settings-select"
                    [ngModel]="chat.modelName()"
                    (ngModelChange)="onModelChange($event)">
              @if (chat.modelName() && !hasModelInList(chat.modelName()!)) {
                <option [ngValue]="chat.modelName()">{{ chat.modelName() }}</option>
              }
              @for (group of modelService.models(); track group.group) {
                <optgroup [label]="group.group">
                  @for (model of group.models; track model) {
                    <option [ngValue]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </select>
          </div>
          <div class="settings-row">
            <label class="settings-label">Temperature: {{ chat.temperature() }}</label>
            <input type="range" class="settings-slider" min="0" max="2" step="0.1"
                   [ngModel]="chat.temperature()"
                   (ngModelChange)="onTemperatureChange($event)">
          </div>
        </div>
      }

      <!-- Task bar -->
      @if (chat.tasks().length) {
        <div class="task-bar">
          <div class="task-header">
            <span class="task-header-icon">checklist</span>
            Tasks {{ completedTaskCount() }}/{{ chat.tasks().length }}
          </div>
          <div class="task-list">
            @for (task of chat.tasks(); track task.id) {
              <div class="task-item" [class.task-completed]="task.status === 'completed'">
                <span class="task-check">{{ task.status === 'completed' ? 'check_circle' : 'radio_button_unchecked' }}</span>
                <span class="task-desc">{{ task.description }}</span>
              </div>
            }
          </div>
        </div>
      }

      <!-- Messages -->
      <div class="messages" #messagesContainer (scroll)="onMessagesScroll()">
        @for (msg of chat.messages(); track $index) {
          <div class="message" [class]="'message-' + msg.role"
               [class.historical]="msg.historical"
               [class.tool-only]="msg.role === 'assistant' && !msg.content && msg.toolCalls?.length">
            @if (msg.role === 'system') {
              <div class="system-message">
                <span class="system-icon">info</span>
                {{ msg.content }}
              </div>
              @if (chat.isSessionPaused() && $last) {
                <button class="resume-btn" (click)="resumeSession()" [disabled]="isResuming()">
                  @if (isResuming()) {
                    <span class="resume-btn-spinner"></span>
                    Resuming...
                  } @else {
                    <span class="resume-icon">play_arrow</span>
                    Resume Session
                  }
                </button>
              }
            } @else if (msg.role === 'assistant' && !msg.content && msg.toolCalls?.length) {
              <!-- Tool-only message: compact inline indicator -->
              <div class="tool-only-row">
                <span class="tool-only-icon">{{ toolIcon(msg.toolCalls![0].tool) }}</span>
                <span class="tool-only-label">
                  {{ groupToolCalls(msg.toolCalls!) }}
                </span>
                <span class="tool-summary-dot" [class]="toolSummaryStatus(msg.toolCalls!)"></span>
              </div>
            } @else {
              <div class="avatar">
                <span class="avatar-icon">{{ msg.role === 'user' ? 'person' : 'smart_toy' }}</span>
              </div>
              <div class="message-body">
                @if (msg.role === 'user') {
                  <div class="user-text">{{ msg.content }}</div>
                } @else {
                  @if (msg.content) {
                    <markdown [data]="msg.content"></markdown>
                  }
                  @if (msg.toolCalls?.length) {
                    <details class="tool-summary">
                      <summary class="tool-summary-line">
                        <span class="tool-summary-chevron">chevron_right</span>
                        <span class="tool-summary-text">
                          Used {{ msg.toolCalls!.length }} tool{{ msg.toolCalls!.length !== 1 ? 's' : '' }}:
                          {{ groupToolCalls(msg.toolCalls!) }}
                        </span>
                        <span class="tool-summary-dot" [class]="toolSummaryStatus(msg.toolCalls!)"></span>
                      </summary>
                      <div class="tool-detail-list">
                        @for (tc of msg.toolCalls; track tc.id) {
                          <details class="tool-detail-item">
                            <summary class="tool-detail-header">
                              <span class="tool-icon">{{ toolIcon(tc.tool) }}</span>
                              <span class="tool-detail-name">{{ tc.tool }}</span>
                              <span class="tool-detail-args">{{ formatToolArgs(tc.args) }}</span>
                              <span class="tool-detail-status" [class]="'status-' + tc.status">{{ tc.status }}</span>
                            </summary>
                            @if (tc.result) {
                              <div class="tool-preview">{{ previewResult(tc.result) }}</div>
                              <pre class="tool-detail-result">{{ tc.result }}</pre>
                            }
                          </details>
                        }
                      </div>
                    </details>
                  }
                }
              </div>
            }
          </div>
          @if (msg.historical && chat.messages()[$index + 1] && !chat.messages()[$index + 1].historical) {
            <div class="session-divider">
              <span class="divider-line"></span>
              <span class="divider-text">Session resumed</span>
              <span class="divider-line"></span>
            </div>
          }
        } @empty {
          @if (!chat.isStreaming()) {
            <div class="empty-state">
              @if (chat.sessionReady()) {
                <span class="empty-state-text">Start a conversation...</span>
              } @else {
                <div class="startup-spinner-container">
                  <div class="startup-spinner"></div>
                  <span class="startup-label">
                    @if (chat.isCreating()) {
                      Creating session...
                    } @else if (chat.isConnected()) {
                      @switch (chat.startupPhase()) {
                        @case ('provisioning') { Provisioning agent... }
                        @case ('booting') { Agent starting... }
                        @case ('connecting') { Connecting to agent... }
                        @default { Session starting... }
                      }
                    } @else {
                      Connecting...
                    }
                  </span>
                </div>
              }
            </div>
          }
        }

        <!-- Resume spinner: shown when history exists but session not yet ready -->
        @if (chat.messages().length && !chat.sessionReady() && !chat.isStreaming()) {
          <div class="startup-spinner-container resume-spinner">
            <div class="startup-spinner"></div>
            <span class="startup-label">
              @if (chat.isCreating()) {
                Creating session...
              } @else if (chat.isConnected()) {
                @switch (chat.startupPhase()) {
                  @case ('provisioning') { Provisioning agent... }
                  @case ('booting') { Agent starting... }
                  @case ('connecting') { Connecting to agent... }
                  @default { Reconnecting agent... }
                }
              } @else {
                Connecting...
              }
            </span>
          </div>
        }

        <!-- Streaming response -->
        @if (chat.isStreaming()) {
          <div class="message message-assistant">
            <div class="avatar">
              <span class="avatar-icon">smart_toy</span>
            </div>
            <div class="message-body">
              @if (chat.streamingText()) {
                <markdown [data]="chat.streamingText()"></markdown>
              }
              @if (chat.currentToolCalls().length) {
                @if (hasRunningTools(chat.currentToolCalls())) {
                  <div class="tool-progress">
                    <span class="tool-progress-spinner"></span>
                    <span class="tool-progress-text">Running {{ currentToolLabel(chat.currentToolCalls()) }}...</span>
                  </div>
                }
                @if (hasCompletedTools(chat.currentToolCalls())) {
                  <details class="tool-summary">
                    <summary class="tool-summary-line">
                      <span class="tool-summary-chevron">chevron_right</span>
                      <span class="tool-summary-text">
                        Used {{ completedToolCount(chat.currentToolCalls()) }} tool{{ completedToolCount(chat.currentToolCalls()) !== 1 ? 's' : '' }}:
                        {{ groupToolCalls(completedOnly(chat.currentToolCalls())) }}
                      </span>
                      <span class="tool-summary-dot completed"></span>
                    </summary>
                    <div class="tool-detail-list">
                      @for (tc of completedOnly(chat.currentToolCalls()); track tc.id) {
                        <details class="tool-detail-item">
                          <summary class="tool-detail-header">
                            <span class="tool-detail-name">{{ tc.tool }}</span>
                            <span class="tool-detail-args">{{ formatToolArgs(tc.args) }}</span>
                            <span class="tool-detail-status status-completed">completed</span>
                          </summary>
                          @if (tc.result) {
                            <pre class="tool-detail-result">{{ tc.result }}</pre>
                          }
                        </details>
                      }
                    </div>
                  </details>
                }
              }
              @if (!chat.streamingText() && !chat.currentToolCalls().length) {
                <div class="thinking">
                  <span class="thinking-dot"></span>
                  <span class="thinking-dot"></span>
                  <span class="thinking-dot"></span>
                </div>
              }
            </div>
          </div>
        }

        <!-- Permission request -->
        @if (chat.pendingPermission(); as perm) {
          <div class="permission-request">
            <div class="perm-header">
              <span class="perm-icon">shield</span>
              Permission required
            </div>
            <div class="perm-body">
              <strong>{{ perm.tool }}</strong>
              <pre class="perm-args">{{ perm.args | json }}</pre>
            </div>
            <div class="perm-actions">
              <button class="perm-btn approve" (click)="chat.approve()">Approve</button>
              <button class="perm-btn auto-accept" (click)="approveAndAutoAccept()">Auto-accept</button>
              <button class="perm-btn deny" (click)="chat.deny()">Deny</button>
            </div>
          </div>
        }
      </div>

      <!-- Error banner -->
      @if (chat.error(); as err) {
        <div class="error-banner">
          <span class="error-icon">error</span>
          {{ err }}
          <button class="error-dismiss" (click)="chat.error.set(null)">dismiss</button>
        </div>
      }

      <!-- Input -->
      <div class="input-area">
        @if (chat.isStreaming()) {
          <button class="interrupt-btn" (click)="chat.interrupt()">
            <span class="interrupt-icon">stop_circle</span>
            Stop
          </button>
        }
        <div class="input-wrapper">
          <!-- Slash command autocomplete -->
          @if (showSlashMenu()) {
            <div class="slash-menu">
              @for (cmd of filteredCommands(); track cmd.command) {
                <div
                  class="slash-item"
                  [class.selected]="$index === slashSelectedIndex()"
                  (click)="selectSlashCommand(cmd)"
                  (mouseenter)="slashSelectedIndex.set($index)"
                >
                  <span class="slash-cmd">{{ cmd.command }}</span>
                  <span class="slash-desc">{{ cmd.description }}</span>
                </div>
              }
            </div>
          }
          <textarea
            #inputEl
            class="chat-input"
            [(ngModel)]="inputText"
            (ngModelChange)="onInputChange($event)"
            (keydown)="onKeydown($event)"
            [placeholder]="inputPlaceholder()"
            [disabled]="!chat.isConnected()"
            rows="1"
          ></textarea>
        </div>
        <button
          class="send-btn"
          (click)="send()"
          [disabled]="!canSend()"
          [class.pending]="isPendingSend()"
        >
          @if (isPendingSend()) {
            <span class="send-spinner"></span>
          } @else {
            <span class="send-icon">send</span>
          }
        </button>
      </div>
    </div>
  `,
    styles: [
        `
      :host {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--app-bg, #1e1e2e);
      }

      .chat-container {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      /* Header */

      .chat-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 16px;
        border-bottom: 1px solid var(--border-color, #313244);
        background: var(--panel-bg, #181825);
        flex-shrink: 0;
      }

      .header-left {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .back-link {
        display: flex;
        align-items: center;
        color: var(--text-muted, #6c7086);
        text-decoration: none;
        margin-right: 4px;
      }

      .back-link:hover { color: var(--text-primary, #cdd6f4); }

      .back-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
      }

      .header-icon, .perm-icon, .error-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        color: var(--accent-color, #cba6f7);
      }

      .header-title {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
      }

      .status-dot.connected { background: #a6e3a1; }
      .status-dot.connecting { background: #f9e2af; animation: pulse 1s infinite; }
      .status-dot.disconnected { background: var(--surface-2, #585b70); }
      .status-dot.error { background: #f38ba8; }

      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
      }

      .status-label {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      /* Status bar */

      .status-bar {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 4px 16px;
        border-bottom: 1px solid var(--border-color, #313244);
        background: var(--panel-bg, #181825);
        flex-shrink: 0;
      }

      .status-chip {
        font-size: 10px;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--surface-0, #313244);
        color: var(--text-muted, #6c7086);
      }

      .model-chip {
        color: var(--accent-color, #cba6f7);
        font-family: 'JetBrains Mono', monospace;
      }

      /* Task bar */
      .task-bar {
        padding: 6px 16px;
        border-bottom: 1px solid #313244;
        background: #181825;
        flex-shrink: 0;
      }
      .task-header {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        color: #6c7086;
        margin-bottom: 4px;
      }
      .task-header-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
      }
      .task-list {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .task-item {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        color: #cdd6f4;
      }
      .task-item .task-check {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
        color: #6c7086;
      }
      .task-completed {
        color: #6c7086;
        text-decoration: line-through;
      }
      .task-completed .task-check {
        color: #a6e3a1;
      }

      .header-right {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .mode-select {
        padding: 4px 8px;
        border-radius: 4px;
        border: 1px solid var(--border-color, #313244);
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
      }

      .settings-btn {
        display: flex;
        align-items: center;
        padding: 4px 8px;
        border-radius: 4px;
        border: 1px solid var(--border-color, #313244);
        background: transparent;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        transition: all 0.15s ease;
      }
      .settings-btn:hover, .settings-btn.active {
        color: var(--accent-color, #cba6f7);
        border-color: var(--accent-color, #cba6f7);
      }
      .settings-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .settings-panel {
        padding: 10px 16px;
        border-bottom: 1px solid var(--border-color, #313244);
        background: var(--panel-bg, #181825);
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        flex-shrink: 0;
      }
      .settings-row {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .settings-label {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        white-space: nowrap;
      }
      .settings-select {
        padding: 3px 6px;
        border-radius: 4px;
        border: 1px solid var(--border-color, #313244);
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        max-width: 220px;
      }
      .settings-slider {
        width: 100px;
        accent-color: var(--accent-color, #cba6f7);
      }

      .connect-btn, .disconnect-btn {
        padding: 4px 12px;
        border-radius: 4px;
        border: 1px solid var(--border-color, #313244);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .connect-btn {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border-color: var(--accent-color, #cba6f7);
      }

      .disconnect-btn {
        background: transparent;
        color: var(--text-muted, #6c7086);
      }

      .disconnect-btn:hover {
        color: #f38ba8;
        border-color: #f38ba8;
      }

      .ide-btn {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 12px;
        border-radius: 4px;
        border: 1px solid #89b4fa;
        background: transparent;
        color: #89b4fa;
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .ide-btn:hover {
        background: rgba(137, 180, 250, 0.1);
      }

      .ide-btn:disabled {
        opacity: 0.6;
        cursor: default;
      }

      .gitea-btn {
        border-color: #a6e3a1;
        color: #a6e3a1;
      }

      .gitea-btn:hover {
        background: rgba(166, 227, 161, 0.1);
      }

      .ide-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .ide-spinner {
        width: 12px;
        height: 12px;
        border: 2px solid rgba(137, 180, 250, 0.3);
        border-top-color: #89b4fa;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      /* Connect dialog */

      .connect-dialog {
        padding: 12px 16px;
        background: var(--panel-bg, #181825);
        border-bottom: 1px solid var(--border-color, #313244);
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .connect-field label {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        display: block;
        margin-bottom: 4px;
      }

      .connect-field input {
        width: 100%;
        padding: 6px 10px;
        border-radius: 4px;
        border: 1px solid var(--border-color, #313244);
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-family: inherit;
      }

      .connect-actions {
        display: flex;
        gap: 8px;
      }

      .action-btn {
        padding: 5px 14px;
        border-radius: 4px;
        border: 1px solid var(--accent-color, #cba6f7);
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        font-size: 12px;
        font-family: inherit;
        cursor: pointer;
      }

      .action-btn.secondary {
        background: transparent;
        color: var(--text-muted, #6c7086);
        border-color: var(--border-color, #313244);
      }

      /* Messages */

      .messages {
        flex: 1;
        overflow-y: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 16px;
        scrollbar-width: thin;
      }

      .empty-state {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      .empty-state-text {
        color: var(--text-muted, #6c7086);
        font-size: 14px;
        scrollbar-color: var(--border-color, #313244) transparent;
      }

      .startup-spinner-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 16px;
      }

      .startup-spinner {
        width: 32px;
        height: 32px;
        border: 3px solid var(--border-color, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .startup-label {
        color: var(--text-muted, #6c7086);
        font-size: 13px;
      }

      .message {
        display: flex;
        gap: 10px;
        max-width: 90%;
      }

      .message-user {
        align-self: flex-end;
        flex-direction: row-reverse;
      }

      .message-assistant, .message-system {
        align-self: flex-start;
      }

      .avatar {
        width: 30px;
        height: 30px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        background: var(--surface-0, #313244);
      }

      .message-user .avatar {
        background: var(--accent-color, #cba6f7);
      }

      .avatar-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        color: var(--text-secondary, #a6adc8);
      }

      .message-user .avatar-icon {
        color: var(--timeline-bg, #11111b);
      }

      .message-body {
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
        line-height: 1.5;
        min-width: 0;
      }

      .message-user .message-body {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border-bottom-right-radius: 4px;
      }

      .message-assistant .message-body {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        border-bottom-left-radius: 4px;
      }

      .user-text {
        white-space: pre-wrap;
        word-break: break-word;
      }

      /* historical messages use session divider instead of dimming */

      .system-message {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        padding: 4px 12px;
        background: var(--surface-0, #313244);
        border-radius: 6px;
        width: fit-content;
        margin: 0 auto;
      }

      .system-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
      }

      .resume-btn {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 8px auto 0;
        padding: 6px 16px;
        border-radius: 6px;
        border: 1px solid var(--accent-color, #cba6f7);
        background: var(--accent-color, #cba6f7);
        color: var(--app-bg, #1e1e2e);
        font-size: 12px;
        font-family: inherit;
        font-weight: 600;
        cursor: pointer;
        transition: opacity 0.15s ease;
      }

      .resume-btn:hover { opacity: 0.85; }
      .resume-btn:disabled { opacity: 0.5; cursor: wait; }

      .resume-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .resume-btn-spinner {
        width: 14px;
        height: 14px;
        border: 2px solid var(--app-bg, #1e1e2e);
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .resume-spinner {
        padding: 24px 0;
      }

      /* Tool-only messages: compact inline indicators (no avatar/bubble) */

      .message.tool-only {
        max-width: none;
        gap: 0;
      }

      .tool-only-row {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 2px 8px 2px 40px;  /* indent to align with message body (avatar 30px + gap 10px) */
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .tool-only-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
        color: var(--text-muted, #6c7086);
      }

      .tool-only-label {
        white-space: nowrap;
      }

      /* Tool summary (collapsed by default) */

      .tool-summary {
        margin-top: 8px;
        font-size: 12px;
      }

      .tool-summary-line {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 4px 8px;
        cursor: pointer;
        border-radius: 6px;
        color: var(--text-muted, #6c7086);
        transition: background 0.15s;
        list-style: none;
      }

      .tool-summary-line::-webkit-details-marker { display: none; }
      .tool-summary-line:hover { background: rgba(255, 255, 255, 0.04); }

      .tool-summary-chevron {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
        transition: transform 0.15s;
      }

      details[open] > .tool-summary-line .tool-summary-chevron {
        transform: rotate(90deg);
      }

      .tool-summary-text { flex: 1; }

      .tool-summary-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        flex-shrink: 0;
      }

      .tool-summary-dot.completed { background: #a6e3a1; }
      .tool-summary-dot.running { background: #f9e2af; animation: pulse 1s infinite; }
      .tool-summary-dot.denied { background: #f38ba8; }
      .tool-summary-dot.mixed { background: #f9e2af; }

      /* Tool detail list (level 2) */

      .tool-detail-list {
        padding: 4px 0 4px 20px;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .tool-detail-item { font-size: 11px; }

      .tool-detail-header {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 3px 8px;
        cursor: pointer;
        border-radius: 4px;
        color: var(--text-muted, #6c7086);
        list-style: none;
      }

      .tool-detail-header::-webkit-details-marker { display: none; }
      .tool-detail-header:hover { background: rgba(255, 255, 255, 0.03); }

      .tool-detail-name {
        font-family: 'JetBrains Mono', monospace;
        color: var(--accent-color, #cba6f7);
        font-weight: 500;
      }

      .tool-detail-args {
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
        min-width: 0;
      }

      .tool-detail-status {
        font-size: 10px;
        margin-left: auto;
      }

      .tool-detail-status.status-completed { color: #a6e3a1; }
      .tool-detail-status.status-running { color: #f9e2af; }
      .tool-detail-status.status-denied { color: #f38ba8; }
      .tool-detail-status.status-pending { color: var(--text-muted, #6c7086); }

      .tool-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 12px;
        color: var(--text-muted, #6c7086);
        width: 16px;
        text-align: center;
        flex-shrink: 0;
      }

      .tool-preview {
        padding: 2px 8px 2px 28px;
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        opacity: 0.6;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-family: 'JetBrains Mono', monospace;
      }

      .tool-detail-item[open] > .tool-preview { display: none; }

      .tool-detail-result {
        margin: 4px 0 4px 8px;
        padding: 8px 10px;
        background: var(--panel-bg, #181825);
        border-radius: 6px;
        font-size: 11px;
        max-height: 200px;
        overflow: auto;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: 'JetBrains Mono', monospace;
        line-height: 1.4;
        color: var(--text-secondary, #a6adc8);
      }

      /* Inline tool progress (streaming) */

      .tool-progress {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 4px 8px;
        font-size: 12px;
        color: var(--text-muted, #6c7086);
        margin-top: 6px;
      }

      .tool-progress-spinner {
        width: 12px;
        height: 12px;
        border: 2px solid var(--border-color, #313244);
        border-top-color: #f9e2af;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
        flex-shrink: 0;
      }

      .tool-progress-text {
        font-style: italic;
      }

      /* Session divider */

      .session-divider {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 0;
      }

      .divider-line {
        flex: 1;
        height: 1px;
        background: var(--border-color, #313244);
      }

      .divider-text {
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* Thinking dots */

      .thinking {
        display: flex;
        gap: 4px;
        padding: 4px 0;
      }

      .thinking-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--text-muted, #6c7086);
        animation: bounce 1.4s infinite ease-in-out;
      }

      .thinking-dot:nth-child(1) { animation-delay: 0s; }
      .thinking-dot:nth-child(2) { animation-delay: 0.2s; }
      .thinking-dot:nth-child(3) { animation-delay: 0.4s; }

      @keyframes bounce {
        0%, 80%, 100% { transform: translateY(0); }
        40% { transform: translateY(-6px); }
      }

      /* Permission request */

      .permission-request {
        margin: 0 auto;
        width: 80%;
        border: 1px solid #f9e2af;
        border-radius: 8px;
        padding: 12px;
        background: rgba(249, 226, 175, 0.05);
      }

      .perm-header {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        font-weight: 600;
        color: #f9e2af;
        margin-bottom: 8px;
      }

      .perm-header .perm-icon { color: #f9e2af; }

      .perm-body {
        font-size: 12px;
        color: var(--text-secondary, #a6adc8);
      }

      .perm-args {
        margin: 4px 0 8px;
        padding: 6px 8px;
        background: var(--surface-0, #313244);
        border-radius: 4px;
        font-size: 11px;
        max-height: 120px;
        overflow: auto;
      }

      .perm-actions {
        display: flex;
        gap: 8px;
      }

      .perm-btn {
        padding: 5px 16px;
        border-radius: 4px;
        border: none;
        font-size: 12px;
        font-family: inherit;
        cursor: pointer;
        font-weight: 600;
      }

      .perm-btn.approve {
        background: #a6e3a1;
        color: var(--timeline-bg, #11111b);
      }

      .perm-btn.auto-accept {
        background: #89b4fa;
        color: var(--timeline-bg, #11111b);
      }

      .perm-btn.deny {
        background: #f38ba8;
        color: var(--timeline-bg, #11111b);
      }

      /* Error banner */

      .error-banner {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 16px;
        background: rgba(243, 139, 168, 0.1);
        border-top: 1px solid #f38ba8;
        font-size: 12px;
        color: #f38ba8;
      }

      .error-dismiss {
        margin-left: auto;
        background: transparent;
        border: none;
        color: #f38ba8;
        font-size: 11px;
        cursor: pointer;
        font-family: inherit;
        text-decoration: underline;
      }

      /* Input area */

      .input-area {
        display: flex;
        align-items: flex-end;
        gap: 8px;
        padding: 12px 16px;
        border-top: 1px solid var(--border-color, #313244);
        background: var(--panel-bg, #181825);
        flex-shrink: 0;
      }

      .interrupt-btn {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 6px 12px;
        border-radius: 6px;
        border: 1px solid #f38ba8;
        background: transparent;
        color: #f38ba8;
        font-size: 12px;
        font-family: inherit;
        cursor: pointer;
        flex-shrink: 0;
      }

      .interrupt-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .chat-input {
        flex: 1;
        padding: 8px 12px;
        border-radius: 8px;
        border: 1px solid var(--border-color, #313244);
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-size: 13px;
        font-family: inherit;
        resize: none;
        min-height: 38px;
        max-height: 120px;
        line-height: 1.4;
      }

      .chat-input:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .chat-input:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .send-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 38px;
        height: 38px;
        border-radius: 8px;
        border: none;
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        cursor: pointer;
        flex-shrink: 0;
        transition: opacity 0.15s ease;
      }

      .send-btn:disabled {
        opacity: 0.3;
        cursor: not-allowed;
      }

      .send-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
      }

      .send-btn.pending {
        opacity: 0.7;
        cursor: wait;
      }

      .send-spinner {
        width: 16px;
        height: 16px;
        border: 2px solid var(--timeline-bg, #11111b);
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* Slash command autocomplete */

      .input-wrapper {
        flex: 1;
        position: relative;
        min-width: 0;
      }

      .input-wrapper .chat-input {
        width: 100%;
      }

      .slash-menu {
        position: absolute;
        bottom: 100%;
        left: 0;
        right: 0;
        margin-bottom: 4px;
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #313244);
        border-radius: 8px;
        padding: 4px;
        box-shadow: 0 -4px 12px rgba(0, 0, 0, 0.3);
        z-index: 10;
      }

      .slash-item {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        border-radius: 6px;
        cursor: pointer;
        transition: background 0.1s ease;
      }

      .slash-item:hover, .slash-item.selected {
        background: var(--surface-0, #313244);
      }

      .slash-cmd {
        font-weight: 600;
        font-size: 13px;
        color: var(--accent-color, #cba6f7);
        min-width: 100px;
      }

      .slash-desc {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      /* Markdown content styling */

      .message-body ::ng-deep pre {
        background: var(--panel-bg, #181825);
        border-radius: 8px;
        padding: 12px 16px;
        overflow-x: auto;
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        line-height: 1.5;
        border: 1px solid var(--border-color, #313244);
        margin: 8px 0;
      }

      .message-body ::ng-deep pre code {
        background: transparent;
        padding: 0;
      }

      .message-body ::ng-deep .code-copy-btn {
        position: absolute;
        top: 6px;
        right: 6px;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        padding: 2px 4px;
        cursor: pointer;
        opacity: 0;
        transition: opacity 0.15s;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      .message-body ::ng-deep pre:hover .code-copy-btn {
        opacity: 1;
      }

      .message-body ::ng-deep .code-copy-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
        color: var(--text-muted, #6c7086);
      }

      .message-body ::ng-deep code {
        background: rgba(203, 166, 247, 0.12);
        padding: 1px 5px;
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.9em;
      }

      /* Collapsible code blocks */
      .message-body ::ng-deep .code-collapse {
        border: 1px solid var(--border-color, #313244);
        border-radius: 8px;
        margin: 8px 0;
        overflow: hidden;
      }

      .message-body ::ng-deep .code-collapse > pre {
        margin: 0;
        border: none;
        border-radius: 0;
      }

      .message-body ::ng-deep .code-collapse:not([open]) > pre {
        display: none;
      }

      .message-body ::ng-deep .code-collapse-summary {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 12px;
        background: var(--panel-bg, #181825);
        cursor: pointer;
        font-size: 12px;
        color: var(--text-muted, #6c7086);
        user-select: none;
        list-style: none;
      }

      .message-body ::ng-deep .code-collapse-summary::-webkit-details-marker {
        display: none;
      }

      .message-body ::ng-deep .code-collapse-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        color: var(--accent-color, #cba6f7);
      }

      .message-body ::ng-deep .code-collapse-label {
        font-family: 'JetBrains Mono', monospace;
        font-weight: 600;
        color: var(--text-color, #cdd6f4);
      }

      .message-body ::ng-deep .code-collapse-hint {
        margin-left: auto;
        font-size: 11px;
        opacity: 0.6;
      }

      .message-body ::ng-deep .code-collapse[open] .code-collapse-hint {
        display: none;
      }

      .message-body ::ng-deep table {
        border-collapse: collapse;
        width: 100%;
        margin: 8px 0;
        font-size: 12px;
      }

      .message-body ::ng-deep th,
      .message-body ::ng-deep td {
        padding: 6px 12px;
        border-bottom: 1px solid var(--border-color, #313244);
        text-align: left;
      }

      .message-body ::ng-deep th {
        font-weight: 600;
        color: var(--accent-color, #cba6f7);
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      .message-body ::ng-deep tr:hover {
        background: rgba(255, 255, 255, 0.03);
      }

      .message-body ::ng-deep a {
        color: #89b4fa;
        text-decoration: none;
      }

      .message-body ::ng-deep a:hover {
        text-decoration: underline;
      }

      .message-body ::ng-deep blockquote {
        border-left: 3px solid var(--accent-color, #cba6f7);
        margin: 8px 0;
        padding: 4px 12px;
        color: var(--text-secondary, #a6adc8);
      }

      .message-body ::ng-deep ul,
      .message-body ::ng-deep ol {
        margin: 6px 0;
        padding-left: 20px;
      }

      .message-body ::ng-deep ul {
        list-style-type: disc;
      }

      .message-body ::ng-deep ol {
        list-style-type: decimal;
      }

      .message-body ::ng-deep li {
        margin: 3px 0;
        line-height: 1.5;
      }

      .message-body ::ng-deep li > ul,
      .message-body ::ng-deep li > ol {
        margin: 2px 0;
      }

      .message-body ::ng-deep .citation-web {
        color: var(--accent-color, #cba6f7);
        text-decoration: underline dotted;
        text-underline-offset: 2px;
      }

      .message-body ::ng-deep .citation-web:hover {
        text-decoration-style: solid;
      }

      .message-body ::ng-deep .citation-doc {
        color: #89b4fa;
        font-style: italic;
        cursor: help;
        border-bottom: 1px dashed #89b4fa;
      }
    `,
    ],
})
export class PersistentChatComponent implements AfterViewChecked, OnDestroy {
    readonly chat = inject(PersistentChatService);
    private readonly api = inject(ApiService);
    readonly modelService = inject(ModelService);

    @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;
    @ViewChild('inputEl') inputEl!: ElementRef<HTMLTextAreaElement>;

    inputText = '';

    // Settings panel
    readonly showSettings = signal(false);

    // Resume state
    readonly isResuming = signal(false);

    // Slash command autocomplete
    readonly showSlashMenu = signal(false);
    readonly slashSelectedIndex = signal(0);
    readonly filteredCommands = signal<SlashCommand[]>([]);

    // IDE status
    readonly ideStatus = signal<IdeSessionStatus | null>(null);
    private idePollingTimer: ReturnType<typeof setInterval> | null = null;
    private idePollingAttempts = 0;

    private autoScroll = true;

    constructor() {
        // Start/stop IDE polling when connection state changes
        effect(() => {
            const connected = this.chat.isConnected();
            const threadId = this.chat.threadId();
            if (connected && threadId) {
                this.startIdePolling(threadId);
            } else {
                this.stopIdePolling();
            }
        });

        // Load available models eagerly so the dropdown is ready
        this.modelService.load();

        // Auto-scroll when messages, streaming, or tool calls change
        effect(() => {
            this.chat.messages();
            this.chat.streamingText();
            this.chat.currentToolCalls();
            this.chat.pendingPermission();

            if (this.autoScroll) {
                setTimeout(() => this.scrollToBottom(), 0);
            }
        });
    }

    readonly completedTaskCount = computed(() =>
        this.chat.tasks().filter(t => t.status === 'completed').length
    );

    readonly connectionClass = computed(() => this.chat.connectionState());
    readonly connectionLabel = computed(() => {
        switch (this.chat.connectionState()) {
            case 'connected':
                return 'Connected';
            case 'connecting':
                return 'Connecting...';
            case 'error':
                return 'Error';
            default:
                return 'Disconnected';
        }
    });

    readonly inputPlaceholder = computed(() => {
        if (!this.chat.isConnected()) return 'Connect to start chatting...';
        if (this.chat.isConnected() && !this.chat.sessionReady()) return 'Type your message while the session starts...';
        if (this.chat.isStreaming()) return 'Agent is working...';
        return 'Type a message... (Enter to send, Shift+Enter for newline)';
    });

    /** True when there is a pending message waiting for the session to become ready. */
    readonly isPendingSend = computed(
        () => this.chat.pendingMessage() !== null,
    );

    readonly canSend = computed(
        () =>
            this.chat.isConnected() &&
            this.inputText.trim().length > 0 &&
            !this.isPendingSend(),
    );

    ngAfterViewChecked(): void {
        this.collapseCodeBlocks();
        this.addCopyButtons();
    }

    ngOnDestroy(): void {
        // Don't disconnect — keep session alive across navigation
        this.stopIdePolling();
    }

    send(): void {
        const text = this.inputText.trim();
        if (!text) return;

        this.showSlashMenu.set(false);
        this.chat.sendMessage(text);
        this.inputText = '';
        this.autoScroll = true;

        // Resize textarea back
        setTimeout(() => {
            if (this.inputEl?.nativeElement) {
                this.inputEl.nativeElement.style.height = 'auto';
            }
        });
    }

    onInputChange(value: string): void {
        const trimmed = value.trimStart();
        if (trimmed.startsWith('/')) {
            const query = trimmed.split(/\s/)[0].toLowerCase();
            const filtered = SLASH_COMMANDS.filter(c => c.command.startsWith(query));
            this.filteredCommands.set(filtered);
            this.showSlashMenu.set(filtered.length > 0);
            this.slashSelectedIndex.set(0);
        } else {
            this.showSlashMenu.set(false);
        }
    }

    onMessagesScroll(): void {
        const el = this.messagesContainer?.nativeElement;
        if (!el) return;
        // If user is within 80px of the bottom, re-enable auto-scroll; otherwise pause it.
        this.autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    }

    selectSlashCommand(cmd: SlashCommand): void {
        this.inputText = cmd.command + ' ';
        this.showSlashMenu.set(false);
        this.inputEl?.nativeElement?.focus();
    }

    onKeydown(event: KeyboardEvent): void {
        // Slash menu navigation
        if (this.showSlashMenu()) {
            const cmds = this.filteredCommands();
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                this.slashSelectedIndex.update(i => Math.min(i + 1, cmds.length - 1));
                return;
            }
            if (event.key === 'ArrowUp') {
                event.preventDefault();
                this.slashSelectedIndex.update(i => Math.max(i - 1, 0));
                return;
            }
            if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
                event.preventDefault();
                const selected = cmds[this.slashSelectedIndex()];
                if (selected) {
                    this.selectSlashCommand(selected);
                }
                return;
            }
            if (event.key === 'Escape') {
                this.showSlashMenu.set(false);
                return;
            }
        }

        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            this.send();
        }
    }

    approveAndAutoAccept(): void {
        this.chat.approve();
        this.chat.setMode('auto_accept');
    }

    onModeChange(event: Event): void {
        const mode = (event.target as HTMLSelectElement).value as 'supervised' | 'auto_accept' | 'autonomous';
        this.chat.setMode(mode);
    }

    onModelChange(model: string): void {
        if (model) {
            this.chat.modelName.set(model);
            this.chat.updateConfig({llm: {model}});
        }
    }

    onTemperatureChange(temperature: number): void {
        this.chat.temperature.set(temperature);
        this.chat.updateConfig({llm: {temperature}});
    }

    hasModelInList(model: string): boolean {
        return this.modelService.models().some(g => g.models.includes(model));
    }

    openIde(url: string): void {
        window.open(url, '_blank');
    }

    openSessionFiles(): void {
        const folder = this.chat.ncSessionFolder();
        if (!folder || !environment.nextcloudUrl) return;
        const folderName = folder.split('/').pop();
        window.open(`${environment.nextcloudUrl}/apps/files/?dir=/${folderName}`, '_blank');
    }

    async resumeSession(): Promise<void> {
        this.isResuming.set(true);
        try {
            await this.chat.resumeSession();
        } catch (e: any) {
            this.chat.error.set(e?.error?.detail || 'Failed to resume session');
        } finally {
            this.isResuming.set(false);
        }
    }

    private startIdePolling(threadId: string): void {
        this.stopIdePolling();
        this.idePollingAttempts = 0;
        // Fetch immediately, then poll every 10s
        this.fetchIdeStatus(threadId);
        this.idePollingTimer = setInterval(() => this.fetchIdeStatus(threadId), 10_000);
    }

    private stopIdePolling(): void {
        if (this.idePollingTimer) {
            clearInterval(this.idePollingTimer);
            this.idePollingTimer = null;
        }
    }

    private fetchIdeStatus(threadId: string): void {
        this.idePollingAttempts++;
        this.api.getThreadIdeStatus(threadId).subscribe(status => {
            this.ideStatus.set(status);
            // Stop polling once active or after 30 attempts (5 min)
            if (status?.status === 'active' || this.idePollingAttempts >= 30) {
                this.stopIdePolling();
            }
        });
    }

    private scrollToBottom(): void {
        const el = this.messagesContainer?.nativeElement;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }

    /** Wrap tall <pre> blocks in a collapsed <details> element. */
    private collapseCodeBlocks(): void {
        const container = this.messagesContainer?.nativeElement;
        if (!container) return;
        const blocks = container.querySelectorAll<HTMLPreElement>(
            '.message-body pre:not([data-collapsed])',
        );
        for (const pre of Array.from(blocks)) {
            pre.setAttribute('data-collapsed', '');
            if (pre.scrollHeight <= 200) continue;
            const lang = pre.querySelector('code')?.className?.match(/language-(\S+)/)?.[1] || '';
            const wrapper = document.createElement('details');
            wrapper.className = 'code-collapse';
            const summary = document.createElement('summary');
            summary.className = 'code-collapse-summary';
            summary.innerHTML = `<span class="code-collapse-icon">code</span>`
                + `<span class="code-collapse-label">${lang || 'code'}</span>`
                + `<span class="code-collapse-hint">Click to expand</span>`;
            wrapper.appendChild(summary);
            pre.parentNode!.insertBefore(wrapper, pre);
            wrapper.appendChild(pre);
        }
    }

    /** Add a copy-to-clipboard button to every <pre> block that doesn't have one yet. */
    private addCopyButtons(): void {
        const container = this.messagesContainer?.nativeElement;
        if (!container) return;
        const blocks = container.querySelectorAll<HTMLPreElement>(
            '.message-body pre:not([data-copy-btn])',
        );
        for (const pre of Array.from(blocks)) {
            pre.setAttribute('data-copy-btn', '');
            const btn = document.createElement('button');
            btn.className = 'code-copy-btn';
            btn.innerHTML = '<span class="code-copy-icon">content_copy</span>';
            btn.title = 'Copy to clipboard';
            btn.addEventListener('click', () => {
                const code = pre.querySelector('code')?.textContent || pre.textContent || '';
                navigator.clipboard.writeText(code).then(() => {
                    btn.innerHTML = '<span class="code-copy-icon">check</span>';
                    setTimeout(() => {
                        btn.innerHTML = '<span class="code-copy-icon">content_copy</span>';
                    }, 1500);
                });
            });
            pre.style.position = 'relative';
            pre.appendChild(btn);
        }
    }

    // Tool call display helpers

    groupToolCalls(calls: ToolCallInfo[]): string {
        const counts = new Map<string, number>();
        for (const tc of calls) {
            counts.set(tc.tool, (counts.get(tc.tool) || 0) + 1);
        }
        return Array.from(counts.entries())
            .map(([name, count]) => count > 1 ? `${name} x${count}` : name)
            .join(', ');
    }

    toolSummaryStatus(calls: ToolCallInfo[]): string {
        if (calls.some(tc => tc.status === 'denied')) return 'denied';
        if (calls.some(tc => tc.status === 'running')) return 'running';
        if (calls.every(tc => tc.status === 'completed')) return 'completed';
        return 'mixed';
    }

    formatToolArgs(args: Record<string, unknown>): string {
        if (!args) return '';
        const entries = Object.entries(args);
        if (entries.length === 0) return '';
        for (const [, v] of entries) {
            if (typeof v === 'string' && v.length > 0) {
                return v.length > 60 ? v.substring(0, 60) + '...' : v;
            }
        }
        return JSON.stringify(args).substring(0, 60) + '...';
    }

    hasRunningTools(calls: ToolCallInfo[]): boolean {
        return calls.some(tc => tc.status === 'running' || tc.status === 'pending');
    }

    hasCompletedTools(calls: ToolCallInfo[]): boolean {
        return calls.some(tc => tc.status === 'completed');
    }

    completedOnly(calls: ToolCallInfo[]): ToolCallInfo[] {
        return calls.filter(tc => tc.status === 'completed');
    }

    completedToolCount(calls: ToolCallInfo[]): number {
        return calls.filter(tc => tc.status === 'completed').length;
    }

    currentToolLabel(calls: ToolCallInfo[]): string {
        const running = calls.filter(tc => tc.status === 'running' || tc.status === 'pending');
        if (running.length === 0) return '';
        if (running.length === 1) return running[0].tool;
        const unique = [...new Set(running.map(tc => tc.tool))];
        return unique.length === 1 ? `${unique[0]} (${running.length})` : unique.join(', ');
    }

    toolIcon(name: string): string {
        const t = name.toLowerCase();
        if (t.includes('read') || t.includes('write') || t.includes('edit') || t.includes('list_files')) return 'description';
        if (t.includes('git')) return 'history';
        if (t.includes('bash') || t.includes('shell') || t.includes('run_command')) return 'terminal';
        if (t.includes('web') || t.includes('search') || t.includes('fetch')) return 'public';
        if (t.includes('grep') || t.includes('search_files')) return 'search';
        return 'build';
    }

    previewResult(result: string): string {
        const line = result.trim().split('\n')[0];
        return line.length > 120 ? line.substring(0, 120) + '...' : line;
    }
}
