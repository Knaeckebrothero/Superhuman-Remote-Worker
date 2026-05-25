import {
    AfterViewChecked,
    Component,
    computed,
    effect,
    ElementRef,
    HostListener,
    inject,
    OnDestroy,
    OnInit,
    signal,
    ViewChild,
} from '@angular/core';
import {NgTemplateOutlet, TitleCasePipe} from '@angular/common';
import {HttpClient} from '@angular/common/http';
import {FormsModule} from '@angular/forms';
import {Router, RouterLink} from '@angular/router';
import {firstValueFrom, Subscription} from 'rxjs';
import {MarkdownComponent} from 'ngx-markdown';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ChatAttachment, PermissionRequest, PersistentChatService, ToolCallInfo,} from '../../core/services/persistent-chat.service';
import {
    AssistantTurn,
    countEvents,
    isAssistantTurn,
    isSystemTurn,
    isUserTurn,
    lastTextOf,
    TextEvent,
    ThoughtEvent,
    ToolCallEvent,
    Turn,
    TurnEvent,
} from '../../core/models/turn.model';
import {ApiService, IdeSessionStatus} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import {I18nService} from '../../core/services/i18n.service';
import {FileHandlingService} from '../../core/services/file-handling.service';
import {DeviceCapabilitiesService} from '../../core/services/device-capabilities.service';
import {VoiceRecordingService} from '../../core/services/voice-recording.service';
import {FilePreview, FileType} from '../../core/models/file.model';
import {RecordingConfig} from '../../core/models/recording.model';
import {environment} from '../../core/environment';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {AppButtonComponent} from '../../ui/button';
import {AppBadgeComponent} from '../../ui/badge';
import {AppSelectComponent} from '../../ui/select';
import {AppIconComponent} from '../../ui/icon';
import {AppDialogComponent} from '../../ui/dialog';
import {AppToastService} from '../../ui/toast';
import {ErrorMessageService} from '../../core/services/error-message.service';

interface SlashCommand {
    command: string;
    descriptionKey: string;
}

interface TtsMessageState {
    audioUrl?: string;
    isGenerating: boolean;
    isPlaying: boolean;
    error: boolean;
}

interface Suggestion {
    icon: string;
    en: string;
    de: string;
}

interface DisplayedSuggestion {
    icon: string;
    text: string;
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

/**
 * Pure decision helpers for the session-startup UX. Exported (and unit
 * tested in persistent-chat.component.spec.ts) so the logic is verified
 * without a full component render — see automations-page.component.spec.ts
 * for the same split.
 */

/**
 * Whether the slim startup banner should show: only once a turn already
 * exists while the session is still starting (a queued message, or a resumed
 * session's history). The @empty centered card covers the no-turns case, so
 * the two are mutually exclusive — the card never floats over the message
 * list (the old .startup-wrapper.resume overlay bug). Hides automatically
 * when the session goes ready (isStartingSession → false).
 */
export function isStartupBannerVisible(isStartingSession: boolean, turnCount: number): boolean {
    return isStartingSession && turnCount > 0;
}

/**
 * Whether the composer should accept input: while connected OR while the
 * session is still starting (so the user can type from t=0 and have the
 * message queued + auto-sent on session.state). Deliberately false during a
 * mid-session reconnect (connected=false, starting=false) — markSessionReady
 * flushes the pending queue only once, so a message queued then would never
 * send.
 */
export function canComposeDuringSession(isConnected: boolean, isStartingSession: boolean): boolean {
    return isConnected || isStartingSession;
}

/**
 * The startup step to surface in the banner: the active one, or the last
 * step as a fallback when none is active (the brief gap where every phase is
 * recorded done before sessionReady flips). Null for an empty list.
 */
export function pickCurrentStartupStep<T extends {state: string}>(steps: readonly T[]): T | null {
    return steps.find((s) => s.state === 'active') ?? steps[steps.length - 1] ?? null;
}

@Component({
    selector: 'app-persistent-chat',
    standalone: true,
    imports: [
        FormsModule,
        NgTemplateOutlet,
        TitleCasePipe,
        RouterLink,
        MarkdownComponent,
        SidebarToggleComponent,
        TranslocoPipe,
        AppButtonComponent,
        AppBadgeComponent,
        AppSelectComponent,
        AppIconComponent,
        AppDialogComponent,
    ],
    template: `
    <div class="chat-container">
      <!-- Drag-and-drop overlay (covers the chat area while files are being dragged) -->
      @if (isDragOver()) {
        <div class="drop-overlay" aria-hidden="true">
          <div class="drop-overlay-card">
            <app-icon size="lg" class="drop-overlay-icon">cloud_upload</app-icon>
            <span class="drop-overlay-text">{{ 'chat.composer.dropHint' | transloco }}</span>
          </div>
        </div>
      }

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
            <app-button variant="ghost" size="sm" (clicked)="disconnectAndLeave()">
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

      <!--
        Shared template for the per-tool-call expandable card list. Used by
        three sites: the streaming "completed tools" block, the finalized
        message-with-content branch, and the tool-only-message branch. Keep
        the template the single source of truth — adding/changing args
        formatting, status icons, decision badges, etc. should only happen
        here.
      -->
      <ng-template #toolDetails let-tools>
        <div class="tool-detail-list">
          @for (tc of tools; track tc.id) {
            <details class="tool-card" [class.has-decision]="!!tc.decision" [class.tool-error]="tc.status === 'error'" [attr.open]="(tc.status === 'denied' || tc.status === 'error') ? '' : null">
              <summary class="tool-head">
                <app-icon size="sm" class="tool-icon">{{ toolIcon(tc.tool) }}</app-icon>
                @if (tc.decision; as d) {
                  <span class="approval-badge" [class]="'approval-' + d">
                    <app-icon size="sm" class="approval-badge-icon">{{ d === 'approved' ? 'check_circle' : 'block' }}</app-icon>
                    {{ ('chat.approval.badge.' + d) | transloco }}
                  </span>
                }
                <span class="tool-name">{{ tc.tool }}</span>
                @if (formatToolArgs(tc.args); as a) {
                  <span class="tool-args">({{ a }})</span>
                }
                <span class="tool-status" [class]="'status-' + tc.status">
                  <app-icon size="sm" class="tool-status-icon">{{ statusIcon(tc.status) }}</app-icon>
                  {{ translateStatus(tc.status) }}
                </span>
              </summary>
              @if (tc.result) {
                <div class="tool-body"><pre class="tool-result">{{ tc.result }}</pre></div>
              }
            </details>
          }
        </div>
      </ng-template>

      <!-- Per-event card templates (referenced by the turn loop below). -->
      <ng-template #thoughtCard let-event>
        <details class="thinking-block event-thought"
                 [attr.open]="event.status === 'streaming' || chat.narrationMode() === 'verbose' ? '' : null">
          <summary class="thinking-header">
            <app-icon size="sm" class="thinking-icon">psychology</app-icon>
            <span class="thinking-label">
              {{ (event.status === 'streaming' ? 'chat.thinking.now' : 'chat.thinking.past') | transloco }}
            </span>
          </summary>
          <div class="thinking-content">{{ event.content }}</div>
        </details>
      </ng-template>

      <!-- Startup banner: slim, non-scrolling status strip shown once a turn
           exists while the session is still starting. Reuses the .startup-step
           row so it matches the centered card; replaces the old floating
           .startup-wrapper.resume overlay so the card never sits over the
           message list. Auto-hides when sessionReady flips. -->
      @if (startupBannerVisible()) {
        <div class="startup-banner">
          @if (currentStartupStep(); as step) {
            <div class="startup-step state-active">
              <span class="step-spinner" aria-hidden="true"></span>
              <span class="step-label">{{ ('chat.startup.steps.' + step.key) | transloco }}</span>
              <time class="step-time">{{ formatElapsed(step.elapsedMs) }}</time>
            </div>
          }
        </div>
      }

      <!-- Messages -->
      <div class="messages" #messagesContainer (scroll)="onMessagesScroll()">
        @for (turn of chat.turns(); track turn.id; let isLast = $last) {
          @switch (turn.kind) {
            @case ('system') {
              <div class="message message-system">
                <div class="system-message">
                  <app-icon size="sm" class="system-icon">info</app-icon>
                  {{ turn.content }}
                </div>
              </div>
            }
            @case ('user') {
              <div class="message message-user" [class.historical]="turn.historical">
                <div class="avatar">
                  <app-icon size="sm" class="avatar-icon">person</app-icon>
                </div>
                <div class="message-body">
                  @if (turn.content) {
                    <div class="user-text">{{ turn.content }}</div>
                  }
                  @if (turn.attachments?.length) {
                    <div class="user-attachments">
                      @for (att of turn.attachments; track att.path) {
                        <span class="user-attachment-chip" [title]="att.path">
                          <app-icon size="sm">{{
                            att.mimeType.startsWith('image/') ? 'image' :
                            att.mimeType.startsWith('video/') ? 'videocam' :
                            att.mimeType.startsWith('audio/') ? 'audiotrack' :
                            'description'
                          }}</app-icon>
                          <span class="user-attachment-name">{{ att.name }}</span>
                        </span>
                      }
                    </div>
                  }
                </div>
              </div>
            }
            @case ('assistant') {
              @let isCollapsed = isTurnCollapsed(turn);
              @let counts = turnEventCounts(turn);
              @let last = lastTextEvent(turn);
              @let streaming = turn.status === 'streaming';
              @let ttsKey = 'turn:' + turn.id;
              @let ttsS = ttsStateFor(ttsKey);
              <div class="message message-assistant turn-bubble"
                   [class.historical]="turn.historical"
                   [class.streaming]="streaming"
                   [class.collapsed]="isCollapsed"
                   [class.dimmed]="isShowingReconnectBanner() && isLast">
                <div class="avatar">
                  <app-icon size="sm" class="avatar-icon">smart_toy</app-icon>
                </div>
                <div class="message-body turn-body">
                  <!-- Whole-turn chevron: collapses every event into the
                       per-type badge summary + last-text headline. Hidden
                       when the turn has 0–1 events (nothing to collapse). -->
                  @if (turn.events.length > 1) {
                    <button type="button"
                            class="turn-chevron"
                            [attr.aria-expanded]="!isCollapsed"
                            (click)="toggleTurnCollapse(turn)">
                      <app-icon size="sm" class="turn-chevron-icon">{{ isCollapsed ? 'chevron_right' : 'expand_more' }}</app-icon>
                      <span class="turn-chevron-badge">
                        @if (counts.thoughts > 0) {
                          <span class="badge-thought" [title]="('chat.turn.thoughtCount' | transloco:{count: counts.thoughts})">◐ {{ counts.thoughts }}</span>
                        }
                        @if (counts.texts > 0 && !isCollapsed) {
                          <span class="badge-text" [title]="('chat.turn.textCount' | transloco:{count: counts.texts})">✎ {{ counts.texts }}</span>
                        }
                        @if (counts.tools > 0) {
                          <span class="badge-tool" [title]="('chat.turn.toolCount' | transloco:{count: counts.tools})">▶ {{ counts.tools }}</span>
                        }
                      </span>
                    </button>
                  }

                  @if (isCollapsed) {
                    <!-- Collapsed: single-line preview of the final text
                         event. Plain text (not <markdown>) so the truncate
                         mixin works — markdown emits inner block elements
                         that defeat nowrap. -->
                    @if (last) {
                      <span class="turn-headline">{{ last.content }}</span>
                    } @else {
                      <span class="turn-headline-empty">{{ 'chat.turn.collapsedEmpty' | transloco }}</span>
                    }
                  } @else {
                    <!-- Expanded: every event rendered as its own card. -->
                    @for (event of turn.events; track event.id) {
                      @switch (event.kind) {
                        @case ('thought') {
                          @if (chat.narrationMode() !== 'silent') {
                            <ng-container [ngTemplateOutlet]="thoughtCard" [ngTemplateOutletContext]="{ $implicit: event }"></ng-container>
                          }
                        }
                        @case ('text') {
                          <div class="event-text">
                            <markdown [data]="event.content"></markdown>
                          </div>
                        }
                        @case ('tool_call') {
                          <div class="event-tool">
                            <ng-container [ngTemplateOutlet]="toolDetails" [ngTemplateOutletContext]="{ $implicit: [event] }"></ng-container>
                            @if (event.decision; as d) {
                              <div class="mile-resolved" [class.approved]="d === 'approved'" [class.rejected]="d === 'denied'">
                                <app-icon size="sm" class="mile-resolved-icon">{{ d === 'approved' ? 'check_circle' : 'block' }}</app-icon>
                                <span class="resolved-label">{{ ('chat.approval.badge.' + d) | transloco }}</span>
                                <span class="resolved-title">{{ event.tool }}</span>
                              </div>
                            }
                          </div>
                        }
                      }
                    }

                    <!-- Streaming pulse while the turn is in flight with nothing yet. -->
                    @if (streaming && turn.events.length === 0 && !chat.pendingPermission()) {
                      <div class="thinking">
                        <span class="thinking-dot"></span>
                        <span class="thinking-dot"></span>
                        <span class="thinking-dot"></span>
                      </div>
                    }
                  }

                  <!-- TTS button on the turn's final text. -->
                  @if (last && !streaming) {
                    <div class="message-actions">
                      <button
                        type="button"
                        class="msg-action-btn tts-btn"
                        [class.is-playing]="ttsS.isPlaying"
                        [class.is-error]="ttsS.error"
                        [disabled]="ttsS.isGenerating"
                        [title]="(
                          ttsS.isPlaying ? 'chat.tts.stop' :
                          ttsS.isGenerating ? 'chat.tts.generating' :
                          ttsS.error ? 'chat.tts.error' :
                          'chat.tts.play'
                        ) | transloco"
                        (click)="toggleTts(ttsKey, last.content)"
                      >
                        @if (ttsS.isGenerating) {
                          <span class="action-spinner-sm"></span>
                        } @else if (ttsS.isPlaying) {
                          <app-icon size="sm">stop</app-icon>
                        } @else if (ttsS.error) {
                          <app-icon size="sm">error_outline</app-icon>
                        } @else {
                          <app-icon size="sm">volume_up</app-icon>
                        }
                      </button>
                    </div>
                  }
                </div>
              </div>
            }
          }
          <!-- Divider between historical-loaded turns and the live session. -->
          @if (showSessionDividerAfter(turn, $index)) {
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
                <div class="empty-inner">
                  <img class="empty-mark" src="assets/icons/icon-mark.svg" alt="" />
                  <h2 class="empty-title">{{ 'chat.empty.title' | transloco }}</h2>
                  <p class="empty-subtitle">{{ 'chat.empty.subtitle' | transloco }}</p>
                  @if (displayedSuggestions().length > 0) {
                    <div class="suggestion-grid">
                      @for (s of displayedSuggestions(); track $index) {
                        <button type="button" class="suggestion-chip"
                                (click)="pickSuggestion(s)">
                          <app-icon size="lg" class="suggestion-icon">{{ s.icon }}</app-icon>
                          <span class="suggestion-text">{{ s.text }}</span>
                        </button>
                      }
                    </div>
                  }
                </div>
              } @else if (chat.isStartingSession()) {
                <div class="startup-wrapper">
                  <ng-container *ngTemplateOutlet="startupCardTpl"></ng-container>
                </div>
              }
            </div>
          }
        }

        <ng-template #startupCardTpl>
          <div class="startup-card">
            <div class="startup-card-head">
              <span class="startup-card-spinner"></span>
              <span class="startup-card-title">
                {{ (chat.turns().length > 0 ? 'chat.startup.titleResume' : 'chat.startup.title') | transloco }}
              </span>
            </div>
            <div class="startup-steps">
              @for (step of startupSteps(); track step.key) {
                <div class="startup-step" [class]="'state-' + step.state">
                  @if (step.state === 'active') {
                    <span class="step-spinner" aria-hidden="true"></span>
                  } @else {
                    <app-icon size="lg" class="step-icon">{{ stepIcon(step.state) }}</app-icon>
                  }
                  <span class="step-label">{{ ('chat.startup.steps.' + step.key) | transloco }}</span>
                  <time class="step-time">{{ formatElapsed(step.elapsedMs) }}</time>
                </div>
              }
            </div>
          </div>
        </ng-template>

        <!-- Inline approval card (mile marker) — anchored to the live turn,
             not gated on streaming state so it stays visible across edge cases. -->
        @if (chat.pendingPermission(); as perm) {
          <div class="mile">
            <div class="mile-label">{{ 'chat.permission.title' | transloco }}</div>
            <div class="mile-title">{{ permissionTitle(perm) }}</div>
            <div class="mile-detail">
              <app-icon size="sm" class="mile-detail-icon">{{ toolIcon(perm.tool) }}</app-icon>
              <code class="mile-tool">{{ perm.tool }}</code>
              @if (formatToolArgs(perm.args); as a) {
                <code class="mile-args">({{ a }})</code>
              }
            </div>
            <div class="mile-actions">
              <app-button variant="success" size="sm" (clicked)="chat.approve()">{{ 'chat.permission.approve' | transloco }}</app-button>
              <app-button variant="info" size="sm" (clicked)="approveAndAutoAccept()">{{ 'chat.permission.autoAccept' | transloco }}</app-button>
              <app-button variant="danger" size="sm" (clicked)="chat.stop()">{{ 'chat.permission.stop' | transloco }}</app-button>
            </div>
          </div>
        }

        <!-- Ended-session end-marker + resume card — replaces the composer
             when the thread is in 'ended' status. -->
        @if (chat.threadStatus() === 'ended') {
          <div class="end-marker">
            <span class="end-line"></span>
            <div class="end-tag">
              <app-icon size="sm" class="end-icon">flag</app-icon>
              <span>{{ 'chat.ended.endedAt' | transloco:{ date: formatEndedAt(chat.endedAt()) } }}</span>
            </div>
            <span class="end-line"></span>
          </div>
          <div class="resume-card">
            <div class="resume-body">
              <div class="resume-eyebrow">{{ 'chat.ended.eyebrow' | transloco }}</div>
              <h3 class="resume-title">{{ 'chat.ended.title' | transloco }}</h3>
              <p class="resume-text">{{ 'chat.ended.body' | transloco }}</p>
            </div>
            <div class="resume-actions">
              <app-button variant="primary"
                          [loading]="isResuming()"
                          (clicked)="resumeSession()">
                @if (isResuming()) {
                  {{ 'chat.system.resuming' | transloco }}
                } @else {
                  <app-icon size="sm" class="resume-icon">play_arrow</app-icon>
                  {{ 'chat.ended.resume' | transloco }}
                }
              </app-button>
            </div>
          </div>
        }

        <!-- Reconnect banner: WS dropped on a still-active thread.
             Mutually exclusive with the F3 resume card (threadStatus !== 'ended')
             and the F2 startup card (sessionReady === true). -->
        @if (isShowingReconnectBanner()) {
          <div class="reconnect-banner">
            <app-icon size="sm" class="rb-icon">cloud_off</app-icon>
            <div class="rb-body">
              <strong>{{ 'chat.disconnected.title' | transloco }}</strong>
              @if (chat.reconnectGaveUp()) {
                <span>{{ 'chat.disconnected.gaveUp' | transloco }}</span>
              } @else if (chat.reconnectAttempt() > 0) {
                <span>{{ 'chat.disconnected.retrying' | transloco:{ attempt: chat.reconnectAttempt(), max: chat.reconnectMaxAttempts } }}</span>
              } @else {
                <span>{{ 'chat.disconnected.dropped' | transloco }}</span>
              }
            </div>
            <app-button variant="secondary" size="sm" (clicked)="chat.reconnectNow()">
              {{ (chat.reconnectGaveUp() ? 'chat.disconnected.reconnect' : 'chat.disconnected.retryNow') | transloco }}
            </app-button>
          </div>
        }

        <!-- Jump-to-latest pill: appears when the user has scrolled up while
             new messages arrive. Sticky-positioned so it floats over the stream
             without needing a wrapper element. -->
        @if (showJumpToLatest()) {
          <button class="jump-latest" type="button" (click)="jumpToLatest()">
            <app-icon size="sm">arrow_downward</app-icon>
            <span>{{ 'chat.jumpToLatest' | transloco: { count: newMessageCount() } }}</span>
          </button>
        }
      </div>

      <!-- Error banner -->
      @if (chat.error(); as err) {
        @if (!isShowingReconnectBanner()) {
          <div class="error-banner">
            <app-icon size="sm" class="error-icon">error</app-icon>
            {{ err }}
            <button class="error-dismiss" (click)="chat.error.set(null)">{{ 'chat.error.dismiss' | transloco }}</button>
          </div>
        }
      }

      <!-- Input -->
      @if (chat.threadStatus() !== 'ended') {
      <div class="composer-wrap">
        <div
          class="composer"
          [class.focused]="inputFocused()"
          [class.disabled]="!canCompose()"
          [class.recording]="isRecording()"
        >
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

          <!-- Attachment preview chips -->
          @if (chat.pendingAttachments().length > 0 && !isRecording()) {
            <div class="attachment-row">
              @for (preview of chat.pendingAttachments(); track preview.id) {
                <div class="attachment-chip" [class.is-image]="preview.type === 'image'">
                  @if (preview.type === 'image' && preview.preview) {
                    <button
                      type="button"
                      class="attachment-thumb"
                      (click)="openImagePreview(preview)"
                      [attr.aria-label]="preview.name"
                    >
                      <img [src]="preview.preview" [alt]="preview.name" />
                    </button>
                  } @else {
                    <span class="attachment-icon">
                      <app-icon size="sm">{{
                        preview.type === 'audio' ? 'audiotrack' :
                        preview.type === 'video' ? 'videocam' :
                        preview.type === 'document' ? 'description' : 'insert_drive_file'
                      }}</app-icon>
                    </span>
                  }
                  <span class="attachment-meta">
                    <span class="attachment-name">{{ preview.name }}</span>
                    <span class="attachment-size">{{ preview.sizeFormatted }}</span>
                  </span>
                  <button
                    type="button"
                    class="attachment-remove"
                    (click)="removeAttachment(preview.id)"
                    [attr.aria-label]="'chat.composer.remove' | transloco"
                    [title]="'chat.composer.remove' | transloco"
                  >
                    <app-icon size="sm">close</app-icon>
                  </button>
                </div>
              }
            </div>
          }

          <!-- Upload error banner -->
          @if (chat.attachmentError(); as err) {
            <div class="attachment-error">
              <app-icon size="sm">error_outline</app-icon>
              <span>{{ err }}</span>
            </div>
          }

          <!-- Recording mode: waveform + duration + controls -->
          @if (isRecording()) {
            <div class="recording-strip">
              <button
                type="button"
                class="recording-btn cancel"
                (click)="cancelRecording()"
                [attr.aria-label]="'chat.composer.recordingCancel' | transloco"
                [title]="'chat.composer.recordingCancel' | transloco"
              >
                <app-icon size="sm">close</app-icon>
              </button>
              <canvas #waveformCanvas class="recording-canvas" width="600" height="56"></canvas>
              <span class="recording-time">
                <span class="recording-dot"></span>
                {{ recordingDuration() }}s
              </span>
              <button
                type="button"
                class="recording-btn confirm"
                (click)="stopRecording()"
                [attr.aria-label]="'chat.composer.recordingStop' | transloco"
                [title]="'chat.composer.recordingStop' | transloco"
              >
                <app-icon size="sm">check</app-icon>
              </button>
            </div>
          } @else {
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
              [disabled]="!canCompose()"
              rows="1"
            ></textarea>
          }

          @if (!isRecording()) {
          <div class="composer-row">
            <!-- Attach button + popover menu -->
            <div class="attach-wrap">
              <button
                type="button"
                class="ctrl"
                [disabled]="!chat.isConnected() || chat.isUploadingAttachments()"
                [title]="'chat.composer.attach' | transloco"
                [class.active]="attachmentMenuOpen()"
                (click)="attachmentMenuOpen() ? closeAttachmentMenu() : openAttachmentMenu()"
              >
                <app-icon size="sm" class="ctrl-icon">attach_file</app-icon>
                <span class="ctrl-label">{{ 'chat.composer.attach' | transloco }}</span>
              </button>
              @if (attachmentMenuOpen()) {
                <div class="attach-menu" (click)="$event.stopPropagation()">
                  <button type="button" class="attach-menu-item" (click)="pickFile()">
                    <app-icon size="sm">folder_open</app-icon>
                    <span>{{ 'chat.composer.chooseFile' | transloco }}</span>
                  </button>
                  @if (hasCamera()) {
                    <button type="button" class="attach-menu-item" (click)="pickCamera()">
                      <app-icon size="sm">photo_camera</app-icon>
                      <span>{{ 'chat.composer.takePhoto' | transloco }}</span>
                    </button>
                  }
                </div>
                <div class="attach-menu-backdrop" (click)="closeAttachmentMenu()"></div>
              }
            </div>

            <!-- Direct camera shortcut on mobile devices -->
            @if (hasCamera() && isMobileDevice()) {
              <button
                type="button"
                class="ctrl"
                [disabled]="!chat.isConnected() || chat.isUploadingAttachments()"
                [title]="'chat.composer.takePhoto' | transloco"
                (click)="pickCamera()"
              >
                <app-icon size="sm" class="ctrl-icon">photo_camera</app-icon>
              </button>
            }

            <span class="spacer"></span>

            <!-- Mic button: shown only when no text/attachments queued and not streaming -->
            @if (
              hasAudioInput()
              && inputText.trim().length === 0
              && chat.pendingAttachments().length === 0
              && !chat.isStreaming()
              && !isPendingSend()
            ) {
              <button
                type="button"
                class="ctrl mic"
                [disabled]="!chat.isConnected()"
                [title]="'chat.composer.recordVoice' | transloco"
                (click)="startRecording()"
              >
                <app-icon size="sm" class="ctrl-icon">mic</app-icon>
              </button>
            }

            <button
              type="button"
              class="send"
              [class.stop]="chat.isStreaming() && !chat.isInterrupting()"
              [class.interrupting]="chat.isInterrupting()"
              [class.pending]="isPendingSend()"
              [title]="(chat.isStreaming() ? 'chat.composer.stop' : 'chat.composer.send') | transloco"
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
          }
        </div>

        <!-- Hidden file inputs -->
        <input
          #fileInput
          type="file"
          multiple
          accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.txt,.md,.csv,.xls,.xlsx,.zip"
          (change)="onFilesSelected($event)"
          style="display: none;"
        />
        <input
          #cameraInput
          type="file"
          accept="image/*"
          capture="environment"
          (change)="onFilesSelected($event)"
          style="display: none;"
        />
      </div>
      }

      <!-- Image preview dialog -->
      <app-dialog
        [open]="imagePreviewUrl() !== null"
        [title]="imagePreviewName()"
        size="lg"
        (closed)="closeImagePreview()"
      >
        @if (imagePreviewUrl(); as url) {
          <img [src]="url" [alt]="imagePreviewName()" class="image-preview-img" />
        }
      </app-dialog>
    </div>
  `,
    styleUrls: ['./persistent-chat.component.scss'],
})
export class PersistentChatComponent implements OnInit, AfterViewChecked, OnDestroy {
    readonly chat = inject(PersistentChatService);
    private readonly api = inject(ApiService);
    readonly modelService = inject(ModelService);
    private readonly transloco = inject(TranslocoService);
    private readonly i18n = inject(I18nService);
    private readonly http = inject(HttpClient);
    private readonly fileHandling = inject(FileHandlingService);
    private readonly deviceCapabilities = inject(DeviceCapabilitiesService);
    private readonly voiceRecording = inject(VoiceRecordingService);
    private readonly router = inject(Router);
    private readonly toast = inject(AppToastService);
    private readonly errors = inject(ErrorMessageService);

    @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;
    @ViewChild('inputEl') inputEl!: ElementRef<HTMLTextAreaElement>;
    @ViewChild('fileInput') fileInput?: ElementRef<HTMLInputElement>;
    @ViewChild('cameraInput') cameraInput?: ElementRef<HTMLInputElement>;
    @ViewChild('waveformCanvas') waveformCanvas?: ElementRef<HTMLCanvasElement>;

    inputText = '';

    // Settings panel
    readonly showSettings = signal(false);

    // Resume state
    readonly isResuming = signal(false);

    // Input state
    readonly inputFocused = signal(false);

    // Composer attachments — device capabilities, recording, image preview.
    readonly hasCamera = signal(false);
    readonly hasAudioInput = signal(false);
    readonly isMobileDevice = signal(false);
    readonly attachmentMenuOpen = signal(false);
    readonly isRecording = signal(false);
    readonly recordingDuration = signal(0);
    readonly imagePreviewUrl = signal<string | null>(null);
    readonly imagePreviewName = signal<string>('');
    // Drag-and-drop overlay state. dragEnterCount handles the
    // dragenter/dragleave-on-child-element quirk: the leave fires every
    // time the cursor crosses any nested element border, so we only hide
    // the overlay when the counter returns to zero.
    readonly isDragOver = signal(false);
    private dragEnterCount = 0;

    private capabilitiesSub?: Subscription;
    private recordingStateSub?: Subscription;

    // Per-turn TTS state. Keyed by a stable string ("turn:<id>") so playback
    // state survives across re-renders even if the turn list is reordered.
    readonly ttsState = signal<Record<string, TtsMessageState>>({});
    private currentTtsAudio: HTMLAudioElement | null = null;
    private currentTtsKey: string | null = null;
    // Tracks blob URLs we've created so we can revoke them on destroy.
    private readonly ttsBlobUrls = new Set<string>();

    // Slash command autocomplete
    readonly showSlashMenu = signal(false);
    readonly slashSelectedIndex = signal(0);
    readonly filteredCommands = signal<SlashCommand[]>([]);

    // Empty-state suggestions (loaded once, picked once per mount)
    private readonly pickedSuggestions = signal<Suggestion[]>([]);
    readonly displayedSuggestions = computed<DisplayedSuggestion[]>(() => {
        const lang = this.i18n.activeLang();
        return this.pickedSuggestions().map(s => ({
            icon: s.icon,
            text: lang === 'de-DE' ? (s.de || s.en) : s.en,
        }));
    });

    formatEndedAt(value: string | null): string {
        if (!value) return '';
        const lang = this.i18n.activeLang();
        try {
            return new Intl.DateTimeFormat(lang, {
                dateStyle: 'long',
                timeStyle: 'short',
            }).format(new Date(value));
        } catch {
            return value;
        }
    }

    // Startup step list — drives the provisioning card.
    // Order maps to the phases the orchestrator emits via the `status` WS message.
    private readonly STARTUP_PHASE_ORDER = ['creating', 'provisioning', 'booting', 'connecting'] as const;

    // Per-phase timing. Plain mutable maps + a revision signal so the computed
    // re-runs on transitions without forming a self-referential signal loop.
    private phaseStarts: Record<string, number> = {};
    private phaseDurations: Record<string, number> = {};
    private readonly phaseRevision = signal(0);
    private readonly nowTick = signal<number>(Date.now());
    private startupTickInterval: ReturnType<typeof setInterval> | null = null;
    private prevActivePhase: string | null = null;
    private prevReadyForTiming = false;
    private prevThreadIdForTiming: string | null = null;

    readonly startupSteps = computed(() => {
        const order = this.STARTUP_PHASE_ORDER;
        const phase = this.chat.startupPhase();
        const isResuming = this.chat.turns().length > 0;
        // Subscribe to phase tracking changes and to the live tick.
        this.phaseRevision();
        const now = this.nowTick();
        let activeIdx: number;
        if (this.chat.isCreating()) {
            activeIdx = 0;
        } else if (phase && (order as readonly string[]).includes(phase)) {
            activeIdx = (order as readonly string[]).indexOf(phase);
        } else {
            // No live phase signal. Phases arrive in two batches: 'creating'
            // is set client-side and cleared in disconnect() before the
            // server-emitted 'provisioning' status frame arrives over the
            // control WS. During that gap, the previous fallback (`isResuming
            // ? 1 : 0`) regressed step 0 back to 'active', wiping the just-
            // recorded "2.0s" with a fresh spinner. Use the completed-count
            // as the floor so a step that finished stays done.
            const completedCount = order.filter(k => this.phaseDurations[k] != null).length;
            activeIdx = Math.max(completedCount, isResuming ? 1 : 0);
        }
        return order.map((key, idx) => {
            const state: 'done' | 'active' | 'todo' =
                idx < activeIdx ? 'done' : (idx === activeIdx ? 'active' : 'todo');
            let elapsedMs: number | null = null;
            if (state === 'done') {
                elapsedMs = this.phaseDurations[key] ?? null;
            } else if (state === 'active' && this.phaseStarts[key] != null) {
                elapsedMs = Math.max(0, now - this.phaseStarts[key]);
            }
            return { key, state, elapsedMs };
        });
    });

    /** The startup step to surface in the slim banner (active, or last as a fallback). */
    readonly currentStartupStep = computed(() => pickCurrentStartupStep(this.startupSteps()));

    /**
     * Show the slim startup banner once a turn exists while still starting —
     * mutually exclusive with the @empty centered card, so the card never
     * floats over the message list.
     */
    readonly startupBannerVisible = computed(() =>
        isStartupBannerVisible(this.chat.isStartingSession(), this.chat.turns().length),
    );

    /**
     * Whether the composer accepts input: during startup (type + queue +
     * flush on ready) and while connected; false during a mid-session
     * reconnect.
     */
    readonly canCompose = computed(() =>
        canComposeDuringSession(this.chat.isConnected(), this.chat.isStartingSession()),
    );

    stepIcon(state: 'done' | 'active' | 'todo'): string {
        if (state === 'done') return 'check_circle';
        if (state === 'active') return 'progress_activity';
        return 'radio_button_unchecked';
    }

    formatElapsed(ms: number | null): string {
        if (ms == null) return '—';
        const s = Math.max(0, ms) / 1000;
        if (s < 10) return `${s.toFixed(1)}s`;
        if (s < 60) return `${Math.round(s)}s`;
        const m = Math.floor(s / 60);
        const rs = Math.round(s % 60);
        return `${m}m${rs.toString().padStart(2, '0')}s`;
    }

    // IDE status
    readonly ideStatus = signal<IdeSessionStatus | null>(null);
    private idePollingTimer: ReturnType<typeof setInterval> | null = null;
    private idePollingAttempts = 0;

    private autoScroll = true;
    private lastSeenMessageCount = 0;

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

        // Load empty-state suggestions and pick 4 at random for this mount
        this.http.get<Suggestion[]>('assets/suggestions.json').subscribe({
            next: (data) => {
                const shuffled = [...data].sort(() => Math.random() - 0.5);
                this.pickedSuggestions.set(shuffled.slice(0, Math.min(4, shuffled.length)));
            },
            error: () => this.pickedSuggestions.set([]),
        });

        // Auto-scroll when turns or in-flight events change. Reading both the
        // turn list and the in-flight turn's events array keeps the effect
        // subscribed to deltas on the active streaming turn.
        effect(() => {
            this.chat.turns();
            const active = this.chat.currentStreamingTurn();
            if (active) active.events.length;
            this.chat.pendingPermission();

            if (this.autoScroll) {
                setTimeout(() => this.scrollToBottom(), 0);
            }
        });

        // Track new messages that arrive while the user has scrolled up.
        // Drives the "Jump to latest · N new" pill (F5).
        effect(() => {
            const len = this.chat.turns().length;
            const away = this.scrolledAway();
            if (len < this.lastSeenMessageCount) {
                // Thread switch or messages cleared — start fresh.
                this.newMessageCount.set(0);
            } else if (away && len > this.lastSeenMessageCount) {
                const delta = len - this.lastSeenMessageCount;
                this.newMessageCount.update(n => n + delta);
            } else if (!away) {
                this.newMessageCount.set(0);
            }
            this.lastSeenMessageCount = len;
        });

        // Track startup phase transitions to record per-step durations.
        effect(() => {
            const phase = this.chat.startupPhase();
            const isCreating = this.chat.isCreating();
            const ready = this.chat.sessionReady();
            const tid = this.chat.threadId();
            const order = this.STARTUP_PHASE_ORDER as readonly string[];

            // Reset when switching to a different thread, or when sessionReady
            // drops back to false. The null → realId transition during new-thread
            // creation must NOT reset — that would wipe the 'creating' phase
            // timing recorded while threadId was still null.
            const threadChanged = this.prevThreadIdForTiming != null && tid !== this.prevThreadIdForTiming;
            if (threadChanged || (this.prevReadyForTiming && !ready)) {
                this.phaseStarts = {};
                this.phaseDurations = {};
                this.prevActivePhase = null;
                this.phaseRevision.update(v => v + 1);
            }
            this.prevThreadIdForTiming = tid;
            this.prevReadyForTiming = ready;

            // Determine effective active phase (mirrors startupSteps logic, but
            // we only record timing once we have a real signal — not during the
            // brief null gap on resume).
            let active: string | null = null;
            if (!ready) {
                if (isCreating) active = 'creating';
                else if (phase && order.includes(phase)) active = phase;
            }

            if (active !== this.prevActivePhase) {
                const now = Date.now();
                if (this.prevActivePhase != null) {
                    const start = this.phaseStarts[this.prevActivePhase];
                    if (start != null) {
                        this.phaseDurations[this.prevActivePhase] = now - start;
                    }
                }
                if (active && this.phaseStarts[active] == null) {
                    this.phaseStarts[active] = now;
                }
                this.prevActivePhase = active;
                this.phaseRevision.update(v => v + 1);
            }

            // Tick every second while a phase is active so the live elapsed
            // time on the active row updates without manual refresh.
            if (active && !this.startupTickInterval) {
                this.nowTick.set(Date.now());
                this.startupTickInterval = setInterval(() => this.nowTick.set(Date.now()), 1000);
            } else if (!active && this.startupTickInterval) {
                clearInterval(this.startupTickInterval);
                this.startupTickInterval = null;
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

    readonly isShowingReconnectBanner = computed(() =>
        this.chat.connectionState() === 'disconnected'
        && this.chat.threadStatus() === 'active'
        && this.chat.sessionReady()
        && this.chat.turns().length > 0,
    );

    readonly scrolledAway = signal(false);
    readonly newMessageCount = signal(0);
    readonly showJumpToLatest = computed(
        () => this.scrolledAway() && this.newMessageCount() > 0,
    );

    readonly inputPlaceholder = computed(() => {
        // Track language changes so placeholder re-translates when i18n switches.
        this.i18n.activeLang();
        if (this.isShowingReconnectBanner()) return this.transloco.translate('chat.input.reconnecting');
        if (this.chat.isStartingSession()) return this.transloco.translate('chat.input.sessionStarting');
        if (!this.chat.isConnected()) return this.transloco.translate('chat.input.connect');
        if (this.chat.isInterrupting()) return this.transloco.translate('chat.input.stopping');
        if (this.chat.isStreaming()) return this.transloco.translate('chat.input.working');
        if (this.chat.isUploadingAttachments()) return this.transloco.translate('chat.input.uploading');
        return this.transloco.translate('chat.input.default');
    });

    /** True when there is a pending message waiting for the session to become ready. */
    readonly isPendingSend = computed(
        () =>
            this.chat.pendingMessage() !== null ||
            this.chat.isUploadingAttachments(),
    );

    readonly canSend = computed(
        () =>
            this.canCompose() &&
            (this.inputText.trim().length > 0 || this.chat.pendingAttachments().length > 0) &&
            !this.isPendingSend(),
    );

    ngOnInit(): void {
        this.capabilitiesSub = this.deviceCapabilities.getCapabilities().subscribe((caps) => {
            this.hasCamera.set(caps.hasCamera);
            this.hasAudioInput.set(caps.hasAudioInput);
            this.isMobileDevice.set(caps.isMobile);
        });
        this.recordingStateSub = this.voiceRecording.getRecordingState().subscribe((state) => {
            this.isRecording.set(state.isRecording);
            this.recordingDuration.set(state.duration);
        });
    }

    ngAfterViewChecked(): void {
        this.collapseCodeBlocks();
        this.addCopyButtons();
    }

    ngOnDestroy(): void {
        // Don't disconnect — keep session alive across navigation
        this.stopIdePolling();
        if (this.startupTickInterval) {
            clearInterval(this.startupTickInterval);
            this.startupTickInterval = null;
        }
        this.capabilitiesSub?.unsubscribe();
        this.recordingStateSub?.unsubscribe();
        if (this.isRecording()) {
            this.voiceRecording.cancelRecording();
        }
        // Tear down TTS playback + free blob URLs.
        if (this.currentTtsAudio) {
            try {
                this.currentTtsAudio.pause();
                this.currentTtsAudio.src = '';
            } catch {
                // ignore
            }
            this.currentTtsAudio = null;
            this.currentTtsKey = null;
        }
        this.ttsBlobUrls.forEach((url) => URL.revokeObjectURL(url));
        this.ttsBlobUrls.clear();
    }

    autoResizeInput(): void {
        const el = this.inputEl?.nativeElement;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = el.scrollHeight + 'px';
    }

    send(): void {
        const text = this.inputText.trim();
        if (!text && this.chat.pendingAttachments().length === 0) return;

        this.showSlashMenu.set(false);
        // Clear textarea immediately — sendMessage is async because of uploads.
        this.inputText = '';
        this.autoScroll = true;
        // Fire-and-forget. Errors are surfaced via chat.attachmentError().
        void this.chat.sendMessage(text);

        // Resize textarea back
        setTimeout(() => {
            if (this.inputEl?.nativeElement) {
                this.inputEl.nativeElement.style.height = 'auto';
            }
        });
    }

    // ===== Composer attachment / camera / voice handlers =====

    /** Open the attachment menu. */
    openAttachmentMenu(): void {
        this.attachmentMenuOpen.set(true);
    }

    /** Close the attachment menu. */
    closeAttachmentMenu(): void {
        this.attachmentMenuOpen.set(false);
    }

    /** Open the OS file picker. */
    pickFile(): void {
        this.closeAttachmentMenu();
        this.fileInput?.nativeElement.click();
    }

    /** Capture a photo: hidden camera input on mobile, getUserMedia overlay on desktop. */
    pickCamera(): void {
        this.closeAttachmentMenu();
        if (this.isMobileDevice()) {
            this.cameraInput?.nativeElement.click();
        } else {
            void this.openDesktopCamera();
        }
    }

    /** Handler for both `<input type=file>` (file picker and mobile camera). */
    async onFilesSelected(event: Event): Promise<void> {
        const input = event.target as HTMLInputElement;
        if (!input.files || input.files.length === 0) return;
        const previews = await this.fileHandling.createFilePreviews(Array.from(input.files));
        this.chat.addAttachments(previews);
        // Allow re-selecting the same file later.
        input.value = '';
    }

    /** Drop one queued attachment. */
    removeAttachment(id: string): void {
        this.chat.removeAttachment(id);
    }

    /** Open the image preview dialog. */
    openImagePreview(preview: FilePreview): void {
        if (preview.type !== FileType.IMAGE || !preview.preview) return;
        this.imagePreviewName.set(preview.name);
        this.imagePreviewUrl.set(preview.preview);
    }

    closeImagePreview(): void {
        this.imagePreviewUrl.set(null);
        this.imagePreviewName.set('');
    }

    /** Begin a hold-to-record voice message session. */
    async startRecording(): Promise<void> {
        if (this.isRecording()) return;
        const config: RecordingConfig = {
            isHoldToRecord: true,
            maxDuration: 600,
            audioConstraints: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        };
        try {
            await this.voiceRecording.startRecording(config, this.waveformCanvas?.nativeElement);
        } catch (e: any) {
            // Surface a permission/hardware error in the same banner used by uploads.
            const msg = e?.name === 'NotAllowedError'
                ? this.transloco.translate('chat.composer.micDenied')
                : this.transloco.translate('chat.composer.micError');
            this.chat.attachmentError.set(msg);
        }
    }

    /** Stop recording and queue the resulting blob as an attachment. */
    async stopRecording(): Promise<void> {
        if (!this.isRecording()) return;
        const result = await this.voiceRecording.stopRecording();
        if (!result || result.duration < 1) return;
        const preview = await this.fileHandling.createAudioFilePreview(result);
        this.chat.addAttachments([preview]);
    }

    cancelRecording(): void {
        this.voiceRecording.cancelRecording();
    }

    /** Desktop camera path: live MediaStream in a fullscreen overlay; capture to JPEG. */
    private async openDesktopCamera(): Promise<void> {
        let stream: MediaStream | null = null;
        try {
            stream = await navigator.mediaDevices.getUserMedia({video: true});
        } catch (e: any) {
            const msg = e?.name === 'NotAllowedError'
                ? this.transloco.translate('chat.composer.cameraDenied')
                : this.transloco.translate('chat.composer.cameraError');
            this.chat.attachmentError.set(msg);
            return;
        }

        // The overlay is appended to document.body so it sits above the
        // app shell and isn't constrained by any scoped scroll containers.
        // That also means scoped component styles don't reach it — inline
        // styles below.
        const overlay = document.createElement('div');
        overlay.style.cssText =
            'position:fixed;inset:0;background:rgba(0,0,0,0.92);' +
            'z-index:9999;display:flex;flex-direction:column;align-items:center;' +
            'justify-content:center;gap:20px;';
        const video = document.createElement('video');
        video.srcObject = stream;
        video.autoplay = true;
        video.playsInline = true;
        video.style.cssText = 'max-width:90vw;max-height:70vh;border-radius:8px;background:#000;';
        const buttons = document.createElement('div');
        buttons.style.cssText = 'display:flex;gap:12px;';
        const captureBtn = document.createElement('button');
        captureBtn.type = 'button';
        captureBtn.textContent = this.transloco.translate('chat.composer.capturePhoto');
        captureBtn.style.cssText =
            'padding:10px 20px;font-size:14px;font-weight:500;border:none;' +
            'border-radius:6px;background:#3399D6;color:#fff;cursor:pointer;';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.textContent = this.transloco.translate('common.cancel');
        cancelBtn.style.cssText =
            'padding:10px 20px;font-size:14px;font-weight:500;border:none;' +
            'border-radius:6px;background:#444;color:#fff;cursor:pointer;';
        buttons.appendChild(captureBtn);
        buttons.appendChild(cancelBtn);
        overlay.appendChild(video);
        overlay.appendChild(buttons);
        document.body.appendChild(overlay);

        const cleanup = () => {
            stream?.getTracks().forEach((t) => t.stop());
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        };

        await new Promise<void>((resolve) => (video.onloadedmetadata = () => resolve()));

        captureBtn.onclick = () => {
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            const ctx = canvas.getContext('2d');
            if (!ctx) {
                cleanup();
                return;
            }
            ctx.drawImage(video, 0, 0);
            canvas.toBlob(
                async (blob) => {
                    if (blob) {
                        const file = new File([blob], `photo-${Date.now()}.jpg`, {
                            type: 'image/jpeg',
                            lastModified: Date.now(),
                        });
                        const previews = await this.fileHandling.createFilePreviews([file]);
                        this.chat.addAttachments(previews);
                    }
                    cleanup();
                },
                'image/jpeg',
                0.9,
            );
        };
        cancelBtn.onclick = cleanup;
        overlay.onclick = (e) => {
            if (e.target === overlay) cleanup();
        };
    }

    // ===== TTS playback =====

    /** Read state for a given turn key (always returns a defaulted object). */
    ttsStateFor(key: string): TtsMessageState {
        return (
            this.ttsState()[key] ?? {isGenerating: false, isPlaying: false, error: false}
        );
    }

    /** Mutate state for one turn key. */
    private setTtsState(key: string, patch: Partial<TtsMessageState>): void {
        this.ttsState.update((cur) => ({
            ...cur,
            [key]: {...this.ttsStateFor(key), ...patch},
        }));
    }

    /**
     * Play, pause, or generate-then-play TTS for an assistant turn's final text.
     *
     * - First click: fetch the audio (formulation + synthesis on the server),
     *   then start playback.
     * - Subsequent clicks: toggle play/pause on the cached blob.
     */
    async toggleTts(key: string, content: string): Promise<void> {
        const state = this.ttsStateFor(key);
        const threadId = this.chat.threadId();
        if (!threadId || !content.trim()) return;

        // Stop any other turn that's playing.
        if (this.currentTtsKey !== null && this.currentTtsKey !== key) {
            this.stopCurrentTts();
        }

        if (state.isPlaying) {
            this.stopCurrentTts();
            return;
        }

        if (state.audioUrl) {
            this.playCachedTts(key, state.audioUrl);
            return;
        }

        // Need to fetch the audio first.
        this.setTtsState(key, {isGenerating: true, error: false});
        const lang = this.i18n.activeLang().startsWith('de') ? 'de' : 'en';
        let result;
        try {
            result = await firstValueFrom(
                this.api.generateTTS(threadId, content, {language: lang, reformulate: true}),
            );
        } catch (e) {
            console.error('TTS generate threw', e);
            this.setTtsState(key, {isGenerating: false, error: true});
            return;
        }
        if (result === null || result === 'unavailable') {
            this.setTtsState(key, {
                isGenerating: false,
                error: result === null,
            });
            return;
        }
        const url = URL.createObjectURL(result);
        this.ttsBlobUrls.add(url);
        this.setTtsState(key, {isGenerating: false, audioUrl: url, error: false});
        this.playCachedTts(key, url);
    }

    private playCachedTts(key: string, url: string): void {
        if (!this.currentTtsAudio) {
            this.currentTtsAudio = new Audio();
            this.currentTtsAudio.addEventListener('ended', () => this.onTtsEnded());
            this.currentTtsAudio.addEventListener('pause', () => this.onTtsPaused());
            this.currentTtsAudio.addEventListener('error', () => this.onTtsError());
        }
        this.currentTtsAudio.src = url;
        this.currentTtsKey = key;
        this.setTtsState(key, {isPlaying: true});
        this.currentTtsAudio.play().catch((e) => {
            console.error('TTS playback failed', e);
            this.setTtsState(key, {isPlaying: false, error: true});
            this.currentTtsKey = null;
        });
    }

    private stopCurrentTts(): void {
        if (!this.currentTtsAudio || this.currentTtsKey === null) return;
        try {
            this.currentTtsAudio.pause();
            this.currentTtsAudio.currentTime = 0;
        } catch {
            // ignore
        }
        const k = this.currentTtsKey;
        this.currentTtsKey = null;
        this.setTtsState(k, {isPlaying: false});
    }

    private onTtsEnded(): void {
        if (this.currentTtsKey === null) return;
        const k = this.currentTtsKey;
        this.currentTtsKey = null;
        this.setTtsState(k, {isPlaying: false});
    }

    private onTtsPaused(): void {
        if (this.currentTtsKey === null || !this.currentTtsAudio) return;
        const a = this.currentTtsAudio;
        if (a.ended || a.currentTime === 0) return;
        const k = this.currentTtsKey;
        this.setTtsState(k, {isPlaying: false});
    }

    private onTtsError(): void {
        if (this.currentTtsKey === null) return;
        const k = this.currentTtsKey;
        this.currentTtsKey = null;
        this.setTtsState(k, {isPlaying: false, error: true});
    }

    // ===== Drag-and-drop file handling =====

    /** True when the dragged payload includes files (not text or HTML). */
    private hasFilePayload(event: DragEvent): boolean {
        const types = event.dataTransfer?.types;
        if (!types) return false;
        // Spec says types is DOMStringList; browsers expose Array-like.
        for (let i = 0; i < types.length; i++) {
            if (types[i] === 'Files') return true;
        }
        return false;
    }

    @HostListener('dragenter', ['$event'])
    onDragEnter(event: DragEvent): void {
        if (!this.hasFilePayload(event)) return;
        event.preventDefault();
        this.dragEnterCount++;
        if (this.dragEnterCount === 1) this.isDragOver.set(true);
    }

    @HostListener('dragover', ['$event'])
    onDragOver(event: DragEvent): void {
        if (!this.hasFilePayload(event)) return;
        // preventDefault on dragover is what tells the browser this is a
        // valid drop target. Without it, the drop event never fires.
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    }

    @HostListener('dragleave', ['$event'])
    onDragLeave(event: DragEvent): void {
        if (!this.hasFilePayload(event)) return;
        this.dragEnterCount = Math.max(0, this.dragEnterCount - 1);
        if (this.dragEnterCount === 0) this.isDragOver.set(false);
    }

    @HostListener('drop', ['$event'])
    async onDrop(event: DragEvent): Promise<void> {
        if (!this.hasFilePayload(event)) return;
        event.preventDefault();
        this.dragEnterCount = 0;
        this.isDragOver.set(false);

        const files = event.dataTransfer?.files;
        if (!files || files.length === 0) return;
        // Honour the same gating as the file picker — if the session is
        // disconnected, the upload would fail anyway.
        if (!this.chat.isConnected()) return;

        const previews = await this.fileHandling.createFilePreviews(Array.from(files));
        if (previews.length > 0) this.chat.addAttachments(previews);
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
        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        this.autoScroll = nearBottom;
        this.scrolledAway.set(!nearBottom);
        if (nearBottom) {
            this.newMessageCount.set(0);
        }
    }

    jumpToLatest(): void {
        this.autoScroll = true;
        this.scrolledAway.set(false);
        this.newMessageCount.set(0);
        this.lastSeenMessageCount = this.chat.turns().length;
        this.scrollToBottom();
    }

    selectSlashCommand(cmd: SlashCommand): void {
        this.inputText = cmd.command + ' ';
        this.showSlashMenu.set(false);
        this.inputEl?.nativeElement?.focus();
    }

    pickSuggestion(s: DisplayedSuggestion): void {
        this.inputText = s.text;
        setTimeout(() => {
            this.inputEl?.nativeElement?.focus();
            this.autoResizeInput();
        });
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

    async disconnectAndLeave(): Promise<void> {
        try {
            await this.chat.endSession();
        } catch (e: any) {
            this.toast.danger(this.errors.translate(e, 'errors.sessions.endFailed'));
        } finally {
            this.router.navigate(['/sessions']);
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

    // ===== Turn-bubble helpers =====

    /**
     * Auto-collapse threshold: assistant turns with more than this many events
     * collapse to the headline-only view by default once they're done. Streaming
     * turns are never auto-collapsed. The user can override either way via the
     * chevron, in which case userTurnCollapsed wins.
     */
    private readonly AUTO_COLLAPSE_THRESHOLD = 8;

    /**
     * Per-turn user override for collapse state. Keyed by turn id. Value is
     * `true` when the user explicitly collapsed it, `false` when they explicitly
     * expanded it. Absent = use the auto rule.
     */
    private readonly userTurnCollapsed = signal<Record<string, boolean>>({});

    /** Whether the given assistant turn should render in collapsed mode. */
    isTurnCollapsed(turn: AssistantTurn): boolean {
        const explicit = this.userTurnCollapsed()[turn.id];
        if (explicit !== undefined) return explicit;
        if (turn.status === 'streaming') return false;
        return turn.events.length > this.AUTO_COLLAPSE_THRESHOLD;
    }

    /** Toggle the user-explicit collapse state for a turn. */
    toggleTurnCollapse(turn: AssistantTurn): void {
        const wasCollapsed = this.isTurnCollapsed(turn);
        this.userTurnCollapsed.update((cur) => ({...cur, [turn.id]: !wasCollapsed}));
    }

    /** Per-type event counts for the chevron badge. */
    turnEventCounts(turn: AssistantTurn) {
        return countEvents(turn);
    }

    /** Last text event in a turn — used as the collapsed-view headline. */
    lastTextEvent(turn: AssistantTurn): TextEvent | undefined {
        return lastTextOf(turn);
    }

    /**
     * True when the current turn is historical and the next turn isn't —
     * the boundary between session reload and live activity.
     */
    showSessionDividerAfter(turn: Turn, index: number): boolean {
        const next = this.chat.turns()[index + 1];
        if (!next) return false;
        const turnHistorical = (turn.kind === 'assistant' || turn.kind === 'user') && !!turn.historical;
        const nextHistorical = (next.kind === 'assistant' || next.kind === 'user') && !!next.historical;
        return turnHistorical && !nextHistorical;
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

    permissionTitle(perm: PermissionRequest): string {
        return this.toolLabel({...perm, status: 'pending'} as ToolCallInfo);
    }

    formatTime(d: Date | string | number): string {
        const date = d instanceof Date ? d : new Date(d);
        return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
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

    statusIcon(status: string): string {
        switch (status) {
            case 'completed': return 'check_circle';
            case 'running': return 'progress_activity';
            case 'denied': return 'block';
            case 'pending': return 'radio_button_unchecked';
            case 'error': return 'error';
            default: return 'help';
        }
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
        if (calls.some(tc => tc.status === 'error')) return 'error';
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
        return calls.some(tc => tc.status === 'completed' || tc.status === 'denied' || tc.status === 'error');
    }

    completedOnly(calls: ToolCallInfo[]): ToolCallInfo[] {
        return calls.filter(tc => tc.status === 'completed' || tc.status === 'denied' || tc.status === 'error');
    }

    completedToolCount(calls: ToolCallInfo[]): number {
        return calls.filter(tc => tc.status === 'completed' || tc.status === 'denied' || tc.status === 'error').length;
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
