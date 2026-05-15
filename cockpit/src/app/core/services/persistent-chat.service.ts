import {computed, inject, Injectable, NgZone, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../environment';
import {ThreadStatus} from '../models/api.model';
import {FilePreview, ThreadUploadedFile, UploadStatus} from '../models/file.model';
import {
    AssistantTurn,
    ConversationState,
    EMPTY_CONVERSATION,
    isAssistantTurn,
    SystemTurn,
    ToolCallEvent,
    Turn,
    UserTurn,
} from '../models/turn.model';
import {ApiService} from './api.service';
import {IndexedDbService} from './indexed-db.service';
import {reduce, ReducerAction} from './turn-reducer';

/**
 * Transport architecture (post WS→SSE migration, 2026-05-13):
 *
 *  • Server → client: GET /api/persistent/threads/{id}/stream — Server-Sent
 *    Events. EventSource handles reconnect natively with the `Last-Event-ID`
 *    header (set automatically by the browser from the latest `id:` line we
 *    received). We additionally persist the cursor `(epoch, seq)` in IndexedDB
 *    so cross-tab-close resume works — when the user reopens the tab, we read
 *    the saved cursor and pass it as the initial `Last-Event-ID` so any events
 *    the agent produced while we were away replay cleanly.
 *
 *  • Client → server: POST /api/persistent/threads/{id}/input (messages) and
 *    POST /api/persistent/threads/{id}/interrupt (interrupt). Canonical REST.
 *
 *  • Control plane: the agent's existing WebSocket handler is retained for the
 *    smaller verbs — approve/deny, slash commands, mode + narration + config
 *    updates, vm-upgrade. Reason: these only fire while the user is actively
 *    in the cockpit. A future PR can fold them into a generic
 *    POST /control endpoint, but doing so requires invasive refactoring of
 *    the agent's WS dispatch (moving its `_ws_send` error returns over to
 *    proper HTTP semantics + broadcasting success notifications via
 *    `_broadcast` so SSE consumers see them too). Out of scope for the
 *    migration that gets browser-close-survival to users.
 *
 * `gone_beyond_horizon`: the server emits this single named event when the
 * cursor is outside replay range (epoch mismatch or seq older than retention).
 * Handler: drop the cursor, REST-reload the message history snapshot, reopen
 * the stream without a cursor.
 */

const CONTROL_WS_RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000];
const CONTROL_WS_RECONNECT_MAX_ATTEMPTS = 8;

/** Attachment chip shown alongside a user message. */
export interface ChatAttachment {
    name: string;
    size: number;
    mimeType: string;
    /** Workspace-relative path, e.g. "uploads/photo.jpg". */
    path: string;
}

/** Info about a tool call within an assistant message. */
export interface ToolCallInfo {
    id: string;
    tool: string;
    args: Record<string, unknown>;
    result?: string;
    status: 'pending' | 'running' | 'completed' | 'denied' | 'error';
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
 * Persistent agent session client. See file header for transport rationale.
 *
 * All state is exposed as Angular signals for reactive UI updates.
 */
@Injectable({providedIn: 'root'})
export class PersistentChatService {
    private readonly http = inject(HttpClient);
    private readonly api = inject(ApiService);
    private readonly cache = inject(IndexedDbService);
    private readonly zone = inject(NgZone);

    // --- Connection state ---
    readonly connectionState = signal<ConnectionState>('disconnected');
    readonly isConnected = computed(() => this.connectionState() === 'connected');
    readonly threadId = signal<string | null>(null);

    // --- Reconnect surface (kept for back-compat with the resume banner UI).
    // EventSource handles reconnect natively, so these mostly stay quiet —
    // we only bump `reconnectAttempt` while the SSE is in CONNECTING after an
    // earlier OPEN, and only set `reconnectGaveUp` on terminal CLOSED.
    readonly reconnectAttempt = signal<number>(0);
    readonly reconnectGaveUp = signal<boolean>(false);
    readonly reconnectMaxAttempts = -1; // unbounded; browser owns the loop

    // --- Conversation state ---
    //
    // One signal holds the whole conversation as an ordered list of typed
    // Turns (user / assistant / system). Each AssistantTurn carries an
    // ordered list of typed events (thought | text | tool_call). The reducer
    // (./turn-reducer.ts) maps wire-level SSE events to state mutations and
    // is keyed by stable ids so SSE replay is idempotent.
    //
    // See `docs/features/session_turn_rendering.md` for the design.
    readonly conversation = signal<ConversationState>(EMPTY_CONVERSATION);
    readonly turns = computed(() => this.conversation().turns);
    readonly currentStreamingTurn = computed<AssistantTurn | null>(() => {
        const state = this.conversation();
        if (!state.activeAssistantTurnId) return null;
        const t = state.turns.find(
            (turn): turn is AssistantTurn =>
                isAssistantTurn(turn) && turn.id === state.activeAssistantTurnId,
        );
        return t ?? null;
    });
    readonly isStreaming = computed(() => this.conversation().activeAssistantTurnId !== null);
    readonly isInterrupting = signal(false);
    readonly historyLoaded = signal(false);

    // --- Permission state ---
    readonly permissionMode = signal<PermissionMode>('supervised');
    readonly pendingPermission = signal<PermissionRequest | null>(null);

    // --- Narration state ---
    readonly narrationMode = signal<NarrationMode>('auto');

    // --- Turn tracking ---
    /**
     * The numeric `turn_id` of the in-flight turn, or null when no turn is
     * streaming. Derived from the active turn in `conversation` — synthetic
     * turns (e.g. greetings) have non-numeric ids and resolve to null.
     */
    readonly currentTurnId = computed<number | null>(() => {
        const id = this.conversation().activeAssistantTurnId;
        if (!id) return null;
        const n = Number(id);
        return Number.isFinite(n) ? n : null;
    });
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

    // --- Pending attachments (queued in composer before send) ---
    readonly pendingAttachments = signal<FilePreview[]>([]);

    // --- Upload state (true while the next send is busy uploading files) ---
    readonly isUploadingAttachments = signal(false);

    // --- Last upload error (cleared on next successful send) ---
    readonly attachmentError = signal<string | null>(null);

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

    private sse: EventSource | null = null;
    private controlWs: WebSocket | null = null;
    private controlWsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private controlWsReconnectAttempt = 0;
    private intentionalClose = false;

    /**
     * Connect to a persistent agent session.
     *
     * Loads thread metadata + transcript history from REST, then opens the
     * SSE stream (replay-from-cursor when we have one cached) and the
     * control WS.
     */
    async connect(threadId: string): Promise<void> {
        this.disconnect();
        this.dispatch({type: 'reset', threadId});
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

        this.intentionalClose = false;
        await this._openSse(threadId);
        this._openControlWs(threadId);
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

    /**
     * Load message history from REST endpoint and rehydrate as Turns.
     *
     * Server returns flat HistoryMessage rows (one per agent iteration); we
     * group consecutive assistant rows on `turn_number` into a single
     * AssistantTurn so the rendered bubble matches the live experience.
     * Thinking content is not persisted in `thread_messages` so historical
     * turns won't have thought events — known gap, see
     * `docs/features/session_turn_rendering.md`.
     */
    private async loadHistory(threadId: string): Promise<void> {
        try {
            const resp = await firstValueFrom(
                this.http.get<{ messages: HistoryMessage[]; total: number }>(
                    `${environment.apiUrl}/persistent/threads/${threadId}/messages`
                )
            );

            if (resp.messages?.length) {
                const turns = historyToTurns(resp.messages);
                this.dispatch({type: 'load_history', threadId, turns});
            }
            this.historyLoaded.set(true);
        } catch {
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

    // ── SSE receive path ─────────────────────────────────────────────────

    /**
     * Open the SSE event stream. When IndexedDB has a cached cursor for
     * this thread we can't use the EventSource constructor to pass a custom
     * `Last-Event-ID` header (the API doesn't accept request headers), so
     * we encode the cursor as a query parameter — the server reads either
     * the header or `?last_event_id=...`. After the first event arrives,
     * the browser tracks the cursor itself via the `id:` lines.
     */
    private async _openSse(threadId: string): Promise<void> {
        const cursor = await this.cache.getThreadCursor(threadId);
        const cursorQuery = cursor ? `?last_event_id=${encodeURIComponent(`${cursor.epoch}:${cursor.seq}`)}` : '';
        const url = `${environment.apiUrl}/persistent/threads/${threadId}/stream${cursorQuery}`;

        // withCredentials true so the srw_session cookie rides along on the
        // cross-origin SSE handshake.
        this.sse = new EventSource(url, {withCredentials: true});

        this.sse.onopen = () => {
            this.zone.run(() => {
                this.connectionState.set('connected');
                this.error.set(null);
                this.reconnectAttempt.set(0);
                this.reconnectGaveUp.set(false);
            });
        };

        this.sse.onmessage = (event: MessageEvent) => {
            this.zone.run(() => this._handleSseFrame(event));
        };

        this.sse.addEventListener('gone_beyond_horizon', (event) => {
            this.zone.run(() => this._handleGoneBeyondHorizon(event as MessageEvent));
        });

        this.sse.onerror = () => {
            this.zone.run(() => {
                if (!this.sse) return;
                if (this.sse.readyState === EventSource.CLOSED) {
                    // Terminal — auth failure, thread gone, etc. The browser
                    // gave up. Don't bury the UI in a generic banner; let
                    // the threadStatus refresh below surface "ended" if
                    // that's what happened.
                    this.connectionState.set('error');
                    this.reconnectGaveUp.set(true);
                    this._refreshStatusAfterDrop(threadId);
                } else {
                    // CONNECTING — the browser is retrying. Show reconnecting.
                    this.connectionState.set('connecting');
                    this.reconnectAttempt.update(n => n + 1);
                }
            });
        };
    }

    private _handleSseFrame(event: MessageEvent): void {
        // event.lastEventId is "<epoch>:<seq>". Save before dispatch so a
        // dispatch error doesn't lose our place — the SSE replay logic
        // tolerates re-receiving the same seq (it'll just be a no-op given
        // the seq > $3 guard server-side).
        if (event.lastEventId) {
            const tid = this.threadId();
            if (tid) this._saveCursor(tid, event.lastEventId);
        }

        let frame: { method: string; params?: Record<string, unknown> };
        try {
            frame = JSON.parse(event.data);
        } catch {
            return;
        }
        this._handleEvent(frame);
    }

    /**
     * gone_beyond_horizon: cursor is too stale to replay (epoch mismatch or
     * seq older than retention). Drop our cursor, REST-reload the transcript
     * snapshot so the user at least sees completed turns, and reopen the
     * stream from current tail.
     */
    private async _handleGoneBeyondHorizon(event: MessageEvent): Promise<void> {
        const tid = this.threadId();
        if (!tid) return;
        await this.cache.deleteThreadCursor(tid);
        if (this.sse) {
            this.sse.close();
            this.sse = null;
        }
        // Reload transcript so visible history doesn't have a silent gap.
        await this.loadHistory(tid);
        // Reopen with no cursor — server starts us at the current tail.
        await this._openSse(tid);
    }

    private _saveCursor(threadId: string, lastEventId: string): void {
        // Parse "<epoch>:<seq>"; ignore malformed (keepalives, etc.).
        const colon = lastEventId.indexOf(':');
        if (colon <= 0) return;
        const epoch = Number(lastEventId.slice(0, colon));
        const seq = Number(lastEventId.slice(colon + 1));
        if (!Number.isFinite(epoch) || !Number.isFinite(seq)) return;
        // Fire-and-forget; cursor staleness is recoverable.
        void this.cache.setThreadCursor(threadId, epoch, seq);
    }

    /**
     * After SSE drops to CLOSED, the agent may have flipped this thread to
     * `ended` (idle archive, /done from another client). Re-fetch meta so the
     * UI swaps to the resume card instead of stuck on "connection error".
     */
    private _refreshStatusAfterDrop(threadId: string): void {
        setTimeout(async () => {
            if (this.threadId() !== threadId) return;
            await this.loadThreadMeta(threadId);
        }, 1500);
    }

    // ── Control WS (slash commands + permission decisions) ───────────────

    private _openControlWs(threadId: string): void {
        const apiUrl = environment.apiUrl;
        const wsBase = apiUrl.replace(/\/api\/?$/, '').replace(/^http/, 'ws');
        const url = `${wsBase}/ws/persistent/${threadId}`;

        this.controlWs = new WebSocket(url);
        this.controlWs.onclose = () => {
            this.controlWs = null;
            if (this.intentionalClose) return;
            if (this.threadId() !== threadId) return;
            // Tiny linear backoff — primary connection signal lives on the SSE
            // so we don't need the WS aggressively reconnecting.
            this._scheduleControlWsReconnect(threadId);
        };
        this.controlWs.onerror = () => {
            // The close handler will fire; nothing to do here.
        };
        // SSE is the canonical receive path for agent-emitted events. We
        // listen here only for `status` frames, which the orchestrator's
        // persistent_ws_proxy emits BEFORE attaching to the agent pod
        // ({phase: provisioning|booting|connecting}) — they never reach
        // the thread_events log and so never come via SSE. Every other
        // method (token, turn.*, thinking, error, ready, session.ended,
        // title.updated, message) is also relayed over this WS for
        // back-compat and would double-dispatch with SSE, so we discard.
        this.controlWs.onmessage = (event: MessageEvent) => {
            let frame: { method?: string; params?: Record<string, unknown> };
            try {
                frame = JSON.parse(event.data);
            } catch {
                return;
            }
            if (frame?.method !== 'status') return;
            this.zone.run(() =>
                this._handleEvent(frame as { method: string; params?: Record<string, unknown> }),
            );
        };
    }

    private _scheduleControlWsReconnect(threadId: string): void {
        if (this.controlWsReconnectAttempt >= CONTROL_WS_RECONNECT_MAX_ATTEMPTS) {
            // Give up silently; user actions that need the WS will reopen
            // on demand via _ensureControlWs.
            return;
        }
        const idx = this.controlWsReconnectAttempt;
        const delay = CONTROL_WS_RECONNECT_DELAYS_MS[Math.min(idx, CONTROL_WS_RECONNECT_DELAYS_MS.length - 1)];
        this.controlWsReconnectAttempt = idx + 1;
        this.controlWsReconnectTimer = setTimeout(() => {
            this.controlWsReconnectTimer = null;
            if (this.intentionalClose) return;
            if (this.threadId() !== threadId) return;
            this._openControlWs(threadId);
        }, delay);
    }

    /** Open a control WS on demand if one isn't already open. Used by
     *  slash-command and permission paths when the user clicks during a
     *  brief reconnect window. */
    private _ensureControlWs(): void {
        const tid = this.threadId();
        if (!tid) return;
        if (this.controlWs?.readyState === WebSocket.OPEN) return;
        if (this.controlWs?.readyState === WebSocket.CONNECTING) return;
        this.controlWsReconnectAttempt = 0;
        this._openControlWs(tid);
    }

    /** Send a control-plane command. If the WS isn't open, open it; the
     *  send goes out as soon as the connection establishes. */
    private _sendControl(data: Record<string, unknown>): void {
        if (this.controlWs?.readyState === WebSocket.OPEN) {
            this.controlWs.send(JSON.stringify(data));
            return;
        }
        // Best-effort: queue by re-opening and sending on next 'open'.
        this._ensureControlWs();
        const ws = this.controlWs;
        if (!ws) return;
        const sendWhenOpen = () => {
            ws.removeEventListener('open', sendWhenOpen);
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(data));
            }
        };
        ws.addEventListener('open', sendWhenOpen);
    }

    /** Cancel the current backoff wait and immediately reopen the SSE.
     *  Resets gave-up state. */
    reconnectNow(): void {
        if (this.intentionalClose) return;
        const tid = this.threadId();
        if (!tid) return;

        this.reconnectGaveUp.set(false);
        this.reconnectAttempt.set(0);
        if (this.sse) {
            this.sse.close();
            this.sse = null;
        }
        this.connectionState.set('connecting');
        void this._openSse(tid);
    }

    /** Disconnect from the session. */
    disconnect(): void {
        this.intentionalClose = true;
        if (this.controlWsReconnectTimer) {
            clearTimeout(this.controlWsReconnectTimer);
            this.controlWsReconnectTimer = null;
        }
        this.controlWsReconnectAttempt = 0;
        if (this.sse) {
            this.sse.close();
            this.sse = null;
        }
        if (this.controlWs) {
            this.controlWs.onclose = null;
            try {
                this.controlWs.close(1000);
            } catch {
                // ignore
            }
            this.controlWs = null;
        }
        this.reconnectAttempt.set(0);
        this.reconnectGaveUp.set(false);
        this.connectionState.set('disconnected');
        // If a turn was streaming when we disconnected, mark it interrupted
        // so isStreaming flips to false and the bubble shows it stopped.
        this._closeActiveTurnIfAny('turn_interrupted');
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

    /** Add files queued in the composer to be uploaded on next send. */
    addAttachments(previews: FilePreview[]): void {
        if (!previews.length) return;
        this.pendingAttachments.update((existing) => [...existing, ...previews]);
        this.attachmentError.set(null);
    }

    /** Drop one queued attachment by id. */
    removeAttachment(id: string): void {
        this.pendingAttachments.update((list) => list.filter((p) => p.id !== id));
    }

    /** Drop all queued attachments. */
    clearAttachments(): void {
        this.pendingAttachments.set([]);
    }

    /** Send a user message (with slash command parsing).
     *  If the session isn't ready yet, queues the message and sends it
     *  automatically once the agent signals readiness.
     *
     *  When ``pendingAttachments`` is non-empty, files are uploaded to the
     *  thread workspace's ``uploads/`` directory first, then a hint listing
     *  the uploaded filenames is appended to the message text the agent
     *  receives. The displayed user message keeps the original text and
     *  shows uploaded files as separate attachment chips.
     */
    async sendMessage(content: string): Promise<void> {
        const trimmed = content.trim();

        // Slash commands bypass attachment logic.
        if (trimmed.startsWith('/')) {
            if (this.handleSlashCommand(trimmed)) return;
        }

        const queued = this.pendingAttachments();
        if (!trimmed && queued.length === 0) return;

        let uploaded: ThreadUploadedFile[] = [];
        if (queued.length > 0) {
            const threadId = this.threadId();
            if (!threadId) {
                this.attachmentError.set('Cannot upload: no active thread');
                return;
            }
            const files = queued.filter((p) => p.file).map((p) => p.file);
            this.isUploadingAttachments.set(true);
            this.attachmentError.set(null);
            queued.forEach((p) => (p.uploadStatus = UploadStatus.UPLOADING));
            try {
                const result = await firstValueFrom(this.api.uploadToThread(threadId, files));
                uploaded = result.files;
                queued.forEach((p) => (p.uploadStatus = UploadStatus.COMPLETED));
            } catch (err) {
                const msg = this.api.humanizeUploadError(err);
                queued.forEach((p) => {
                    p.uploadStatus = UploadStatus.FAILED;
                    p.error = msg;
                });
                this.attachmentError.set(msg);
                return;
            } finally {
                this.isUploadingAttachments.set(false);
            }
            // Successful upload — drop the previews so the composer clears
            this.clearAttachments();
        }

        const attachments: ChatAttachment[] = uploaded.map((f) => ({
            name: f.name,
            size: f.size,
            mimeType: f.mime_type,
            path: f.path,
        }));

        // What the agent sees: text + a plain-language hint about the files.
        let sendContent = trimmed;
        if (attachments.length > 0) {
            const list = attachments.map((a) => a.name).join(', ');
            const hint = `[Attached files in uploads/: ${list}]`;
            sendContent = trimmed ? `${trimmed}\n\n${hint}` : hint;
        }

        // Add to local conversation — content is the user's typed text
        // only; uploaded files render as separate attachment chips.
        this.dispatch({
            type: 'user_message',
            id: makeLocalId('user'),
            content: trimmed,
            attachments: attachments.length > 0 ? attachments : undefined,
            timestamp: Date.now(),
        });

        // If session isn't ready yet, queue and send when ready.
        if (!this.sessionReady()) {
            this.pendingMessage.set(sendContent);
            return;
        }

        this.isWaitingForInput.set(false);
        await this._postInput(sendContent);
    }

    /** POST the input to the orchestrator's REST endpoint. */
    private async _postInput(content: string): Promise<void> {
        const tid = this.threadId();
        if (!tid) return;
        try {
            await firstValueFrom(
                this.http.post<{ accepted: boolean; turn_id: number }>(
                    `${environment.apiUrl}/persistent/threads/${tid}/input`,
                    {content}
                )
            );
        } catch (err: any) {
            // 409 means a duplicate POST landed for the same turn — surface
            // the existing inflight turn_id and otherwise ignore; the user
            // sees the response stream via SSE.
            if (err?.status === 409) return;
            this.error.set(this.sanitizeError(err?.error?.detail || err?.message));
        }
    }

    /** Parse and dispatch slash commands. Returns true if handled. */
    private handleSlashCommand(input: string): boolean {
        const parts = input.split(/\s+/);
        const cmd = parts[0].toLowerCase();
        const arg = parts.slice(1).join(' ');

        switch (cmd) {
            case '/compact':
                this._sendControl({method: 'compact', focus: arg});
                this._systemMessage(`Compacting context${arg ? ` (focus: ${arg})` : ''}...`);
                return true;
            case '/done':
                this._sendControl({method: 'archive'});
                this._systemMessage('Ending session...');
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
                this._sendControl({method: 'undo'});
                this._systemMessage('Undoing last file changes...');
                return true;
            default:
                return false;
        }
    }

    private _systemMessage(content: string): void {
        this.dispatch({
            type: 'system_message',
            id: makeLocalId('sys'),
            content,
            timestamp: Date.now(),
        });
    }

    /** Approve a pending permission request. */
    approve(): void {
        const pending = this.pendingPermission();
        this.pendingPermission.set(null);
        if (pending?.id) {
            this.dispatch({
                type: 'permission_decision',
                toolUseId: pending.id,
                decision: 'approved',
                timestamp: Date.now(),
            });
        }
        this._sendControl({method: 'approve'});
    }

    /** Deny a pending permission request. */
    deny(): void {
        const pending = this.pendingPermission();
        this.pendingPermission.set(null);
        if (pending?.id) {
            // The reducer handles both the existing-pending-call case and
            // the no-prior-tool_started case (synthetic denied entry) — see
            // turn-reducer.ts:permission_decision.
            this.dispatch({
                type: 'permission_decision',
                toolUseId: pending.id,
                decision: 'denied',
                timestamp: Date.now(),
            });
        }
        this._sendControl({method: 'deny'});
    }

    /** Interrupt the current turn — REST POST. */
    async interrupt(): Promise<void> {
        if (this.isInterrupting()) return;
        this.isInterrupting.set(true);
        const tid = this.threadId();
        if (!tid) return;
        try {
            await firstValueFrom(
                this.http.post(`${environment.apiUrl}/persistent/threads/${tid}/interrupt`, {})
            );
        } catch (err: any) {
            // Interrupt failures are rare and the SSE will surface the next
            // turn boundary regardless — log and reset the flag.
            this.isInterrupting.set(false);
            console.warn('[persistent-chat] interrupt failed:', err);
        }
        // On success, the agent emits `interrupt.ack` over SSE and the
        // handler below resets isInterrupting/isStreaming/currentTurnId.
    }

    /** Stop a pending permission prompt + halt the turn so the user can
     *  type a follow-up. Denies the call so the backend isn't stranded
     *  awaiting a decision (the loop would otherwise block on the
     *  `permission_check` await forever), then sends interrupt so the
     *  next loop iteration bails out instead of acting on the denial. */
    stop(): void {
        this.deny();
        void this.interrupt();
    }

    /** Change permission mode. */
    setMode(mode: PermissionMode): void {
        this.permissionMode.set(mode);
        this._sendControl({method: 'mode.set', mode});
    }

    setNarrationMode(mode: NarrationMode): void {
        this.narrationMode.set(mode);
        this._sendControl({method: 'narration.set', mode});
    }

    /** Update session config (model, temperature, etc.) at runtime. */
    updateConfig(config: Record<string, unknown>): void {
        this._sendControl({method: 'config.update', config});
    }

    /** Clear conversation history (local only). */
    clearMessages(): void {
        this.dispatch({type: 'reset', threadId: this.threadId()});
    }

    // ── Event handling (shared by SSE and historical WS path) ───────────

    private _handleEvent(data: { method: string; params?: Record<string, unknown> }): void {
        const params = data.params ?? {};
        const now = Date.now();

        switch (data.method) {
            case 'status':
                this.startupPhase.set((params['phase'] as string) || null);
                break;

            case 'session.state':
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
                this.markSessionReady();
                break;

            case 'greeting': {
                // Synthetic single-turn assistant message — agent welcome line.
                const id = makeLocalId('greet');
                this.dispatch({type: 'turn_started', turnId: id, startedAt: now});
                this.dispatch({
                    type: 'token',
                    content: (params['content'] as string) || '',
                    timestamp: now,
                });
                this.dispatch({type: 'turn_completed', turnId: id, finishedAt: now});
                this.isWaitingForInput.set(true);
                break;
            }

            case 'ready':
                this.isWaitingForInput.set(true);
                // If a turn is still open (race with turn.completed dropped), close it.
                this._closeActiveTurnIfAny('turn_completed');
                this.markSessionReady();
                break;

            case 'turn.started': {
                const turnId = String(params['turn_id'] ?? makeLocalId('turn'));
                this.turnCount.update((c) => c + 1);
                this.dispatch({
                    type: 'turn_started',
                    turnId,
                    startedAt: now,
                    model: (params['model'] as string) || undefined,
                });
                break;
            }

            case 'token':
                this.dispatch({
                    type: 'token',
                    content: (params['content'] as string) || '',
                    timestamp: now,
                });
                break;

            case 'thinking':
                this.dispatch({
                    type: 'thinking',
                    content: (params['content'] as string) || '',
                    timestamp: now,
                });
                break;

            case 'tool.started':
                this.dispatch({
                    type: 'tool_started',
                    toolUseId: (params['id'] as string) || '',
                    tool: (params['tool'] as string) || '',
                    args: (params['args'] as Record<string, unknown>) || {},
                    category: (params['category'] as string) || undefined,
                    timestamp: now,
                });
                break;

            case 'tool.completed':
                this.dispatch({
                    type: 'tool_completed',
                    toolUseId: (params['id'] as string) || '',
                    result: (params['result'] as string) || '',
                    isError: !!params['is_error'],
                    timestamp: now,
                });
                break;

            case 'permission.request': {
                const id = (params['id'] as string) || '';
                const tool = (params['tool'] as string) || '';
                const args = (params['args'] as Record<string, unknown>) || {};
                this.pendingPermission.set({id, tool, args});
                this.dispatch({
                    type: 'permission_request',
                    toolUseId: id,
                    tool,
                    args,
                    timestamp: now,
                });
                break;
            }

            case 'turn.completed': {
                const turnId = String(params['turn_id'] ?? this.conversation().activeAssistantTurnId ?? '');
                if (turnId) {
                    this.dispatch({type: 'turn_completed', turnId, finishedAt: now});
                }
                this.isInterrupting.set(false);
                break;
            }

            case 'interrupt.ack':
                this._closeActiveTurnIfAny('turn_interrupted');
                this.isInterrupting.set(false);
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
                this._systemMessage(
                    `Context compacted: ${params['before']} → ${params['after']} messages`,
                );
                break;

            case 'session.ended':
                this._systemMessage('Session ended.');
                this.isWaitingForInput.set(false);
                this.threadStatus.set('ended');
                this.endedAt.set(new Date().toISOString());
                break;

            case 'session.idle_timeout':
                this._systemMessage(
                    `Session paused after ${(params['timeout_minutes'] as number) || 30} minutes of inactivity. Your work has been saved.`,
                );
                this.isWaitingForInput.set(false);
                this.threadStatus.set('ended');
                this.endedAt.set(new Date().toISOString());
                break;

            case 'vm_upgrade.needed':
                this._systemMessage(
                    `VM upgrade needed: ${(params['reason'] as string) || 'sudo detected'}. `
                    + `Use the upgrade button or send /upgrade to switch to a VM workspace.`,
                );
                break;

            case 'vm_upgrade.started':
                this._systemMessage('Upgrading workspace to VM, please wait...');
                break;

            case 'vm_upgrade.complete':
                this._systemMessage('VM upgrade complete. Workspace is now running on a VM with sudo access.');
                break;

            case 'vm_upgrade.failed':
                this._systemMessage(
                    `VM upgrade failed: ${(params['reason'] as string) || 'unknown error'}`,
                );
                break;

            case 'tasks.updated':
                this.tasks.set((params['tasks'] as SessionTask[]) || []);
                break;

            case 'file.checkpoint':
                this.undoAvailable.set(true);
                break;

            case 'files.restored':
                this.undoAvailable.set(false);
                this._systemMessage(
                    `Restored ${(params['paths'] as string[])?.length || 0} file(s) to pre-edit state.`,
                );
                break;

            case 'error':
                this.error.set(this.sanitizeError(params['message'] as string));
                break;
        }
    }

    /** Close the in-flight turn (if any) as either done or interrupted. */
    private _closeActiveTurnIfAny(kind: 'turn_completed' | 'turn_interrupted'): void {
        const activeId = this.conversation().activeAssistantTurnId;
        if (!activeId) return;
        this.dispatch({type: kind, turnId: activeId, finishedAt: Date.now()});
    }

    /** Apply a reducer action to the conversation state. */
    private dispatch(action: ReducerAction): void {
        this.conversation.update((s) => reduce(s, action));
    }

    /**
     * Convert backend exception strings into friendly user-facing messages.
     * Raw Python tracebacks and library-internal error strings (e.g. LangChain
     * "Got unknown type ...", "'NoneType' object has no attribute ...") leak
     * implementation details and confuse users. Log the original to console
     * for debugging; surface a generic message instead.
     */
    private sanitizeError(raw: string | undefined | null): string {
        if (!raw) return 'Unknown error';
        const msg = String(raw);

        // Always preserve the original for devs.
        console.warn('[persistent-chat] backend error:', msg);

        // Race-condition fallout: session detached mid-turn.
        if (/'NoneType' object has no attribute/i.test(msg)) {
            return 'Session was interrupted. Try sending your message again or refresh the page.';
        }

        // LangChain provider couldn't classify a message — usually fires
        // after a streaming response gets corrupted (e.g. WS reconnect).
        if (/Got unknown type/i.test(msg)) {
            return 'The assistant returned a malformed response. Try sending your message again.';
        }

        // LLM upstream timeout (10 min) — actionable, but the raw string
        // already says it cleanly.
        if (/LLM call timed out/i.test(msg)) {
            return msg;
        }

        // Python traceback leaked through.
        if (/Traceback \(most recent call last\)/i.test(msg)) {
            return 'Something went wrong on the server. Check the console for details.';
        }

        // Cap length so a long error doesn't blow out the banner.
        if (msg.length > 240) {
            return msg.slice(0, 240) + '…';
        }
        return msg;
    }

    /** Mark the session as ready and flush any pending message. */
    private markSessionReady(): void {
        if (this.sessionReady()) return;
        this.sessionReady.set(true);

        const pending = this.pendingMessage();
        if (pending) {
            this.pendingMessage.set(null);
            // Send directly — the message was already added to the messages
            // array when the user submitted it, so we skip sendMessage() to
            // avoid duplicates.
            this.isWaitingForInput.set(false);
            void this._postInput(pending);
        }
    }

}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let _localIdCounter = 0;

/** Generate a short, monotonic, locally-unique id for synthetic turns. */
function makeLocalId(prefix: string): string {
    _localIdCounter += 1;
    return `${prefix}-${Date.now()}-${_localIdCounter}`;
}

/**
 * Group flat HistoryMessage rows into Turns for the rehydration path.
 *
 * - Multiple assistant rows that share a `turn_number` collapse into one
 *   AssistantTurn. Each row contributes (in order) an optional ThoughtEvent
 *   (when `thinking` is populated — see migration 0011), an optional
 *   TextEvent (when `content` is non-empty), and one ToolCallEvent per
 *   entry in `tool_calls`.
 * - `role='tool'` rows are matched back to their originating ToolCallEvent
 *   by `tool_call_id` and populate its `result` / `resultStatus`. Rows
 *   without a matching call (pre-0011 historical data) are dropped — same
 *   user-visible behavior as before this migration.
 */
function historyToTurns(messages: HistoryMessage[]): Turn[] {
    const turns: Turn[] = [];
    const turnByNumber = new Map<number, AssistantTurn>();
    const toolCallById = new Map<string, ToolCallEvent>();

    for (const m of messages) {
        const isUser = ['human', 'user', 'HumanMessageChunk'].includes(m.role);
        const isAssistant = ['ai', 'assistant', 'AIMessageChunk'].includes(m.role);
        const isTool = m.role === 'tool' || m.role === 'ToolMessageChunk';
        if (!isUser && !isAssistant && !isTool) continue;

        const ts = m.created_at ? Date.parse(m.created_at) || Date.now() : Date.now();

        if (isUser) {
            const u: UserTurn = {
                kind: 'user',
                id: m.id,
                content: m.content || '',
                timestamp: ts,
                historical: true,
            };
            turns.push(u);
            continue;
        }

        if (isTool) {
            // Match result back to the originating call. Rows missing
            // tool_call_id (pre-migration 0011 data) can't be linked and
            // are silently dropped — same as the prior behavior.
            const callId = m.tool_call_id;
            if (!callId) continue;
            const tc = toolCallById.get(callId);
            if (!tc) continue;
            tc.result = m.content ?? '';
            tc.resultStatus = 'ok';
            // Don't clobber a 'denied' decision recorded on the AI side.
            if (tc.status !== 'denied') tc.status = 'completed';
            continue;
        }

        // Assistant row.
        let turn = m.turn_number != null ? turnByNumber.get(m.turn_number) : undefined;
        if (!turn) {
            turn = {
                kind: 'assistant',
                id: m.id,
                events: [],
                status: 'done',
                startedAt: ts,
                finishedAt: ts,
                historical: true,
            };
            if (m.turn_number != null) turnByNumber.set(m.turn_number, turn);
            turns.push(turn);
        }

        if (m.thinking) {
            turn.events.push({
                kind: 'thought',
                id: `${turn.id}.b${turn.events.length}`,
                content: m.thinking,
                status: 'done',
                startedAt: ts,
            });
        }
        if (m.content) {
            turn.events.push({
                kind: 'text',
                id: `${turn.id}.b${turn.events.length}`,
                content: m.content,
                status: 'done',
                startedAt: ts,
            });
        }
        if (m.tool_calls) {
            for (const tc of m.tool_calls) {
                const event: ToolCallEvent = {
                    kind: 'tool_call',
                    id: tc.id || `${turn.id}.tc${turn.events.length}`,
                    tool: tc.name || '',
                    args: tc.args || {},
                    status: tc.decision === 'denied' ? 'denied' : 'completed',
                    decision: tc.decision,
                    startedAt: ts,
                };
                turn.events.push(event);
                if (tc.id) toolCallById.set(tc.id, event);
            }
        }
        turn.finishedAt = ts;
    }

    return turns;
}

/** Shape of a message from the REST history endpoint. */
interface HistoryMessage {
    id: string;
    role: string;
    content: string | null;
    tool_calls: { name: string; args: Record<string, unknown>; id: string; decision?: 'approved' | 'denied' }[] | null;
    turn_number: number | null;
    /** Set only on role='tool' rows — points to the AI message's tool_calls[].id. */
    tool_call_id?: string | null;
    /** Set only on role='ai' rows that carry reasoning content. */
    thinking?: string | null;
    created_at: string | null;
}
