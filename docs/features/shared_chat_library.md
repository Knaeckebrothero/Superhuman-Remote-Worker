# Shared Chat UI Library — Design Document

> **Status**: Draft
> **Purpose**: Extract a reusable Angular chat UI library from Advanced-LLM-Chat (develop) for use in both the Advanced-LLM-Chat project and the SRW cockpit.

## Motivation

Both projects need an agentic chat UI that renders streaming responses with tool calls, reasoning steps, markdown, and file attachments. The Advanced-LLM-Chat (develop) already has a polished, production-tested implementation. The cockpit builder derived from it but lost quality. Rather than maintaining two divergent implementations, we extract one shared library and optimize it once.

## Core Idea

The library is a **display layer**. It receives messages from an Observable (or Signal) provided by the consuming app and renders them. Each app has its own service that handles streaming, persistence, and state — the library doesn't care where messages come from.

```
┌─────────────────────────────────────────────────┐
│  App (Advanced-LLM-Chat / SRW Cockpit)          │
│                                                   │
│  ┌─────────────────────┐                          │
│  │ ChatStateService     │  ← app-specific         │
│  │ (streaming, storage, │     (IndexedDB, REST,    │
│  │  sync, API calls)    │      signals, whatever)  │
│  └────────┬────────────┘                          │
│           │ Observable<Message[]>                  │
│           │ Observable<Message | null>  (streaming)│
│           │ Observable<boolean>         (isStreaming)│
│           ▼                                       │
│  ┌─────────────────────┐                          │
│  │ ngx-agent-chat       │  ← shared library       │
│  │ (display + input)    │     (components, models, │
│  │                      │      scroll, markdown)   │
│  └────────┬────────────┘                          │
│           │ @Output events                        │
│           │ (messageSent, edit, delete, rate, etc.)│
│           ▼                                       │
│  ChatStateService handles the action              │
└─────────────────────────────────────────────────┘
```

## Distribution

**Separate repo, installed via git URL** — same pattern as CitationEngine.

```bash
npm install git+https://github.com/<org>/ngx-agent-chat.git#v1.0.0
```

Angular library (`ng generate library ngx-agent-chat`), built with `ng-packagr`.

---

## What The Library Contains

### 1. Models

The shared message types. Extracted from Advanced-LLM-Chat's `data/models/message.model.ts`.

```typescript
// Message types
export type MessageType = 'text' | 'agent';

// Agent step types — extensible for future features
export type AgentStepType =
  | 'thought'           // Reasoning
  | 'tool_call'         // Tool invocation
  | 'tool_result'       // Tool output
  | 'observation'       // Agent observation
  | 'approval_request'  // Needs user approval (interactive agent)
  | 'file_change';      // File diff display (interactive agent)

export type AgentStatus = 'thinking' | 'responding' | 'complete' | 'error' | 'waiting';

export interface IMessageMetadata {
  id: number;
  conversationId: string;
  roleName: string;
  time: Date;
  version?: number;
  lastModified?: number;
  rating?: number | null;
}

export interface ITextContent {
  type: 'text';
  content: string;
  attachments?: IFilePreview[];
}

export interface IAgentStep {
  id: string;
  type: AgentStepType;
  title: string;
  content: string;
  timestamp: number;
  duration?: number;
  callId?: string;
  metadata?: Record<string, unknown>;
}

export interface IAgentContent {
  type: 'agent';
  steps: IAgentStep[];
  content: string;
  status: AgentStatus;
  error?: string;
}

export type IMessageContent = ITextContent | IAgentContent;

export interface IMessage<T extends IMessageContent = IMessageContent> {
  metadata: IMessageMetadata;
  content: T;
}

// File attachments
export enum FileType { IMAGE = 'image', VIDEO = 'video', AUDIO = 'audio', DOCUMENT = 'document', OTHER = 'other' }
export enum UploadStatus { PENDING = 'pending', UPLOADING = 'uploading', COMPLETED = 'completed', FAILED = 'failed' }

export interface IFilePreview {
  id: string;
  file?: File;
  name: string;
  size: number;
  sizeFormatted: string;
  type: FileType;
  mimeType: string;
  preview?: string;
  uploadStatus: UploadStatus;
  error?: string;
  transcript?: string;
}
```

### 2. Message Class

The `Message` class from Advanced-LLM-Chat — pure data, no service dependencies. Factory methods, type guards, serialization, display helpers. Included as-is.

### 3. Components

All standalone, all use Material for structure, all themed via CSS variables.

#### `<ngx-chat-container>` — The main component

Composes messages + input + smart scroll. **This is what apps drop in.**

```typescript
@Component({ selector: 'ngx-chat-container', standalone: true })
export class ChatContainerComponent {
  // Data in (from app's service via Observable/Signal)
  @Input() messages: Message[] = [];
  @Input() streamingMessage: Message<IAgentContent> | null = null;
  @Input() isStreaming = false;
  @Input() isLoading = false;
  @Input() hasReachedEnd = false;
  @Input() isMobile = false;

  // Display toggles
  @Input() showActions = true;
  @Input() showAvatars = true;
  @Input() showTimestamps = true;
  @Input() enableVoice = true;
  @Input() enableCamera = false;
  @Input() enableFiles = true;
  @Input() enableEditing = true;
  @Input() enableRating = true;
  @Input() placeholder = 'Type a message...';

  // Events out (app handles all business logic)
  @Output() messageSent = new EventEmitter<string>();
  @Output() filesSelected = new EventEmitter<IFilePreview[]>();
  @Output() stopRequested = new EventEmitter<void>();
  @Output() editMessage = new EventEmitter<{ messageId: number; content: string }>();
  @Output() deleteMessage = new EventEmitter<number>();
  @Output() regenerateMessage = new EventEmitter<Message>();
  @Output() rateMessage = new EventEmitter<{ messageId: number; rating: number | null }>();
  @Output() approvalResponse = new EventEmitter<{ stepId: string; approved: boolean }>();
  @Output() loadOlderMessages = new EventEmitter<void>();
}
```

Owns the smart scroll logic (anchor-based, user intent detection, mobile debouncing) from Advanced-LLM-Chat's `ChatUiComponent`.

#### `<ngx-chat-message>` — Single message renderer

Text messages (markdown + attachments) and agent messages (steps + streaming cursor). All actions are `@Output` events — no parent injection.

#### `<ngx-agent-steps>` — Collapsible reasoning panel

Gemini-style accordion. Extended with `approval_request` (approve/deny buttons) and `file_change` (diff view) step types.

#### `<ngx-chat-input>` — Rich input field

Already standalone in Advanced-LLM-Chat. Text, files, voice, camera — all feature-flagged via `@Input` booleans.

---

## What Stays App-Specific

Everything that isn't display:

- **ChatStateService** — where messages come from (IndexedDB + sync, REST + signals, WebSocket, whatever)
- **Streaming** — SSE, WebSocket, polling — the app handles it and pushes updates to the service
- **Persistence** — IndexedDB, REST, localStorage — app's problem
- **Auth, API, conversations** — completely different per app
- **TTS, file upload** — app provides via optional `@Input` handler interfaces
- **Theme** — app sets CSS variables

---

## CSS Variable Contract

The library references these. Apps set them to match their theme.

```css
:root {
  --chat-bg: #1e1e2e;
  --chat-border: #313244;
  --chat-user-bg: transparent;
  --chat-assistant-bg: transparent;
  --chat-user-color: #cdd6f4;
  --chat-assistant-color: #cdd6f4;
  --chat-step-thought-color: #f9e2af;
  --chat-step-tool-color: #cba6f7;
  --chat-step-result-color: #a6e3a1;
  --chat-step-observation-color: #89b4fa;
  --chat-input-bg: #313244;
  --chat-input-color: #cdd6f4;
  --chat-input-border: #45475a;
  --chat-action-color: #6c7086;
  --chat-action-hover: #cdd6f4;
  --chat-action-active: #cba6f7;
  --chat-cursor-color: #cba6f7;
  --chat-error-color: #f38ba8;
  --chat-success-color: #a6e3a1;
}
```

---

## How Each App Uses It

### Advanced-LLM-Chat

```typescript
@Component({
  imports: [ChatContainerComponent],
  template: `
    <ngx-chat-container
      [messages]="(messages$ | async) ?? []"
      [streamingMessage]="(streamingMessage$ | async) ?? null"
      [isStreaming]="(isStreaming$ | async) ?? false"
      [isMobile]="(isMobile$ | async) ?? false"
      [enableVoice]="true"
      [enableCamera]="true"
      [ttsHandler]="ttsHandler"
      (messageSent)="onMessageSent($event)"
      (filesSelected)="onFilesSelected($event)"
      (stopRequested)="cancelStreaming()"
      (editMessage)="patchMessage($event.messageId, $event.content)"
      (deleteMessage)="deleteMessage($event)"
      (regenerateMessage)="regenerateMessage($event)"
      (rateMessage)="rateMessage($event.messageId, $event.rating)"
      (loadOlderMessages)="loadOlderMessages()">
    </ngx-chat-container>
  `
})
export class ChatUiComponent {
  // Existing ChatStateService feeds data in, handles actions out
  constructor(private chatState: ChatStateService) {}
  messages$ = this.chatState.messages$;
  streamingMessage$ = this.chatState.state$.pipe(map(s => s.streamingMessage));
  isStreaming$ = this.chatState.state$.pipe(map(s => s.isStreaming));
  // ...
}
```

The 700-line `ChatUiComponent`, 430-line `ChatUiMessageComponent`, and `AgentStepsComponent` are all replaced by the library. `ChatStateService`, repositories, streaming service, API service stay untouched.

### SRW Cockpit — Builder

```typescript
@Component({
  imports: [ChatContainerComponent],
  template: `
    <ngx-chat-container
      [messages]="messages()"
      [streamingMessage]="streamingMessage()"
      [isStreaming]="isStreaming()"
      [showActions]="false"
      [enableVoice]="false"
      [enableCamera]="false"
      [enableFiles]="false"
      (messageSent)="sendMessage($event)"
      (stopRequested)="stopStreaming()"
      (approvalResponse)="handleApproval($event)">
    </ngx-chat-container>
  `
})
export class InstructionBuilderComponent {
  // Signal-based state, SSE streaming via BuilderStreamService — all stays
  messages = signal<Message[]>([]);
  streamingMessage = signal<Message<IAgentContent> | null>(null);
  isStreaming = signal(false);
  // ...
}
```

### SRW Cockpit — Interactive Agent (future)

```typescript
@Component({
  imports: [ChatContainerComponent],
  template: `
    <ngx-chat-container
      [messages]="messages()"
      [streamingMessage]="streamingMessage()"
      [isStreaming]="isStreaming()"
      [enableFiles]="true"
      (messageSent)="sendMessage($event)"
      (stopRequested)="cancelTurn()"
      (approvalResponse)="respondToApproval($event)">
    </ngx-chat-container>
  `
})
export class InteractiveAgentComponent {
  // WebSocket-based service feeds messages, handles approvals
}
```

---

## Extraction Plan

### Phase 1: Scaffold + models
- Create repo, `ng generate library ngx-agent-chat`
- Copy models (`message.model.ts`, `file.model.ts`) with new step types
- Copy `Message` class
- Unit tests

### Phase 2: Components
- Extract `AgentStepsComponent` (add approval/file_change rendering)
- Extract `ChatMessageComponent` (decouple from parent → `@Output` events)
- Extract `ChatInputComponent` (already standalone, add feature flag inputs)
- Extract `ChatContainerComponent` (smart scroll from `ChatUiComponent`)
- SCSS with CSS variable references

### Phase 3: Wire up apps
- Advanced-LLM-Chat: replace local components with library imports
- SRW cockpit builder: replace builder chat with library
- Verify both render identically

### Phase 4: Audio (optional)
- `AudioMessageComponent`, `VoiceRecordingService` — only if cockpit also needs voice

---

## Peer Dependencies

```json
{
  "@angular/core": "^19.0.0 || ^20.0.0 || ^21.0.0",
  "@angular/material": "^19.0.0 || ^20.0.0 || ^21.0.0",
  "ngx-markdown": "^19.0.0 || ^20.0.0 || ^21.0.0",
  "prismjs": "^1.29.0"
}
```

## Open Questions

1. **i18n**: Keep labels as `@Input` strings (apps handle their own translation) or include `ngx-translate` keys?
2. **AudioMessageComponent**: Separate entry point (`ngx-agent-chat/audio`) or always included?
3. **Repo name**: `ngx-agent-chat`? Other suggestions?
