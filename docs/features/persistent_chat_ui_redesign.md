# Persistent Chat UI Redesign

## Problem

The persistent agent chat UI renders tool calls as a flat list of individual boxes below the assistant's message content. This creates several issues:

### Visual Noise

A single weather query that returns a clean 6-line response is followed by **7 separate tool call boxes**, each with a wrench icon, tool name, "completed" badge, and collapsible "Result" toggle. The tool call section occupies 3x more vertical space than the actual answer. The user has to scroll past a wall of repetitive `web_search — completed / Result` items to see the next message.

### No Semantic Grouping

All tool calls are displayed identically regardless of type. A `web_search` looks the same as a `read_file`, `run_command`, or `cite_web`. There's no visual hierarchy indicating which calls were part of the same reasoning step or which one produced the content the agent used in its response.

### Tool Calls Detach from Context

Tool calls sit in a separate `div.tool-calls` block below the message body. The agent's natural language response and the work that produced it are visually disconnected. Modern UIs interleave tool status inline with the response flow.

### Missing Progressive Disclosure

The current UI has a single expand level: collapsed summary vs. full raw result in a `<pre>` block. There's no intermediate view (e.g., a one-line output summary before showing the full dump).

### Rough Visual Polish

- The `<details>` element uses browser-default disclosure triangles
- Tool status text (`completed`, `running`) is plain 10px text with no semantic color or icon
- Avatar circles appear on every message (modern UIs show them once per response block)
- Historical messages have a blanket `opacity: 0.7` dim that makes them feel disabled rather than contextual
- No visual distinction between the agent's final answer and intermediate thinking

## Current Implementation

| File | Lines | Purpose |
|------|-------|---------|
| `persistent-chat.component.ts` | 1,099 | Main chat component (template + styles + logic) |
| `persistent-chat.service.ts` | 454 | WebSocket state, message/tool call model |
| `chat-history.component.ts` | 928 | Historical conversation viewer |
| `chat.model.ts` | 77 | Data model interfaces |

**Current tool call rendering** (lines 113-131 of persistent-chat.component.ts):
```html
@if (msg.toolCalls?.length) {
  <div class="tool-calls">
    @for (tc of msg.toolCalls; track tc.id) {
      <div class="tool-call" [class]="'tool-' + tc.status">
        <div class="tool-header">
          <span class="tool-icon">build</span>
          <span class="tool-name">{{ tc.tool }}</span>
          <span class="tool-status">{{ tc.status }}</span>
        </div>
        @if (tc.result) {
          <details class="tool-result">
            <summary>Result</summary>
            <pre>{{ tc.result }}</pre>
          </details>
        }
      </div>
    }
  </div>
}
```

## Design Reference: Modern Coding Agent UIs

### Claude.ai — Inline Status Chips

Tool use appears as compact inline indicators within the response flow. A web search shows "Searched 3 sources" as a single-line annotation, not a separate block per call. Artifacts open in a side panel. The assistant message reads as natural prose with lightweight status annotations woven in.

### ChatGPT — Collapsed Tool Summaries

When ChatGPT invokes browsing or code execution, a brief inline indicator appears ("Searching the web...", "Analyzing..."). Once complete, it collapses to a one-line summary. Web results show numbered citation pills the user can click.

### Devin — Collapsed Progress Cards with Timeline

Each intermediate step (shell command, file edit, browser action) is a collapsed card in a chronological timeline. The chat message itself is the final answer. Users expand individual steps if they care about the details. A timeline slider lets users scrub through the agent's work history.

### Vercel AI SDK — Typed Parts

Messages are arrays of typed parts (`text`, `tool-*`, `reasoning`, `step-start`). Each tool part has a state machine (`input-streaming` -> `output-available` -> `output-error`). Different tool types render as different UI components. Step-start parts act as visual dividers between consecutive tool calls.

### Common Pattern: Three-Level Progressive Disclosure

```
Level 1: Summary line                     [always visible]
         "Searched 5 sources"  or  "Ran web_search x3, cite_web x2"

Level 2: Individual call list             [expand once]
         web_search("weather Bad Orb") → 200 OK, 3 results
         web_search("Bad Orb Hessen") → 200 OK, 2 results

Level 3: Full raw output                  [expand again]
         { "results": [ { "title": "...", ... } ] }
```

## Proposed Changes

### 1. Aggregate Tool Call Summary (Primary Fix)

Replace the flat list of individual tool call boxes with a single **collapsed summary line** that groups calls by tool name.

**Before:**
```
[Message content]

  🔧 web_search    completed
    ▸ Result
  🔧 web_search    completed
    ▸ Result
  🔧 web_search    completed
    ▸ Result
  🔧 cite_web      completed
    ▸ Result
```

**After:**
```
[Message content]

  ▸ Used 4 tools: web_search ×3, cite_web ×1         2.1s
```

Clicking the summary expands to the individual calls (level 2), each still expandable to show raw output (level 3).

**Implementation sketch:**

```html
@if (msg.toolCalls?.length) {
  <details class="tool-summary">
    <summary class="tool-summary-line">
      <span class="tool-summary-icon">&#9662;</span>
      <span class="tool-summary-text">
        Used {{ msg.toolCalls.length }} tool{{ msg.toolCalls.length > 1 ? 's' : '' }}:
        {{ groupToolCalls(msg.toolCalls) }}
      </span>
      @if (allCompleted(msg.toolCalls)) {
        <span class="tool-summary-status completed">
          <span class="status-dot"></span>
        </span>
      }
    </summary>
    <div class="tool-detail-list">
      @for (tc of msg.toolCalls; track tc.id) {
        <details class="tool-detail-item">
          <summary class="tool-detail-header">
            <span class="tool-detail-name">{{ tc.tool }}</span>
            <span class="tool-detail-args">{{ formatArgs(tc.args) }}</span>
            <span class="tool-detail-status" [class]="tc.status">{{ tc.status }}</span>
          </summary>
          @if (tc.result) {
            <pre class="tool-detail-result">{{ tc.result }}</pre>
          }
        </details>
      }
    </div>
  </details>
}
```

Helper method:
```typescript
groupToolCalls(calls: ToolCallInfo[]): string {
  const counts = new Map<string, number>();
  for (const tc of calls) {
    counts.set(tc.tool, (counts.get(tc.tool) || 0) + 1);
  }
  return Array.from(counts.entries())
    .map(([name, count]) => count > 1 ? `${name} x${count}` : name)
    .join(', ');
}
```

### 2. Status Indicators with Semantic Color

Replace plain text status with small colored dots/pills:

| Status | Color | Icon |
|--------|-------|------|
| `running` | `#f9e2af` (yellow) | Animated spinner (existing) |
| `completed` | `#a6e3a1` (green) | Small filled circle |
| `denied` | `#f38ba8` (red) | Small x icon |
| `pending` | `#6c7086` (muted) | Empty circle |

The status dot replaces the text label at the summary level. Text labels remain at level 2 (expanded individual calls).

### 3. Inline Tool Progress During Streaming

While the agent is actively calling tools (before the final message content arrives), show a compact inline activity indicator within the message area:

```
  ⟳ Searching the web...              [during execution]
  ✓ Searched 3 sources                [after completion, before content]

  [Message content appears here as it streams]

  ▸ Used 3 tools: web_search ×3      [final collapsed summary]
```

This requires tracking tool call events separately from the final message assembly. The service already emits `tool_start` and `tool_complete` WebSocket events — surface them as transient inline indicators that resolve into the collapsed summary once the turn completes.

### 4. Improved Markdown & Code Block Rendering

The current `<markdown>` component uses default ngx-markdown styling. Add targeted CSS for:

**Code blocks:**
```scss
.message-body :deep(pre) {
  background: var(--panel-bg, #181825);
  border-radius: 8px;
  padding: 12px 16px;
  overflow-x: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--border-color, #313244);
}

.message-body :deep(pre code) {
  background: transparent;
  padding: 0;
}

.message-body :deep(code) {
  background: rgba(203, 166, 247, 0.15);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.9em;
}
```

**Tables (for structured responses like the weather example):**
```scss
.message-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
  font-size: 13px;
}

.message-body :deep(th),
.message-body :deep(td) {
  padding: 6px 12px;
  border-bottom: 1px solid var(--border-color, #313244);
  text-align: left;
}

.message-body :deep(th) {
  font-weight: 600;
  color: var(--accent-color, #cba6f7);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.message-body :deep(tr:hover) {
  background: rgba(255, 255, 255, 0.03);
}
```

### 5. Clean Up Message Chrome

**Reduce avatar repetition:** Only show the avatar on the first message in a consecutive block from the same role. Subsequent messages from the same role show with an indent but no avatar.

**Remove historical dim:** Replace `opacity: 0.7` on historical messages with a subtle left-border indicator or a "conversation resumed" divider between historical and live messages.

```html
@if (isFirstLiveMessage(i)) {
  <div class="session-divider">
    <span>Session resumed</span>
  </div>
}
```

**Thinking indicator:** Replace the current three-dot animation with a contextual label:
```
  Thinking...                    [initial, no tool calls yet]
  Running web_search...          [tool in progress]
  Processing results...          [post-tool, generating response]
```

### 6. Compact Tool Summary Styling

```scss
.tool-summary {
  margin-top: 8px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-muted, #6c7086);
}

.tool-summary-line {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  cursor: pointer;
  border-radius: 6px;
  transition: background 0.15s;
  list-style: none;  /* Remove default marker */
}

.tool-summary-line:hover {
  background: rgba(255, 255, 255, 0.04);
}

.tool-summary-line::-webkit-details-marker {
  display: none;
}

.tool-summary-icon {
  font-size: 10px;
  transition: transform 0.15s;
}

details[open] > .tool-summary-line .tool-summary-icon {
  transform: rotate(90deg);
}

.tool-summary-text {
  flex: 1;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}

.tool-summary-status.completed .status-dot {
  background: #a6e3a1;
}

.tool-detail-list {
  padding: 4px 0 4px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.tool-detail-item {
  font-size: 11px;
  color: var(--text-muted, #6c7086);
}

.tool-detail-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  cursor: pointer;
  border-radius: 4px;
  list-style: none;
}

.tool-detail-header::-webkit-details-marker {
  display: none;
}

.tool-detail-header:hover {
  background: rgba(255, 255, 255, 0.03);
}

.tool-detail-name {
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent-color, #cba6f7);
}

.tool-detail-args {
  color: var(--text-muted, #6c7086);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
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

.tool-detail-status.completed { color: #a6e3a1; }
.tool-detail-status.running { color: #f9e2af; }
.tool-detail-status.denied { color: #f38ba8; }

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
}
```

## Implementation Priority

| # | Change | Impact | Effort |
|---|--------|--------|--------|
| 1 | Aggregate tool call summary | High — eliminates the primary visual noise | Small — template + one helper method |
| 2 | Status dots with semantic color | Medium — cleaner at a glance | Small — CSS only |
| 3 | Markdown/code block styling | Medium — polished content rendering | Small — CSS only |
| 4 | Historical/live message divider | Medium — clearer session context | Small — template + CSS |
| 5 | Inline tool progress during streaming | High — feels responsive and modern | Medium — requires tracking streaming tool events separately |
| 6 | Avatar deduplication | Low — minor visual cleanup | Small — template logic |

Items 1-4 can be done in a single PR. Item 5 requires changes to both the component and the service's WebSocket event handling. Item 6 is cosmetic polish.

## Files to Modify

| File | Changes |
|------|---------|
| `cockpit/src/app/shared/components/persistent-chat/persistent-chat.component.ts` | Template: aggregated tool summary, status dots, avatar dedup, session divider. Styles: tool summary CSS, markdown deep styles. Logic: `groupToolCalls()`, `allCompleted()`, `formatArgs()`, `isFirstLiveMessage()` helpers. |
| `cockpit/src/app/shared/components/chat-history/chat-history.component.ts` | Same aggregated tool pattern for historical view consistency |
| `cockpit/src/app/core/services/persistent-chat.service.ts` | (Item 5 only) Surface tool_start/tool_complete events as transient streaming indicators |

## Non-Goals

- **Split-screen layout** (Cursor/OpenHands style) — the persistent agent is a chat-first interface, not an IDE. A side panel for artifacts may come later but is out of scope here.
- **Reasoning/thinking trace display** — the persistent agent doesn't expose chain-of-thought. If it does in the future, add a collapsible "Thought for Xs" block following Claude.ai's pattern.
- **Citation pills** — the agent already renders citations as inline markdown links. No change needed unless we want hover-preview cards later.
