import {
    afterNextRender,
    AfterViewChecked,
    Component,
    computed,
    effect,
    ElementRef,
    HostListener,
    inject,
    Injector,
    OnDestroy,
    OnInit,
    QueryList,
    signal,
    ViewChild,
    ViewChildren,
} from '@angular/core';
import {DecimalPipe, NgTemplateOutlet, TitleCasePipe} from '@angular/common';
import {HttpClient} from '@angular/common/http';
import {FormsModule} from '@angular/forms';
import {Router, RouterLink} from '@angular/router';
import {firstValueFrom, Subscription} from 'rxjs';
import {MarkdownComponent} from 'ngx-markdown';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ChatAttachment, PermissionRequest, PersistentChatService, RunningToolInfo, ToolCallInfo,} from '../../core/services/persistent-chat.service';
import {
    AssistantTurn,
    countEvents,
    EventGroup,
    firstSentence,
    firstTextOf,
    groupEvents,
    isAssistantTurn,
    isSystemTurn,
    isUserTurn,
    lastTextOf,
    TextEvent,
    ThoughtEvent,
    ToolCallEvent,
    trailingText,
    Turn,
    TurnEvent,
} from '../../core/models/turn.model';
import {DiffLine, lineDiff} from '../../core/util/line-diff';
import {ApiService, IdeSessionStatus} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import {I18nService} from '../../core/services/i18n.service';
import {FileHandlingService} from '../../core/services/file-handling.service';
import {ChatPreferencesService} from '../../core/services/chat-preferences.service';
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
    isGenerating: boolean;
    error: boolean;
    /** The spoken (rewritten) text read aloud — all chunks joined — shown in
     *  the collapsible "Spoken version" when it differs from the message. */
    text?: string;
    // Each section is its own player. A long message plans into ordered chunks;
    // we synthesize them in sequence, render a player as each becomes ready, and
    // auto-advance between them while keeping every section individually replayable.
    chunks?: string[];
    /** Blob URL per chunk, filled as each is synthesized (undefined until then). */
    chunkUrls?: (string | undefined)[];
    /** Index currently being synthesized (drives the "Generating part N" status). */
    synthIndex?: number;
    /** Index that should start playing the moment its player is available — set
     *  when the previous section ends before the next has finished synthesizing. */
    playPending?: number;
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

/**
 * The running-command card to surface on (re)attach, or null. Suppressed when
 * the in-flight tool call is already rendered inside a visible turn (warm
 * reconnect) so it isn't double-shown; surfaced when it isn't (cold reload
 * mid-turn, where the in-flight turn isn't in REST history yet).
 */
export function pickRunningCommandCard(
    runningTool: RunningToolInfo | null,
    turns: readonly Turn[],
): RunningToolInfo | null {
    if (!runningTool) return null;
    for (const turn of turns) {
        if (!isAssistantTurn(turn)) continue;
        for (const ev of turn.events) {
            if (ev.kind === 'tool_call' && ev.id === runningTool.id) return null;
        }
    }
    return runningTool;
}

/**
 * Decide whether the IDE button should open code-server, and to which URL.
 * Returns the code-server URL only when the workspace is active; null
 * otherwise — including when the pre-flight status fetch returned null (a
 * swallowed 401 the auth interceptor has already turned into a re-login).
 */
export function pickCodeServerUrlToOpen(status: IdeSessionStatus | null): string | null {
    return status?.status === 'active' && status.code_server_url
        ? status.code_server_url
        : null;
}

/**
 * Pull file payloads out of a paste's clipboard items (#11 paste-to-attach),
 * dropping the `kind: 'string'` entries (plain text / HTML) so a text paste
 * falls through to the textarea untouched. Clipboard images frequently arrive
 * as a nameless blob — synthesize a stable, collision-free filename so the
 * attachment chip has a label and the agent hint reads sensibly. Pure and
 * DOM-creation-free (takes the item list, takes `now` instead of calling
 * Date.now()) so the selection logic is unit-testable; the component handler
 * does the async preview + signal update.
 */
export function extractClipboardFiles(
    items: DataTransferItemList | null | undefined,
    now: number,
): File[] {
    if (!items) return [];
    const files: File[] = [];
    for (const item of Array.from(items)) {
        if (item.kind !== 'file') continue;
        const file = item.getAsFile();
        if (!file) continue;
        if (file.name) {
            files.push(file);
        } else {
            const ext = (file.type.split('/')[1] || 'bin').split(';')[0];
            files.push(
                new File([file], `pasted-${now}-${files.length}.${ext}`, {
                    type: file.type,
                    lastModified: now,
                }),
            );
        }
    }
    return files;
}

/**
 * Whether a run of consecutive tool calls should render as the folded
 * "N× tool calls" disclosure vs. plain inline cards. A run folds only when the
 * user hasn't chosen the always-inline "Tool calls → Expanded" preference AND
 * it's long enough to be worth grouping (Slice 3 threshold). When expanded, no
 * run folds — every call renders inline with no fold control, like a short run.
 */
export function shouldFoldToolRun(
    toolCount: number,
    toolCallsExpanded: boolean,
    threshold: number,
): boolean {
    return !toolCallsExpanded && toolCount >= threshold;
}

/** Structured diff/content view for a file-mutating tool card (#7). */
interface FileEditView {
    path: string;
    /** Drives the header label + icon: 'replace' renders a true diff;
     *  the rest are all-additions (no "before" is available). */
    mode: 'replace' | 'append' | 'prepend' | 'write';
    lines: DiffLine[];
    /** Lines dropped by the render cap, if any (shown as a "+N more" footer). */
    truncated: number;
}

@Component({
    selector: 'app-persistent-chat',
    standalone: true,
    imports: [
        FormsModule,
        NgTemplateOutlet,
        TitleCasePipe,
        DecimalPipe,
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
                <button class="ide-btn" (click)="openCodeServer()" [title]="'chat.header.ideActiveTooltip' | transloco">
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
          @if (chat.agentSilenceSeconds() >= 30 && !chat.compaction()) {
            <app-badge tone="warning" size="sm">{{ 'chat.status.agentQuiet' | transloco:{ seconds: chat.agentSilenceSeconds() } }}</app-badge>
          }
          @if (chat.compaction(); as comp) {
            <app-badge tone="warning" size="sm">{{ 'chat.compactionLive.footer' | transloco:{ current: comp.currentPass > 0 ? comp.currentPass : 1, total: comp.nPasses, elapsed: compactionElapsed() } }}</app-badge>
          }
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
            <label class="settings-label">{{ 'chat.settings.reasoning' | transloco }}</label>
            <app-select size="sm" [fullWidth]="false"
                        [value]="chatPrefs.reasoningExpanded() ? 'expanded' : 'collapsed'"
                        (changed)="onReasoningDefaultChange($event)">
              <option value="expanded">{{ 'chat.settings.reasoningExpanded' | transloco }}</option>
              <option value="collapsed">{{ 'chat.settings.reasoningCollapsed' | transloco }}</option>
            </app-select>
          </div>
          <div class="settings-row">
            <label class="settings-label">{{ 'chat.settings.toolCalls' | transloco }}</label>
            <app-select size="sm" [fullWidth]="false"
                        [value]="chatPrefs.toolCallsExpanded() ? 'expanded' : 'collapsed'"
                        (changed)="onToolCallsDefaultChange($event)">
              <option value="expanded">{{ 'chat.settings.toolCallsExpanded' | transloco }}</option>
              <option value="collapsed">{{ 'chat.settings.toolCallsCollapsed' | transloco }}</option>
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
              @if (fileEditView(tc); as fev) {
                <!-- #7: diff/content view for edit_file/write_file, built from
                     the call args. 'replace' is a true old→new diff; the rest
                     are all-additions (no "before" available). -->
                <div class="tool-body tool-diff">
                  <div class="diff-head">
                    <app-icon size="sm" class="diff-mode-icon">{{ fev.mode === 'write' ? 'note_add' : 'difference' }}</app-icon>
                    <span class="diff-mode">{{ ('chat.diff.' + fev.mode) | transloco }}</span>
                    @if (fev.path) {
                      <span class="diff-path">{{ fev.path }}</span>
                    }
                  </div>
                  <div class="diff-body">
                    @for (ln of fev.lines; track $index) {
                      <div class="diff-line" [class.add]="ln.type === 'add'" [class.del]="ln.type === 'del'">
                        <span class="diff-sign">{{ diffSign(ln.type) }}</span><span class="diff-text">{{ ln.text }}</span>
                      </div>
                    }
                  </div>
                  @if (fev.truncated > 0) {
                    <div class="diff-truncated">{{ 'chat.diff.truncated' | transloco:{count: fev.truncated} }}</div>
                  }
                </div>
              } @else if (tc.result) {
                <div class="tool-body"><pre class="tool-result">{{ tc.result }}</pre></div>
              }
            </details>
          }
        </div>
      </ng-template>

      <!-- Per-event card templates (referenced by the turn loop below). -->
      <ng-template #thoughtCard let-event>
        <details class="thinking-block event-thought" [attr.open]="chatPrefs.reasoningExpanded() ? '' : null">
          <summary class="thinking-header">
            <app-icon size="sm" class="thinking-icon">psychology</app-icon>
            <span class="thinking-label">
              {{ (event.status === 'streaming' ? 'chat.thinking.now' : 'chat.thinking.past') | transloco }}
            </span>
          </summary>
          <div class="thinking-content">
            <markdown [data]="event.content"></markdown>
          </div>
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
        <!-- Centered reading column: caps prose line length while the scrollbar
             stays at the pane edge. .jump-latest is kept OUTSIDE this wrapper so
             it floats over the scroll container (sticky + align-self:center). -->
        <div class="messages-inner">
        @for (turn of chat.visibleTurns(); track turn.id; let isLast = $last) {
          @switch (turn.kind) {
            @case ('system') {
              <div class="message message-system">
                <div class="system-message">
                  <app-icon size="sm" class="system-icon">info</app-icon>
                  {{ turn.content }}
                </div>
              </div>
            }
            @case ('compaction') {
              <!-- Compaction boundary: divider banner (reuses .session-divider),
                   expandable to the summary so the user sees the agent's state. -->
              <div class="session-divider">
                <span class="divider-line"></span>
                <span class="divider-text">{{ 'chat.compaction.banner' | transloco }}</span>
                <span class="divider-line"></span>
              </div>
              @if (turn.summary) {
                <details class="compaction-summary">
                  <summary>{{ 'chat.compaction.viewSummary' | transloco }}</summary>
                  <!-- The agent's summary is markdown (headings, lists, code). Render it
                       as such, reusing the chat's markdown cascade via the message-body
                       class (its base styles are benign; the bubble styling is gated on
                       .message-user/.message-assistant, which this isn't). -->
                  <div class="compaction-summary-body message-body">
                    <markdown [data]="turn.summary"></markdown>
                  </div>
                </details>
              }
            }
            @case ('user') {
              <div class="message message-user" [class.historical]="turn.historical">
                <div class="avatar">
                  <app-icon size="sm" class="avatar-icon">person</app-icon>
                </div>
                <div class="message-body">
                  @if (turn.content) {
                    @if (isUserMessageLong(turn.content)) {
                      <details class="user-text-collapsible">
                        <summary class="user-text-summary">
                          <span class="user-text-preview">{{ userMessagePreview(turn.content) }}</span>
                          <span class="user-text-hint user-text-hint-closed">[…]</span>
                          <span class="user-text-hint user-text-hint-open">▴</span>
                        </summary>
                        <div class="user-text">{{ turn.content }}</div>
                      </details>
                    } @else {
                      <div class="user-text">{{ turn.content }}</div>
                    }
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
                  <!-- Whole-turn chevron: folds the lead-up (reasoning + tool
                       calls) behind the per-type badge summary, leaving the
                       final answer visible. Hidden when the turn has 0–1 events
                       (nothing to collapse). -->
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
                    <!-- Collapsed: fold the lead-up (opening text, reasoning,
                         tool calls) but keep the final answer — the prose after
                         the last tool/thought — fully rendered as markdown (#8
                         refinement). The chevron + count badge signal the hidden
                         work. When the turn ends on a tool/thought (no closing
                         prose), fall back to a one-line headline (plain text so
                         the truncate mixin works; markdown emits block elements
                         that defeat nowrap). -->
                    @let answer = finalAnswer(turn);
                    @if (answer) {
                      <div class="event-text turn-final-answer">
                        <markdown [data]="answer"></markdown>
                      </div>
                    } @else {
                      <span class="turn-headline">{{ collapsedHeadline(turn) }}</span>
                    }
                  } @else {
                    <!-- Expanded: events rendered as cards, with consecutive
                         tool runs grouped (#10). A run of TOOL_GROUP_THRESHOLD+
                         tools collapses into one disclosure; shorter runs and
                         every thought/text render individually. -->
                    @for (group of groupedEvents(turn); track group.id) {
                      @if (group.kind === 'tools') {
                        @if (foldToolRun(group.tools)) {
                          <!-- Folded run: cornerless "N× tool calls", auto-open on error/denied.
                               Suppressed entirely when Tool calls → Expanded (every run inline). -->
                          <details class="tool-group" [attr.open]="toolGroupHasProblem(group.tools) ? '' : null">
                            <summary class="tool-group-head">
                              <app-icon size="sm" class="tool-group-chevron">chevron_right</app-icon>
                              <span class="tool-group-label">{{ 'chat.turn.toolGroup' | transloco:{count: group.tools.length} }}</span>
                              <span class="tool-group-names">{{ toolGroupSummary(group.tools) }}</span>
                            </summary>
                            <div class="tool-group-body">
                              <ng-container [ngTemplateOutlet]="toolDetails" [ngTemplateOutletContext]="{ $implicit: group.tools }"></ng-container>
                            </div>
                          </details>
                        } @else {
                          <!-- Short run: each tool inline, exactly as before. -->
                          @for (event of group.tools; track event.id) {
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
                      } @else {
                        @switch (group.event.kind) {
                          @case ('thought') {
                            @if (chat.narrationMode() !== 'silent') {
                              <ng-container [ngTemplateOutlet]="thoughtCard" [ngTemplateOutletContext]="{ $implicit: group.event }"></ng-container>
                            }
                          }
                          @case ('text') {
                            <div class="event-text">
                              <markdown [data]="group.event.content"></markdown>
                            </div>
                          }
                          @case ('compaction') {
                            <!-- Mid-turn compaction marker at its true position
                                 in the event stream (same divider + expandable
                                 summary as the between-turns banner). -->
                            <div class="session-divider event-compaction">
                              <span class="divider-line"></span>
                              <span class="divider-text">{{ 'chat.compaction.banner' | transloco }}</span>
                              <span class="divider-line"></span>
                            </div>
                            @if (group.event.summary) {
                              <details class="compaction-summary">
                                <summary>{{ 'chat.compaction.viewSummary' | transloco }}</summary>
                                <div class="compaction-summary-body message-body">
                                  <markdown [data]="group.event.summary"></markdown>
                                </div>
                              </details>
                            }
                          }
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

                  <!-- Read aloud: the button generates speech; once generated it
                       is replaced by a native <audio> player, with the spoken
                       (markdown-stripped) text in a collapsible panel below. -->
                  @if (last && !streaming) {
                    @if (!ttsS.chunks) {
                      @if (ttsS.isGenerating) {
                        <!-- Planning: spinner + status, before any section exists. -->
                        <div class="tts-prep">
                          <span class="action-spinner-sm"></span>
                          <span class="tts-status-text">{{ 'chat.tts.preparing' | transloco }}</span>
                        </div>
                      } @else {
                        <!-- The read button (or an error to retry). -->
                        <div class="message-actions">
                          <button
                            type="button"
                            class="msg-action-btn tts-btn"
                            [class.is-error]="ttsS.error"
                            [title]="(ttsS.error ? 'chat.tts.error' : 'chat.tts.play') | transloco"
                            (click)="toggleTts(ttsKey, last.content)"
                          >
                            @if (ttsS.error) {
                              <app-icon size="md">error_outline</app-icon>
                            } @else {
                              <app-icon size="md">volume_up</app-icon>
                            }
                          </button>
                        </div>
                      }
                    } @else {
                      <!-- One player per section, each appearing as it's ready,
                           with a spinner + "Generating part N" trailing below. -->
                      <div class="tts-players">
                        @for (chunk of ttsS.chunks; track $index) {
                          @if (ttsS.chunkUrls?.[$index]; as url) {
                            <div class="tts-player-row">
                              <audio
                                #ttsAudioEl
                                class="tts-player"
                                controls
                                preload="metadata"
                                [attr.data-tts-key]="ttsKey"
                                [attr.data-tts-index]="$index"
                                [src]="url"
                                (loadedmetadata)="onPlayerReady($event, ttsKey, $index)"
                                (play)="onPlayerPlay($event)"
                                (ended)="onChunkEnded(ttsKey, $index)"
                              ></audio>
                              @if (ttsS.chunks.length > 1) {
                                <span class="tts-part">{{ 'chat.tts.part' | transloco:{ current: $index + 1, total: ttsS.chunks.length } }}</span>
                              }
                            </div>
                          }
                        }
                        @if (ttsS.isGenerating) {
                          <div class="tts-status">
                            <span class="action-spinner-sm"></span>
                            <span class="tts-status-text">{{ 'chat.tts.generatingPart' | transloco:{ current: (ttsS.synthIndex ?? 0) + 1, total: ttsS.chunks.length } }}</span>
                          </div>
                        }
                      </div>
                    }
                    <!-- Spoken version: only shown when formulation actually
                         rewrote the text (otherwise it would just mirror the
                         message bubble). Collapsed by default, like reasoning. -->
                    @if (ttsS.chunks && ttsS.text && ttsS.text.trim() !== last.content.trim()) {
                      <details class="thinking-block tts-spoken">
                        <summary class="thinking-header">
                          <app-icon size="sm" class="thinking-icon">graphic_eq</app-icon>
                          <span class="thinking-label">{{ 'chat.tts.spokenVersion' | transloco }}</span>
                        </summary>
                        <div class="thinking-content tts-spoken-text">{{ ttsS.text }}</div>
                      </details>
                    }
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

        <!-- Running-command card — shown on (re)attach when the agent is
             blocked in a tool call that isn't already rendered in a visible
             turn (cold reload mid-turn: the in-flight turn isn't in REST
             history yet). Reuses the .mile marker styles (no new SCSS). -->
        @if (runningCommandCard(); as rc) {
          <div class="mile">
            <div class="mile-label">{{ 'chat.stream.running' | transloco:{ tool: rc.tool } }}</div>
            <div class="mile-detail">
              <app-icon size="sm" class="mile-detail-icon">progress_activity</app-icon>
              @if (formatToolArgs(rc.args); as a) {
                <code class="mile-args">{{ a }}</code>
              }
            </div>
            <div class="mile-title">{{ 'chat.stream.waitingForCommand' | transloco }}</div>
          </div>
        }

        <!-- Live context-compaction progress (compaction.started/progress
             frames; cleared by context.compacted / compaction.failed).
             Segmented bar for ≤20 passes, continuous bar + counter above —
             the UI must render fine for ANY pass count. -->
        @if (chat.compaction(); as comp) {
          <div class="compaction-progress" role="status">
            <div class="compaction-header">
              <span class="action-spinner-sm" aria-hidden="true"></span>
              <span class="compaction-title">{{ 'chat.compactionLive.title' | transloco }}</span>
              <span class="compaction-meta">
                {{ comp.trigger }}
                @if (comp.ctxUsedPct != null) {
                  · {{ 'chat.compactionLive.ctx' | transloco:{ pct: comp.ctxUsedPct } }}
                }
              </span>
              @if (comp.ctxUsedTokens != null && comp.ctxLimitTokens != null) {
                <span class="compaction-tokens">{{ formatTokens(comp.ctxUsedTokens) }} / {{ formatTokens(comp.ctxLimitTokens) }} tok</span>
              }
            </div>
            <div class="compaction-bar">
              @if (compactionSegments().length) {
                @for (seg of compactionSegments(); track seg) {
                  <span class="compaction-segment"
                        [class.done]="seg < comp.currentPass"
                        [class.active]="seg === comp.currentPass"></span>
                }
              } @else {
                <span class="compaction-segment continuous">
                  <span class="compaction-fill"
                        [style.width.%]="comp.nPasses > 0 ? (100 * (comp.currentPass > 0 ? comp.currentPass - 1 : 0) / comp.nPasses) : 0"></span>
                </span>
              }
            </div>
            <div class="compaction-detail">
              <span class="compaction-pass">
                @if (comp.currentPass > 0) {
                  {{ 'chat.compactionLive.pass' | transloco:{ current: comp.currentPass, total: comp.nPasses, first: comp.firstMsg ?? '?', last: comp.lastMsg ?? '?' } }}
                  @if (comp.attempt > 1) {
                    <span class="compaction-retry">{{ 'chat.compactionLive.retry' | transloco:{ attempt: comp.attempt } }}</span>
                  }
                } @else {
                  {{ 'chat.compactionLive.planning' | transloco }}
                }
              </span>
              @if (comp.inTokens != null) {
                <span class="compaction-reduction">
                  {{ formatTokens(comp.inTokens) }} →
                  @if (comp.outTokens != null) {
                    {{ formatTokens(comp.outTokens) }}
                  } @else {
                    …
                  }
                </span>
              }
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

        </div><!-- /.messages-inner -->

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
        <!-- Live token telemetry (usage.updated frames): latest context fill
             + cumulative output/reasoning for the running turn. -->
        @if (chat.usage(); as u) {
          <div class="usage-panel" aria-hidden="true">
            @if (u.inputTokens != null) {
              <span class="usage-item"><span class="usage-label">{{ 'chat.usage.input' | transloco }}</span>{{ u.inputTokens | number }}</span>
            }
            @if (u.reasoningTokensTurn > 0) {
              <span class="usage-item"><span class="usage-label">{{ 'chat.usage.reasoning' | transloco }}</span>{{ u.reasoningTokensTurn | number }}</span>
            }
            <span class="usage-item"><span class="usage-label">{{ 'chat.usage.output' | transloco }}</span>{{ u.outputTokensTurn | number }}</span>
            @if (usageCtxPct() != null) {
              <span class="usage-item"><span class="usage-label">{{ 'chat.usage.ctx' | transloco }}</span>
                <span class="usage-gauge"><span class="usage-gauge-fill" [class.hot]="usageCtxPct()! >= 80" [style.width.%]="usageCtxPct()"></span></span>
                <span [class.usage-hot]="usageCtxPct()! >= 80">{{ usageCtxPct() }}%</span>
              </span>
            }
          </div>
        }
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

          <!-- Transcribing indicator: brief, after a recording is confirmed -->
          @if (isTranscribing()) {
            <div class="transcribing-hint">
              <span class="action-spinner-sm" aria-hidden="true"></span>
              <span>{{ 'chat.composer.transcribing' | transloco }}</span>
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
              (paste)="onPaste($event)"
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
                [disabled]="!chat.isConnected() || isTranscribing()"
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
    readonly chatPrefs = inject(ChatPreferencesService);
    private readonly deviceCapabilities = inject(DeviceCapabilitiesService);
    private readonly voiceRecording = inject(VoiceRecordingService);
    private readonly router = inject(Router);
    private readonly toast = inject(AppToastService);
    private readonly errors = inject(ErrorMessageService);
    private readonly injector = inject(Injector);

    /**
     * The running-command card to show on (re)attach (or null). Surfaces the
     * agent's in-flight tool call when it isn't already visible in a turn — see
     * pickRunningCommandCard.
     */
    readonly runningCommandCard = computed(() =>
        pickRunningCommandCard(this.chat.runningTool(), this.chat.turns()),
    );

    // --- Live compaction progress (docs/features/context_summarization_rework.md S3)
    /** 1s tick driving the elapsed timer; interval runs only mid-compaction. */
    private readonly compactionNow = signal(Date.now());
    private compactionTimer: ReturnType<typeof setInterval> | null = null;

    readonly compactionElapsed = computed(() => {
        const comp = this.chat.compaction();
        if (!comp) return '0:00';
        const secs = Math.max(0, Math.floor((this.compactionNow() - comp.startedAt) / 1000));
        const m = Math.floor(secs / 60);
        return `${m}:${(secs % 60).toString().padStart(2, '0')}`;
    });

    /** One segment per pass for small counts; [] switches the template to the
     * continuous bar — a 9000-pass compaction must render fine too. */
    readonly compactionSegments = computed(() => {
        const comp = this.chat.compaction();
        if (!comp || comp.nPasses < 1 || comp.nPasses > 20) return [] as number[];
        return Array.from({length: comp.nPasses}, (_, i) => i + 1);
    });

    formatTokens(n: number): string {
        return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
    }

    /** Context fill % for the usage panel gauge (null until usage known). */
    readonly usageCtxPct = computed(() => {
        const u = this.chat.usage();
        if (!u || u.inputTokens == null || !u.ctxLimitTokens) return null;
        return Math.min(100, Math.round((100 * u.inputTokens) / u.ctxLimitTokens));
    });

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
    readonly isTranscribing = signal(false);
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
    // The native <audio> players (one per generated turn). Used to pause the
    // others when one starts — browsers happily play several at once otherwise.
    @ViewChildren('ttsAudioEl')
    private ttsPlayers?: QueryList<ElementRef<HTMLAudioElement>>;
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
    /** Suppresses the scroll handler while restoring position after prepending older turns. */
    private isRestoringScroll = false;

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

        // Elapsed-timer tick, only while a compaction is in flight.
        effect(() => {
            const active = this.chat.compaction() !== null;
            if (active && this.compactionTimer === null) {
                this.compactionNow.set(Date.now());
                this.compactionTimer = setInterval(
                    () => this.compactionNow.set(Date.now()),
                    1000,
                );
            } else if (!active && this.compactionTimer !== null) {
                clearInterval(this.compactionTimer);
                this.compactionTimer = null;
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

        // Attachment chips grow the composer the same way a multi-line draft
        // does, shrinking the .messages viewport. Re-pin to the latest turn when
        // the queue changes (Attach button, paste, or drag-drop) so adding a
        // file never hides the most recent history. Gated on autoScroll so a
        // user who scrolled up to read older turns isn't yanked back down.
        effect(() => {
            this.chat.pendingAttachments().length;
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
                this.chat.growWindow(delta); // anchor the visible top while scrolled away
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
        if (this.compactionTimer) {
            clearInterval(this.compactionTimer);
            this.compactionTimer = null;
        }
        this.capabilitiesSub?.unsubscribe();
        this.recordingStateSub?.unsubscribe();
        if (this.isRecording()) {
            this.voiceRecording.cancelRecording();
        }
        // Free TTS blob URLs. The native <audio> elements are torn down with
        // the component's DOM, which stops any in-flight playback.
        this.ttsBlobUrls.forEach((url) => URL.revokeObjectURL(url));
        this.ttsBlobUrls.clear();
    }

    autoResizeInput(): void {
        const el = this.inputEl?.nativeElement;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = el.scrollHeight + 'px';
        // The composer and the message list are flex siblings, so growing the
        // textarea shrinks the .messages viewport from the bottom. Re-pin to the
        // latest turn — only when the user was already following the bottom — so
        // a multi-line draft never scrolls the most recent history out of view.
        if (this.autoScroll) {
            setTimeout(() => this.scrollToBottom(), 0);
        }
    }

    send(): void {
        const text = this.inputText.trim();
        if (!text && this.chat.pendingAttachments().length === 0) return;

        this.showSlashMenu.set(false);
        // Clear textarea immediately — sendMessage is async because of uploads.
        this.inputText = '';
        this.autoScroll = true;
        // Fire-and-forget. On a hard send failure the service rolls back the
        // optimistic bubble; restore the draft so the user can retry (unless
        // they've already started typing a new one). The error banner set by
        // the service explains why.
        void this.chat.sendMessage(text).then((ok) => {
            if (ok === false && !this.inputText.trim()) {
                this.inputText = text;
            }
        });

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

    /**
     * Paste-to-attach (#11): when the clipboard carries files — a screenshot,
     * a copied image, or a file from the OS file manager — divert them into
     * the same attachment flow as the Attach button and drag-drop, rather than
     * letting the browser paste a data URL (or nothing) into the textarea.
     * Plain-text pastes carry no file items, so they fall through untouched.
     */
    async onPaste(event: ClipboardEvent): Promise<void> {
        const files = extractClipboardFiles(event.clipboardData?.items, Date.now());
        if (files.length === 0) return; // text paste — let the default run
        event.preventDefault();
        const previews = await this.fileHandling.createFilePreviews(files);
        if (previews.length > 0) this.chat.addAttachments(previews);
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

    /**
     * Stop recording, transcribe the clip to editable composer text, and keep
     * the audio attached.
     *
     * The transcript is appended into `inputText` (never clobbering a typed
     * draft). Reactivity note: `inputText` is a plain field, so assigning it
     * does NOT re-evaluate the `canSend` computed on its own — the
     * `addAttachments` call below writes the `pendingAttachments` signal, which
     * both enables Send and triggers the change-detection pass that re-syncs the
     * textarea to show the transcript. Keep `addAttachments` as the last step.
     */
    async stopRecording(): Promise<void> {
        if (!this.isRecording()) return;
        const result = await this.voiceRecording.stopRecording();
        if (!result || result.duration < 1) return;
        const preview = await this.fileHandling.createAudioFilePreview(result);

        const threadId = this.chat.threadId();
        if (threadId) {
            this.isTranscribing.set(true);
            try {
                const res = await firstValueFrom(
                    this.api.transcribeVoice(threadId, preview.file),
                );
                if (res && res !== 'unavailable' && res.text.trim()) {
                    const transcript = res.text.trim();
                    this.inputText = this.inputText.trim()
                        ? `${this.inputText.trim()}\n\n${transcript}`
                        : transcript;
                    queueMicrotask(() => this.autoResizeInput());
                } else if (res === null) {
                    // Transport/server error — keep the audio, surface a notice.
                    this.chat.attachmentError.set(
                        this.transloco.translate('chat.composer.transcribeError'),
                    );
                }
                // 'unavailable' (no STT model configured) → attach audio silently.
            } finally {
                this.isTranscribing.set(false);
            }
        }

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
        return this.ttsState()[key] ?? {isGenerating: false, error: false};
    }

    /** Mutate state for one turn key. */
    private setTtsState(key: string, patch: Partial<TtsMessageState>): void {
        this.ttsState.update((cur) => ({
            ...cur,
            [key]: {...this.ttsStateFor(key), ...patch},
        }));
    }

    /**
     * Read an assistant turn's final text aloud. Plans the message into ordered
     * chunks (server cleans + splits at natural breakpoints), then synthesizes
     * them in sequence — rendering a player as each section becomes ready and
     * auto-advancing between them. Every section stays individually playable, so
     * you can scrub back and forth afterwards.
     */
    async toggleTts(key: string, content: string): Promise<void> {
        const threadId = this.chat.threadId();
        if (!threadId || !content.trim()) return;
        const state = this.ttsStateFor(key);
        if (state.isGenerating || state.chunks) return; // already running or done

        this.setTtsState(key, {isGenerating: true, error: false});
        let plan;
        try {
            plan = await firstValueFrom(this.api.planTTS(threadId, content));
        } catch (e) {
            console.error('TTS plan threw', e);
            this.setTtsState(key, {isGenerating: false, error: true});
            return;
        }
        // 'unavailable' (204) = no TTS model configured → stay silent, no error.
        // null = a real failure → show the error state.
        if (plan === null || plan === 'unavailable') {
            this.setTtsState(key, {isGenerating: false, error: plan === null});
            return;
        }
        if (plan.length === 0) {
            this.setTtsState(key, {isGenerating: false});
            return;
        }
        this.setTtsState(key, {
            chunks: plan,
            chunkUrls: new Array(plan.length),
            text: plan.join('\n\n'),
            playPending: 0, // the first section autoplays once it loads
        });
        // Synthesize sections in order; each renders as it's ready and the
        // first autoplays. A later failure just stops the chain (earlier
        // sections stay playable); the first failing is a hard error.
        for (let i = 0; i < plan.length; i++) {
            this.setTtsState(key, {synthIndex: i});
            const url = await this.synthTtsChunk(key, threadId, i);
            if (!url) {
                if (i === 0) {
                    // Nothing playable — reset to the read/error button to retry.
                    this.setTtsState(key, {
                        isGenerating: false,
                        error: true,
                        chunks: undefined,
                        chunkUrls: undefined,
                        text: undefined,
                        synthIndex: undefined,
                        playPending: undefined,
                    });
                } else {
                    // Earlier sections stay playable; just stop the chain here.
                    this.setTtsState(key, {isGenerating: false, synthIndex: undefined});
                }
                return;
            }
        }
        this.setTtsState(key, {isGenerating: false, synthIndex: undefined});
    }

    /** Synthesize chunk `i` (already cleaned by the plan, reformulate=false),
     *  store + return its blob URL, or null on failure. */
    private async synthTtsChunk(
        key: string,
        threadId: string,
        i: number,
    ): Promise<string | null> {
        const chunks = this.ttsStateFor(key).chunks;
        if (!chunks || i < 0 || i >= chunks.length) return null;
        const cached = this.ttsStateFor(key).chunkUrls?.[i];
        if (cached) return cached;
        const lang = this.i18n.activeLang().startsWith('de') ? 'de' : 'en';
        let res;
        try {
            res = await firstValueFrom(
                this.api.generateTTS(threadId, chunks[i], {language: lang, reformulate: false}),
            );
        } catch (e) {
            console.error('TTS chunk synth threw', e);
            return null;
        }
        if (res === null || res === 'unavailable') return null;
        const url = URL.createObjectURL(res.audio);
        this.ttsBlobUrls.add(url);
        const urls = (this.ttsStateFor(key).chunkUrls ?? []).slice();
        urls[i] = url;
        this.setTtsState(key, {chunkUrls: urls});
        return url;
    }

    /** Locate a turn's section player by index (data-attrs on each <audio>). */
    private findTtsPlayer(key: string, index: number): HTMLAudioElement | null {
        const ref = this.ttsPlayers?.find(
            (r) =>
                r.nativeElement.dataset['ttsKey'] === key &&
                r.nativeElement.dataset['ttsIndex'] === String(index),
        );
        return ref?.nativeElement ?? null;
    }

    /**
     * A section's player loaded. Autoplay it only if it's the one we're waiting
     * for (the first section, or the one queued after the previous ended) — so
     * sections synthesized in the background don't all start at once.
     */
    onPlayerReady(event: Event, key: string, index: number): void {
        if (this.ttsStateFor(key).playPending !== index) return;
        this.setTtsState(key, {playPending: undefined});
        (event.target as HTMLAudioElement).play().catch(() => {
            /* autoplay blocked — native controls remain available */
        });
    }

    /** Auto-advance: when a section ends, play the next one — or queue it if it
     *  isn't synthesized yet (onPlayerReady picks it up when it loads). */
    onChunkEnded(key: string, index: number): void {
        const total = this.ttsStateFor(key).chunks?.length ?? 0;
        const next = index + 1;
        if (next >= total) return; // whole message played
        const el = this.findTtsPlayer(key, next);
        if (el) {
            el.play().catch(() => {});
        } else {
            this.setTtsState(key, {playPending: next});
        }
    }

    /** Pause every other player when one starts — one voice at a time. */
    onPlayerPlay(event: Event): void {
        const active = event.target as HTMLAudioElement;
        this.ttsPlayers?.forEach((ref) => {
            const el = ref.nativeElement;
            if (el !== active && !el.paused) el.pause();
        });
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
        // Ignore the programmatic scroll we trigger while restoring position.
        if (this.isRestoringScroll) return;
        // Near the top → widen the window toward older history (scroll-preserving).
        if (el.scrollTop < 120 && this.chat.hasOlderTurns()) {
            this.loadOlderHistory();
        }
        // If user is within 80px of the bottom, re-enable auto-scroll; otherwise pause it.
        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        this.autoScroll = nearBottom;
        this.scrolledAway.set(!nearBottom);
        if (nearBottom) {
            this.newMessageCount.set(0);
            this.chat.resetWindow(); // re-bound the DOM once back at the bottom
        }
    }

    jumpToLatest(): void {
        this.autoScroll = true;
        this.scrolledAway.set(false);
        this.newMessageCount.set(0);
        this.chat.resetWindow();
        this.lastSeenMessageCount = this.chat.turns().length;
        this.scrollToBottom();
    }

    /**
     * Widen the render window toward older history, preserving scroll position
     * so the viewport doesn't jump when older turns are prepended. The window
     * grows over the already-loaded conversation (no network call) — the
     * scroll-delta restore is salvaged from Advanced-LLM-Chat's scroll solution.
     */
    private loadOlderHistory(): void {
        const el = this.messagesContainer?.nativeElement;
        if (!el) return;
        const prevHeight = el.scrollHeight;
        const prevTop = el.scrollTop;
        this.isRestoringScroll = true;
        this.chat.loadOlderTurns();
        afterNextRender(
            () => {
                const cur = this.messagesContainer?.nativeElement;
                if (cur) {
                    cur.scrollTop = prevTop + (cur.scrollHeight - prevHeight);
                }
                // Release after the synthetic scroll event has fired and been ignored.
                setTimeout(() => (this.isRestoringScroll = false), 50);
            },
            {injector: this.injector},
        );
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

    /** Display-only: whether reasoning ("thinking") blocks open expanded by default. */
    onReasoningDefaultChange(value: string | null): void {
        if (value === 'expanded' || value === 'collapsed') {
            this.chatPrefs.setReasoningExpanded(value === 'expanded');
        }
    }

    /** Display-only: whether tool-call runs render inline ("expanded") or folded. */
    onToolCallsDefaultChange(value: string | null): void {
        if (value === 'expanded' || value === 'collapsed') {
            this.chatPrefs.setToolCallsExpanded(value === 'expanded');
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

    openCodeServer(): void {
        // Pre-flight the IDE status through the auth interceptor before opening:
        // the XHR validates/bumps the BFF session and, on a 401 (idle-expired
        // session), the interceptor redirects to re-login — so a direct
        // window.open never dumps raw 401 JSON. Mirrors job-list.component.ts.
        const threadId = this.chat.threadId();
        if (!threadId) return;
        this.api.getThreadIdeStatus(threadId).subscribe(status => {
            this.ideStatus.set(status);
            const url = pickCodeServerUrlToOpen(status);
            if (url) window.open(url, '_blank');
        });
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
     * fold their lead-up by default once they're done (the final answer stays
     * visible). Streaming turns are never auto-collapsed. The user can override
     * either way via the chevron, in which case userTurnCollapsed wins.
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

    /**
     * Intra-turn tool grouping (Slice 3 / #10). A run of this many or more
     * consecutive tool calls collapses into a single "N× tool calls"
     * disclosure; shorter runs render inline. A run is broken by any
     * thought/text, so `[tool,tool,thought,tool,tool]` stays two groups.
     */
    private readonly TOOL_GROUP_THRESHOLD = 4;

    /** Coalesce a turn's events into render groups (consecutive tools merged). */
    groupedEvents(turn: AssistantTurn): EventGroup[] {
        return groupEvents(turn.events);
    }

    /**
     * Whether this tool run renders as the folded "N× tool calls" disclosure.
     * Folds only long runs, and only while the user hasn't opted into the
     * always-inline "Tool calls → Expanded" display preference.
     */
    foldToolRun(tools: ToolCallEvent[]): boolean {
        return shouldFoldToolRun(
            tools.length,
            this.chatPrefs.toolCallsExpanded(),
            this.TOOL_GROUP_THRESHOLD,
        );
    }

    /** True if a grouped run should auto-open: any member errored or was denied. */
    toolGroupHasProblem(tools: ToolCallEvent[]): boolean {
        return tools.some((t) => t.status === 'error' || t.status === 'denied' || t.resultStatus === 'error');
    }

    /** Human one-liner of the distinct tools in a run ("read_file, edit_file x2"). */
    toolGroupSummary(tools: ToolCallEvent[]): string {
        return this.groupToolCallsHuman(tools);
    }

    /** Last text event in a turn — used for the TTS "read aloud" button. */
    lastTextEvent(turn: AssistantTurn): TextEvent | undefined {
        return lastTextOf(turn);
    }

    /**
     * The turn's final answer — the trailing prose after the last tool/thought.
     * Stays fully visible when the turn is collapsed (only the lead-up folds).
     * Empty when the turn ends on a tool/thought, in which case the collapsed
     * view falls back to {@link collapsedHeadline}.
     */
    finalAnswer(turn: AssistantTurn): string {
        return trailingText(turn);
    }

    /**
     * One-line fallback headline for a collapsed turn that has no closing prose
     * (ends on a tool/thought). Prefers the first sentence of the agent's
     * opening text; otherwise a tool/thought digest.
     */
    collapsedHeadline(turn: AssistantTurn): string {
        const first = firstTextOf(turn);
        const sentence = first ? firstSentence(first.content) : '';
        if (sentence) return sentence;
        const c = countEvents(turn);
        if (c.tools > 0) return this.transloco.translate('chat.turn.toolCount', {count: c.tools});
        if (c.thoughts > 0) return this.transloco.translate('chat.turn.thoughtCount', {count: c.thoughts});
        return this.transloco.translate('chat.turn.collapsedEmpty');
    }

    /** Render cap for a single diff card — bounds DOM for huge write_file bodies. */
    private readonly DIFF_LINE_CAP = 400;

    /**
     * Diff/content view for an edit_file / write_file tool card (#7), built
     * straight from the call args (no backend round-trip): edit_file replace
     * carries old_string→new_string so we show a real diff; append/prepend/
     * write have no "before" and render as all-additions. Returns null for any
     * other tool, and for failed calls (so the error message shows instead of a
     * diff that never applied).
     */
    fileEditView(tc: ToolCallEvent): FileEditView | null {
        if (tc.status === 'error') return null;
        const args = tc.args || {};
        const str = (k: string): string => (typeof args[k] === 'string' ? (args[k] as string) : '');
        const path = str('path');
        let mode: FileEditView['mode'];
        let lines: DiffLine[];
        if (tc.tool === 'write_file') {
            mode = 'write';
            lines = lineDiff('', str('content'));
        } else if (tc.tool === 'edit_file') {
            const position = str('position');
            if (position === 'end') {
                mode = 'append';
                lines = lineDiff('', str('new_string'));
            } else if (position === 'start') {
                mode = 'prepend';
                lines = lineDiff('', str('new_string'));
            } else {
                mode = 'replace';
                lines = lineDiff(str('old_string'), str('new_string'));
            }
        } else {
            return null;
        }
        if (lines.length === 0) return null;
        const truncated = Math.max(0, lines.length - this.DIFF_LINE_CAP);
        if (truncated > 0) lines = lines.slice(0, this.DIFF_LINE_CAP);
        return {path, mode, lines, truncated};
    }

    /** Gutter sign for a diff line. */
    diffSign(type: DiffLine['type']): string {
        return type === 'add' ? '+' : type === 'del' ? '-' : ' ';
    }

    /**
     * Threshold for autocollapsing user messages. Content with more than this
     * many lines folds into a <details> summary so a multi-screen prompt
     * doesn't dominate the scroll.
     */
    private readonly USER_MESSAGE_COLLAPSE_LINES = 8;

    /** True when a user message exceeds the autocollapse line threshold. */
    isUserMessageLong(content: string): boolean {
        if (!content) return false;
        let lines = 1;
        for (let i = 0; i < content.length; i++) {
            if (content.charCodeAt(i) === 10) {
                lines++;
                if (lines > this.USER_MESSAGE_COLLAPSE_LINES) return true;
            }
        }
        return false;
    }

    /** First line of a user message — shown in the collapsed summary. */
    userMessagePreview(content: string): string {
        if (!content) return '';
        const newlineIdx = content.indexOf('\n');
        return newlineIdx === -1 ? content : content.slice(0, newlineIdx);
    }

    /**
     * True when the current turn is historical and the next turn isn't —
     * the boundary between session reload and live activity.
     */
    showSessionDividerAfter(turn: Turn, index: number): boolean {
        const next = this.chat.visibleTurns()[index + 1];
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
