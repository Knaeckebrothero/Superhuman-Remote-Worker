import { Component, inject, signal, computed, ElementRef, ViewChild, AfterViewChecked, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MarkdownComponent } from 'ngx-markdown';
import { JobArtifactService, PendingWorkspaceEdit } from '../../core/services/job-artifact.service';
import { BuilderStreamService, BuilderMessage, WorkspaceProposal } from '../../core/services/builder-stream.service';
import { ApiService } from '../../core/services/api.service';

type BuilderStepType = 'thought' | 'tool_call' | 'tool_result' | 'inspection_result' | 'workspace_proposal';
type BuilderStepStatus = 'active' | 'complete';

interface BuilderStep {
  id: string;
  type: BuilderStepType;
  title: string;
  content: string;
  status: BuilderStepStatus;
  timestamp: number;
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
  imports: [FormsModule, MarkdownComponent],
  template: `
    <div class="builder-container">
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
                @if (msg.steps && msg.steps.length > 0) {
                  <details class="thought-process-panel">
                    <summary class="thought-process-summary">
                      <span class="thought-process-icon complete-icon">check_circle</span>
                      <span class="thought-header-text">Thought process</span>
                      <span class="thought-step-count">({{ msg.steps.length }})</span>
                      <span class="chevron-icon">expand_more</span>
                    </summary>
                    <div class="thought-process-steps">
                      @for (step of msg.steps; track step.id) {
                        <details class="step-item">
                          <summary class="step-header" [class]="'step-header--' + step.type">
                            <span class="step-icon" [class]="'step-icon--' + step.type">
                              {{ getStepIcon(step.type) }}
                            </span>
                            <span class="step-title">{{ step.title }}</span>
                          </summary>
                          @if (step.content) {
                            <div class="step-content" [class.inspection-content]="step.type === 'inspection_result'">{{ step.content }}</div>
                          }
                        </details>
                      }
                    </div>
                  </details>
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
                        <span class="tool-call-icon">build</span>
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
              <div class="message-avatar">smart_toy</div>
              <div class="message-body">
                @if (streamingSteps().length > 0) {
                  <details class="thought-process-panel">
                    <summary class="thought-process-summary">
                      <span class="thought-process-icon">psychology</span>
                      <span class="thought-header-text">{{ currentStepTitle() }}</span>
                      <span class="thought-step-count">({{ streamingSteps().length }})</span>
                      <span class="spinner-small"></span>
                    </summary>
                    <div class="thought-process-steps">
                      @for (step of streamingSteps(); track step.id) {
                        <div class="step-item-flat" [class]="'step-flat--' + step.type">
                          <span class="step-icon" [class]="'step-icon--' + step.type">
                            {{ getStepIcon(step.type) }}
                          </span>
                          <span class="step-title">{{ step.title }}</span>
                          @if (step.status === 'active') {
                            <span class="spinner-tiny"></span>
                          }
                        </div>
                      }
                    </div>
                  </details>
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
                <span class="proposal-icon">edit_note</span>
                <span class="proposal-title">
                  {{ edit.proposal.operation === 'write' ? 'Write' : 'Edit' }}:
                  <code>{{ edit.proposal.path }}</code>
                </span>
                <span class="proposal-badge" [class]="'badge-' + edit.status">{{ edit.status }}</span>
              </div>
              @if (edit.status === 'pending') {
                <div class="proposal-diff">
                  @if (edit.proposal.operation === 'edit') {
                    <div class="diff-section diff-remove">
                      <span class="diff-label">Remove</span>
                      <pre class="diff-content">{{ edit.proposal.old_text }}</pre>
                    </div>
                    <div class="diff-section diff-add">
                      <span class="diff-label">Insert</span>
                      <pre class="diff-content">{{ edit.proposal.new_text }}</pre>
                    </div>
                  } @else {
                    @if (edit.proposal.current_content) {
                      <details class="diff-details">
                        <summary class="diff-details-summary">Current content ({{ edit.proposal.current_content!.length }} chars)</summary>
                        <pre class="diff-content diff-current">{{ edit.proposal.current_content }}</pre>
                      </details>
                    }
                    <details class="diff-details" open>
                      <summary class="diff-details-summary">New content ({{ edit.proposal.content!.length }} chars)</summary>
                      <pre class="diff-content diff-new">{{ edit.proposal.content }}</pre>
                    </details>
                  }
                </div>
                <div class="proposal-actions">
                  <button class="proposal-btn apply-btn" (click)="applyWorkspaceEdit(edit)">
                    <span class="btn-icon">check</span> Apply
                  </button>
                  <button class="proposal-btn dismiss-btn-ws" (click)="dismissWorkspaceEdit(edit)">
                    <span class="btn-icon">close</span> Dismiss
                  </button>
                </div>
              }
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

      @if (isRestoringSession()) {
        <div class="session-loading">
          <span class="spinner-small"></span>
          Restoring conversation...
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
            [disabled]="artifacts.streaming() || isCreatingSession() || isRestoringSession()"
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

      .step-icon--inspection_result {
        color: #94e2d5;
      }

      /* Thought Process Panel */
      .thought-process-panel {
        border-radius: 8px;
        background: rgba(139, 92, 246, 0.06);
        border: 1px solid rgba(139, 92, 246, 0.15);
        overflow: hidden;
      }

      .thought-process-panel[open] > .thought-process-summary .chevron-icon {
        transform: rotate(180deg);
      }

      .thought-process-summary {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 10px;
        font-size: 11px;
        font-weight: 600;
        color: #a78bfa;
        cursor: pointer;
        user-select: none;
        list-style: none;
      }

      .thought-process-summary::-webkit-details-marker {
        display: none;
      }

      .thought-process-summary:hover {
        background: rgba(139, 92, 246, 0.08);
      }

      .thought-process-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        flex-shrink: 0;
      }

      .thought-process-icon.complete-icon {
        color: #a6e3a1;
      }

      .thought-header-text {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .thought-step-count {
        flex-shrink: 0;
        opacity: 0.6;
        font-weight: 400;
      }

      .chevron-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
        margin-left: auto;
        transition: transform 0.2s ease;
      }

      .thought-process-steps {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: 0 6px 6px;
        max-height: 250px;
        overflow-y: auto;
      }

      /* Individual step (collapsible, for finalized messages) */
      .step-item {
        border-radius: 6px;
        overflow: hidden;
      }

      .step-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        font-size: 11px;
        cursor: pointer;
        user-select: none;
        list-style: none;
        border-radius: 6px;
        color: var(--text-primary, #cdd6f4);
      }

      .step-header::-webkit-details-marker {
        display: none;
      }

      .step-header:hover {
        background: rgba(255, 255, 255, 0.04);
      }

      .step-content {
        padding: 6px 8px 8px 36px;
        font-size: 11px;
        line-height: 1.4;
        color: var(--text-muted, #6c7086);
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 200px;
        overflow-y: auto;
      }

      /* Individual step (flat, non-collapsible, for streaming) */
      .step-item-flat {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 5px 8px;
        font-size: 11px;
        border-radius: 6px;
        color: var(--text-primary, #cdd6f4);
      }

      /* Step icons with gradient backgrounds */
      .step-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 14px;
        width: 22px;
        height: 22px;
        border-radius: 6px;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        color: #fff;
      }

      .step-icon--thought {
        background: linear-gradient(135deg, #8b5cf6, #a78bfa);
      }

      .step-icon--tool_call {
        background: linear-gradient(135deg, #f59e0b, #fbbf24);
      }

      .step-icon--tool_result {
        background: linear-gradient(135deg, #10b981, #34d399);
      }

      .step-icon--workspace_proposal {
        background: linear-gradient(135deg, #f97316, #fab387);
      }

      .step-title {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
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
        width: 14px;
        height: 14px;
        border: 2px solid rgba(167, 139, 250, 0.2);
        border-top-color: #a78bfa;
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
        flex-shrink: 0;
      }

      .spinner-tiny {
        width: 12px;
        height: 12px;
        border: 1.5px solid rgba(255, 255, 255, 0.15);
        border-top-color: currentColor;
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
        flex-shrink: 0;
        margin-left: auto;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
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
        border-bottom: 1px solid rgba(250, 179, 135, 0.15);
        font-size: 12px;
        font-weight: 600;
        color: #fab387;
      }

      .proposal-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
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

      .proposal-badge {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        padding: 2px 6px;
        border-radius: 4px;
        flex-shrink: 0;
      }

      .badge-pending {
        background: rgba(250, 179, 135, 0.2);
        color: #fab387;
      }

      .badge-approved {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .badge-dismissed {
        background: rgba(108, 112, 134, 0.2);
        color: #6c7086;
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
        background: rgba(243, 139, 168, 0.12);
        color: #f38ba8;
      }

      .diff-add {
        border: 1px solid rgba(166, 227, 161, 0.25);
      }

      .diff-add .diff-label {
        background: rgba(166, 227, 161, 0.12);
        color: #a6e3a1;
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

      .proposal-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 5px 12px;
        border: none;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .proposal-btn .btn-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .apply-btn {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .apply-btn:hover {
        background: rgba(166, 227, 161, 0.35);
      }

      .dismiss-btn-ws {
        background: rgba(108, 112, 134, 0.15);
        color: var(--text-muted, #6c7086);
      }

      .dismiss-btn-ws:hover {
        background: rgba(108, 112, 134, 0.3);
      }

      /* Remove white-space:pre-wrap on markdown rendered content */
      .message-assistant .message-content.markdown-body {
        white-space: normal;
      }

      /* Mobile adjustments */
      @media (max-width: 768px) {
        .message {
          max-width: 95%;
        }

        .input-area {
          padding-bottom: env(safe-area-inset-bottom, 0px);
        }

        .chat-input {
          font-size: 16px;
        }
      }
    `,
  ],
})
export class InstructionBuilderComponent implements AfterViewChecked, OnInit {
  readonly artifacts = inject(JobArtifactService);
  private readonly stream = inject(BuilderStreamService);
  private readonly api = inject(ApiService);

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
            content: m.content || '(applied changes)',
            toolCalls: m.tool_calls?.map((tc) => ({ tool: tc.tool, args: tc.args })),
          }));
          this.messages.set(restored);
          this.shouldScrollToBottom = true;
        }
        this.isRestoringSession.set(false);
      },
      error: () => {
        this.isRestoringSession.set(false);
      },
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
        this.addStep(type as BuilderStepType, title, '', 'active');
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
          ? `Searching: ${(args as Record<string, string>)['query']}`
          : `Running: ${this.formatToolName(tool)}`;
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
          this.addStep('tool_result', `Result: ${this.formatToolName(tool)}`, '', 'complete');
        }
        this.shouldScrollToBottom = true;
      },
      onWorkspaceProposal: (proposal) => {
        this.completeActiveSteps();
        const label = proposal.operation === 'write' ? 'Write' : 'Edit';
        this.addStep('workspace_proposal', `${label}: ${proposal.path}`, '', 'complete');
        this.artifacts.addWorkspaceProposal(proposal);
        this.shouldScrollToBottom = true;
      },
      onDone: () => {
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
            content: content || '(applied changes)',
            toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
            steps: finalSteps.length > 0 ? finalSteps : undefined,
          },
        ]);
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
          this.error.set(`Failed to write ${p.path}`);
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

  /** Returns the title of the most recent streaming step for the collapsed header. */
  currentStepTitle(): string {
    const steps = this.streamingSteps();
    if (steps.length === 0) return 'Thinking...';
    return steps[steps.length - 1].title;
  }

  getStepIcon(type: BuilderStepType): string {
    switch (type) {
      case 'thought': return 'psychology';
      case 'tool_call': return 'build';
      case 'tool_result': return 'check_circle';
      case 'inspection_result': return 'visibility';
      case 'workspace_proposal': return 'edit_note';
    }
  }

  private addStep(type: BuilderStepType, title: string, content: string, status: BuilderStepStatus): void {
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

  private completeActiveSteps(typeFilter?: BuilderStepType): void {
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
