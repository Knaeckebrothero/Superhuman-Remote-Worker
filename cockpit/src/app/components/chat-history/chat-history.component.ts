import { Component, inject, computed, effect, signal, ElementRef, viewChild } from '@angular/core';
import { DataService } from '../../core/services/data.service';
import { RequestService } from '../../core/services/request.service';
import { ChatEntry } from '../../core/models/chat.model';

/** Parsed shell pane from <open_shells> block */
interface ShellPane {
  name: string;
  type: string;
  content: string;
  hasNewOutput: boolean;
  isIdle: boolean;
}

/**
 * Chat History component that displays a clean sequential view of conversations.
 * Shows input -> response turns in a messenger-style layout.
 *
 * Now uses DataService for index-based filtering.
 * Chat entries are filtered by slider position - shows entries up to sliderIndex.
 */
@Component({
  selector: 'app-chat-history',
  standalone: true,
  template: `
    <div class="chat-container">
      <!-- Header -->
      <div class="chat-header">
        <span class="header-title">Chat History</span>
        @if (data.currentJobId()) {
          <span class="entry-count">{{ entryCount() }}</span>
          <button class="refresh-btn" (click)="data.refresh()" title="Refresh">
            &#x21BB;
          </button>
        }
      </div>

      <!-- Loading State -->
      @if (data.isLoading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
        </div>
      }

      <!-- Error State -->
      @if (data.error()) {
        <div class="error-state">
          <span>{{ data.error() }}</span>
          <button (click)="data.refresh()">Retry</button>
        </div>
      }

      <!-- Empty State -->
      @if (!data.isLoading() && !data.error() && entries().length === 0 && data.currentJobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4AC;</span>
          <span>No chat history for this job</span>
          <span class="empty-hint">Chat entries are created when the agent runs</span>
        </div>
      }

      <!-- No Job Selected -->
      @if (!data.currentJobId() && !data.isLoading()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F50D;</span>
          <span>Select a job to view chat history</span>
          <span class="empty-hint">Use the job dropdown at the top to select a job</span>
        </div>
      }

      <!-- Chat Messages -->
      @if (entries().length > 0) {
        <div class="chat-list" #chatList>
          @for (entry of entries(); track entry._id; let idx = $index) {
            <div class="chat-turn">
              <!-- Turn Header -->
              <div class="turn-header">
                <span class="turn-number">#{{ idx + 1 }}</span>
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

              <!-- Shell State (if present) -->
              @if (entry.shell_state) {
                @let panes = parseShellState(entry.shell_state);
                @if (panes.length > 0) {
                  <details class="shell-state-section">
                    <summary class="shell-state-header">
                      <span class="shell-icon">&#x1F4BB;</span>
                      <span>Shell State</span>
                      <span class="shell-pane-count">{{ panes.length }} pane{{ panes.length !== 1 ? 's' : '' }}</span>
                    </summary>
                    <div class="shell-widget">
                      @let activeTab = getSelectedTab(entry._id, panes[0].name);
                      <div class="shell-tab-bar">
                        @for (pane of panes; track pane.name) {
                          <button
                            class="shell-tab"
                            [class.active]="activeTab === pane.name"
                            [class.new-output]="pane.hasNewOutput"
                            [class.idle]="pane.isIdle"
                            (click)="selectTab(entry._id, pane.name)"
                          >
                            <span class="tab-type-badge" [attr.data-type]="pane.type">{{ pane.type }}</span>
                            <span class="tab-name">{{ pane.name }}</span>
                            @if (pane.hasNewOutput) {
                              <span class="new-output-badge">NEW</span>
                            }
                            @if (pane.isIdle) {
                              <span class="idle-badge">idle</span>
                            }
                          </button>
                        }
                      </div>
                      <div class="shell-terminal-pane">
                        @for (pane of panes; track pane.name) {
                          @if (activeTab === pane.name) {
                            <pre class="terminal-content">{{ pane.content || '(empty)' }}</pre>
                          }
                        }
                      </div>
                    </div>
                  </details>
                }
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

      <!-- Position indicator -->
      @if (entries().length > 0) {
        <div class="position-bar">
          <span class="position-info">
            Showing {{ entries().length }} chat turns
          </span>
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
        gap: 8px;
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

      .entry-count {
        margin-left: auto;
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
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

      /* Position Bar */
      .position-bar {
        display: flex;
        align-items: center;
        padding: 8px 12px;
        background: var(--surface-0, #313244);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .position-info {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      /* Shell State Widget */
      .shell-state-section {
        margin: 8px 0;
        background: #11111b;
        border: 1px solid #313244;
        border-radius: 8px;
        overflow: hidden;
      }

      .shell-state-header {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 10px;
        font-size: 11px;
        font-weight: 600;
        color: #94e2d5;
        cursor: pointer;
        user-select: none;
        background: rgba(0, 0, 0, 0.3);
      }

      .shell-state-header:hover {
        background: rgba(0, 0, 0, 0.5);
      }

      .shell-icon {
        font-size: 14px;
      }

      .shell-pane-count {
        margin-left: auto;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .shell-widget {
        display: flex;
        flex-direction: column;
      }

      .shell-tab-bar {
        display: flex;
        gap: 0;
        background: #181825;
        border-bottom: 1px solid #313244;
        overflow-x: auto;
      }

      .shell-tab {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 12px;
        border: none;
        background: transparent;
        color: var(--text-muted, #6c7086);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        white-space: nowrap;
        border-bottom: 2px solid transparent;
        transition: all 0.15s ease;
      }

      .shell-tab:hover {
        background: rgba(255, 255, 255, 0.05);
        color: var(--text-primary, #cdd6f4);
      }

      .shell-tab.active {
        color: #94e2d5;
        border-bottom-color: #94e2d5;
        background: rgba(148, 226, 213, 0.05);
      }

      .tab-type-badge {
        font-size: 9px;
        padding: 1px 4px;
        border-radius: 3px;
        font-weight: 600;
        text-transform: uppercase;
      }

      .tab-type-badge[data-type="shell"] {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .tab-type-badge[data-type="claude-code"] {
        background: rgba(203, 166, 247, 0.2);
        color: #cba6f7;
      }

      .tab-type-badge[data-type="ssh"] {
        background: rgba(137, 180, 250, 0.2);
        color: #89b4fa;
      }

      .tab-name {
        font-weight: 600;
      }

      .new-output-badge {
        font-size: 8px;
        padding: 1px 4px;
        border-radius: 3px;
        background: rgba(250, 179, 135, 0.3);
        color: #fab387;
        font-weight: 700;
      }

      .idle-badge {
        font-size: 9px;
        color: var(--text-muted, #6c7086);
        font-style: italic;
      }

      .shell-terminal-pane {
        background: #11111b;
        min-height: 60px;
        max-height: 250px;
        overflow: auto;
      }

      .terminal-content {
        margin: 0;
        padding: 10px 12px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        line-height: 1.5;
        color: #a6e3a1;
        white-space: pre-wrap;
        word-break: break-word;
      }
    `,
  ],
})
export class ChatHistoryComponent {
  readonly data = inject(DataService);
  private readonly requestService = inject(RequestService);

  // Reference to the chat list container for auto-scrolling
  private readonly chatListRef = viewChild<ElementRef<HTMLDivElement>>('chatList');

  // Track the previous entry count to detect when new entries are added
  private previousEntryCount = 0;

  // Track selected tab per chat entry (entry._id -> pane name)
  private readonly selectedTabs = signal<Map<string, string>>(new Map());

  // Cache parsed shell panes to avoid re-parsing on every change detection
  private readonly parsedShellCache = new Map<string, ShellPane[]>();

  // Use DataService's visible chat entries (filtered by slider position)
  readonly entries = computed(() => this.data.visibleChatEntries());

  // Entry count display
  readonly entryCount = computed(() => {
    return `${this.entries().length} turns`;
  });

  constructor() {
    // Effect to auto-scroll when entries change
    effect(() => {
      const entries = this.entries();
      const currentCount = entries.length;
      const chatList = this.chatListRef();

      // Scroll to bottom when entries are added (count increased)
      if (chatList && currentCount > this.previousEntryCount) {
        // Use requestAnimationFrame to ensure DOM has updated
        requestAnimationFrame(() => {
          const el = chatList.nativeElement;
          el.scrollTop = el.scrollHeight;
        });
      }

      this.previousEntryCount = currentCount;
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
    return date.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
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
    const entries = this.entries();
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

  /**
   * Parse a <terminal_state> block into structured pane objects.
   * Uses a cache keyed by raw content to avoid re-parsing.
   *
   * Format:
   *   <terminal_state>
   *   [name] (type)
   *   [name2] (type2) [NEW OUTPUT]
   *   </terminal_state>
   */
  parseShellState(raw: string): ShellPane[] {
    const cached = this.parsedShellCache.get(raw);
    if (cached) return cached;

    const panes: ShellPane[] = [];

    // Strip <terminal_state> wrapper
    const inner = raw
      .replace(/<\/?terminal_state>/g, '')
      .trim();

    if (!inner) {
      this.parsedShellCache.set(raw, panes);
      return panes;
    }

    // Split into pane blocks by header lines: [name] (type) ...
    const headerPattern = /^\[([^\]]+)\]\s+\(([^)]+)\)\s*(.*)$/;
    const lines = inner.split('\n');
    let current: { name: string; type: string; flags: string; contentLines: string[] } | null = null;

    for (const line of lines) {
      const match = line.match(headerPattern);
      if (match) {
        // Save previous pane
        if (current) {
          panes.push(this.buildPane(current));
        }
        current = {
          name: match[1],
          type: match[2],
          flags: match[3].trim(),
          contentLines: [],
        };
      } else if (current) {
        current.contentLines.push(line);
      }
    }

    // Don't forget the last pane
    if (current) {
      panes.push(this.buildPane(current));
    }

    this.parsedShellCache.set(raw, panes);
    return panes;
  }

  private buildPane(raw: { name: string; type: string; flags: string; contentLines: string[] }): ShellPane {
    const hasNewOutput = raw.flags.includes('[NEW OUTPUT]');
    const idleMatch = raw.flags.match(/idle\s+\d+/);
    const isIdle = !!idleMatch;

    // Trim trailing empty lines from content
    const contentLines = [...raw.contentLines];
    while (contentLines.length > 0 && contentLines[contentLines.length - 1].trim() === '') {
      contentLines.pop();
    }

    return {
      name: raw.name,
      type: raw.type,
      content: contentLines.join('\n'),
      hasNewOutput,
      isIdle,
    };
  }

  /** Get currently selected tab for a chat entry, defaulting to first pane name if provided. */
  getSelectedTab(entryId: string, fallback: string = ''): string {
    return this.selectedTabs().get(entryId) || fallback;
  }

  /** Select a tab for a specific chat entry. */
  selectTab(entryId: string, paneName: string): void {
    this.selectedTabs.update(map => {
      const next = new Map(map);
      next.set(entryId, paneName);
      return next;
    });
  }
}
