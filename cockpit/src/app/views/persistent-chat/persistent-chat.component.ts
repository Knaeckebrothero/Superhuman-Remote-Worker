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
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {PersistentChatService, ToolCallInfo,} from '../../core/services/persistent-chat.service';
import {ApiService, IdeSessionStatus} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import {I18nService} from '../../core/services/i18n.service';
import {environment} from '../../core/environment';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {AppButtonComponent} from '../../ui/button';
import {AppBadgeComponent} from '../../ui/badge';
import {AppSelectComponent} from '../../ui/select';
import {AppIconComponent} from '../../ui/icon';

interface SlashCommand {
    command: string;
    descriptionKey: string;
}

const SLASH_COMMANDS: SlashCommand[] = [
    {command: '/compact', descriptionKey: 'chat.slash.compact'},
    {command: '/done', descriptionKey: 'chat.slash.done'},
    {command: '/undo', descriptionKey: 'chat.slash.undo'},
    {command: '/auto', descriptionKey: 'chat.slash.auto'},
    {command: '/supervised', descriptionKey: 'chat.slash.supervised'},
    {command: '/autonomous', descriptionKey: 'chat.slash.autonomous'},
    {command: '/silent', descriptionKey: 'chat.slash.silent'},
    {command: '/verbose', descriptionKey: 'chat.slash.verbose'},
];

const TOOL_LABELS: Record<string, string> = {
    // Workspace — files
    read_file: 'Reading',
    write_file: 'Writing',
    edit_file: 'Editing',
    list_files: 'Exploring files',
    file_exists: 'Checking file',
    delete_file: 'Deleting file',
    search_files: 'Searching files',
    move_file: 'Moving file',
    rename_file: 'Renaming file',
    copy_file: 'Copying file',
    create_directory: 'Creating directory',
    delete_directory: 'Deleting directory',
    get_document_info: 'Inspecting document',

    // Shell
    run_command: 'Running command',
    shell_execute: 'Running command',
    shell_read: 'Reading output',

    // Git
    git_log: 'Viewing git history',
    git_show: 'Viewing commit',
    git_diff: 'Comparing changes',
    git_status: 'Checking git status',
    git_tags: 'Listing tags',
    git_merge_squash: 'Merging changes',
    git_worktree_cleanup: 'Cleaning worktree',

    // Research & web
    web_search: 'Searching the web',
    extract_webpage: 'Extracting page content',
    crawl_website: 'Crawling website',
    map_website: 'Mapping website',
    browse_website: 'Browsing website',
    download_from_website: 'Downloading from web',
    research_topic: 'Researching topic',
    search_papers: 'Searching papers',
    download_paper: 'Downloading paper',
    get_paper_info: 'Getting paper info',

    // Browser (direct)
    browser_navigate: 'Navigating to page',
    browser_snapshot: 'Capturing page',
    browser_click: 'Clicking element',
    browser_type: 'Typing in browser',
    browser_select: 'Selecting option',
    browser_scroll: 'Scrolling page',
    browser_screenshot: 'Taking screenshot',
    browser_back: 'Going back',
    browser_close: 'Closing browser',

    // Tasks & todos
    task_add: 'Adding task',
    task_complete: 'Completing task',
    task_list: 'Listing tasks',
    next_phase_todos: 'Planning next phase',
    todo_complete: 'Completing todo',
    todo_list: 'Listing todos',
    todo_rewind: 'Rewinding todo',

    // Knowledge base
    kb_write: 'Writing to knowledge base',
    kb_update: 'Updating knowledge',
    kb_read: 'Reading knowledge',
    kb_list: 'Listing knowledge',
    kb_search: 'Searching knowledge',
    kb_related: 'Finding related knowledge',

    // Citation
    cite_document: 'Citing document',
    cite_web: 'Citing web source',
    list_sources: 'Listing sources',
    search_library: 'Searching library',

    // Communication & delegation
    send_message: 'Sending message',
    delegate_work: 'Delegating work',

    // Job lifecycle
    mark_complete: 'Marking complete',
    job_complete: 'Completing job',
};

const CATEGORY_LABELS: Record<string, string> = {
    workspace: 'Working with files',
    git: 'Version control',
    shell: 'Running commands',
    research: 'Research & browsing',
    browser_direct: 'Browsing',
    core: 'Task management',
    session_task: 'Task management',
    knowledge: 'Knowledge base',
    citation: 'Citations & sources',
    sql: 'Database queries',
    mongodb: 'Database queries',
    graph: 'Knowledge graph',
    cloud: 'Cloud storage',
    communication: 'Communication',
    delegation: 'Delegating work',
    orchestrator: 'Orchestrator',
    evaluation: 'Evaluation',
};

@Component({
    selector: 'app-persistent-chat',
    standalone: true,
    imports: [
        FormsModule,
        JsonPipe,
        TitleCasePipe,
        RouterLink,
        MarkdownComponent,
        SidebarToggleComponent,
        TranslocoPipe,
        AppButtonComponent,
        AppBadgeComponent,
        AppSelectComponent,
        AppIconComponent,
    ],
    template: `
    <div class="chat-container">
      <!-- Header -->
      <div class="chat-header">
        <div class="header-left">
          <app-sidebar-toggle />
          <a class="back-link" routerLink="/sessions">
            <app-icon size="md" class="back-icon">arrow_back</app-icon>
          </a>
          <app-icon size="md" class="header-icon">smart_toy</app-icon>
          <span class="header-title">{{ chat.sessionTitle() || ('chat.defaultTitle' | transloco) }}</span>
          @if (chat.threadId(); as tid) {
            <span class="header-session-id" title="Session ID">{{ tid.slice(0, 8) }}</span>
          }
          <span class="status-dot" [class]="connectionClass()"></span>
          <span class="status-label">{{ connectionLabel() }}</span>
        </div>
        <div class="header-right">
          @if (chat.isConnected()) {
            <button class="settings-btn" (click)="showSettings.update(v => !v)"
                    [class.active]="showSettings()" [title]="'chat.header.settingsTooltip' | transloco">
              <app-icon size="sm" class="settings-icon">tune</app-icon>
            </button>
          }

          @if (chat.isConnected()) {
            @if (chat.cloudSessionUrl() || chat.ncSessionFolder()) {
              <button class="ide-btn" (click)="openSessionFiles()" [title]="'chat.header.filesTooltip' | transloco">
                <app-icon size="sm" class="ide-icon">cloud</app-icon>
                {{ 'chat.header.filesButton' | transloco }}
              </button>
            }
            @if (ideStatus(); as ide) {
              @if (ide.gitea_url) {
                <button class="ide-btn gitea-btn" (click)="openIde(ide.gitea_url!)" [title]="'chat.header.gitTooltip' | transloco">
                  <app-icon size="sm" class="ide-icon">history</app-icon>
                  {{ 'chat.header.gitButton' | transloco }}
                </button>
              }
              @if (ide.status === 'active' && ide.code_server_url) {
                <button class="ide-btn" (click)="openIde(ide.code_server_url!)" [title]="'chat.header.ideActiveTooltip' | transloco">
                  <app-icon size="sm" class="ide-icon">code</app-icon>
                  {{ 'chat.header.ideButton' | transloco }}
                </button>
              } @else if (ide.status === 'restoring') {
                <button class="ide-btn ide-loading" disabled [title]="'chat.header.ideLoadingTooltip' | transloco">
                  <span class="ide-spinner"></span>
                  {{ 'chat.header.ideButton' | transloco }}
                </button>
              }
            }
            <app-button variant="ghost" size="sm" (clicked)="chat.disconnect()">
              {{ 'chat.header.disconnect' | transloco }}
            </app-button>
          }
        </div>
      </div>

      <!-- Status bar -->
      @if (chat.isConnected()) {
        <div class="status-bar">
          @if (chat.modelName()) {
            <app-badge tone="accent" size="sm">{{ chat.modelName() }}</app-badge>
          }
          @if (chat.temperature()) {
            <app-badge tone="neutral" size="sm">{{ 'chat.status.temp' | transloco:{ value: chat.temperature() } }}</app-badge>
          }
          <app-badge tone="neutral" size="sm">{{ 'chat.status.turn' | transloco:{ count: chat.turnCount() } }}</app-badge>
          <app-badge tone="accent" size="sm">{{ chat.permissionMode() | titlecase }}</app-badge>
        </div>
      }

      <!-- Settings panel -->
      @if (showSettings()) {
        <div class="settings-panel">
          <div class="settings-row">
            <label class="settings-label">{{ 'chat.settings.mode' | transloco }}</label>
            <app-select size="sm" [fullWidth]="false"
                        [value]="chat.permissionMode()"
                        (changed)="onPermissionModeChange($event)">
              <option value="supervised">{{ 'chat.settings.modeSupervised' | transloco }}</option>
              <option value="auto_accept">{{ 'chat.settings.modeAutoAccept' | transloco }}</option>
              <option value="autonomous">{{ 'chat.settings.modeAutonomous' | transloco }}</option>
            </app-select>
          </div>
          <div class="settings-row">
            <label class="settings-label">{{ 'chat.settings.narration' | transloco }}</label>
            <app-select size="sm" [fullWidth]="false"
                        [value]="chat.narrationMode()"
                        (changed)="onNarrationModeSelect($event)">
              <option value="auto">{{ 'chat.settings.narrationAuto' | transloco }}</option>
              <option value="verbose">{{ 'chat.settings.narrationVerbose' | transloco }}</option>
              <option value="silent">{{ 'chat.settings.narrationSilent' | transloco }}</option>
            </app-select>
          </div>
          <div class="settings-row">
            <label class="settings-label">{{ 'chat.settings.model' | transloco }}</label>
            <app-select size="sm" [fullWidth]="false"
                        [value]="chat.modelName()"
                        (changed)="onModelSelect($event)">
              @if (chat.modelName() && !hasModelInList(chat.modelName()!)) {
                <option [value]="chat.modelName()">{{ chat.modelName() }}</option>
              }
              @for (group of modelService.models(); track group.group) {
                <optgroup [label]="group.group">
                  @for (model of group.models; track model) {
                    <option [value]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </app-select>
          </div>
          <div class="settings-row">
            <label class="settings-label">{{ 'chat.settings.temperature' | transloco:{ value: chat.temperature() } }}</label>
            <input type="range" class="settings-slider" min="0" max="2" step="0.1"
                   [ngModel]="chat.temperature()"
                   (ngModelChange)="onTemperatureChange($event)">
          </div>
        </div>
      }

      <!-- Task bar -->
      @if (chat.tasks().length) {
        <div class="task-bar">
          <div class="task-header"
               [class.task-header-clickable]="chat.tasks().length > 1"
               (click)="chat.tasks().length > 1 ? toggleTasksCollapsed() : null">
            <app-icon size="sm" class="task-header-icon">checklist</app-icon>
            {{ 'chat.tasks.header' | transloco:{ done: completedTaskCount(), total: chat.tasks().length } }}
            @if (chat.tasks().length > 1) {
              <app-icon size="sm" class="task-chevron" [class.task-chevron-open]="!tasksCollapsed()">expand_more</app-icon>
            }
          </div>
          <div class="task-list">
            @if (chat.tasks().length <= 1 || !tasksCollapsed()) {
              @for (task of chat.tasks(); track task.id) {
                <div class="task-item" [class.task-completed]="task.status === 'completed'">
                  <app-icon size="sm" class="task-check">{{ task.status === 'completed' ? 'check_circle' : 'radio_button_unchecked' }}</app-icon>
                  <span class="task-desc">{{ task.description }}</span>
                </div>
              }
            } @else {
              @if (nextPendingTask(); as task) {
                <div class="task-item">
                  <app-icon size="sm" class="task-check">radio_button_unchecked</app-icon>
                  <span class="task-desc">{{ task.description }}</span>
                </div>
              } @else {
                <div class="task-item task-completed">
                  <app-icon size="sm" class="task-check" style="color: var(--success)">check_circle</app-icon>
                  <span class="task-desc">{{ 'chat.tasks.allCompleted' | transloco }}</span>
                </div>
              }
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
                <app-icon size="sm" class="system-icon">info</app-icon>
                {{ msg.content }}
              </div>
              @if (chat.isSessionPaused() && $last) {
                <div class="resume-btn-wrapper">
                  <app-button variant="primary" size="sm"
                              [loading]="isResuming()"
                              (clicked)="resumeSession()">
                    @if (isResuming()) {
                      {{ 'chat.system.resuming' | transloco }}
                    } @else {
                      <app-icon size="sm" class="resume-icon">play_arrow</app-icon>
                      {{ 'chat.system.resumeSession' | transloco }}
                    }
                  </app-button>
                </div>
              }
            } @else if (msg.role === 'assistant' && !msg.content && msg.toolCalls?.length) {
              <!-- Tool-only message: compact inline indicator -->
              <div class="tool-only-row">
                <app-icon size="sm" class="tool-only-icon">{{ toolIcon(msg.toolCalls![0].tool) }}</app-icon>
                <span class="tool-only-label">
                  {{ toolSummaryLabel(msg.toolCalls!) }}
                </span>
                <span class="tool-summary-dot" [class]="toolSummaryStatus(msg.toolCalls!)"></span>
              </div>
            } @else {
              <div class="avatar">
                <app-icon size="sm" class="avatar-icon">{{ msg.role === 'user' ? 'person' : 'smart_toy' }}</app-icon>
              </div>
              <div class="message-body">
                @if (msg.role === 'user') {
                  <div class="user-text">{{ msg.content }}</div>
                } @else {
                  @if (msg.thinking && chat.narrationMode() !== 'silent') {
                    <details class="thinking-block" [attr.open]="chat.narrationMode() === 'verbose' ? '' : null">
                      <summary class="thinking-header">
                        <app-icon size="sm" class="thinking-icon">psychology</app-icon>
                        <span class="thinking-label">{{ 'chat.thinking.past' | transloco }}</span>
                      </summary>
                      <div class="thinking-content">{{ msg.thinking }}</div>
                    </details>
                  }
                  @if (msg.content) {
                    <markdown [data]="msg.content"></markdown>
                  }
                  @if (msg.toolCalls?.length) {
                    <details class="tool-summary" [attr.open]="hasDeniedTools(msg.toolCalls!) || chat.narrationMode() === 'verbose' ? '' : null">
                      <summary class="tool-summary-line">
                        <app-icon size="sm" class="tool-summary-chevron">chevron_right</app-icon>
                        <span class="tool-summary-text">
                          {{ (msg.toolCalls!.length === 1 ? 'chat.tools.usedOne' : 'chat.tools.usedMany') | transloco:{ count: msg.toolCalls!.length } }}
                          {{ toolSummaryLabel(msg.toolCalls!) }}
                        </span>
                        <span class="tool-summary-dot" [class]="toolSummaryStatus(msg.toolCalls!)"></span>
                      </summary>
                      <div class="tool-detail-list">
                        @for (tc of msg.toolCalls; track tc.id) {
                          <details class="tool-detail-item" [attr.open]="tc.status === 'denied' ? '' : null">
                            <summary class="tool-detail-header">
                              <app-icon size="sm" class="tool-icon">{{ toolIcon(tc.tool) }}</app-icon>
                              <span class="tool-detail-name">{{ tc.tool }}</span>
                              <span class="tool-detail-args">{{ formatToolArgs(tc.args) }}</span>
                              <span class="tool-detail-status" [class]="'status-' + tc.status">{{ translateStatus(tc.status) }}</span>
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
              <span class="divider-text">{{ 'chat.system.sessionResumed' | transloco }}</span>
              <span class="divider-line"></span>
            </div>
          }
        } @empty {
          @if (!chat.isStreaming()) {
            <div class="empty-state">
              @if (chat.sessionReady()) {
                <span class="empty-state-text">{{ 'chat.system.emptyPrompt' | transloco }}</span>
              } @else {
                <div class="startup-spinner-container">
                  <div class="startup-spinner"></div>
                  <span class="startup-label">
                    @if (chat.isCreating()) {
                      {{ 'chat.startup.creating' | transloco }}
                    } @else if (chat.isConnected()) {
                      @switch (chat.startupPhase()) {
                        @case ('provisioning') { {{ 'chat.startup.provisioning' | transloco }} }
                        @case ('booting') { {{ 'chat.startup.booting' | transloco }} }
                        @case ('connecting') { {{ 'chat.startup.connecting' | transloco }} }
                        @default { {{ 'chat.startup.starting' | transloco }} }
                      }
                    } @else {
                      {{ 'chat.startup.connectingFallback' | transloco }}
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
                {{ 'chat.startup.creating' | transloco }}
              } @else if (chat.isConnected()) {
                @switch (chat.startupPhase()) {
                  @case ('provisioning') { {{ 'chat.startup.provisioning' | transloco }} }
                  @case ('booting') { {{ 'chat.startup.booting' | transloco }} }
                  @case ('connecting') { {{ 'chat.startup.connecting' | transloco }} }
                  @default { {{ 'chat.startup.reconnecting' | transloco }} }
                }
              } @else {
                {{ 'chat.startup.connectingFallback' | transloco }}
              }
            </span>
          </div>
        }

        <!-- Streaming response -->
        @if (chat.isStreaming()) {
          <div class="message message-assistant">
            <div class="avatar">
              <app-icon size="sm" class="avatar-icon">smart_toy</app-icon>
            </div>
            <div class="message-body">
              @if (chat.streamingThinking() && chat.narrationMode() !== 'silent') {
                <details class="thinking-block" open>
                  <summary class="thinking-header">
                    <app-icon size="sm" class="thinking-icon">psychology</app-icon>
                    <span class="thinking-label">{{ 'chat.thinking.now' | transloco }}</span>
                  </summary>
                  <div class="thinking-content">{{ chat.streamingThinking() }}</div>
                </details>
              }
              @if (chat.streamingText()) {
                <markdown [data]="chat.streamingText()"></markdown>
              }
              @if (chat.currentToolCalls().length) {
                @if (hasRunningTools(chat.currentToolCalls())) {
                  <div class="tool-progress">
                    <span class="tool-progress-spinner"></span>
                    <span class="tool-progress-text">{{ currentToolLabelHuman(chat.currentToolCalls()) }}...</span>
                  </div>
                }
                @if (hasCompletedTools(chat.currentToolCalls())) {
                  <details class="tool-summary" open>
                    <summary class="tool-summary-line">
                      <app-icon size="sm" class="tool-summary-chevron">chevron_right</app-icon>
                      <span class="tool-summary-text">
                        {{ (completedToolCount(chat.currentToolCalls()) === 1 ? 'chat.tools.usedOne' : 'chat.tools.usedMany') | transloco:{ count: completedToolCount(chat.currentToolCalls()) } }}
                        {{ toolSummaryLabel(completedOnly(chat.currentToolCalls())) }}
                      </span>
                      <span class="tool-summary-dot completed"></span>
                    </summary>
                    <div class="tool-detail-list">
                      @for (tc of completedOnly(chat.currentToolCalls()); track tc.id) {
                        <details class="tool-detail-item">
                          <summary class="tool-detail-header">
                            <span class="tool-detail-name">{{ tc.tool }}</span>
                            <span class="tool-detail-args">{{ formatToolArgs(tc.args) }}</span>
                            <span class="tool-detail-status status-completed">{{ 'chat.tools.statusCompleted' | transloco }}</span>
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
              <app-icon size="sm" class="perm-icon">shield</app-icon>
              {{ 'chat.permission.title' | transloco }}
            </div>
            <div class="perm-body">
              <strong>{{ perm.tool }}</strong>
              <pre class="perm-args">{{ perm.args | json }}</pre>
            </div>
            <div class="perm-actions">
              <app-button variant="success" size="sm" (clicked)="chat.approve()">{{ 'chat.permission.approve' | transloco }}</app-button>
              <app-button variant="info" size="sm" (clicked)="approveAndAutoAccept()">{{ 'chat.permission.autoAccept' | transloco }}</app-button>
              <app-button variant="danger" size="sm" (clicked)="chat.deny()">{{ 'chat.permission.deny' | transloco }}</app-button>
            </div>
          </div>
        }
      </div>

      <!-- Error banner -->
      @if (chat.error(); as err) {
        <div class="error-banner">
          <app-icon size="sm" class="error-icon">error</app-icon>
          {{ err }}
          <button class="error-dismiss" (click)="chat.error.set(null)">{{ 'chat.error.dismiss' | transloco }}</button>
        </div>
      }

      <!-- Input -->
      <div class="input-area">
        <div class="input-card" [class.focused]="inputFocused()">
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
                  <span class="slash-desc">{{ cmd.descriptionKey | transloco }}</span>
                </div>
              }
            </div>
          }
          <textarea
            #inputEl
            class="chat-input"
            [(ngModel)]="inputText"
            (ngModelChange)="onInputChange($event)"
            (input)="autoResizeInput()"
            (keydown)="onKeydown($event)"
            (focus)="inputFocused.set(true)"
            (blur)="inputFocused.set(false)"
            [placeholder]="inputPlaceholder()"
            [disabled]="!chat.isConnected()"
            rows="1"
          ></textarea>
          <button
            class="action-btn"
            [class.stop]="chat.isStreaming() && !chat.isInterrupting()"
            [class.interrupting]="chat.isInterrupting()"
            [class.pending]="isPendingSend()"
            (click)="chat.isStreaming() ? chat.interrupt() : send()"
            [disabled]="chat.isInterrupting() || (!chat.isStreaming() && !canSend())"
          >
            @if (isPendingSend() || chat.isInterrupting()) {
              <span class="action-spinner"></span>
            } @else if (chat.isStreaming()) {
              <app-icon size="sm" class="action-icon">stop</app-icon>
            } @else {
              <app-icon size="sm" class="action-icon">arrow_upward</app-icon>
            }
          </button>
        </div>
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
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        background: var(--panel-bg, var(--panel-bg));
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
        color: var(--text-muted, var(--text-muted));
        text-decoration: none;
        margin-right: 4px;
      }

      .back-link:hover { color: var(--text-primary, var(--text-primary)); }

      .header-icon, .perm-icon, .error-icon {
        color: var(--accent-color, var(--accent-color));
      }

      .header-title {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary, var(--text-primary));
      }

      .header-session-id {
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        color: var(--text-muted, var(--text-muted));
      }

      .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
      }

      .status-dot.connected { background: var(--success); }
      .status-dot.connecting { background: var(--warning); animation: pulse 1s infinite; }
      .status-dot.disconnected { background: var(--surface-2, #585b70); }
      .status-dot.error { background: var(--danger); }

      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
      }

      .status-label {
        font-size: 11px;
        color: var(--text-muted, var(--text-muted));
      }

      /* Status bar */

      .status-bar {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 4px 16px;
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        background: var(--panel-bg, var(--panel-bg));
        flex-shrink: 0;
      }

      .status-bar > app-badge {
        flex-shrink: 0;
      }

      /* Task bar */
      .task-bar {
        padding: 6px 16px;
        border-bottom: 1px solid var(--surface-0);
        background: var(--panel-bg);
        flex-shrink: 0;
      }
      .task-header {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        color: var(--text-muted);
        margin-bottom: 4px;
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
        color: var(--text-primary);
      }
      .task-item .task-check {
        color: var(--text-muted);
      }
      .task-completed {
        color: var(--text-muted);
        text-decoration: line-through;
      }
      .task-completed .task-check {
        color: var(--success);
      }
      .task-header-clickable {
        cursor: pointer;
        user-select: none;
        border-radius: 4px;
        transition: background 0.15s;
      }
      .task-header-clickable:hover {
        background: rgba(255, 255, 255, 0.04);
      }
      .task-chevron {
        margin-left: auto;
        transition: transform 0.2s ease;
      }
      .task-chevron-open {
        transform: rotate(180deg);
      }

      .header-right {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .settings-btn {
        display: flex;
        align-items: center;
        padding: 4px 8px;
        border-radius: 4px;
        border: 1px solid var(--border-color, var(--surface-0));
        background: transparent;
        color: var(--text-muted, var(--text-muted));
        cursor: pointer;
        transition: all 0.15s ease;
      }
      .settings-btn:hover, .settings-btn.active {
        color: var(--accent-color, var(--accent-color));
        border-color: var(--accent-color, var(--accent-color));
      }

      .settings-panel {
        padding: 10px 16px;
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        background: var(--panel-bg, var(--panel-bg));
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
        color: var(--text-muted, var(--text-muted));
        white-space: nowrap;
      }
      .settings-slider {
        width: 100px;
        accent-color: var(--accent-color, var(--accent-color));
      }

      .ide-btn {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 12px;
        border-radius: 4px;
        border: 1px solid var(--info);
        background: transparent;
        color: var(--info);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .ide-btn:hover {
        background: var(--info-tint);
      }

      .ide-btn:disabled {
        opacity: 0.6;
        cursor: default;
      }

      .gitea-btn {
        border-color: var(--success);
        color: var(--success);
      }

      .gitea-btn:hover {
        background: var(--success-tint);
      }


      .ide-spinner {
        width: 12px;
        height: 12px;
        border: 2px solid var(--info-tint);
        border-top-color: var(--info);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
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
        color: var(--text-muted, var(--text-muted));
        font-size: 14px;
        scrollbar-color: var(--border-color, var(--surface-0)) transparent;
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
        border: 3px solid var(--border-color, var(--surface-0));
        border-top-color: var(--accent-color, var(--accent-color));
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .startup-label {
        color: var(--text-muted, var(--text-muted));
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
        background: var(--surface-0, var(--surface-0));
      }

      .message-user .avatar {
        background: var(--accent-color, var(--accent-color));
      }

      .avatar-icon {
        color: var(--text-secondary, var(--text-secondary));
      }

      .message-user .avatar-icon {
        color: var(--timeline-bg, var(--timeline-bg));
      }

      .message-body {
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
        line-height: 1.5;
        min-width: 0;
      }

      .message-user .message-body {
        background: var(--accent-color, var(--accent-color));
        color: var(--timeline-bg, var(--timeline-bg));
        border-bottom-right-radius: 4px;
      }

      .message-assistant .message-body {
        background: var(--surface-0, var(--surface-0));
        color: var(--text-primary, var(--text-primary));
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
        color: var(--text-muted, var(--text-muted));
        padding: 4px 12px;
        background: var(--surface-0, var(--surface-0));
        border-radius: 6px;
        width: fit-content;
        margin: 0 auto;
      }


      .resume-btn-wrapper {
        display: flex;
        justify-content: center;
        margin-top: 8px;
      }

      .resume-icon {
        margin-right: 4px;
        vertical-align: middle;
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
        color: var(--text-muted, var(--text-muted));
      }

      .tool-only-icon {
        color: var(--text-muted, var(--text-muted));
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
        color: var(--text-muted, var(--text-muted));
        transition: background 0.15s;
        list-style: none;
      }

      .tool-summary-line::-webkit-details-marker { display: none; }
      .tool-summary-line:hover { background: rgba(255, 255, 255, 0.04); }

      .tool-summary-chevron {
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

      .tool-summary-dot.completed { background: var(--success); }
      .tool-summary-dot.running { background: var(--warning); animation: pulse 1s infinite; }
      .tool-summary-dot.denied { background: var(--danger); }
      .tool-summary-dot.mixed { background: var(--warning); }

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
        color: var(--text-muted, var(--text-muted));
        list-style: none;
      }

      .tool-detail-header::-webkit-details-marker { display: none; }
      .tool-detail-header:hover { background: rgba(255, 255, 255, 0.03); }

      .tool-detail-name {
        font-family: 'JetBrains Mono', monospace;
        color: var(--accent-color, var(--accent-color));
        font-weight: 500;
      }

      .tool-detail-args {
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        color: var(--text-muted, var(--text-muted));
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

      .tool-detail-status.status-completed { color: var(--success); }
      .tool-detail-status.status-running { color: var(--warning); }
      .tool-detail-status.status-denied { color: var(--danger); }
      .tool-detail-status.status-pending { color: var(--text-muted, var(--text-muted)); }

      .tool-icon {
        color: var(--text-muted, var(--text-muted));
        width: 16px;
        text-align: center;
        flex-shrink: 0;
      }

      .tool-preview {
        padding: 2px 8px 2px 28px;
        font-size: 10px;
        color: var(--text-muted, var(--text-muted));
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
        background: var(--panel-bg, var(--panel-bg));
        border-radius: 6px;
        font-size: 11px;
        max-height: 200px;
        overflow: auto;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: 'JetBrains Mono', monospace;
        line-height: 1.4;
        color: var(--text-secondary, var(--text-secondary));
      }

      /* Inline tool progress (streaming) */

      .tool-progress {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 4px 8px;
        font-size: 12px;
        color: var(--text-muted, var(--text-muted));
        margin-top: 6px;
      }

      .tool-progress-spinner {
        width: 12px;
        height: 12px;
        border: 2px solid var(--border-color, var(--surface-0));
        border-top-color: var(--warning);
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
        background: var(--border-color, var(--surface-0));
      }

      .divider-text {
        font-size: 10px;
        color: var(--text-muted, var(--text-muted));
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* Thinking/reasoning block */

      .thinking-block {
        margin: 4px 0 8px;
        border-left: 2px solid var(--border, var(--surface-1));
        border-radius: 4px;
      }

      .thinking-block > .thinking-header {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        cursor: pointer;
        font-size: 12px;
        color: var(--text-muted, var(--text-muted));
        list-style: none;
        user-select: none;
      }

      .thinking-block > .thinking-header::-webkit-details-marker {
        display: none;
      }

      .thinking-block > .thinking-header:hover {
        color: var(--text-secondary, var(--text-secondary));
      }


      .thinking-label {
        font-style: italic;
      }

      .thinking-content {
        padding: 4px 10px 10px;
        font-size: 12px;
        line-height: 1.5;
        color: var(--text-muted, var(--text-muted));
        white-space: pre-wrap;
        max-height: 300px;
        overflow-y: auto;
      }

      .thinking-block[open] > .thinking-header {
        padding-bottom: 2px;
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
        background: var(--text-muted, var(--text-muted));
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
        border: 1px solid var(--warning);
        border-radius: 8px;
        padding: 12px;
        background: var(--warning-tint);
      }

      .perm-header {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        font-weight: 600;
        color: var(--warning);
        margin-bottom: 8px;
      }

      .perm-header .perm-icon { color: var(--warning); }

      .perm-body {
        font-size: 12px;
        color: var(--text-secondary, var(--text-secondary));
      }

      .perm-args {
        margin: 4px 0 8px;
        padding: 6px 8px;
        background: var(--surface-0, var(--surface-0));
        border-radius: 4px;
        font-size: 11px;
        max-height: 120px;
        overflow: auto;
      }

      .perm-actions {
        display: flex;
        gap: 8px;
      }

      /* Error banner */

      .error-banner {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 16px;
        background: var(--danger-tint);
        border-top: 1px solid var(--danger);
        font-size: 12px;
        color: var(--danger);
      }

      .error-dismiss {
        margin-left: auto;
        background: transparent;
        border: none;
        color: var(--danger);
        font-size: 11px;
        cursor: pointer;
        font-family: inherit;
        text-decoration: underline;
      }

      /* Input area */

      .input-area {
        padding: 12px 16px 16px;
        background: var(--panel-bg, var(--panel-bg));
        flex-shrink: 0;
      }

      .input-card {
        position: relative;
        display: flex;
        align-items: flex-end;
        gap: 8px;
        padding: 8px 8px 8px 16px;
        border-radius: 20px;
        border: 1px solid var(--border-color, var(--surface-0));
        background: var(--surface-0, var(--surface-0));
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
      }

      .input-card.focused {
        border-color: var(--accent-color, var(--accent-color));
        box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent-color) 20%, transparent);
      }

      .chat-input {
        flex: 1;
        padding: 6px 0;
        border: none;
        background: transparent;
        color: var(--text-primary, var(--text-primary));
        font-size: 14px;
        font-family: inherit;
        resize: none;
        min-height: 24px;
        max-height: 180px;
        line-height: 1.5;
        overflow-y: auto;
      }

      .chat-input:focus,
      .chat-input:focus-visible {
        outline: none;
        border: none;
        box-shadow: none;
      }

      .chat-input:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .chat-input::placeholder {
        color: var(--text-muted, var(--text-muted));
      }

      /* Action button — send / stop / spinner in one spot */

      .action-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
        border-radius: 50%;
        border: none;
        background: var(--accent-color, var(--accent-color));
        color: var(--timeline-bg, var(--timeline-bg));
        cursor: pointer;
        flex-shrink: 0;
        transition: background 0.15s ease, opacity 0.15s ease;
      }

      .action-btn:disabled {
        opacity: 0.3;
        cursor: not-allowed;
      }

      .action-btn.stop {
        background: var(--danger);
        opacity: 1;
      }

      .action-btn.pending,
      .action-btn.interrupting {
        opacity: 0.7;
        cursor: wait;
      }

      .action-btn.interrupting {
        background: var(--danger);
      }


      .action-spinner {
        width: 16px;
        height: 16px;
        border: 2px solid var(--timeline-bg, var(--timeline-bg));
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* Slash command autocomplete */

      .slash-menu {
        position: absolute;
        bottom: 100%;
        left: 0;
        right: 0;
        margin-bottom: 8px;
        background: var(--panel-bg, var(--panel-bg));
        border: 1px solid var(--border-color, var(--surface-0));
        border-radius: 12px;
        padding: 4px;
        box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.4);
        z-index: 10;
      }

      .slash-item {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 12px;
        border-radius: 8px;
        cursor: pointer;
        transition: background 0.1s ease;
      }

      .slash-item:hover, .slash-item.selected {
        background: var(--surface-0, var(--surface-0));
      }

      .slash-cmd {
        font-weight: 600;
        font-size: 13px;
        color: var(--accent-color, var(--accent-color));
        min-width: 100px;
      }

      .slash-desc {
        font-size: 12px;
        color: var(--text-muted, var(--text-muted));
      }

      /* Markdown content styling */

      .message-body ::ng-deep pre {
        background: var(--panel-bg, var(--panel-bg));
        border-radius: 8px;
        padding: 12px 16px;
        overflow-x: auto;
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        line-height: 1.5;
        border: 1px solid var(--border-color, var(--surface-0));
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
        background: var(--surface-0, var(--surface-0));
        border: 1px solid var(--border-color, var(--surface-0));
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
        color: var(--text-muted, var(--text-muted));
      }

      .message-body ::ng-deep code {
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
        padding: 1px 5px;
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.9em;
      }

      /* Collapsible code blocks */
      .message-body ::ng-deep .code-collapse {
        border: 1px solid var(--border-color, var(--surface-0));
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
        background: var(--panel-bg, var(--panel-bg));
        cursor: pointer;
        font-size: 12px;
        color: var(--text-muted, var(--text-muted));
        user-select: none;
        list-style: none;
      }

      .message-body ::ng-deep .code-collapse-summary::-webkit-details-marker {
        display: none;
      }

      .message-body ::ng-deep .code-collapse-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        color: var(--accent-color, var(--accent-color));
      }

      .message-body ::ng-deep .code-collapse-label {
        font-family: 'JetBrains Mono', monospace;
        font-weight: 600;
        color: var(--text-color, var(--text-primary));
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
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        text-align: left;
      }

      .message-body ::ng-deep th {
        font-weight: 600;
        color: var(--accent-color, var(--accent-color));
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      .message-body ::ng-deep tr:hover {
        background: rgba(255, 255, 255, 0.03);
      }

      .message-body ::ng-deep a {
        color: var(--info);
        text-decoration: none;
      }

      .message-body ::ng-deep a:hover {
        text-decoration: underline;
      }

      .message-body ::ng-deep blockquote {
        border-left: 3px solid var(--accent-color, var(--accent-color));
        margin: 8px 0;
        padding: 4px 12px;
        color: var(--text-secondary, var(--text-secondary));
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
        color: var(--accent-color, var(--accent-color));
        text-decoration: underline dotted;
        text-underline-offset: 2px;
      }

      .message-body ::ng-deep .citation-web:hover {
        text-decoration-style: solid;
      }

      .message-body ::ng-deep .citation-doc {
        color: var(--info);
        font-style: italic;
        cursor: help;
        border-bottom: 1px dashed var(--info);
      }

      @media (max-width: 768px) {
        .chat-header {
          flex-wrap: wrap;
          gap: 4px;
          padding: 6px 8px;
        }

        .messages {
          padding: 10px;
        }

        .message {
          max-width: 98%;
        }

        .permission-request {
          width: 95%;
        }

        .chat-input {
          font-size: 16px;
        }

        .input-area {
          padding: 8px;
          padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px));
        }
      }
    `,
    ],
})
export class PersistentChatComponent implements AfterViewChecked, OnDestroy {
    readonly chat = inject(PersistentChatService);
    private readonly api = inject(ApiService);
    readonly modelService = inject(ModelService);
    private readonly transloco = inject(TranslocoService);
    private readonly i18n = inject(I18nService);

    @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;
    @ViewChild('inputEl') inputEl!: ElementRef<HTMLTextAreaElement>;

    inputText = '';

    // Settings panel
    readonly showSettings = signal(false);

    // Resume state
    readonly isResuming = signal(false);

    // Input state
    readonly inputFocused = signal(false);

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

    readonly tasksCollapsed = signal(true);

    readonly nextPendingTask = computed(() => {
        const tasks = this.chat.tasks();
        return tasks.filter(t => t.status === 'pending' || t.status === 'in_progress').pop() ?? null;
    });

    toggleTasksCollapsed(): void {
        this.tasksCollapsed.update(v => !v);
    }

    readonly connectionClass = computed(() => this.chat.connectionState());
    readonly connectionLabel = computed(() => {
        // Track language changes so the label re-translates when i18n switches.
        this.i18n.activeLang();
        const state = this.chat.connectionState();
        const key =
            state === 'connected' ? 'chat.connection.connected'
                : state === 'connecting' ? 'chat.connection.connecting'
                    : state === 'error' ? 'chat.connection.error'
                        : 'chat.connection.disconnected';
        return this.transloco.translate(key);
    });

    readonly inputPlaceholder = computed(() => {
        // Track language changes so placeholder re-translates when i18n switches.
        this.i18n.activeLang();
        if (!this.chat.isConnected()) return this.transloco.translate('chat.input.connect');
        if (this.chat.isConnected() && !this.chat.sessionReady()) return this.transloco.translate('chat.input.sessionStarting');
        if (this.chat.isInterrupting()) return this.transloco.translate('chat.input.stopping');
        if (this.chat.isStreaming()) return this.transloco.translate('chat.input.working');
        return this.transloco.translate('chat.input.default');
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

    autoResizeInput(): void {
        const el = this.inputEl?.nativeElement;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = el.scrollHeight + 'px';
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

    onPermissionModeChange(value: string | null): void {
        if (value === 'supervised' || value === 'auto_accept' || value === 'autonomous') {
            this.chat.setMode(value);
        }
    }

    onNarrationModeSelect(value: string | null): void {
        if (value === 'silent' || value === 'verbose' || value === 'auto') {
            this.chat.setNarrationMode(value);
        }
    }

    onModelSelect(model: string | null): void {
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
        // Prefer the backend-computed URL (works for all backends).
        const cloudUrl = this.chat.cloudSessionUrl();
        if (cloudUrl) {
            window.open(cloudUrl, '_blank');
            return;
        }
        // Legacy fallback for Nextcloud sessions without a computed URL.
        const folder = this.chat.ncSessionFolder();
        if (!folder || !environment.cloudUrl) return;
        const folderName = folder.split('/').pop();
        window.open(`${environment.cloudUrl}/apps/files/?dir=/${folderName}`, '_blank');
    }

    async resumeSession(): Promise<void> {
        this.isResuming.set(true);
        try {
            await this.chat.resumeSession();
        } catch (e: any) {
            this.chat.error.set(e?.error?.detail || this.transloco.translate('chat.system.resumeFailed'));
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
            const expandHint = this.transloco.translate('chat.code.expandHint');
            summary.innerHTML = `<span class="code-collapse-icon">code</span>`
                + `<span class="code-collapse-label">${lang || 'code'}</span>`
                + `<span class="code-collapse-hint">${expandHint}</span>`;
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
            btn.title = this.transloco.translate('chat.code.copyTooltip');
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

    toolLabel(tc: ToolCallInfo): string {
        // Reference activeLang so Angular re-renders when the language changes.
        this.i18n.activeLang();
        const translatedKey = `chat.toolLabels.${tc.tool}`;
        const translated = this.transloco.translate(translatedKey);
        const base = translated !== translatedKey ? translated : TOOL_LABELS[tc.tool];
        if (base) {
            const context = this.toolLabelContext(tc.tool, tc.args);
            return context ? `${base} ${context}` : base;
        }
        return this.fallbackToolLabel(tc.tool);
    }

    private toolLabelContext(tool: string, args: Record<string, unknown>): string {
        if (!args) return '';
        const t = tool.toLowerCase();

        // File operations: show the filename
        if (t.includes('read') || t.includes('write') || t.includes('edit') || t === 'list_files' || t === 'file_exists') {
            const filePath = (args['file_path'] || args['path'] || args['file'] || args['pattern'] || '') as string;
            if (filePath) {
                const name = filePath.split('/').pop() || filePath;
                return name.length > 40 ? name.substring(0, 40) + '...' : name;
            }
        }

        // Grep/search: show the pattern
        if (t.includes('grep') || t.includes('search')) {
            const pattern = (args['pattern'] || args['query'] || '') as string;
            if (pattern) {
                const display = pattern.length > 30 ? pattern.substring(0, 30) + '...' : pattern;
                return this.transloco.translate('chat.toolContext.searchFor', {term: display});
            }
        }

        // Shell: show truncated command
        if (t === 'run_command' || t === 'shell_execute' || t === 'bash') {
            const cmd = (args['command'] || args['description'] || '') as string;
            if (cmd) {
                const display = cmd.length > 40 ? cmd.substring(0, 40) + '...' : cmd;
                return this.transloco.translate('chat.toolContext.commandDash', {cmd: display});
            }
        }

        return '';
    }

    private fallbackToolLabel(name: string): string {
        return name
            .replace(/_/g, ' ')
            .replace(/([a-z])([A-Z])/g, '$1 $2')
            .replace(/^./, c => c.toUpperCase());
    }

    groupToolCallsHuman(calls: ToolCallInfo[]): string {
        const groups = new Map<string, { count: number; label: string }>();
        for (const tc of calls) {
            const existing = groups.get(tc.tool);
            if (existing) {
                existing.count++;
            } else {
                groups.set(tc.tool, {count: 1, label: this.toolLabel(tc)});
            }
        }
        return Array.from(groups.values())
            .map(({count, label}) => count > 1 ? `${label} x${count}` : label)
            .join(', ');
    }

    currentToolLabelHuman(calls: ToolCallInfo[]): string {
        const running = calls.filter(tc => tc.status === 'running' || tc.status === 'pending');
        if (running.length === 0) return '';
        if (running.length === 1) return this.toolLabel(running[0]);
        const unique = new Map<string, ToolCallInfo>();
        for (const tc of running) {
            if (!unique.has(tc.tool)) unique.set(tc.tool, tc);
        }
        if (unique.size === 1) {
            const first = unique.values().next().value!;
            return `${this.toolLabel(first)} (${running.length})`;
        }
        return Array.from(unique.values()).map(tc => this.toolLabel(tc)).join(', ');
    }

    hasDeniedTools(calls: ToolCallInfo[]): boolean {
        return calls.some(tc => tc.status === 'denied');
    }

    groupToolCallsByIntent(calls: ToolCallInfo[]): string {
        this.i18n.activeLang();
        const groups = new Map<string, number>();
        for (const tc of calls) {
            const cat = tc.category || '';
            const catKey = cat ? `chat.toolCategories.${cat}` : '';
            const translated = catKey ? this.transloco.translate(catKey) : '';
            const fromI18n = translated && translated !== catKey ? translated : '';
            const label = fromI18n || CATEGORY_LABELS[cat] || this.toolLabel(tc);
            groups.set(label, (groups.get(label) || 0) + 1);
        }
        return Array.from(groups.entries())
            .map(([label, count]) => count > 1 ? `${label} x${count}` : label)
            .join(', ');
    }

    translateStatus(status: string): string {
        this.i18n.activeLang();
        const key = `chat.tools.status${status.charAt(0).toUpperCase()}${status.slice(1)}`;
        const translated = this.transloco.translate(key);
        return translated !== key ? translated : status;
    }

    toolSummaryLabel(calls: ToolCallInfo[]): string {
        // Use intent grouping when categories are available, else human labels
        const hasCategories = calls.some(tc => tc.category);
        return hasCategories ? this.groupToolCallsByIntent(calls) : this.groupToolCallsHuman(calls);
    }

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
