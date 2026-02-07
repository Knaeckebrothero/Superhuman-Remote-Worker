import { Component, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RequestService } from '../../core/services/request.service';
import { LLMMessage } from '../../core/models/request.model';

/**
 * Request Viewer component that displays LLM request/response conversations.
 * Shows messages in a messenger-style layout with role-based colors.
 */
@Component({
  selector: 'app-request-viewer',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="viewer-container">
      <!-- Search Bar -->
      <div class="search-bar">
        <input
          type="text"
          class="doc-id-input"
          placeholder="Enter document ID (24 hex chars)..."
          [(ngModel)]="docIdInput"
          (keyup.enter)="onLoad()"
        />
        <button
          class="load-btn"
          (click)="onLoad()"
          [disabled]="requestService.isLoading()"
        >
          {{ requestService.isLoading() ? 'Loading...' : 'Load' }}
        </button>
      </div>

      <!-- Error State -->
      @if (requestService.error()) {
        <div class="error-banner">
          {{ requestService.error() }}
        </div>
      }

      <!-- Loading State -->
      @if (requestService.isLoading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
        </div>
      }

      <!-- Empty State -->
      @if (!requestService.isLoading() && !requestService.hasRequest() && !requestService.error()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4AC;</span>
          <span>Enter a document ID to view the request</span>
          <span class="empty-hint">
            Click on a request ID in Agent Activity to auto-load
          </span>
        </div>
      }

      <!-- Request Content -->
      @if (requestService.hasRequest()) {
        <div class="content-area">
          <!-- Metadata Card -->
          <div class="metadata-card">
            <div class="metadata-row">
              <span class="meta-label">ID:</span>
              <span class="meta-value mono">{{ requestService.request()?._id }}</span>
            </div>
            <div class="metadata-row">
              <span class="meta-label">Job:</span>
              <span class="meta-value mono">{{ requestService.request()?.job_id?.slice(0, 8) }}...</span>
            </div>
            <div class="metadata-row">
              <span class="meta-label">Model:</span>
              <span class="meta-value mono">{{ requestService.request()?.model }}</span>
            </div>
            <div class="metadata-row">
              <span class="meta-label">Iteration:</span>
              <span class="meta-value mono">{{ requestService.request()?.iteration ?? 'N/A' }}</span>
            </div>
            @if (requestService.request()?.latency_ms) {
              <div class="metadata-row">
                <span class="meta-label">Latency:</span>
                <span class="meta-value mono">{{ formatLatency(requestService.request()?.latency_ms!) }}</span>
              </div>
            }
            @if (requestService.tokenSummary()) {
              <div class="metadata-row">
                <span class="meta-label">Tokens:</span>
                <span class="meta-value mono">
                  {{ requestService.tokenSummary()?.prompt }} in /
                  {{ requestService.tokenSummary()?.completion }} out
                  @if (requestService.tokenSummary()?.reasoning) {
                    / {{ requestService.tokenSummary()?.reasoning }} reasoning
                  }
                </span>
              </div>
            }
            @if (requestService.request()?.response?.response_metadata; as meta) {
              @if (meta['finish_reason']) {
                <div class="metadata-row">
                  <span class="meta-label">Finish:</span>
                  <span class="meta-value mono">{{ meta['finish_reason'] }}</span>
                </div>
              }
              @if (meta['system_fingerprint']) {
                <div class="metadata-row">
                  <span class="meta-label">Fingerprint:</span>
                  <span class="meta-value mono">{{ meta['system_fingerprint'] }}</span>
                </div>
              }
            }
            @if (requestService.request()?.request?.model_kwargs; as kwargs) {
              <div class="metadata-row">
                <span class="meta-label">Params:</span>
                <span class="meta-value mono">{{ formatJson(kwargs) }}</span>
              </div>
            }
          </div>

          <!-- Tool Definitions Section (collapsible) -->
          @if (requestService.request()?.request?.tools; as tools) {
            <details class="tools-section">
              <summary class="section-header clickable">
                Tool Definitions ({{ tools.length }})
              </summary>
              <div class="tools-list">
                @for (tool of tools; track tool.function.name) {
                  <div class="tool-def-item">
                    <span class="tool-name">{{ tool.function.name }}</span>
                    <span class="tool-desc">{{ tool.function.description }}</span>
                  </div>
                }
              </div>
            </details>
          }

          <!-- Request Messages Section -->
          <div class="section-header">
            Request ({{ requestService.messageCount() }} messages)
          </div>
          <div class="messages-container">
            @for (msg of requestService.request()?.request?.messages; track $index) {
              <div class="message-wrapper" [class]="'role-' + msg.role">
                <div class="message-bubble" [class]="'bubble-' + msg.role">
                  <div class="message-role">{{ getRoleLabel(msg.role) }}</div>
                  <div class="message-content">{{ msg.content }}</div>
                  @if (msg.tool_calls && msg.tool_calls.length > 0) {
                    <div class="tool-calls-section">
                      <div class="tool-calls-header">Tool Calls:</div>
                      @for (tc of msg.tool_calls; track tc.id) {
                        <div class="tool-call-item">
                          <span class="tool-name">{{ tc.name }}</span>
                          <pre class="tool-args">{{ formatJson(tc.args) }}</pre>
                        </div>
                      }
                    </div>
                  }
                  @if (msg.tool_call_id) {
                    <div class="tool-info">
                      <span class="tool-label">Tool:</span>
                      <span class="tool-value">{{ msg.name }}</span>
                    </div>
                  }
                </div>
              </div>
            }
          </div>

          <!-- Response Section -->
          <div class="section-header">Response</div>
          <div class="messages-container">
            @if (requestService.request()?.response; as resp) {
              <div class="message-wrapper role-assistant">
                <div class="message-bubble bubble-assistant">
                  <div class="message-role">{{ getRoleLabel(resp.role) }}</div>
                  <div class="message-content">{{ resp.content }}</div>
                  @if (resp.tool_calls && resp.tool_calls.length > 0) {
                    <div class="tool-calls-section">
                      <div class="tool-calls-header">Tool Calls:</div>
                      @for (tc of resp.tool_calls; track tc.id) {
                        <div class="tool-call-item">
                          <span class="tool-name">{{ tc.name }}</span>
                          <pre class="tool-args">{{ formatJson(tc.args) }}</pre>
                        </div>
                      }
                    </div>
                  }
                </div>
              </div>
            }
          </div>

          <!-- Reasoning Section (Collapsible) -->
          @if (getReasoningContent()) {
            <details class="reasoning-section">
              <summary class="reasoning-header">
                Reasoning
                @if (requestService.tokenSummary()?.reasoning) {
                  ({{ requestService.tokenSummary()?.reasoning }} tokens)
                }
              </summary>
              <div class="reasoning-content">
                {{ getReasoningContent() }}
              </div>
            </details>
          }
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

      .viewer-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        position: relative;
      }

      /* Search Bar */
      .search-bar {
        display: flex;
        gap: 8px;
        padding: 8px;
        background: var(--surface-0, #313244);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .doc-id-input {
        flex: 1;
        padding: 6px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: var(--panel-bg, #181825);
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-family: 'JetBrains Mono', monospace;
      }

      .doc-id-input:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .load-btn {
        padding: 6px 16px;
        border: none;
        border-radius: 4px;
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .load-btn:hover:not(:disabled) {
        filter: brightness(1.1);
      }

      .load-btn:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      /* Error Banner */
      .error-banner {
        padding: 8px 12px;
        background: rgba(243, 139, 168, 0.15);
        border-bottom: 1px solid #f38ba8;
        color: #f38ba8;
        font-size: 12px;
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
        text-align: center;
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

      /* Content Area */
      .content-area {
        flex: 1;
        overflow: auto;
        padding: 12px;
      }

      /* Metadata Card */
      .metadata-card {
        background: var(--surface-0, #313244);
        border-radius: 8px;
        padding: 12px;
        margin-bottom: 16px;
      }

      .metadata-row {
        display: flex;
        gap: 8px;
        margin-bottom: 4px;
        font-size: 12px;
      }

      .metadata-row:last-child {
        margin-bottom: 0;
      }

      .meta-label {
        color: var(--text-muted, #6c7086);
        min-width: 60px;
      }

      .meta-value {
        color: var(--text-primary, #cdd6f4);
      }

      .meta-value.mono {
        font-family: 'JetBrains Mono', monospace;
      }

      /* Section Headers */
      .section-header {
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted, #6c7086);
        margin: 16px 0 8px 0;
        padding-bottom: 4px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      /* Messages Container */
      .messages-container {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      /* Message Wrapper - controls alignment */
      .message-wrapper {
        display: flex;
      }

      .message-wrapper.role-system {
        justify-content: center;
      }

      .message-wrapper.role-human,
      .message-wrapper.role-user {
        justify-content: flex-end;
      }

      .message-wrapper.role-assistant {
        justify-content: flex-start;
      }

      .message-wrapper.role-tool {
        justify-content: flex-start;
      }

      /* Message Bubbles */
      .message-bubble {
        max-width: 85%;
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
        line-height: 1.5;
      }

      .bubble-system {
        background: rgba(69, 71, 90, 0.5);
        color: var(--text-secondary, #a6adc8);
        font-style: italic;
        text-align: left;
        max-width: 95%;
      }

      .bubble-human,
      .bubble-user {
        background: rgba(166, 227, 161, 0.2);
        border: 1px solid rgba(166, 227, 161, 0.3);
        color: var(--text-primary, #cdd6f4);
      }

      .bubble-assistant {
        background: rgba(137, 180, 250, 0.2);
        border: 1px solid rgba(137, 180, 250, 0.3);
        color: var(--text-primary, #cdd6f4);
      }

      .bubble-tool {
        background: rgba(203, 166, 247, 0.2);
        border: 1px solid rgba(203, 166, 247, 0.3);
        color: var(--text-primary, #cdd6f4);
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
      }

      .message-role {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 4px;
        opacity: 0.7;
      }

      .message-content {
        white-space: pre-wrap;
        word-break: break-word;
      }

      /* Tool Calls Section */
      .tool-calls-section {
        margin-top: 10px;
        padding-top: 10px;
        border-top: 1px solid rgba(255, 255, 255, 0.1);
      }

      .tool-calls-header {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: #fab387;
        margin-bottom: 8px;
      }

      .tool-call-item {
        margin-bottom: 8px;
      }

      .tool-call-item:last-child {
        margin-bottom: 0;
      }

      .tool-name {
        display: inline-block;
        padding: 2px 8px;
        background: rgba(250, 179, 135, 0.2);
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #fab387;
        margin-bottom: 4px;
      }

      .tool-args {
        margin: 4px 0 0 0;
        padding: 8px;
        background: rgba(0, 0, 0, 0.3);
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #a6e3a1;
        overflow-x: auto;
        white-space: pre-wrap;
        word-break: break-all;
        max-height: 150px;
        overflow-y: auto;
      }

      .tool-info {
        margin-top: 6px;
        font-size: 11px;
      }

      .tool-label {
        color: var(--text-muted, #6c7086);
      }

      .tool-value {
        font-family: 'JetBrains Mono', monospace;
        color: #cba6f7;
      }

      /* Tool Definitions Section */
      .tools-section {
        margin-top: 8px;
        background: var(--surface-0, #313244);
        border-radius: 8px;
        overflow: hidden;
      }

      .section-header.clickable {
        cursor: pointer;
        user-select: none;
        border-bottom: none;
        padding: 8px 12px;
        margin: 0;
      }

      .section-header.clickable:hover {
        background: rgba(255, 255, 255, 0.05);
      }

      .tools-list {
        padding: 8px 12px;
        border-top: 1px solid var(--border-color, #45475a);
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .tool-def-item {
        display: flex;
        gap: 8px;
        align-items: baseline;
        font-size: 12px;
      }

      .tool-def-item .tool-name {
        flex-shrink: 0;
      }

      .tool-desc {
        color: var(--text-muted, #6c7086);
        font-size: 11px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      /* Reasoning Section */
      .reasoning-section {
        margin-top: 16px;
        background: var(--surface-0, #313244);
        border-radius: 8px;
        overflow: hidden;
      }

      .reasoning-header {
        padding: 10px 14px;
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        color: var(--text-secondary, #a6adc8);
        user-select: none;
      }

      .reasoning-header:hover {
        background: rgba(255, 255, 255, 0.05);
      }

      .reasoning-content {
        padding: 12px 14px;
        border-top: 1px solid var(--border-color, #45475a);
        font-size: 12px;
        line-height: 1.6;
        color: var(--text-primary, #cdd6f4);
        white-space: pre-wrap;
        max-height: 400px;
        overflow-y: auto;
      }
    `,
  ],
})
export class RequestViewerComponent {
  readonly requestService = inject(RequestService);
  docIdInput = '';

  onLoad(): void {
    this.requestService.loadRequest(this.docIdInput.trim());
  }

  getRoleLabel(role: string): string {
    const labels: Record<string, string> = {
      system: 'SYSTEM',
      human: 'USER',
      user: 'USER',
      assistant: 'ASSISTANT',
      tool: 'TOOL',
    };
    return labels[role] || role.toUpperCase();
  }

  formatLatency(ms: number): string {
    if (ms < 1000) {
      return `${ms}ms`;
    }
    return `${(ms / 1000).toFixed(1)}s`;
  }

  formatJson(obj: Record<string, unknown>): string {
    try {
      return JSON.stringify(obj, null, 2);
    } catch {
      return '[object]';
    }
  }

  getReasoningContent(): string | null {
    const response = this.requestService.request()?.response;
    return response?.additional_kwargs?.reasoning_content ?? null;
  }
}
