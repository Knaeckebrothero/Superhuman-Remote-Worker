import { Component, inject, signal, ElementRef, ViewChild, AfterViewChecked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { JobArtifactService } from '../../core/services/job-artifact.service';
import { BuilderStreamService, BuilderMessage } from '../../core/services/builder-stream.service';

/** Local chat message for display purposes. */
interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  toolCalls?: { tool: string; args: Record<string, unknown> }[];
}

@Component({
  selector: 'app-instruction-builder',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="builder-container">
      <div class="header-bar">
        <span class="title">
          <span class="header-icon">construction</span>
          Instruction Builder
        </span>
        @if (artifacts.streaming()) {
          <span class="streaming-badge">
            <span class="pulse-dot"></span>
            AI responding...
          </span>
        }
      </div>

      @if (!artifacts.sessionId()) {
        <div class="empty-state">
          <span class="empty-icon">smart_toy</span>
          <span class="empty-title">AI Instruction Builder</span>
          <span class="empty-desc">
            Describe what you want the agent to do, and I'll help you write
            the instructions, configure settings, and set up the job.
          </span>
          <span class="empty-hint">
            Start typing below to begin a conversation.
          </span>
        </div>
      } @else {
        <div class="messages-container" #messagesContainer>
          @for (msg of messages(); track $index) {
            <div class="message" [class]="'message-' + msg.role">
              <div class="message-avatar">
                {{ msg.role === 'user' ? 'person' : 'smart_toy' }}
              </div>
              <div class="message-body">
                <div class="message-content">{{ msg.content }}</div>
                @if (msg.toolCalls && msg.toolCalls.length > 0) {
                  <div class="tool-calls">
                    @for (tc of msg.toolCalls; track $index) {
                      <div class="tool-call-chip">
                        <span class="tool-call-icon">build</span>
                        <span class="tool-call-name">{{ formatToolName(tc.tool) }}</span>
                      </div>
                    }
                  </div>
                }
              </div>
            </div>
          }

          @if (streamingText()) {
            <div class="message message-assistant">
              <div class="message-avatar">smart_toy</div>
              <div class="message-body">
                <div class="message-content streaming">{{ streamingText() }}<span class="cursor-blink">|</span></div>
              </div>
            </div>
          }
        </div>
      }

      @if (isCreatingSession()) {
        <div class="session-loading">
          <span class="spinner-small"></span>
          Starting session...
        </div>
      }

      <div class="input-area">
        @if (error()) {
          <div class="input-error">
            <span class="error-text">{{ error() }}</span>
            <div class="error-actions">
              @if (lastFailedMessage()) {
                <button class="retry-btn" (click)="retryLastMessage()">Retry</button>
              }
              <button class="dismiss-btn" (click)="dismissError()">close</button>
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
            placeholder="Describe what the agent should do..."
            [disabled]="artifacts.streaming() || isCreatingSession()"
            rows="1"
          ></textarea>
          @if (artifacts.streaming()) {
            <button
              class="stop-btn"
              (click)="stopStreaming()"
              title="Stop generation"
            >
              stop
            </button>
          } @else {
            <button
              class="send-btn"
              (click)="sendMessage()"
              [disabled]="!inputText.trim() || isCreatingSession()"
            >
              send
            </button>
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
      }

      .header-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .title {
        display: flex;
        align-items: center;
        gap: 6px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .header-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        color: var(--accent-color, #cba6f7);
      }

      .streaming-badge {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        color: var(--accent-color, #cba6f7);
      }

      .pulse-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--accent-color, #cba6f7);
        animation: pulse 1.5s ease-in-out infinite;
      }

      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.3; }
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
        font-family: 'Material Symbols Outlined';
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
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 12px;
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
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .message-user .message-avatar {
        background: rgba(137, 180, 250, 0.15);
        color: #89b4fa;
      }

      .message-assistant .message-avatar {
        background: rgba(203, 166, 247, 0.15);
        color: var(--accent-color, #cba6f7);
      }

      .message-body {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .message-content {
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
        line-height: 1.5;
        white-space: pre-wrap;
        word-break: break-word;
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
        background: rgba(166, 227, 161, 0.1);
        border: 1px solid rgba(166, 227, 161, 0.2);
        font-size: 11px;
        color: #a6e3a1;
      }

      .tool-call-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 13px;
      }

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
        background: rgba(243, 139, 168, 0.12);
        border: 1px solid rgba(243, 139, 168, 0.2);
        font-size: 12px;
        color: #f38ba8;
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

      .retry-btn {
        padding: 3px 8px;
        border: 1px solid rgba(243, 139, 168, 0.3);
        border-radius: 4px;
        background: rgba(243, 139, 168, 0.1);
        color: inherit;
        font-size: 11px;
        cursor: pointer;
        white-space: nowrap;
      }

      .retry-btn:hover {
        background: rgba(243, 139, 168, 0.2);
      }

      .dismiss-btn {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        padding: 2px;
        border: none;
        border-radius: 4px;
        background: transparent;
        color: inherit;
        cursor: pointer;
        line-height: 1;
      }

      .dismiss-btn:hover {
        background: rgba(255, 255, 255, 0.1);
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

      .send-btn {
        flex-shrink: 0;
        width: 40px;
        height: 40px;
        border: none;
        border-radius: 8px;
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        font-family: 'Material Symbols Outlined';
        font-size: 20px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.15s ease;
      }

      .send-btn:hover:not(:disabled) {
        filter: brightness(1.1);
      }

      .send-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .stop-btn {
        flex-shrink: 0;
        width: 40px;
        height: 40px;
        border: none;
        border-radius: 8px;
        background: rgba(243, 139, 168, 0.2);
        color: #f38ba8;
        font-family: 'Material Symbols Outlined';
        font-size: 20px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.15s ease;
      }

      .stop-btn:hover {
        background: rgba(243, 139, 168, 0.35);
      }

      .spinner-small {
        width: 16px;
        height: 16px;
        border: 2px solid rgba(0, 0, 0, 0.2);
        border-top-color: currentColor;
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }
    `,
  ],
})
export class InstructionBuilderComponent implements AfterViewChecked {
  readonly artifacts = inject(JobArtifactService);
  private readonly stream = inject(BuilderStreamService);

  @ViewChild('messagesContainer') messagesContainer?: ElementRef<HTMLDivElement>;
  @ViewChild('inputArea') inputArea?: ElementRef<HTMLTextAreaElement>;

  readonly messages = signal<ChatMessage[]>([]);
  readonly streamingText = signal<string>('');
  readonly error = signal<string | null>(null);
  readonly isCreatingSession = signal(false);
  readonly lastFailedMessage = signal<string | null>(null);

  inputText = '';
  private shouldScrollToBottom = false;

  ngAfterViewChecked(): void {
    if (this.shouldScrollToBottom) {
      this.scrollToBottom();
      this.shouldScrollToBottom = false;
    }
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
    const toolCalls: { tool: string; args: Record<string, unknown> }[] = [];

    await this.stream.sendMessage(sessionId, text, {
      onToken: (token) => {
        this.streamingText.update((current) => current + token);
        this.shouldScrollToBottom = true;
      },
      onToolCall: (tool, args) => {
        toolCalls.push({ tool, args });
      },
      onDone: () => {
        const content = this.streamingText();
        this.streamingText.set('');
        this.messages.update((msgs) => [
          ...msgs,
          {
            role: 'assistant',
            content: content || '(applied changes)',
            toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
          },
        ]);
        this.shouldScrollToBottom = true;
      },
      onError: (message) => {
        const content = this.streamingText();
        this.streamingText.set('');
        if (content) {
          this.messages.update((msgs) => [
            ...msgs,
            {
              role: 'assistant',
              content,
              toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
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
            this.artifacts.sessionId.set(session.id);
            resolve(session.id);
          } else {
            this.error.set('Failed to create builder session');
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
