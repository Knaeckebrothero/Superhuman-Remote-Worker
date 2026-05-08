import {computed, inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../environment';
import {ThreadStatus} from '../models/api.model';

/** A chat message in the persistent session. */
export interface ChatMessage {
    role: 'user' | 'assistant' | 'system';
    content: string;
    toolCalls?: ToolCallInfo[];
    /** Model reasoning/thinking text (collapsed by default in UI). */
    thinking?: string;
    timestamp: Date;
    /** True for messages loaded from DB history (not live). */
    historical?: boolean;
}

/** Info about a tool call within an assistant message. */
export interface ToolCallInfo {
    id: string;
    tool: string;
    args: Record<string, unknown>;
    result?: string;
    status: 'pending' | 'running' | 'completed' | 'denied';
    /** Tool category from the registry (e.g. workspace, git, research). */
    category?: string;
    /**
     * Supervised approval outcome, if this call passed through a permission
     * gate. Persisted on the backend so it survives history reload.
     * Absent for autonomous / auto-accepted calls.
     */
    decision?: 'approved' | 'denied';
}

/** Permission request from the agent. */
export interface PermissionRequest {
    /** Tool call id — correlates the eventual decision back to the call. */
    id: string;
    tool: string;
    args: Record<string, unknown>;
}

/** A task tracked during the session. */
export interface SessionTask {
    id: string;
    description: string;
    status: 'pending' | 'in_progress' | 'completed';
    priority: string;
    notes: string;
}

type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error';
type PermissionMode = 'supervised' | 'auto_accept' | 'autonomous';
export type NarrationMode = 'silent' | 'verbose' | 'auto';

/**
 * WebSocket client for persistent agent sessions.
 *
 * Manages the connection to /ws/persistent/{threadId} (via orchestrator proxy)
 * or directly to the agent pod's /ws/chat endpoint for local development.
 *
 * All state is exposed as Angular signals for reactive UI updates.
 */
@Injectable({providedIn: 'root'})
export class PersistentChatService {
    private readonly http = inject(HttpClient);

    // --- Connection state ---
    readonly connectionState = signal<ConnectionState>('disconnected');
    readonly isConnected = computed(() => this.connectionState() === 'connected');
    readonly threadId = signal<string | null>(null);

    // --- Chat state ---
    readonly messages = signal<ChatMessage[]>([]);
    readonly streamingText = signal('');
    readonly streamingThinking = signal('');
    readonly isStreaming = signal(false);
    readonly isInterrupting = signal(false);
    readonly currentToolCalls = signal<ToolCallInfo[]>([]);
    readonly historyLoaded = signal(false);

    // --- Permission state ---
    readonly permissionMode = signal<PermissionMode>('supervised');
    readonly pendingPermission = signal<PermissionRequest | null>(null);

    // --- Narration state ---
    readonly narrationMode = signal<NarrationMode>('auto');

    // --- Turn tracking ---
    readonly currentTurnId = signal<number | null>(null);
    readonly isWaitingForInput = signal(false);

    // --- Session metadata (loaded from REST on connect) ---
    readonly sessionTitle = signal<string | null>(null);
    readonly modelName = signal<string | null>(null);
    readonly temperature = signal<number>(0);
    readonly turnCount = signal<number>(0);
    readonly ncSessionFolder = signal<string | null>(null);
    readonly cloudSessionUrl = signal<string | null>(null);

    // --- Lifecycle state from the row (drives the resume card) ---
    readonly threadStatus = signal<ThreadStatus | null>(null);
    readonly endedAt = signal<string | null>(null);

    // --- Session readiness (agent has finished init and is ready for messages) ---
    readonly sessionReady = signal(false);

    // --- Startup progress phase (sent by orchestrator while waiting for agent) ---
    readonly startupPhase = signal<string | null>(null);

    // --- Pending message (submitted before session was ready) ---
    readonly pendingMessage = signal<string | null>(null);

    // --- Session tasks ---
    readonly tasks = signal<SessionTask[]>([]);

    // --- File undo ---
    readonly undoAvailable = signal(false);

    // --- Creating state (thread being created via API before connect) ---
    readonly isCreating = signal(false);

    // --- Session paused (idle timeout received) ---
    readonly isSessionPaused = signal(false);

    // --- Error ---
    readonly error = signal<string | null>(null);

    private ws: WebSocket | null = null;
    private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    /**
     * Connect to a persistent agent session.
     *
     * Loads history via REST first, then opens the orchestrator's WS proxy.
     */
    async connect(threadId: string): Promise<void> {
        this.disconnect();
        this.messages.set([]);
        this.connectionState.set('connecting');
        this.error.set(null);
        this.historyLoaded.set(false);
        this.sessionReady.set(false);
        this.startupPhase.set(null);
        this.pendingMessage.set(null);
        this.sessionTitle.set(null);
        this.modelName.set(null);
        this.temperature.set(0);
        this.turnCount.set(0);
        this.ncSessionFolder.set(null);
        this.cloudSessionUrl.set(null);
        this.tasks.set([]);
        this.undoAvailable.set(false);
        this.isSessionPaused.set(false);

        this.threadId.set(threadId);
        await this.loadHistory(threadId);
        await this.loadThreadMeta(threadId);

        // Don't auto-connect to ended sessions — render the read-only resume
        // card instead. The user explicitly clicks "Resume" to come back online.
        if (this.threadStatus() === 'ended') {
            this.connectionState.set('disconnected');
            return;
        }

        const apiUrl = environment.apiUrl;
        const wsBase = apiUrl
            .replace(/\/api\/?$/, '')
            .replace(/^http/, 'ws');
        const url = `${wsBase}/ws/persistent/${threadId}`;

        this._connectWs(url);
    }

    /**
     * Create a new persistent thread via REST, then connect.
     * Sets isCreating=true immediately so the UI can show a spinner.
     */
    async createAndConnect(body: Record<string, any>): Promise<string> {
        this.disconnect();
        this.isCreating.set(true);
        this.connectionState.set('connecting');
        this.startupPhase.set('creating');
        try {
            const resp = await firstValueFrom(
                this.http.post<{ thread_id: string }>(`${environment.apiUrl}/persistent/threads`, body)
            );
            const threadId = resp.thread_id;
            this.isCreating.set(false);
            await this.connect(threadId);
            return threadId;
        } catch (e) {
            this.isCreating.set(false);
            this.connectionState.set('error');
            this.startupPhase.set(null);
            throw e;
        }
    }

    /** Load message history from REST endpoint. */
    private async loadHistory(threadId: string): Promise<void> {
        try {
            const resp = await firstValueFrom(
                this.http.get<{ messages: HistoryMessage[]; total: number }>(
                    `${environment.apiUrl}/persistent/threads/${threadId}/messages`
                )
            );

            if (resp.messages?.length) {
                const historical: ChatMessage[] = resp.messages
                    .filter(m => ['user', 'human', 'HumanMessageChunk', 'ai', 'assistant', 'AIMessageChunk'].includes(m.role))
                    .map(m => ({
                        role: ['human', 'user', 'HumanMessageChunk'].includes(m.role) ? 'user' as const : 'assistant' as const,
                        content: m.content || '',
                        toolCalls: m.tool_calls?.map(tc => ({
                            id: tc.id || '',
                            tool: tc.name || '',
                            args: tc.args || {},
                            status: (tc.decision === 'denied' ? 'denied' : 'completed') as 'denied' | 'completed',
                            decision: tc.decision,
                        })),
                        timestamp: new Date(m.created_at || Date.now()),
                        historical: true,
                    }));
                this.messages.set(historical);
            }
            this.historyLoaded.set(true);
        } catch (e) {
            // History load failure is non-fatal — proceed with empty history
            this.historyLoaded.set(true);
        }
    }

    /** Load thread metadata (title, model, turn count) from REST. */
    private async loadThreadMeta(threadId: string): Promise<void> {
        try {
            const thread = await firstValueFrom(
                this.http.get<any>(`${environment.apiUrl}/persistent/threads/${threadId}`)
            );
            this.sessionTitle.set(thread.title || null);
            const model = thread.metadata?.config_override?.llm?.model;
            this.modelName.set(model || thread.config_name || null);
            const temperature = thread.metadata?.config_override?.llm?.temperature;
            if (temperature != null) {
                this.temperature.set(temperature);
            }
            this.turnCount.set(thread.total_turns || 0);
            this.ncSessionFolder.set(thread.nc_session_folder || null);
            this.cloudSessionUrl.set(thread.cloud_session_url || null);
            this.threadStatus.set((thread.status as ThreadStatus) || null);
            this.endedAt.set(thread.ended_at || thread.last_activity || null);
        } catch {
            // Non-fatal — UI will show fallback values
        }
    }

    /** Open the WebSocket connection. */
    private _connectWs(url: string): void {
        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
            this.connectionState.set('connected');
            this.error.set(null);
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleMessage(data);
            } catch {
                // Ignore malformed messages
            }
        };

        this.ws.onclose = (event) => {
            this.connectionState.set('disconnected');
            this.isStreaming.set(false);
            this.isWaitingForInput.set(false);
            if (event.code !== 1000 && event.code !== 4408) {
                this.error.set(`Connection closed: ${event.reason || `code ${event.code}`}`);
            }
        };

        this.ws.onerror = () => {
            this.connectionState.set('error');
            this.error.set('WebSocket connection failed');
        };
    }

    /** Disconnect from the session. */
    disconnect(): void {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        if (this.ws) {
            this.ws.onclose = null; // Prevent error handling on intentional close
            this.ws.close(1000);
            this.ws = null;
        }
        this.connectionState.set('disconnected');
        this.isStreaming.set(false);
        this.isWaitingForInput.set(false);
        this.sessionReady.set(false);
        this.startupPhase.set(null);
        this.pendingMessage.set(null);
        this.pendingPermission.set(null);
        this.sessionTitle.set(null);
        this.modelName.set(null);
        this.temperature.set(0);
        this.turnCount.set(0);
        this.ncSessionFolder.set(null);
        this.cloudSessionUrl.set(null);
        this.threadStatus.set(null);
        this.endedAt.set(null);
        this.tasks.set([]);
        this.undoAvailable.set(false);
        this.isSessionPaused.set(false);
    }

    /**
     * Resume a paused/idle session: POST to resume endpoint, then reconnect.
     */
    async resumeSession(): Promise<void> {
        const threadId = this.threadId();
        if (!threadId) return;
        this.isSessionPaused.set(false);
        await firstValueFrom(
            this.http.post(`${environment.apiUrl}/persistent/threads/${threadId}/resume`, {})
        );
        await this.connect(threadId);
    }

    /** Send a user message (with slash command parsing).
     *  If the session isn't ready yet, queues the message and sends it
     *  automatically once the agent signals readiness.
     */
    sendMessage(content: string): void {
        if (!content.trim()) return;

        // Slash command parsing
        const trimmed = content.trim();
        if (trimmed.startsWith('/')) {
            const handled = this.handleSlashCommand(trimmed);
            if (handled) return;
        }

        // Add to local messages immediately so the user sees their input
        this.messages.update((msgs) => [
            ...msgs,
            {role: 'user', content, timestamp: new Date()},
        ]);

        // If session isn't ready yet, queue and send when ready
        if (!this.sessionReady() || !this.ws) {
            this.pendingMessage.set(content);
            return;
        }

        this.isWaitingForInput.set(false);
        this.send({method: 'message', content});
    }

    /** Parse and dispatch slash commands. Returns true if handled. */
    private handleSlashCommand(input: string): boolean {
        const parts = input.split(/\s+/);
        const cmd = parts[0].toLowerCase();
        const arg = parts.slice(1).join(' ');

        switch (cmd) {
            case '/compact':
                this.send({method: 'compact', focus: arg});
                this.messages.update(msgs => [...msgs, {
                    role: 'system', content: `Compacting context${arg ? ` (focus: ${arg})` : ''}...`,
                    timestamp: new Date(),
                }]);
                return true;
            case '/done':
                this.send({method: 'archive'});
                this.messages.update(msgs => [...msgs, {
                    role: 'system', content: 'Ending session...', timestamp: new Date(),
                }]);
                return true;
            case '/auto':
                this.setMode('auto_accept');
                return true;
            case '/supervised':
                this.setMode('supervised');
                return true;
            case '/autonomous':
                this.setMode('autonomous');
                return true;
            case '/silent':
                this.setNarrationMode('silent');
                return true;
            case '/verbose':
                this.setNarrationMode('verbose');
                return true;
            case '/undo':
                this.send({method: 'undo'});
                this.messages.update(msgs => [...msgs, {
                    role: 'system', content: 'Undoing last file changes...', timestamp: new Date(),
                }]);
                return true;
            default:
                return false;
        }
    }

    /** Approve a pending permission request. */
    approve(): void {
        const pending = this.pendingPermission();
        this.pendingPermission.set(null);
        if (pending?.id) this.recordLiveDecision(pending.id, 'approved');
        this.send({method: 'approve'});
    }

    /** Deny a pending permission request. */
    deny(): void {
        const pending = this.pendingPermission();
        this.pendingPermission.set(null);
        if (pending?.id) {
            // Denied tools never get a tool.started event, so seed a synthetic
            // entry in currentToolCalls so the marker renders inline with the
            // turn rather than vanishing on the next token.
            this.currentToolCalls.update(calls => {
                if (calls.some(c => c.id === pending.id)) return calls;
                return [...calls, {
                    id: pending.id,
                    tool: pending.tool,
                    args: pending.args,
                    status: 'denied',
                    decision: 'denied',
                }];
            });
        }
        this.send({method: 'deny'});
    }

    private recordLiveDecision(id: string, decision: 'approved' | 'denied'): void {
        this.currentToolCalls.update(calls =>
            calls.map(c => (c.id === id ? {...c, decision} : c)),
        );
    }

    /** Interrupt the current turn. */
    interrupt(): void {
        if (this.isInterrupting()) return; // Already interrupting
        this.isInterrupting.set(true);
        this.send({method: 'interrupt'});
    }

    /** Change permission mode. */
    setMode(mode: PermissionMode): void {
        this.permissionMode.set(mode);
        this.send({method: 'mode.set', mode});
    }

    setNarrationMode(mode: NarrationMode): void {
        this.narrationMode.set(mode);
        this.send({method: 'narration.set', mode});
    }

    /** Update session config (model, temperature, etc.) at runtime. */
    updateConfig(config: Record<string, unknown>): void {
        this.send({method: 'config.update', config});
    }

    /** Clear message history (local only). */
    clearMessages(): void {
        this.messages.set([]);
    }

    // --- Private ---

    private send(data: Record<string, unknown>): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        }
    }

    private handleMessage(data: { method: string; params?: Record<string, unknown> }): void {
        const params = data.params ?? {};

        switch (data.method) {
            case 'status':
                // Startup progress from orchestrator (provisioning → booting → connecting)
                this.startupPhase.set((params['phase'] as string) || null);
                break;

            case 'session.state':
                // Sync client state with agent's current state on connect
                if (params['permission_mode']) {
                    this.permissionMode.set(params['permission_mode'] as PermissionMode);
                }
                if (params['narration_mode']) {
                    this.narrationMode.set(params['narration_mode'] as NarrationMode);
                }
                if (params['turn_count'] != null) {
                    this.turnCount.set(params['turn_count'] as number);
                }
                if (params['model']) {
                    this.modelName.set(params['model'] as string);
                }
                if (params['temperature'] != null) {
                    this.temperature.set(params['temperature'] as number);
                }
                // session.state comes from the agent — implies agent is alive.
                // Mark ready as fallback in case the 'ready' message is lost.
                this.markSessionReady();
                break;

            case 'greeting':
                this.messages.update(msgs => [...msgs, {
                    role: 'assistant',
                    content: (params['content'] as string) || '',
                    timestamp: new Date(),
                }]);
                this.isWaitingForInput.set(true);
                break;

            case 'ready':
                this.isWaitingForInput.set(true);
                this.isStreaming.set(false);
                // Finalize any streaming text into a message
                this.finalizeStreaming();
                this.markSessionReady();
                break;

            case 'turn.started':
                this.currentTurnId.set((params['turn_id'] as number) ?? null);
                this.turnCount.update(c => c + 1);
                this.isStreaming.set(true);
                this.streamingText.set('');
                this.streamingThinking.set('');
                this.currentToolCalls.set([]);
                break;

            case 'token':
                this.streamingText.update((t) => t + ((params['content'] as string) || ''));
                break;

            case 'thinking':
                this.streamingThinking.update((t) => t + ((params['content'] as string) || ''));
                break;

            case 'tool.started': {
                const tc: ToolCallInfo = {
                    id: (params['id'] as string) || '',
                    tool: (params['tool'] as string) || '',
                    args: (params['args'] as Record<string, unknown>) || {},
                    status: 'running',
                    category: (params['category'] as string) || undefined,
                };
                this.currentToolCalls.update((calls) => [...calls, tc]);
                break;
            }

            case 'tool.completed': {
                const id = params['id'] as string;
                this.currentToolCalls.update((calls) =>
                    calls.map((tc) =>
                        tc.id === id
                            ? {...tc, status: 'completed' as const, result: (params['result'] as string) || ''}
                            : tc,
                    ),
                );
                break;
            }

            case 'permission.request':
                this.pendingPermission.set({
                    id: (params['id'] as string) || '',
                    tool: (params['tool'] as string) || '',
                    args: (params['args'] as Record<string, unknown>) || {},
                });
                break;

            case 'turn.completed':
                this.finalizeStreaming();
                this.isStreaming.set(false);
                this.isInterrupting.set(false);
                this.currentTurnId.set(null);
                break;

            case 'interrupt.ack':
                // Backend confirmed interrupt — immediately finalize UI
                this.finalizeStreaming();
                this.isStreaming.set(false);
                this.isInterrupting.set(false);
                this.currentTurnId.set(null);
                break;

            case 'mode.changed':
                this.permissionMode.set((params['mode'] as PermissionMode) || 'supervised');
                break;

            case 'narration.changed':
                this.narrationMode.set((params['mode'] as NarrationMode) || 'auto');
                break;

            case 'config.changed':
                if (params['model']) {
                    this.modelName.set(params['model'] as string);
                }
                if (params['temperature'] != null) {
                    this.temperature.set(params['temperature'] as number);
                }
                if (params['permission_mode']) {
                    this.permissionMode.set(params['permission_mode'] as PermissionMode);
                }
                break;

            case 'title.updated':
                if (params['title']) {
                    this.sessionTitle.set(params['title'] as string);
                }
                break;

            case 'context.compacted':
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: `Context compacted: ${params['before']} → ${params['after']} messages`,
                    timestamp: new Date(),
                }]);
                break;

            case 'session.ended':
                this.messages.update(msgs => [...msgs, {
                    role: 'system', content: 'Session ended.', timestamp: new Date(),
                }]);
                this.isWaitingForInput.set(false);
                this.threadStatus.set('ended');
                this.endedAt.set(new Date().toISOString());
                break;

            case 'session.idle_timeout':
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: `Session paused after ${(params['timeout_minutes'] as number) || 30} minutes of inactivity. Your work has been saved.`,
                    timestamp: new Date(),
                }]);
                this.isWaitingForInput.set(false);
                // The agent's idle archive flips the row to 'ended'. Reflect
                // that locally so the UI swaps to the read-only resume card.
                this.threadStatus.set('ended');
                this.endedAt.set(new Date().toISOString());
                break;

            case 'vm_upgrade.needed':
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: `VM upgrade needed: ${(params['reason'] as string) || 'sudo detected'}. `
                        + `Use the upgrade button or send /upgrade to switch to a VM workspace.`,
                    timestamp: new Date(),
                }]);
                break;

            case 'vm_upgrade.started':
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: 'Upgrading workspace to VM, please wait...',
                    timestamp: new Date(),
                }]);
                break;

            case 'vm_upgrade.complete':
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: 'VM upgrade complete. Workspace is now running on a VM with sudo access.',
                    timestamp: new Date(),
                }]);
                break;

            case 'vm_upgrade.failed':
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: `VM upgrade failed: ${(params['reason'] as string) || 'unknown error'}`,
                    timestamp: new Date(),
                }]);
                break;

            case 'tasks.updated':
                this.tasks.set((params['tasks'] as SessionTask[]) || []);
                break;

            case 'file.checkpoint':
                this.undoAvailable.set(true);
                break;

            case 'files.restored':
                this.undoAvailable.set(false);
                this.messages.update(msgs => [...msgs, {
                    role: 'system',
                    content: `Restored ${(params['paths'] as string[])?.length || 0} file(s) to pre-edit state.`,
                    timestamp: new Date(),
                }]);
                break;

            case 'error':
                this.error.set((params['message'] as string) || 'Unknown error');
                break;
        }
    }

    /** Mark the session as ready and flush any pending message. */
    private markSessionReady(): void {
        if (this.sessionReady()) return;
        this.sessionReady.set(true);

        const pending = this.pendingMessage();
        if (pending) {
            this.pendingMessage.set(null);
            // Send directly — the message was already added to the messages array
            // when the user submitted it, so we skip sendMessage() to avoid duplicates.
            this.isWaitingForInput.set(false);
            this.send({method: 'message', content: pending});
        }
    }

    /** Move accumulated streaming text + tool calls + thinking into the messages array. */
    private finalizeStreaming(): void {
        const text = this.streamingText();
        const thinking = this.streamingThinking();
        const tools = this.currentToolCalls();

        if (text || thinking || tools.length > 0) {
            this.messages.update((msgs) => [
                ...msgs,
                {
                    role: 'assistant',
                    content: text,
                    toolCalls: tools.length > 0 ? [...tools] : undefined,
                    thinking: thinking || undefined,
                    timestamp: new Date(),
                },
            ]);
        }

        this.streamingText.set('');
        this.streamingThinking.set('');
        this.currentToolCalls.set([]);
    }
}

/** Shape of a message from the REST history endpoint. */
interface HistoryMessage {
    id: string;
    role: string;
    content: string | null;
    tool_calls: { name: string; args: Record<string, unknown>; id: string; decision?: 'approved' | 'denied' }[] | null;
    turn_number: number | null;
    created_at: string | null;
}
