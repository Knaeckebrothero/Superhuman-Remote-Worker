import {AfterViewChecked, Component, computed, ElementRef, inject, OnInit, signal, ViewChild} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {MarkdownComponent} from 'ngx-markdown';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AgentStepsComponent, AgentStepType, IAgentStep} from '../agent-steps/agent-steps.component';
import {JobArtifactService, PendingWorkspaceEdit} from '../../../core/services/job-artifact.service';
import {BuilderSession, BuilderStreamService} from '../../../core/services/builder-stream.service';
import {ApiService} from '../../../core/services/api.service';
import {UserService} from '../../../core/services/user.service';
import {AppButtonComponent} from '../../../ui/button';
import {AppIconButtonComponent} from '../../../ui/icon-button';
import {AppBadgeComponent, type BadgeTone} from '../../../ui/badge';
import {AppIconComponent} from '../../../ui/icon';
import {AppSpinnerComponent} from '../../../ui/spinner';

type BuilderStepStatus = 'active' | 'complete';

interface BuilderStep extends IAgentStep {
  status: BuilderStepStatus;
}

/** Local chat message for display purposes. */
interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  toolCalls?: { tool: string; args: Record<string, unknown> }[];
  steps?: BuilderStep[];
}

@Component({
  selector: 'app-instruction-builder',
  standalone: true,
  imports: [
    FormsModule,
    MarkdownComponent,
    AgentStepsComponent,
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppIconComponent,
    AppSpinnerComponent,
  ],
  template: `
    <div class="builder-container">
      @if (!artifacts.sessionId()) {
        <div class="empty-state">
          <app-icon size="inherit" class="empty-icon">smart_toy</app-icon>
          <span class="empty-title">{{ 'builder.empty.title' | transloco }}</span>
          <span class="empty-desc">
            {{ 'builder.empty.desc' | transloco }}
          </span>
          <span class="empty-hint">
            {{ 'builder.empty.hint' | transloco }}
          </span>
        </div>
      } @else {
        <div class="messages-container" #messagesContainer>
          @for (msg of messages(); track $index) {
            <div class="message" [class]="'message-' + msg.role">
              <div class="message-avatar">
                <app-icon size="sm">{{ msg.role === 'user' ? 'person' : 'smart_toy' }}</app-icon>
              </div>
              <div class="message-body">
                @if (msg.steps && msg.steps.length > 0) {
                  <app-agent-steps
                    [steps]="toAgentSteps(msg.steps)"
                    [status]="'complete'"
                  ></app-agent-steps>
                }
                @if (msg.role === 'user') {
                  <div class="message-content">{{ msg.content }}</div>
                } @else {
                  <markdown class="message-content markdown-body" [data]="msg.content" clipboard></markdown>
                }
                @if (msg.toolCalls && msg.toolCalls.length > 0) {
                  <div class="tool-calls">
                    @for (tc of msg.toolCalls; track $index) {
                      <div class="tool-call-chip">
                        <app-icon size="sm" class="tool-call-icon">build</app-icon>
                        <span class="tool-call-name">{{ formatToolName(tc.tool) }}</span>
                      </div>
                    }
                  </div>
                }
              </div>
            </div>
          }

          @if (streamingText() || streamingSteps().length > 0) {
            <div class="message message-assistant">
              <div class="message-avatar"><app-icon size="sm">smart_toy</app-icon></div>
              <div class="message-body">
                @if (streamingSteps().length > 0) {
                  <app-agent-steps
                    [steps]="toAgentSteps(streamingSteps())"
                    [status]="'thinking'"
                  ></app-agent-steps>
                }
                @if (streamingText()) {
                  <div class="message-content streaming">
                    <markdown class="markdown-body" [data]="streamingText()"></markdown>
                    <span class="cursor-blink">|</span>
                  </div>
                }
              </div>
            </div>
          }

          @for (edit of pendingEdits(); track edit.id) {
            <div class="workspace-proposal-card">
              <div class="proposal-header">
                <app-icon size="md" class="proposal-icon">edit_note</app-icon>
                <span class="proposal-title">
                  {{ (edit.proposal.operation === 'write' ? 'builder.proposal.write' : 'builder.proposal.edit') | transloco }}:
                  <code>{{ edit.proposal.path }}</code>
                </span>
                <app-badge [tone]="proposalStatusTone(edit.status)" size="xs" [uppercase]="true">{{ edit.status }}</app-badge>
              </div>
              @if (edit.status === 'pending') {
                <div class="proposal-diff">
                  @if (edit.proposal.operation === 'edit') {
                    <div class="diff-section diff-remove">
                      <span class="diff-label">{{ 'builder.proposal.diffRemove' | transloco }}</span>
                      <pre class="diff-content">{{ edit.proposal.old_text }}</pre>
                    </div>
                    <div class="diff-section diff-add">
                      <span class="diff-label">{{ 'builder.proposal.diffInsert' | transloco }}</span>
                      <pre class="diff-content">{{ edit.proposal.new_text }}</pre>
                    </div>
                  } @else {
                    @if (edit.proposal.current_content) {
                      <details class="diff-details">
                        <summary class="diff-details-summary">{{ 'builder.proposal.currentContent' | transloco: {length: edit.proposal.current_content!.length} }}</summary>
                        <pre class="diff-content diff-current">{{ edit.proposal.current_content }}</pre>
                      </details>
                    }
                    <details class="diff-details" open>
                      <summary class="diff-details-summary">{{ 'builder.proposal.newContent' | transloco: {length: edit.proposal.content!.length} }}</summary>
                      <pre class="diff-content diff-new">{{ edit.proposal.content }}</pre>
                    </details>
                  }
                </div>
                <div class="proposal-actions">
                  <app-button variant="success" size="sm" (clicked)="applyWorkspaceEdit(edit)">
                    <app-icon size="sm" class="btn-icon">check</app-icon> {{ 'builder.proposal.apply' | transloco }}
                  </app-button>
                  <app-button variant="ghost" size="sm" (clicked)="dismissWorkspaceEdit(edit)">
                    <app-icon size="sm" class="btn-icon">close</app-icon> {{ 'builder.proposal.dismiss' | transloco }}
                  </app-button>
                </div>
              }
            </div>
          }
        </div>
      }

      @if (isCreatingSession()) {
        <div class="session-loading">
          <app-spinner size="sm" />
          {{ 'builder.session.starting' | transloco }}
        </div>
      }

      @if (isRestoringSession()) {
        <div class="session-loading">
          <app-spinner size="sm" />
          {{ 'builder.session.restoring' | transloco }}
        </div>
      }

      <div class="input-area">
        @if (error()) {
          <div class="input-error">
            <span class="error-text">{{ error() }}</span>
            <div class="error-actions">
              @if (lastFailedMessage()) {
                <app-button variant="danger" size="sm" (clicked)="retryLastMessage()">{{ 'builder.input.retry' | transloco }}</app-button>
              }
              <app-icon-button variant="ghost" size="sm" ariaLabel="Dismiss error" (clicked)="dismissError()">
                <app-icon size="sm" class="error-dismiss-icon">close</app-icon>
              </app-icon-button>
            </div>
          </div>
        }
        <div class="input-row">
          <textarea
            #inputArea
            class="chat-input"
            [(ngModel)]="inputText"
            (keydown)="onKeyDown($event)"
            (input)="autoResizeTextarea()"
            [placeholder]="'builder.input.placeholder' | transloco"
            [disabled]="artifacts.streaming() || isCreatingSession() || isRestoringSession()"
            rows="1"
          ></textarea>
          @if (artifacts.streaming()) {
            <app-icon-button
              variant="danger"
              size="lg"
              ariaLabel="Stop streaming"
              [tooltip]="'builder.input.stopTitle' | transloco"
              (clicked)="stopStreaming()"
            >
              <app-icon size="sm" class="input-action-icon">stop</app-icon>
            </app-icon-button>
          } @else {
            <app-icon-button
              variant="primary"
              size="lg"
              ariaLabel="Send message"
              [disabled]="!inputText.trim() || isCreatingSession()"
              (clicked)="sendMessage()"
            >
              <app-icon size="sm" class="input-action-icon">send</app-icon>
            </app-icon-button>
          }
        </div>
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

      .builder-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        overflow-x: hidden;
      }

      /* Empty state */
      .empty-state {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 32px;
        gap: 12px;
        text-align: center;
      }

      .empty-icon {
        font-size: 48px;
        color: var(--text-muted, #6c7086);
        opacity: 0.5;
      }

      .empty-title {
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .empty-desc {
        font-size: 13px;
        color: var(--text-muted, #6c7086);
        max-width: 360px;
        line-height: 1.5;
      }

      .empty-hint {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        opacity: 0.7;
        margin-top: 8px;
      }

      /* Messages */
      .messages-container {
        flex: 1;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .message {
        display: flex;
        gap: 10px;
        max-width: 90%;
        min-width: 0;
      }

      .message-user {
        align-self: flex-end;
        flex-direction: row-reverse;
      }

      .message-assistant {
        align-self: flex-start;
      }

      .message-avatar {
        flex-shrink: 0;
        width: 28px;
        height: 28px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      .message-user .message-avatar {
        background: var(--info-tint);
        color: var(--info);
      }

      .message-assistant .message-avatar {
        background: rgba(203, 166, 247, 0.15);
        color: var(--accent-color, #cba6f7);
      }

      .message-body {
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-width: 0;
        overflow: hidden;
      }

      .message-content {
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
        line-height: 1.5;
        white-space: pre-wrap;
        word-break: break-word;
        overflow-x: auto;
      }

      .message-user .message-content {
        background: rgba(137, 180, 250, 0.12);
        color: var(--text-primary, #cdd6f4);
        border-bottom-right-radius: 4px;
      }

      .message-assistant .message-content {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        border-bottom-left-radius: 4px;
      }

      .message-content.streaming {
        border: 1px solid rgba(203, 166, 247, 0.2);
      }

      .cursor-blink {
        animation: blink 1s step-end infinite;
        color: var(--accent-color, #cba6f7);
      }

      @keyframes blink {
        0%, 100% { opacity: 1; }
        50% { opacity: 0; }
      }

      /* Tool call chips */
      .tool-calls {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
      }

      .tool-call-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 8px;
        border-radius: 4px;
        background: var(--success-tint);
        border: 1px solid var(--success-tint);
        font-size: 11px;
        color: var(--success);
      }


      /* Inspection result rendering */
      .inspection-content {
        border-left: 3px solid #94e2d5;
        padding-left: 10px;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        font-size: 12px;
        max-height: 300px;
        overflow-y: auto;
        white-space: pre-wrap;
        background: rgba(148, 226, 213, 0.05);
        border-radius: 4px;
        padding: 8px 8px 8px 12px;
      }

      /* Inspection result rendering (used inside library's step content) */

      /* Session loading */
      .session-loading {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        padding: 8px 12px;
        font-size: 12px;
        color: var(--text-muted, #6c7086);
        border-top: 1px solid var(--border-color, #313244);
      }

      /* Input area */
      .input-area {
        flex-shrink: 0;
        padding: 10px 12px;
        border-top: 1px solid var(--border-color, #313244);
        background: var(--panel-header-bg, #1e1e2e);
      }

      .input-error {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 6px 10px;
        margin-bottom: 8px;
        border-radius: 6px;
        background: var(--danger-tint);
        border: 1px solid var(--danger-tint);
        font-size: 12px;
        color: var(--danger);
      }

      .error-text {
        flex: 1;
        min-width: 0;
      }

      .error-actions {
        display: flex;
        align-items: center;
        gap: 4px;
        flex-shrink: 0;
      }


      .input-row {
        display: flex;
        gap: 8px;
        align-items: flex-end;
      }

      .chat-input {
        flex: 1;
        padding: 10px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 8px;
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-family: inherit;
        font-size: 13px;
        line-height: 1.4;
        resize: none;
        max-height: 120px;
        overflow-y: auto;
      }

      .chat-input:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .chat-input:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .chat-input::placeholder {
        color: var(--text-muted, #6c7086);
      }



      /* Markdown content styling */
      :host ::ng-deep .markdown-body {
        font-size: 13px;
        line-height: 1.6;
        color: var(--text-primary, #cdd6f4);
        word-break: break-word;
      }

      :host ::ng-deep .markdown-body p {
        margin: 0 0 0.5em;
      }

      :host ::ng-deep .markdown-body p:last-child {
        margin-bottom: 0;
      }

      :host ::ng-deep .markdown-body h1,
      :host ::ng-deep .markdown-body h2,
      :host ::ng-deep .markdown-body h3,
      :host ::ng-deep .markdown-body h4,
      :host ::ng-deep .markdown-body h5,
      :host ::ng-deep .markdown-body h6 {
        margin: 0.6em 0 0.3em;
        font-weight: 600;
        line-height: 1.3;
        color: var(--text-primary, #cdd6f4);
      }

      :host ::ng-deep .markdown-body h1 { font-size: 1.3em; }
      :host ::ng-deep .markdown-body h2 { font-size: 1.2em; }
      :host ::ng-deep .markdown-body h3 { font-size: 1.1em; }

      :host ::ng-deep .markdown-body pre {
        background: var(--timeline-bg, #11111b);
        padding: 12px;
        border-radius: 8px;
        overflow-x: auto;
        margin: 0.4em 0;
        border: 1px solid var(--border-color, #313244);
        position: relative;
      }

      :host ::ng-deep .markdown-body pre code {
        background: none;
        padding: 0;
        border: none;
        border-radius: 0;
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
      }

      :host ::ng-deep .markdown-body code {
        background: var(--surface-0, #313244);
        padding: 0.15em 0.4em;
        border-radius: 4px;
        font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
        font-size: 0.9em;
        border: 1px solid var(--border-color, #313244);
      }

      :host ::ng-deep .markdown-body ul,
      :host ::ng-deep .markdown-body ol {
        margin: 0.3em 0;
        padding-left: 1.5em;
      }

      :host ::ng-deep .markdown-body li {
        margin: 0.15em 0;
      }

      :host ::ng-deep .markdown-body blockquote {
        margin: 0.4em 0;
        padding: 0.3em 0 0.3em 0.8em;
        border-left: 3px solid var(--accent-color, #cba6f7);
        background: rgba(203, 166, 247, 0.06);
        border-radius: 0 6px 6px 0;
        color: var(--text-secondary, #a6adc8);
      }

      :host ::ng-deep .markdown-body table {
        border-collapse: collapse;
        width: 100%;
        margin: 0.4em 0;
        font-size: 12px;
      }

      :host ::ng-deep .markdown-body th,
      :host ::ng-deep .markdown-body td {
        border: 1px solid var(--border-color, #313244);
        padding: 6px 10px;
        text-align: left;
      }

      :host ::ng-deep .markdown-body th {
        background: var(--surface-0, #313244);
        font-weight: 600;
      }

      :host ::ng-deep .markdown-body tr:nth-child(even) {
        background: rgba(49, 50, 68, 0.3);
      }

      :host ::ng-deep .markdown-body hr {
        border: none;
        border-top: 1px solid var(--border-color, #313244);
        margin: 0.6em 0;
      }

      :host ::ng-deep .markdown-body a {
        color: var(--accent-hover, #b4befe);
        text-decoration: none;
      }

      :host ::ng-deep .markdown-body a:hover {
        text-decoration: underline;
      }

      :host ::ng-deep .markdown-body strong {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      /* Clipboard button on code blocks */
      :host ::ng-deep .markdown-clipboard-button {
        position: absolute;
        top: 6px;
        right: 6px;
        width: 28px;
        height: 28px;
        padding: 0;
        border: none;
        border-radius: 6px;
        background: var(--surface-0, #313244);
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        opacity: 0;
        transition: opacity 0.15s ease;
      }

      :host ::ng-deep pre:hover .markdown-clipboard-button {
        opacity: 1;
      }

      :host ::ng-deep .markdown-clipboard-button:hover {
        background: var(--surface-1, #45475a);
        color: var(--text-primary, #cdd6f4);
      }

      /* Prism syntax highlighting (dark theme) */
      :host ::ng-deep .token.comment,
      :host ::ng-deep .token.prolog,
      :host ::ng-deep .token.doctype,
      :host ::ng-deep .token.cdata {
        color: #6a9955;
      }

      :host ::ng-deep .token.keyword {
        color: #569cd6;
      }

      :host ::ng-deep .token.string,
      :host ::ng-deep .token.attr-value {
        color: #ce9178;
      }

      :host ::ng-deep .token.function {
        color: #dcdcaa;
      }

      :host ::ng-deep .token.number {
        color: #b5cea8;
      }

      :host ::ng-deep .token.operator {
        color: #d4d4d4;
      }

      :host ::ng-deep .token.class-name {
        color: #4ec9b0;
      }

      :host ::ng-deep .token.variable {
        color: #9cdcfe;
      }

      :host ::ng-deep .token.punctuation {
        color: #d4d4d4;
      }

      :host ::ng-deep .token.boolean,
      :host ::ng-deep .token.constant {
        color: #569cd6;
      }

      /* Mobile overrides */
      @media (max-width: 768px) {
        .message {
          max-width: 95%;
        }

        .input-area {
          padding-bottom: calc(10px + env(safe-area-inset-bottom, 0px));
        }

        .chat-input {
          font-size: 16px; /* prevents iOS zoom on focus */
        }

        :host ::ng-deep .markdown-body pre {
          max-width: min(95vw, calc(100% - 20px));
        }

        :host ::ng-deep .markdown-body table {
          display: block;
          overflow-x: auto;
          max-width: min(95vw, calc(100% - 20px));
        }
      }

      /* Workspace Proposal Card */
      .workspace-proposal-card {
        margin: 8px 0;
        border: 1px solid rgba(250, 179, 135, 0.3);
        border-radius: 10px;
        background: rgba(250, 179, 135, 0.06);
        overflow: hidden;
      }

      .proposal-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        border-bottom: 1px solid var(--alert-tint);
        font-size: 12px;
        font-weight: 600;
        color: var(--alert);
      }

      .proposal-icon {
        flex-shrink: 0;
      }

      .proposal-title {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .proposal-title code {
        background: rgba(250, 179, 135, 0.15);
        padding: 1px 5px;
        border-radius: 3px;
        font-size: 11px;
      }

      .proposal-diff {
        padding: 8px 12px;
        max-height: 300px;
        overflow-y: auto;
      }

      .diff-section {
        margin-bottom: 8px;
        border-radius: 6px;
        overflow: hidden;
      }

      .diff-label {
        display: block;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        padding: 4px 8px;
      }

      .diff-remove {
        border: 1px solid rgba(243, 139, 168, 0.25);
      }

      .diff-remove .diff-label {
        background: var(--danger-tint);
        color: var(--danger);
      }

      .diff-add {
        border: 1px solid var(--success-tint);
      }

      .diff-add .diff-label {
        background: var(--success-tint);
        color: var(--success);
      }

      .diff-content {
        margin: 0;
        padding: 8px 10px;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        font-size: 11px;
        line-height: 1.5;
        white-space: pre-wrap;
        word-break: break-word;
        color: var(--text-primary, #cdd6f4);
        max-height: 200px;
        overflow-y: auto;
      }

      .diff-details {
        margin-bottom: 8px;
      }

      .diff-details-summary {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        padding: 4px 0;
        user-select: none;
      }

      .diff-current {
        background: rgba(49, 50, 68, 0.5);
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
      }

      .diff-new {
        background: rgba(166, 227, 161, 0.05);
        border: 1px solid rgba(166, 227, 161, 0.2);
        border-radius: 6px;
      }

      .proposal-actions {
        display: flex;
        gap: 8px;
        padding: 8px 12px;
        border-top: 1px solid rgba(250, 179, 135, 0.15);
      }

      .proposal-actions app-button .btn-icon {
        margin-right: 4px;
        vertical-align: middle;
      }

      /* Remove white-space:pre-wrap on markdown rendered content */
      .message-assistant .message-content.markdown-body {
        white-space: normal;
      }

    `,
  ],
})
export class InstructionBuilderComponent implements AfterViewChecked, OnInit {
  readonly artifacts = inject(JobArtifactService);
  private readonly stream = inject(BuilderStreamService);
  private readonly api = inject(ApiService);
  private readonly userService = inject(UserService);
  private readonly transloco = inject(TranslocoService);

  readonly sessions = signal<BuilderSession[]>([]);
  readonly showSessionDropdown = signal(false);

  @ViewChild('messagesContainer') messagesContainer?: ElementRef<HTMLDivElement>;
  @ViewChild('inputArea') inputArea?: ElementRef<HTMLTextAreaElement>;

  readonly messages = signal<ChatMessage[]>([]);
  readonly streamingText = signal<string>('');
  readonly streamingSteps = signal<BuilderStep[]>([]);
  readonly pendingEdits = computed(() =>
    this.artifacts.pendingWorkspaceEdits().filter((e) => e.status === 'pending'),
  );
  readonly error = signal<string | null>(null);
  readonly isCreatingSession = signal(false);
  readonly isRestoringSession = signal(false);
  readonly lastFailedMessage = signal<string | null>(null);

  inputText = '';
  private shouldScrollToBottom = false;
  private stepCounter = 0;

  /** Set of tool names that return inspection results. */
  private readonly INSPECTION_TOOLS = new Set([
    'list_jobs', 'get_job', 'get_job_progress', 'get_workspace_file',
    'get_workspace_overview', 'get_frozen_job', 'get_todos', 'get_chat_history',
  ]);

  ngOnInit(): void {
    const sessionId = this.artifacts.sessionId();
    if (sessionId && this.messages().length === 0) {
      this.restoreMessages(sessionId);
    }
  }

  ngAfterViewChecked(): void {
    if (this.shouldScrollToBottom) {
      this.scrollToBottom();
      this.shouldScrollToBottom = false;
    }
  }

  /** Restore message history from the server for an existing session. */
  private restoreMessages(sessionId: string): void {
    this.isRestoringSession.set(true);
    this.stream.getMessages(sessionId).subscribe({
      next: (msgs) => {
        if (msgs.length > 0) {
          const restored: ChatMessage[] = msgs.map((m) => ({
            role: m.role,
            content: m.content || this.transloco.translate('builder.stream.appliedChanges'),
            toolCalls: m.tool_calls?.map((tc) => ({ tool: tc.tool, args: tc.args })),
            steps: m.steps?.map((s, i) => ({
              id: `restored-${m.id}-${i}`,
              type: (s.type || 'thought') as AgentStepType,
              title: s.title || '',
              content: s.content || '',
              timestamp: Date.now(),
              status: 'complete' as BuilderStepStatus,
            })),
          }));
          this.messages.set(restored);
          this.shouldScrollToBottom = true;

          // Replay artifact mutations so the create form reflects builder changes
          for (const msg of restored) {
            if (msg.toolCalls) {
              for (const tc of msg.toolCalls) {
                this.artifacts.applyToolCall(tc.tool, tc.args);
              }
            }
          }
        } else {
          // Session exists in localStorage but has no messages (stale) — clear it
          this.artifacts.persistSessionId(null);
        }
        this.isRestoringSession.set(false);
      },
      error: () => {
        this.isRestoringSession.set(false);
      },
    });

    // Also load session title
    this.stream.getSession(sessionId).subscribe((session) => {
      if (session?.title) {
        this.artifacts.sessionTitle.set(session.title);
      }
    });
  }

  onKeyDown(event: KeyboardEvent): void {
    // Enter sends message; Shift+Enter adds a newline
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.sendMessage();
    }
  }

  async sendMessage(): Promise<void> {
    const text = this.inputText.trim();
    if (!text || this.artifacts.streaming() || this.isCreatingSession()) return;

    this.error.set(null);
    this.lastFailedMessage.set(null);

    // Ensure we have a session
    let sessionId = this.artifacts.sessionId();
    if (!sessionId) {
      this.isCreatingSession.set(true);
      sessionId = await this.createSession();
      this.isCreatingSession.set(false);
      if (!sessionId) return;
    }

    // Add user message to local state
    this.messages.update((msgs) => [...msgs, { role: 'user', content: text }]);
    this.inputText = '';
    this.resetTextareaHeight();
    this.shouldScrollToBottom = true;

    // Stream AI response
    await this.streamResponse(sessionId, text);
  }

  private async streamResponse(sessionId: string, text: string): Promise<void> {
    this.streamingText.set('');
    this.streamingSteps.set([]);
    this.stepCounter = 0;
    const toolCalls: { tool: string; args: Record<string, unknown> }[] = [];

    await this.stream.sendMessage(sessionId, text, {
      onToken: (token) => {
        // Complete any active thought step when text starts arriving
        this.completeActiveSteps('thought');
        this.streamingText.update((current) => current + token);
        this.shouldScrollToBottom = true;
      },
      onStep: (type, title) => {
        // Complete any previously active steps
        this.completeActiveSteps();
        this.addStep(type as AgentStepType, title, '', 'active');
        this.shouldScrollToBottom = true;
      },
      onToolCall: (tool, args) => {
        toolCalls.push({ tool, args });
        // Artifact mutation tools appear as immediately-complete tool_call steps
        this.completeActiveSteps();
        this.addStep('tool_call', this.formatToolName(tool), JSON.stringify(args, null, 2), 'complete');
        this.shouldScrollToBottom = true;
      },
      onToolExecuting: (tool, args) => {
        // Complete any active thought step and show executing tool
        this.completeActiveSteps();
        const title = tool === 'web_search'
          ? this.transloco.translate('builder.stream.searching', {query: (args as Record<string, string>)['query']})
          : this.transloco.translate('builder.stream.running', {tool: this.formatToolName(tool)});
        this.addStep('tool_call', title, JSON.stringify(args, null, 2), 'active');
        this.shouldScrollToBottom = true;
      },
      onToolResult: (tool, _summary, content) => {
        // Complete the active tool_call step
        this.completeActiveSteps('tool_call');
        if (content && this.INSPECTION_TOOLS.has(tool)) {
          // Rich inspection result
          this.addStep('inspection_result', `${this.formatToolName(tool)}`, content, 'complete');
        } else {
          this.addStep('tool_result', this.transloco.translate('builder.stream.result', {tool: this.formatToolName(tool)}), '', 'complete');
        }
        this.shouldScrollToBottom = true;
      },
      onWorkspaceProposal: (proposal) => {
        this.completeActiveSteps();
        const label = this.transloco.translate(proposal.operation === 'write' ? 'builder.proposal.write' : 'builder.proposal.edit');
        this.addStep('workspace_proposal', `${label}: ${proposal.path}`, '', 'complete');
        this.artifacts.addWorkspaceProposal(proposal);
        this.shouldScrollToBottom = true;
      },
      onDone: (title?: string) => {
        const content = this.streamingText();
        const steps = this.streamingSteps();
        // Mark remaining active steps as complete
        const finalSteps = steps.map((s) => ({ ...s, status: 'complete' as BuilderStepStatus }));
        this.streamingText.set('');
        this.streamingSteps.set([]);
        this.messages.update((msgs) => [
          ...msgs,
          {
            role: 'assistant',
            content: content || this.transloco.translate('builder.stream.appliedChanges'),
            toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
            steps: finalSteps.length > 0 ? finalSteps : undefined,
          },
        ]);
        if (title) {
          this.artifacts.sessionTitle.set(title);
        }
        this.shouldScrollToBottom = true;
      },
      onError: (message) => {
        const content = this.streamingText();
        const steps = this.streamingSteps();
        const finalSteps = steps.map((s) => ({ ...s, status: 'complete' as BuilderStepStatus }));
        this.streamingText.set('');
        this.streamingSteps.set([]);
        if (content) {
          this.messages.update((msgs) => [
            ...msgs,
            {
              role: 'assistant',
              content,
              toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
              steps: finalSteps.length > 0 ? finalSteps : undefined,
            },
          ]);
        }
        this.lastFailedMessage.set(text);
        this.error.set(message);
      },
    });
  }

  stopStreaming(): void {
    this.stream.abort();
  }

  retryLastMessage(): void {
    const text = this.lastFailedMessage();
    if (!text) return;
    this.error.set(null);
    this.lastFailedMessage.set(null);

    const sessionId = this.artifacts.sessionId();
    if (sessionId) {
      this.streamResponse(sessionId, text);
    }
  }

  dismissError(): void {
    this.error.set(null);
    this.lastFailedMessage.set(null);
  }

  applyWorkspaceEdit(edit: PendingWorkspaceEdit): void {
    const p = edit.proposal;
    let finalContent: string;

    if (p.operation === 'edit' && p.current_content && p.old_text != null && p.new_text != null) {
      finalContent = p.current_content.replace(p.old_text, p.new_text);
    } else if (p.operation === 'write' && p.content != null) {
      finalContent = p.content;
    } else {
      return;
    }

    this.api.writeWorkspaceFile(p.job_id, p.path, finalContent, `Builder: update ${p.path}`).subscribe({
      next: (result) => {
        if (result) {
          this.artifacts.resolveWorkspaceEdit(edit.id, 'approved');
        } else {
          this.error.set(this.transloco.translate('builder.errors.writeFailed', {path: p.path}));
        }
      },
      error: () => {
        this.error.set(`Failed to write ${p.path}`);
      },
    });
  }

  dismissWorkspaceEdit(edit: PendingWorkspaceEdit): void {
    this.artifacts.resolveWorkspaceEdit(edit.id, 'dismissed');
  }

  proposalStatusTone(status: string): BadgeTone {
    switch (status) {
      case 'pending':
        return 'alert';
      case 'approved':
        return 'success';
      case 'dismissed':
        return 'neutral';
      default:
        return 'neutral';
    }
  }

  /** Toggle the session dropdown (lazy-loads sessions on first open). */
  toggleSessionDropdown(): void {
    const next = !this.showSessionDropdown();
    if (next) {
      this.loadSessions();
    }
    this.showSessionDropdown.set(next);
  }

  /** Start a brand new session. */
  startNewSession(): void {
    this.stream.abort();
    this.artifacts.reset();
    this.messages.set([]);
    this.streamingText.set('');
    this.streamingSteps.set([]);
    this.error.set(null);
    this.lastFailedMessage.set(null);
    this.showSessionDropdown.set(false);
  }

  /** Switch to an existing session. */
  switchSession(sessionId: string, title: string | null): void {
    this.stream.abort();
    this.artifacts.instructions.set(null);
    this.artifacts.config.set(null);
    this.artifacts.description.set(null);
    this.artifacts.streaming.set(false);
    this.artifacts.pendingWorkspaceEdits.set([]);
    this.messages.set([]);
    this.streamingText.set('');
    this.streamingSteps.set([]);
    this.error.set(null);
    this.lastFailedMessage.set(null);

    this.artifacts.persistSessionId(sessionId);
    this.artifacts.sessionTitle.set(title);
    this.showSessionDropdown.set(false);

    this.restoreMessages(sessionId);
  }

  private loadSessions(): void {
    const userId = this.userService.currentUserId();
    if (!userId) return;
    this.stream.listSessions(userId).subscribe((sessions) => {
      this.sessions.set(sessions);
    });
  }

  autoResizeTextarea(): void {
    const el = this.inputArea?.nativeElement;
    if (el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    }
  }

  formatToolName(tool: string): string {
    return tool.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  /** Convert BuilderStep[] to IAgentStep[] for the library component. */
  toAgentSteps(steps: BuilderStep[]): IAgentStep[] {
    return steps;
  }

  private addStep(type: AgentStepType, title: string, content: string, status: BuilderStepStatus): void {
    const step: BuilderStep = {
      id: `step-${++this.stepCounter}`,
      type,
      title,
      content,
      status,
      timestamp: Date.now(),
    };
    this.streamingSteps.update((steps) => [...steps, step]);
  }

  private completeActiveSteps(typeFilter?: AgentStepType): void {
    this.streamingSteps.update((steps) => {
      let changed = false;
      const updated = steps.map((s) => {
        if (s.status === 'active' && (!typeFilter || s.type === typeFilter)) {
          changed = true;
          return { ...s, status: 'complete' as BuilderStepStatus };
        }
        return s;
      });
      return changed ? updated : steps;
    });
  }

  private resetTextareaHeight(): void {
    const el = this.inputArea?.nativeElement;
    if (el) {
      el.style.height = 'auto';
    }
  }

  private async createSession(): Promise<string | null> {
    return new Promise<string | null>((resolve) => {
      this.stream.createSession().subscribe({
        next: (session) => {
          if (session) {
            this.artifacts.persistSessionId(session.id);
            resolve(session.id);
          } else {
            this.error.set(this.transloco.translate('builder.session.createFailed'));
            resolve(null);
          }
        },
        error: () => {
          this.error.set('Failed to create builder session');
          resolve(null);
        },
      });
    });
  }

  private scrollToBottom(): void {
    const el = this.messagesContainer?.nativeElement;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }
}
