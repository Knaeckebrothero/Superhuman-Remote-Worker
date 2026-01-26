import { Component, inject, effect } from '@angular/core';
import { ChatService } from '../../core/services/chat.service';
import { RequestService } from '../../core/services/request.service';
import { AuditService } from '../../core/services/audit.service';
import { ChatEntry, ChatInput } from '../../core/models/chat.model';

/**
 * Chat History component that displays a clean sequential view of conversations.
 * Shows input -> response turns in a messenger-style layout.
 */
@Component({
  selector: 'app-chat-history',
  standalone: true,
  template: `
    <div class="chat-container">
      <!-- Header -->
      <div class="chat-header">
        <span class="header-title">Chat History</span>
        @if (chat.selectedJobId()) {
          <button class="refresh-btn" (click)="chat.refresh()" title="Refresh">
            &#x21BB;
          </button>
        }
      </div>

      <!-- Loading State -->
      @if (chat.isLoading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
        </div>
      }

      <!-- Error State -->
      @if (chat.error()) {
        <div class="error-state">
          <span>{{ chat.error() }}</span>
          <button (click)="chat.refresh()">Retry</button>
        </div>
      }

      <!-- Empty State -->
      @if (!chat.isLoading() && !chat.error() && chat.entries().length === 0 && chat.selectedJobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4AC;</span>
          <span>No chat history for this job</span>
          <span class="empty-hint">Chat entries are created when the agent runs</span>
        </div>
      }

      <!-- No Job Selected -->
      @if (!chat.selectedJobId() && !chat.isLoading()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F50D;</span>
          <span>Select a job to view chat history</span>
          <span class="empty-hint">Use the job dropdown at the top to select a job</span>
        </div>
      }

      <!-- Chat Messages -->
      @if (chat.entries().length > 0) {
        <div class="chat-list">
          @for (entry of chat.entries(); track entry._id; let idx = $index) {
            <div class="chat-turn">
              <!-- Turn Header -->
              <div class="turn-header">
                <span class="turn-number">#{{ entry.sequence_number }}</span>
                <span class="phase-badge" [class.strategic]="entry.phase === 'strategic'" [class.tactical]="entry.phase === 'tactical'">
                  {{ entry.phase || 'unknown' }}
                </span>
                <span class="iteration">iter {{ entry.iteration }}</span>
                @if (entry.latency_ms) {
                  <span class="latency">{{ formatLatency(entry.latency_ms) }}</span>
                }
                <span class="timestamp">{{ formatTime(entry.timestamp) }}</span>
              </div>

              <!-- Human Input Messages only (tool results are shown with tool calls below) -->
              @for (input of entry.inputs; track $index) {
                @if (input.type === 'human') {
                  <div class="message input-message">
                    <div class="message-header">
                      <span class="message-type human">&#x1F464; Human</span>
                    </div>
                    <div class="message-content">{{ input.content_preview || input.content }}</div>
                  </div>
                }
              }

              <!-- Reasoning (if present) -->
              @if (entry.reasoning) {
                <details class="reasoning-section">
                  <summary class="reasoning-header">
                    <span class="reasoning-icon">&#x1F9E0;</span>
                    <span>Reasoning</span>
                  </summary>
                  <div class="reasoning-content">{{ entry.reasoning.content_preview || entry.reasoning.content }}</div>
                </details>
              }

              <!-- Response Message (only show if there's content or tool calls) -->
              @if (entry.response.content_preview || entry.response.content || (entry.response.tool_calls && entry.response.tool_calls.length > 0)) {
                <div class="message response-message">
                  <div class="message-header">
                    <span class="message-type assistant">&#x1F916; Assistant</span>
                    @if (entry.request_id) {
                      <span
                        class="request-link"
                        (click)="onRequestIdClick(entry.request_id)"
                        title="View full request"
                      >
                        {{ entry.request_id.slice(0, 8) }}...
                      </span>
                    }
                  </div>
                  @if (entry.response.content_preview || entry.response.content) {
                    <div class="message-content">{{ entry.response.content_preview || entry.response.content }}</div>
                  }

                  <!-- Tool Calls with Results -->
                  @if (entry.response.tool_calls && entry.response.tool_calls.length > 0) {
                    <div class="tool-calls-section">
                      @for (tc of entry.response.tool_calls; track tc.id) {
                        <details class="tool-call-item">
                          <summary class="tool-call-header">
                            <span class="tool-icon">&#x1F527;</span>
                            <span class="tool-name">{{ tc.name }}</span>
                            <span class="tool-args-preview">{{ tc.args_preview }}</span>
                          </summary>
                          <div class="tool-call-result">
                            @if (getToolResult(idx, tc.id); as result) {
                              {{ result }}
                            } @else {
                              <span class="no-result">Result pending or not available</span>
                            }
                          </div>
                        </details>
                      }
                    </div>
                  }
                </div>
              }
            </div>
          }
        </div>
      }

      <!-- Pagination -->
      @if (chat.totalEntries() > 0) {
        <div class="pagination">
          <span class="page-info">{{ chat.paginationSummary() }}</span>
          <div class="page-controls">
            <button
              class="page-btn"
              (click)="chat.firstPage()"
              [disabled]="!chat.canGoPrev()"
              title="First page"
            >
              &lt;&lt;
            </button>
            <button
              class="page-btn"
              (click)="chat.previousPage()"
              [disabled]="!chat.canGoPrev()"
              title="Previous page"
            >
              &lt; Prev
            </button>
            <span class="page-number">
              Page {{ chat.currentPage() }} of {{ chat.totalPages() }}
            </span>
            <button
              class="page-btn"
              (click)="chat.nextPage()"
              [disabled]="!chat.canGoNext()"
              title="Next page"
            >
              Next &gt;
            </button>
            <button
              class="page-btn"
              (click)="chat.lastPage()"
              [disabled]="!chat.canGoNext()"
              title="Last page"
            >
              &gt;&gt;
            </button>
          </div>
        </div>
      }
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: hidden;
      }

      .chat-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        position: relative;
      }

      /* Header */
      .chat-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .header-title {
        font-size: 12px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .refresh-btn {
        padding: 4px 8px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 14px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .refresh-btn:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      /* Loading Overlay */
      .loading-overlay {
        position: absolute;
        top: 50px;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(17, 17, 27, 0.8);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 10;
      }

      .spinner {
        width: 32px;
        height: 32px;
        border: 3px solid var(--surface-0, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }

      /* Error State */
      .error-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        color: #f38ba8;
        flex: 1;
      }

      .error-state button {
        padding: 8px 16px;
        border: 1px solid #f38ba8;
        border-radius: 4px;
        background: transparent;
        color: #f38ba8;
        cursor: pointer;
      }

      .error-state button:hover {
        background: rgba(243, 139, 168, 0.1);
      }

      /* Empty State */
      .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        color: var(--text-muted, #6c7086);
        flex: 1;
      }

      .empty-icon {
        font-size: 48px;
        opacity: 0.5;
      }

      .empty-hint {
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        opacity: 0.6;
        margin-top: 8px;
      }

      /* Chat List */
      .chat-list {
        flex: 1;
        overflow: auto;
        padding: 12px;
      }

      .chat-turn {
        margin-bottom: 16px;
        padding-bottom: 16px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .chat-turn:last-child {
        border-bottom: none;
        margin-bottom: 0;
      }

      /* Turn Header */
      .turn-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
        font-size: 11px;
      }

      .turn-number {
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .phase-badge {
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        background: var(--surface-0, #313244);
        color: var(--text-muted, #6c7086);
      }

      .phase-badge.strategic {
        background: rgba(203, 166, 247, 0.2);
        color: #cba6f7;
      }

      .phase-badge.tactical {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .iteration {
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .latency {
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .timestamp {
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
        margin-left: auto;
      }

      /* Messages */
      .message {
        margin-bottom: 8px;
        border-radius: 8px;
        overflow: hidden;
      }

      .input-message {
        background: rgba(137, 180, 250, 0.1);
        border-left: 3px solid #89b4fa;
        margin-right: 40px;
      }

      .response-message {
        background: rgba(166, 227, 161, 0.1);
        border-left: 3px solid #a6e3a1;
        margin-left: 40px;
      }

      .message-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 6px 10px;
        background: rgba(0, 0, 0, 0.15);
        font-size: 11px;
      }

      .message-type {
        font-weight: 600;
      }

      .message-type.human {
        color: #89b4fa;
      }

      .message-type.tool {
        color: #cba6f7;
      }

      .message-type.assistant {
        color: #a6e3a1;
      }

      .message-content {
        padding: 10px;
        font-size: 12px;
        line-height: 1.5;
        color: var(--text-primary, #cdd6f4);
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 300px;
        overflow-y: auto;
      }

      /* Tool Calls Section */
      .tool-calls-section {
        border-top: 1px solid rgba(255, 255, 255, 0.05);
      }

      .tool-call-item {
        background: rgba(203, 166, 247, 0.1);
        border-left: 3px solid #cba6f7;
        margin: 0;
      }

      .tool-call-item + .tool-call-item {
        border-top: 1px solid rgba(255, 255, 255, 0.05);
      }

      .tool-call-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        font-size: 11px;
        color: #cba6f7;
        cursor: pointer;
        user-select: none;
        background: rgba(0, 0, 0, 0.1);
      }

      .tool-call-header:hover {
        background: rgba(0, 0, 0, 0.2);
      }

      .tool-icon {
        font-size: 14px;
        flex-shrink: 0;
      }

      .tool-name {
        font-family: 'JetBrains Mono', monospace;
        font-weight: 600;
        flex-shrink: 0;
      }

      .tool-args-preview {
        color: var(--text-muted, #6c7086);
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
        min-width: 0;
      }

      .tool-call-result {
        padding: 10px;
        font-size: 12px;
        line-height: 1.5;
        color: var(--text-primary, #cdd6f4);
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 300px;
        overflow-y: auto;
        background: rgba(0, 0, 0, 0.05);
      }

      .no-result {
        color: var(--text-muted, #6c7086);
        font-style: italic;
      }

      /* Reasoning */
      .reasoning-section {
        margin: 8px 40px 8px 0;
        background: rgba(249, 226, 175, 0.1);
        border-left: 3px solid #f9e2af;
        border-radius: 8px;
        overflow: hidden;
      }

      .reasoning-header {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 10px;
        font-size: 11px;
        font-weight: 600;
        color: #f9e2af;
        cursor: pointer;
        user-select: none;
        background: rgba(0, 0, 0, 0.1);
      }

      .reasoning-header:hover {
        background: rgba(0, 0, 0, 0.2);
      }

      .reasoning-icon {
        font-size: 14px;
      }

      .reasoning-content {
        padding: 10px;
        font-size: 12px;
        line-height: 1.5;
        color: var(--text-primary, #cdd6f4);
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 400px;
        overflow-y: auto;
      }

      /* Request Link */
      .request-link {
        color: #89b4fa;
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        text-decoration: underline;
        text-decoration-style: dotted;
        text-underline-offset: 2px;
      }

      .request-link:hover {
        color: #b4befe;
        text-decoration-style: solid;
      }

      /* Pagination */
      .pagination {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: var(--surface-0, #313244);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .page-info {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      .page-controls {
        display: flex;
        align-items: center;
        gap: 12px;
      }

      .page-btn {
        padding: 4px 12px;
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .page-btn:hover:not(:disabled) {
        background: var(--panel-header-bg, #1e1e2e);
        color: var(--text-primary, #cdd6f4);
        border-color: var(--text-muted, #6c7086);
      }

      .page-btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      .page-number {
        font-size: 12px;
        color: var(--text-secondary, #a6adc8);
      }
    `,
  ],
})
export class ChatHistoryComponent {
  readonly chat = inject(ChatService);
  private readonly requestService = inject(RequestService);
  private readonly auditService = inject(AuditService);

  constructor() {
    // Watch for job selection changes in AuditService and load chat history
    effect(() => {
      const jobId = this.auditService.selectedJobId();
      if (jobId && jobId !== this.chat.selectedJobId()) {
        this.chat.loadChatHistory(jobId);
      } else if (!jobId) {
        this.chat.clear();
      }
    });
  }

  formatLatency(ms: number): string {
    if (ms < 1000) {
      return `${ms}ms`;
    }
    return `${(ms / 1000).toFixed(1)}s`;
  }

  formatTime(timestamp: string): string {
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  onRequestIdClick(requestId: string): void {
    this.requestService.loadRequest(requestId);
  }

  /**
   * Get the result of a tool call by looking at the next entry's inputs.
   * Tool results appear as inputs in the following entry with matching tool_call_id.
   */
  getToolResult(currentIndex: number, toolCallId: string): string | null {
    const entries = this.chat.entries();
    const nextEntry = entries[currentIndex + 1];
    if (!nextEntry) {
      return null;
    }

    const toolResult = nextEntry.inputs.find(
      input => input.type === 'tool' && input.tool_call_id === toolCallId
    );

    if (toolResult) {
      return toolResult.content_preview || toolResult.content;
    }
    return null;
  }
}
