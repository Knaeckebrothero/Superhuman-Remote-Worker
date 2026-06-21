import {computed, DestroyRef, effect, inject, Injectable, NgZone, signal, untracked} from '@angular/core';
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
import {TranslocoService} from '@jsverse/transloco';
import {ApiService} from './api.service';
import {IndexedDbService} from './indexed-db.service';
import {NotificationService} from './notification.service';
import {reduce, ReducerAction} from './turn-reducer';
import {AppToastService} from '../../ui/toast';

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

// Control-WS liveness watchdog. The agent's subscriber pump sends a
// `ws.ping` frame every ~20s of idle (src/api/persistent_app.py,
// _WS_PING_INTERVAL_S); if the socket claims OPEN but nothing arrived in
// CONTROL_WS_WATCHDOG_TIMEOUT_MS it's half-open (edge/tunnel idle kill — no
// close frame ever reaches us), so force-close it and let the reconnect
// ladder re-fetch a fresh token. Closes the gap left when F4.5 shipped for
// the SSE only (session_silent_failure_audit.md #9).
const CONTROL_WS_WATCHDOG_INTERVAL_MS = 15_000;
const CONTROL_WS_WATCHDOG_TIMEOUT_MS = 45_000;

// SSE liveness watchdog. The orchestrator emits a typed `ping` event every
// ~20s of idle (see `thread_event_stream` in `orchestrator/main.py`); the
// watchdog checks every WATCHDOG_INTERVAL_MS and forces a reopen if no SSE
// event of any kind has arrived in WATCHDOG_TIMEOUT_MS. This catches silent
// TCP drops (Wi-Fi blip, captive portal, laptop sleep) that don't trip
// `EventSource.onerror` until the OS-level keepalive eventually fires
// hours later. See `docs/issues/persistent_chat_silent_disconnect.md`.
const SSE_WATCHDOG_INTERVAL_MS = 5000;
const SSE_WATCHDOG_TIMEOUT_MS = 45000;

// After an interrupt POST we wait for the agent to emit `interrupt.ack` /
// `turn.completed` over SSE to clear the "Stopping…" state. If that frame is
// lost (silently-stalled stream) the button would wedge forever — re-clicks
// early-return. If nothing clears it within this window, force an SSE reopen
// (replay-from-cursor) which re-delivers the durable turn boundary.
const INTERRUPT_ACK_TIMEOUT_MS = 8000;

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

/**
 * The tool call the agent is currently blocked on, delivered in the
 * session.state welcome frame so a (re)attaching client can render a
 * running-command card even when the in-flight turn isn't in REST history yet
 * (it's persisted only at turn end).
 */
export interface RunningToolInfo {
    id: string;
    tool: string;
    args: Record<string, unknown>;
}

/** Permission request from the agent. */
export interface PermissionRequest {
    /** Tool call id — correlates the eventual decision back to the call. */
    id: string;
    /** Durable DB permission request id, when emitted by the backend. */
    approvalId?: string;
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
 * Live token telemetry for the current turn, driven by per-LLM-call
 * `usage.updated` frames. `inputTokens` is the latest call's prompt size —
 * effectively the current context fill — while output/reasoning accumulate
 * across the turn's calls. (docs/features/context_summarization_rework.md S5)
 */
export interface UsageState {
    turn: number | null;
    inputTokens: number | null;
    outputTokensTurn: number;
    reasoningTokensTurn: number;
    ctxLimitTokens: number | null;
}

/**
 * Live state of an in-flight context compaction, driven by the agent's
 * `compaction.started` / `compaction.progress` frames and cleared by
 * `context.compacted` (success) or `compaction.failed`. Frames are journaled
 * server-side, so a reload mid-compaction reconstructs this from SSE replay
 * (possibly from a progress frame alone — all fields nullable-tolerant).
 * See docs/features/context_summarization_rework.md (S3).
 */
export interface CompactionProgressState {
    trigger: string;
    totalTokens: number | null;
    ctxUsedTokens: number | null;
    ctxLimitTokens: number | null;
    ctxUsedPct: number | null;
    auxLimitTokens: number | null;
    nPasses: number;
    /** 0 while planning (no progress frame yet). */
    currentPass: number;
    firstMsg: number | null;
    lastMsg: number | null;
    inTokens: number | null;
    outTokens: number | null;
    attempt: number;
    stage: string;
    /** Client epoch ms when the first compaction frame arrived (elapsed timer). */
    startedAt: number;
}

/**
 * Response payload for ``GET /api/sessions/{thread_id}/connection`` — the
 * canonical WS URL + JWT for a bound session. The cockpit dials the WS at
 * `ws_url` directly (routed to the agent pod by the orchestrator's edge
 * proxy). See orchestrator/routers/sessions.py for the response shape.
 */
interface ConnectionPayload {
    state: 'ready';
    ws_url: string;
    token: string;
    expires_at: number;
}

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
    private readonly toast = inject(AppToastService);
    private readonly notifications = inject(NotificationService);
    private readonly transloco = inject(TranslocoService);
    private readonly destroyRef = inject(DestroyRef);

    constructor() {
        // Single source of truth for the "Starting session" card phase
        // transitions. The orchestrator emits session.lifecycle events on
        // the user's always-on /notifications/events SSE from every
        // binding path (provision_or_assign for the create-thread fast
        // path, _do_prepare for the cold path). Subscribe via the
        // NotificationService signal so the SSE feed isn't opened twice.
        effect(() => {
            const event = this.notifications.lifecycleEvent();
            const tid = this.threadId();
            if (!event || !tid || event.thread_id !== tid) return;
            // Once the session is actually live, ignore further lifecycle
            // events (a duplicate from a racing /prepare must not regress
            // the UI). isStartingSession also gates rendering on
            // sessionReady, so the card hides naturally.
            if (this.sessionReady()) return;
            switch (event.state) {
                case 'provisioning':
                case 'booting':
                    this.startupPhase.set(event.state);
                    break;
                case 'ready':
                    // Server says the agent is session-ready; the cockpit
                    // is now opening the WS — that's the "establishing
                    // connection" phase. session.state arriving on the WS
                    // will flip sessionReady=true and hide the card.
                    this.startupPhase.set('connecting');
                    break;
                case 'failed':
                    this.error.set(event.reason || 'session preparation failed');
                    break;
            }
        });

        // Invariant: "Stopping…" (isInterrupting) only makes sense while a turn
        // is actually streaming. Whenever streaming ends — turn completed, the
        // turn closed on disconnect, or a reconnect re-synced past it — clear
        // the flag and its fallback timer. This is the safety net that stops a
        // lost interrupt.ack/turn.completed frame from wedging the button: any
        // path that drops the active turn also drops "Stopping…".
        effect(() => {
            if (!this.isStreaming() && untracked(() => this.isInterrupting())) {
                this.isInterrupting.set(false);
                this._clearInterruptFallback();
            }
        });

        // Re-validate the connection whenever the user returns to the tab.
        // The SSE liveness watchdog is a setInterval, which browsers freeze on
        // a backgrounded tab (~5 min) — so a silent drop goes unnoticed until
        // the user comes back. These DOM listeners fire outside Angular's zone,
        // hence zone.run. Registered once on this root singleton and torn down
        // via DestroyRef (matters for test isolation, not prod lifetime).
        if (typeof document !== 'undefined') {
            const onWake = () => this.zone.run(() => this._revalidateConnection());
            const onVisible = () => {
                if (document.visibilityState === 'visible') onWake();
            };
            document.addEventListener('visibilitychange', onVisible);
            window.addEventListener('online', onWake);
            window.addEventListener('focus', onWake);
            this.destroyRef.onDestroy(() => {
                document.removeEventListener('visibilitychange', onVisible);
                window.removeEventListener('online', onWake);
                window.removeEventListener('focus', onWake);
            });
        }
    }

    // --- Connection state ---
    readonly connectionState = signal<ConnectionState>('disconnected');
    readonly isConnected = computed(() => this.connectionState() === 'connected');

    // Agent-liveness, distinct from connection-liveness (audit #8). Seconds
    // since the last agent-origin frame while a turn is open; 0 when idle.
    // Updated on the SSE watchdog's 5s tick.
    readonly agentSilenceSeconds = signal<number>(0);
    private agentLastEventAt = 0;

    // Live context-compaction progress (null = no compaction running).
    // Drives the in-chat progress block + status-bar strip.
    readonly compaction = signal<CompactionProgressState | null>(null);

    // Live per-turn token telemetry (usage.updated frames); null until the
    // first main-LLM call reports usage.
    readonly usage = signal<UsageState | null>(null);
    readonly threadId = signal<string | null>(null);

    /**
     * True iff a session start is *actively in flight* — POST creating a thread,
     * SSE/WS handshaking, or connected-but-waiting-for-the-agent-ready frame.
     * Gates the "Starting session" card so it never lingers after disconnect()
     * (which nulls `threadStatus`, so a `threadStatus !== 'ended'` check alone
     * would render a fake spinner indefinitely).
     */
    readonly isStartingSession = computed(() =>
        !this.sessionReady() &&
        this.threadStatus() !== 'ended' &&
        (this.isCreating() ||
            this.connectionState() === 'connecting' ||
            this.connectionState() === 'connected'),
    );

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

    // --- Render windowing (display-only) ---
    // The full conversation lives in `turns`; the component renders only the
    // most recent `windowSize` turns so the DOM stays bounded on long threads
    // (a single thread can be 800+ turns). `loadOlderTurns` widens the window on
    // scroll-up; `growWindow` keeps the visible top anchored when new turns
    // stream in below a scrolled-away user; `resetWindow` re-bounds the DOM on
    // (re)load and once the user is back at the bottom.
    private readonly DEFAULT_WINDOW = 50;
    private readonly WINDOW_STEP = 50;
    readonly windowSize = signal(this.DEFAULT_WINDOW);
    readonly visibleTurns = computed(() => {
        const all = this.turns();
        const n = this.windowSize();
        return all.length <= n ? all : all.slice(-n);
    });
    readonly hasOlderTurns = computed(() => this.turns().length > this.windowSize());

    /** Widen the render window toward the start of the conversation. */
    loadOlderTurns(): void {
        this.windowSize.update((n) =>
            Math.min(n + this.WINDOW_STEP, this.turns().length),
        );
    }

    /** Anchor the visible top while turns stream in below a scrolled-away user. */
    growWindow(delta: number): void {
        if (delta > 0) this.windowSize.update((n) => n + delta);
    }

    /** Re-bound the DOM to the most recent window (on (re)load / back at bottom). */
    resetWindow(): void {
        this.windowSize.set(this.DEFAULT_WINDOW);
    }

    // --- Permission state ---
    readonly permissionMode = signal<PermissionMode>('supervised');
    readonly pendingPermission = signal<PermissionRequest | null>(null);

    // --- Running-command snapshot ---
    // Set from the session.state welcome frame on (re)attach when the loop is
    // blocked in a tool call; cleared when that tool completes or the turn ends.
    // Lets the UI show a "running command" card instead of a blank "Connecting…"
    // during a long mid-turn block (the in-flight turn isn't in REST history).
    readonly runningTool = signal<RunningToolInfo | null>(null);

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

    // --- Cloud sync degraded (initial cloud->workspace seed failed) ---
    /**
     * True when this session's initial cloud->workspace sync failed: the
     * workspace may be missing files from the cloud, and edits will NOT be
     * saved back to the cloud for the session's lifetime. Sticky for the
     * session (reset on each (re)connect). See docs/issues/main_cloud.md
     * Issue 13.
     */
    readonly cloudSyncDegraded = signal(false);

    // --- Creating state (thread being created via API before connect) ---
    readonly isCreating = signal(false);

    // --- Session paused (idle timeout received) ---
    readonly isSessionPaused = signal(false);

    // --- Error ---
    readonly error = signal<string | null>(null);

    private sse: EventSource | null = null;
    private sseWatchdogTimer: ReturnType<typeof setInterval> | null = null;
    // One-shot fallback: force a reconnect if "Stopping…" doesn't clear after
    // an interrupt POST (lost ack frame). Armed in interrupt(), cleared by the
    // isInterrupting invariant effect.
    private interruptFallbackTimer: ReturnType<typeof setTimeout> | null = null;
    private sseLastEventAt = 0;
    private controlWs: WebSocket | null = null;
    private controlWsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private controlWsReconnectAttempt = 0;
    private controlWsLastMessageAt = 0;
    private controlWsWatchdogTimer: ReturnType<typeof setInterval> | null = null;
    private intentionalClose = false;
    /**
     * Guard against double-opening the control WS while an async
     * /connection (or /prepare → SSE ready → /connection) fetch is in
     * flight. Distinct from `controlWs` (which is null during the fetch
     * window — _ensureControlWs() must not race past).
     */
    private controlWsOpening = false;

    /**
     * Connect to a persistent agent session.
     *
     * Cold path (new thread or thread switch): load thread metadata +
     * transcript history from REST, then open SSE (replay-from-cursor when
     * we have one cached) and the control WS.
     *
     * Same-thread fast path: skip the reset + loadHistory, just refresh
     * transports. This preserves the in-flight assistant turn through
     * chat-page re-mounts and other reconnects against the same thread
     * (docs/issues/persistent_chat_lost_assistant_turn_on_mid_turn_reload.md
     * §Approach 1). loadHistory's GET /messages only returns *persisted*
     * rows; during streaming the AI message isn't fully in thread_messages
     * yet, so re-running it mid-turn would reset state and replace the
     * visible streaming turn with just its persisted prefix. Subsequent SSE
     * events arriving without an active turn are no longer dropped — since
     * §Approach 2, `ensurePlaceholderTurn` absorbs them into a synthetic
     * `recovered:` bubble — but that recovery is a fallback, not a reason to
     * blow away the live turn here.
     */
    async connect(threadId: string): Promise<void> {
        const sameThread = this.threadId() === threadId && this.historyLoaded();
        this.disconnect();
        this.connectionState.set('connecting');
        this.error.set(null);
        this.cloudSyncDegraded.set(false);
        if (!sameThread) {
            // Cold path: wipe and refetch.
            this.dispatch({type: 'reset', threadId});
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
            this.runningTool.set(null);

            this.threadId.set(threadId);
            await this.loadHistory(threadId);
        }
        await this.loadThreadMeta(threadId);

        // Don't auto-connect to ended sessions — render the read-only resume
        // card instead. The user explicitly clicks "Resume" to come back online.
        if (this.threadStatus() === 'ended') {
            this.connectionState.set('disconnected');
            return;
        }

        this.intentionalClose = false;
        await this._openSse(threadId);
        await this._openControlWs(threadId);
    }

    /**
     * Create a new persistent thread via REST, then connect.
     * Sets isCreating=true immediately so the UI can show a spinner.
     */
    async createAndConnect(body: Record<string, any>): Promise<string> {
        this.disconnect();
        // Clear the conversation + threadId synchronously so the "Creating
        // thread …" startup card isn't rendered on top of turns from the
        // session the user just navigated away from. disconnect() intentionally
        // keeps turns visible (the "Disconnect" button is a read-only state),
        // so the create path has to do it explicitly. connect() will reset
        // again with the real thread id once the POST resolves.
        this.dispatch({type: 'reset', threadId: null});
        this.threadId.set(null);
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
            // 1. Cache-first: paint the cached conversation immediately (zero
            //    latency on reopen). Empty when this thread isn't cached yet.
            const cached = await this.cache.getThreadMessages(threadId);
            if (cached.length) {
                this.dispatch({type: 'load_history', threadId, turns: historyToTurns(cached)});
                this.resetWindow();
                this.historyLoaded.set(true);
            }

            // 2. Refresh from the server. With a cache, fetch only what's newer
            //    (?after=<newest cached>, inclusive); otherwise the full thread.
            const newest = cached.length ? cached[cached.length - 1].created_at : null;
            const url = newest
                ? `${environment.apiUrl}/persistent/threads/${threadId}/messages` +
                  `?after=${encodeURIComponent(newest)}`
                : `${environment.apiUrl}/persistent/threads/${threadId}/messages`;
            const resp = await firstValueFrom(
                this.http.get<{messages: HistoryMessage[]; total: number}>(url),
            );
            const fetched = resp.messages ?? [];

            // 3. Append to the cache by id (never full-replace — that loses
            //    history). Best-effort: a no-op when IndexedDB is unavailable.
            if (fetched.length) {
                void this.cache.upsertThreadMessages(
                    fetched.map((m) => ({...m, threadId})),
                );
            }

            // 4. Render the merged set. Merge in memory (dedup by id) rather than
            //    reading the cache back, so the render is correct even when
            //    IndexedDB is unavailable. Skip the re-render when the cache was
            //    already current (nothing new fetched).
            if (fetched.length || !cached.length) {
                const merged = mergeMessagesById(cached, fetched);
                this.dispatch({type: 'load_history', threadId, turns: historyToTurns(merged)});
                this.resetWindow();
            }
            this.historyLoaded.set(true);
        } catch {
            // Network failure is non-fatal — any cached transcript was already
            // painted above; just mark history loaded.
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
        // Drop any stale watchdog from a prior open before installing a new one.
        this._stopSseWatchdog();

        const cursor = await this.cache.getThreadCursor(threadId);
        // ngsw-bypass keeps the Angular service worker out of the SSE path. Its
        // /api/** dataGroup otherwise buffers the stream body (which never ends),
        // stalling EventSource.onopen ~20s. The param's presence alone is enough.
        const params = new URLSearchParams({'ngsw-bypass': 'true'});
        if (cursor) params.set('last_event_id', `${cursor.epoch}:${cursor.seq}`);
        const url = `${environment.apiUrl}/persistent/threads/${threadId}/stream?${params.toString()}`;

        // withCredentials true so the srw_session cookie rides along on the
        // cross-origin SSE handshake.
        this.sse = new EventSource(url, {withCredentials: true});

        this.sse.onopen = () => {
            this.zone.run(() => {
                const wasReconnecting = this.connectionState() !== 'connected';
                this.connectionState.set('connected');
                this.error.set(null);
                this.reconnectAttempt.set(0);
                this.reconnectGaveUp.set(false);
                this._startSseWatchdog(threadId);
                // On a reconnect (not the initial open), refetch thread meta
                // so any title.updated / status frame that crossed the wire
                // while we were disconnected is reconciled. Without this the
                // header can stay stuck on "Untitled Session" after a backend
                // loop_crash even though the title was generated and persisted.
                if (wasReconnecting && this.threadId() === threadId) {
                    void this.loadThreadMeta(threadId);
                    // Slave the control WS to SSE recovery: the WS has no
                    // liveness probe of its own, so re-establish it whenever
                    // the (monitored) SSE recovers. Idempotent — bails if the
                    // WS is already open/connecting.
                    this._ensureControlWs();
                }
            });
        };

        this.sse.onmessage = (event: MessageEvent) => {
            this.sseLastEventAt = Date.now();
            this.zone.run(() => this._handleSseFrame(event));
        };

        // Server idle-heartbeat — no payload to dispatch, just liveness.
        this.sse.addEventListener('ping', () => {
            this.sseLastEventAt = Date.now();
        });

        this.sse.addEventListener('gone_beyond_horizon', (event) => {
            this.sseLastEventAt = Date.now();
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
                    this._stopSseWatchdog();
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

    /**
     * Liveness watchdog for the SSE receive path. Browser `EventSource` only
     * fires `onerror` when the TCP socket closes cleanly or the OS-level
     * keepalive trips (Linux default: 7200s), so silent network drops leave
     * the stream in `readyState === OPEN` for hours. The orchestrator emits
     * a typed `ping` event every ~20s of idle; if we haven't seen *any* SSE
     * event (ping or otherwise) for SSE_WATCHDOG_TIMEOUT_MS, the connection
     * is presumed dead and we force a reopen.
     */
    private _startSseWatchdog(threadId: string): void {
        this._stopSseWatchdog();
        this.sseLastEventAt = Date.now();
        this.agentLastEventAt = Date.now();
        this.sseWatchdogTimer = setInterval(() => {
            // Piggyback the agent-quiet signal on this 5s tick (audit #8):
            // only meaningful while a turn is open — an idle agent being
            // quiet is expected.
            this.zone.run(() => {
                const turnOpen = this.conversation().activeAssistantTurnId != null;
                this.agentSilenceSeconds.set(
                    turnOpen && this.agentLastEventAt > 0
                        ? Math.floor((Date.now() - this.agentLastEventAt) / 1000)
                        : 0,
                );
            });
            if (!this.sse || this.sse.readyState !== EventSource.OPEN) {
                // CONNECTING/CLOSED is already handled by onerror.
                return;
            }
            if (Date.now() - this.sseLastEventAt <= SSE_WATCHDOG_TIMEOUT_MS) {
                return;
            }
            // Silent drop. Tear down and let the existing reopen path
            // (with replay-from-cursor) take over.
            this._stopSseWatchdog();
            this.zone.run(() => {
                if (this.sse) {
                    this.sse.close();
                    this.sse = null;
                }
                this.connectionState.set('connecting');
                this.reconnectAttempt.update(n => n + 1);
                void this._openSse(threadId);
            });
        }, SSE_WATCHDOG_INTERVAL_MS);
    }

    private _stopSseWatchdog(): void {
        if (this.sseWatchdogTimer) {
            clearInterval(this.sseWatchdogTimer);
            this.sseWatchdogTimer = null;
        }
    }

    /**
     * Re-validate liveness on tab resume (visibilitychange / online / focus).
     * The SSE is the single liveness authority: if it isn't OPEN or has gone
     * silent past the watchdog timeout, force a reopen (replay-from-cursor);
     * then ensure the control WS — which has no probe of its own — is back.
     * No-op when there's no active, live session.
     */
    private _revalidateConnection(): void {
        if (this.intentionalClose) return;
        const tid = this.threadId();
        if (!tid) return;
        if (this.threadStatus() === 'ended') return;
        const sseStale =
            !this.sse ||
            this.sse.readyState !== EventSource.OPEN ||
            Date.now() - this.sseLastEventAt > SSE_WATCHDOG_TIMEOUT_MS;
        if (sseStale) {
            // Closes + reopens the SSE and sets connectionState='connecting';
            // the reopen's onopen also re-ensures the control WS (Change 2).
            this.reconnectNow();
        } else {
            // SSE healthy but the WS may have silently dropped — re-ensure it.
            this._ensureControlWs();
        }
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
     * seq older than retention). REST-reload the transcript snapshot so the
     * user sees completed turns, then re-anchor the cursor to the server tail
     * (reported in the frame) and reopen the stream.
     *
     * Re-anchoring (rather than dropping the cursor) is what prevents the
     * duplicate-turn render: with no cursor the server replays the whole epoch
     * from seq 0, re-delivering every completed turn as a "live" copy that the
     * reducer can't reconcile against the just-loaded history (history turns
     * are keyed by DB id, replayed turns by numeric turn_id), so the turn shows
     * twice split by a spurious "SESSION RESUMED" divider. Anchoring to the
     * tail replays only genuinely newer events. See memory
     * project_session_epoch_duplicate_render.
     */
    private async _handleGoneBeyondHorizon(event: MessageEvent): Promise<void> {
        const tid = this.threadId();
        if (!tid) return;
        // The frame carries the live epoch + its tail seq:
        // {"params":{"epoch":N,"server_seq":M,...}}.
        let epoch: number | null = null;
        let serverSeq: number | null = null;
        try {
            const p = JSON.parse(event.data)?.params;
            epoch = typeof p?.epoch === 'number' ? p.epoch : null;
            serverSeq = typeof p?.server_seq === 'number' ? p.server_seq : null;
        } catch {
            // Malformed frame — fall back to dropping the cursor (replay-from-0).
        }
        if (this.sse) {
            this.sse.close();
            this.sse = null;
        }
        // Reload transcript so visible history doesn't have a silent gap.
        await this.loadHistory(tid);
        // Re-anchor to the server tail so the reopened stream replays only
        // events newer than the history we just loaded; drop the cursor only
        // when the frame lacked a usable tail.
        if (epoch != null && serverSeq != null) {
            await this.cache.setThreadCursor(tid, epoch, serverSeq);
        } else {
            await this.cache.deleteThreadCursor(tid);
        }
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

    /**
     * Resolve the canonical WS URL + token for the session via the
     * orchestrator REST endpoints (Tasks 6 + 7), then open the WebSocket.
     *
     * Two REST steps, joined by the SSE notification feed:
     *
     *   1. GET /api/sessions/{tid}/connection
     *      → 200 (ready): {ws_url, token, expires_at}. Open WS directly.
     *      → 425 (not bound yet): cold start, fall through to step 2.
     *
     *   2. POST /api/sessions/{tid}/prepare → 202 {state: provisioning}
     *      → wait for `session.lifecycle` SSE event with state=ready
     *      → GET /connection again, then open WS.
     *
     * Errors here are swallowed: the WS open is best-effort (the SSE
     * receive path is the primary signal). User-initiated commands that
     * need the WS will reopen via _ensureControlWs() on demand.
     */
    private async _openControlWs(threadId: string): Promise<void> {
        if (this.controlWsOpening) return;
        this.controlWsOpening = true;
        try {
            const connection = await this._resolveConnection(threadId);
            if (this.intentionalClose || this.threadId() !== threadId) return;
            // GET /connection only returns 200 after the orchestrator has a
            // bound agent and the agent's /ready probe passes. That REST
            // readiness is enough to unblock the composer; the control WS
            // session.state frame remains a useful reconciliation signal, but
            // must not be the only way to clear the startup card.
            if (connection.state === 'ready') {
                this.markSessionReady();
            }
            this._installControlWs(threadId, connection.ws_url);
        } catch {
            // Resolution failed — leave controlWs null; _ensureControlWs
            // (driven by user clicks) or the reconnect loop will retry.
            if (!this.intentionalClose && this.threadId() === threadId) {
                this._scheduleControlWsReconnect(threadId);
            }
        } finally {
            this.controlWsOpening = false;
        }
    }

    /**
     * Resolve the {ws_url, token} for a thread. On 425 (no binding yet)
     * POST /prepare to kick off provisioning, then poll /connection until
     * 200 — the always-on NotificationService SSE drives the
     * "Starting session" card via lifecycle events in parallel, so this
     * function only owns the token fetch, not the UI phase rendering.
     */
    private async _resolveConnection(threadId: string): Promise<ConnectionPayload> {
        try {
            return await this._fetchConnection(threadId);
        } catch (err: any) {
            if (err?.status !== 425) throw err;
            // Not bound yet — kick off /prepare and poll /connection until
            // the orchestrator binds an agent and the agent's /ready flips
            // true. (The cockpit's startup card is rendered by the
            // session.lifecycle effect in the constructor; we just wait
            // here for the token.)
            await firstValueFrom(
                this.http.post<{ state: string }>(
                    `${environment.apiUrl}/sessions/${threadId}/prepare`,
                    {},
                ),
            );
            return await this._pollConnectionUntilReady(threadId);
        }
    }

    /**
     * Poll GET /connection until it returns 200. Backoff: 1s, capped at
     * 2s. Aborts if the user navigates away (threadId() changes) or the
     * service is intentionally closed. Bounded by READY_TIMEOUT_MS so a
     * stuck attach surfaces as an error instead of polling forever.
     */
    private async _pollConnectionUntilReady(threadId: string): Promise<ConnectionPayload> {
        const READY_TIMEOUT_MS = 180_000;
        const deadline = Date.now() + READY_TIMEOUT_MS;
        let interval = 1_000;
        while (Date.now() < deadline) {
            if (this.intentionalClose || this.threadId() !== threadId) {
                throw new Error('connection cancelled');
            }
            try {
                return await this._fetchConnection(threadId);
            } catch (err: any) {
                if (err?.status === 425 || err?.status === 409) {
                    await new Promise((r) => setTimeout(r, interval));
                    interval = Math.min(2_000, interval + 250);
                    continue;
                }
                throw err;
            }
        }
        throw new Error('session preparation timed out');
    }

    private async _fetchConnection(threadId: string): Promise<ConnectionPayload> {
        return await firstValueFrom(
            this.http.get<ConnectionPayload>(
                `${environment.apiUrl}/sessions/${threadId}/connection`,
            ),
        );
    }

    private _installControlWs(threadId: string, wsUrl: string): void {
        this.controlWs = new WebSocket(wsUrl);
        this.controlWsLastMessageAt = Date.now();
        this._startControlWsWatchdog(threadId);
        this.controlWs.onclose = (event: CloseEvent) => {
            this.controlWs = null;
            this._stopControlWsWatchdog();
            if (this.intentionalClose) return;
            if (this.threadId() !== threadId) return;
            // 4401 = expired/invalid token (Task 8). The agent is still
            // bound — just need a fresh token. Skip /prepare and re-fetch
            // /connection directly.
            if ((event && (event as CloseEvent).code) === 4401) {
                void this._reopenWithFreshToken(threadId);
                return;
            }
            // Tiny linear backoff — primary connection signal lives on the SSE
            // so we don't need the WS aggressively reconnecting.
            this._scheduleControlWsReconnect(threadId);
        };
        this.controlWs.onerror = () => {
            // The close handler will fire; nothing to do here.
        };
        // SSE is the canonical receive path for agent-emitted events. The
        // orchestrator's _broadcast() stamps every persisted event with
        // params._seq = [epoch, seq] before writing it to thread_events,
        // so any frame carrying _seq will be redelivered by SSE — we drop
        // those here to avoid double-dispatch.
        //
        // Frames WITHOUT _seq come from _ws_send() (per-client direct
        // sends, never persisted): orchestrator status frames during WS
        // startup (provisioning/booting/connecting), the agent's
        // session.state welcome frame, and control-plane acks
        // (mode.changed, narration.changed, interrupt.ack, vm_upgrade.*,
        // workspace_upgrade.*).
        // These never reach SSE, so the WS is the only path that delivers
        // them — and session.state is what flips sessionReady on a
        // reconnect to an already-idle loop where the cached SSE cursor
        // sits past the most recent `ready` event.
        this.controlWs.onmessage = (event: MessageEvent) => {
            // Any frame proves liveness — including ws.ping, whose only job
            // is feeding this watchdog.
            this.controlWsLastMessageAt = Date.now();
            let frame: { method?: string; params?: Record<string, unknown> };
            try {
                frame = JSON.parse(event.data);
            } catch {
                return;
            }
            if (!frame?.method || frame.method === 'ws.ping') return;
            if (frame.params && (frame.params as Record<string, unknown>)['_seq'] != null) {
                return;
            }
            this.zone.run(() =>
                this._handleEvent(frame as { method: string; params?: Record<string, unknown> }),
            );
        };
    }

    /**
     * Re-fetch /connection (skip /prepare — the agent is still bound) and
     * reopen the control WS with the fresh token. Triggered by a 4401
     * close on the WS.
     */
    private async _reopenWithFreshToken(threadId: string): Promise<void> {
        if (this.controlWsOpening) return;
        this.controlWsOpening = true;
        try {
            const connection = await this._fetchConnection(threadId);
            if (this.intentionalClose || this.threadId() !== threadId) return;
            this._installControlWs(threadId, connection.ws_url);
        } catch {
            if (!this.intentionalClose && this.threadId() === threadId) {
                this._scheduleControlWsReconnect(threadId);
            }
        } finally {
            this.controlWsOpening = false;
        }
    }

    /** Force-close a half-open control WS that stopped delivering frames.
     *  close() fires onclose locally even when the peer is unreachable, so
     *  the regular reconnect ladder (with a fresh /connection token) takes
     *  over from there. */
    private _startControlWsWatchdog(threadId: string): void {
        this._stopControlWsWatchdog();
        this.controlWsWatchdogTimer = setInterval(() => {
            if (this.threadId() !== threadId || this.intentionalClose) {
                this._stopControlWsWatchdog();
                return;
            }
            const ws = this.controlWs;
            if (!ws || ws.readyState !== WebSocket.OPEN) return;
            if (Date.now() - this.controlWsLastMessageAt > CONTROL_WS_WATCHDOG_TIMEOUT_MS) {
                console.warn('[persistent-chat] control WS silent past watchdog — forcing reconnect');
                ws.close(4002, 'heartbeat timeout');
            }
        }, CONTROL_WS_WATCHDOG_INTERVAL_MS);
    }

    private _stopControlWsWatchdog(): void {
        if (this.controlWsWatchdogTimer) {
            clearInterval(this.controlWsWatchdogTimer);
            this.controlWsWatchdogTimer = null;
        }
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
            void this._openControlWs(threadId);
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
        if (this.controlWsOpening) return;
        this.controlWsReconnectAttempt = 0;
        void this._openControlWs(tid);
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
        this._stopSseWatchdog();
        this._stopControlWsWatchdog();
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
        this.compaction.set(null);
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
     * Resume an ended session and reconnect.
     *
     * /resume is required for ended threads — it flips status from 'ended'
     * → 'created' and clears the stale agent_id, kicking off a background
     * reprovision. From there we just call `connect()`, which routes through
     * `_openControlWs → _resolveConnection`: a `GET /connection` 425 falls
     * through to `_waitForLifecycleReady` driving `POST /prepare` against
     * the lifecycle SSE feed. The advisory lock in the orchestrator
     * serialises /resume's reprovision with /prepare's _do_prepare so we
     * don't double-provision (docs/issues/persistent_thread_double_provisioning_race.md).
     */
    async resumeSession(): Promise<void> {
        const threadId = this.threadId();
        if (!threadId) return;
        this.isSessionPaused.set(false);
        try {
            await firstValueFrom(
                this.http.post(`${environment.apiUrl}/persistent/threads/${threadId}/resume`, {})
            );
        } catch (err) {
            // /resume may 409 if the thread isn't actually 'ended' (e.g. a
            // double-click). Fall through to connect() either way — its
            // cold-start path is self-healing.
        }
        await this.connect(threadId);
    }

    /**
     * End the active session: DELETE the thread server-side (soft — status
     * flips to 'ended', workspace is snapshotted, agent pod is released) and
     * tear down the local transport. The thread row + Gitea repo + cloud
     * session folder are kept so the user can /resume later.
     *
     * Local disconnect runs regardless of the DELETE result so the user
     * never gets stuck on a stale connection if the API call fails.
     */
    async endSession(force = false): Promise<void> {
        const threadId = this.threadId();
        if (!threadId) {
            this.disconnect();
            return;
        }
        try {
            const qs = force ? '?force=true' : '';
            await firstValueFrom(
                this.http.delete(`${environment.apiUrl}/persistent/threads/${threadId}${qs}`),
            );
        } catch (err: unknown) {
            const status = (err as {status?: number})?.status;
            if (status === 409 && !force) {
                // Mid-turn guard (session_silent_failure_audit.md #11): the
                // orchestrator refuses to tear down a session whose agent is
                // mid-turn unless forced. Declining keeps the session alive.
                const proceed = confirm(this.transloco.translate('sessions.confirmEndMidTurn'));
                if (proceed) {
                    await this.endSession(true);
                }
                return;
            }
            this.disconnect();
            throw err;
        }
        this.disconnect();
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
    async sendMessage(content: string): Promise<boolean> {
        const trimmed = content.trim();

        // Slash commands bypass attachment logic.
        if (trimmed.startsWith('/')) {
            if (this.handleSlashCommand(trimmed)) return true;
        }

        const queued = this.pendingAttachments();
        if (!trimmed && queued.length === 0) return true;

        let uploaded: ThreadUploadedFile[] = [];
        if (queued.length > 0) {
            const threadId = this.threadId();
            if (!threadId) {
                this.attachmentError.set('Cannot upload: no active thread');
                return false;
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
                return false;
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
        const localId = makeLocalId('user');
        this.dispatch({
            type: 'user_message',
            id: localId,
            content: trimmed,
            attachments: attachments.length > 0 ? attachments : undefined,
            timestamp: Date.now(),
        });

        // If session isn't ready yet, queue and send when ready.
        if (!this.sessionReady()) {
            this.pendingMessage.set(sendContent);
            return true;
        }

        this.isWaitingForInput.set(false);
        const ok = await this._postInput(sendContent);
        if (!ok) {
            // Hard send failure — roll back the optimistic bubble so the
            // composer can restore the draft for retry (the error banner set
            // by _postInput explains why). A 409 dup is treated as success.
            this.dispatch({type: 'remove_turn', id: localId});
            return false;
        }
        return true;
    }

    /** POST the input to the orchestrator's REST endpoint. Returns true when
     *  the input was accepted (or a 409 dup — the reply still streams via
     *  SSE), false on a hard failure so the caller can roll back. */
    private async _postInput(content: string): Promise<boolean> {
        const tid = this.threadId();
        if (!tid) return false;
        try {
            await firstValueFrom(
                this.http.post<{ accepted: boolean; turn_id: number }>(
                    `${environment.apiUrl}/persistent/threads/${tid}/input`,
                    {content}
                )
            );
            return true;
        } catch (err: any) {
            // 409 means a duplicate POST landed for the same turn — the user
            // still sees the response stream via SSE, so treat it as success
            // and keep the optimistic bubble.
            if (err?.status === 409) return true;
            this.error.set(this.sanitizeError(err?.error?.detail || err?.message));
            return false;
        }
    }

    /** Parse and dispatch slash commands. Returns true if handled. */
    private handleSlashCommand(input: string): boolean {
        const parts = input.split(/\s+/);
        const cmd = parts[0].toLowerCase();
        const arg = parts.slice(1).join(' ');

        switch (cmd) {
            case '/compact':
                // No local "Compacting context..." echo: the agent's
                // compaction.started/progress frames drive the live progress
                // block, and a no-op answers with a summary-less
                // context.compacted (rendered as a system line below).
                this._sendControl({method: 'compact', focus: arg});
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
            case '/upgrade-workspace': {
                // Lite (virtual) -> sandbox|vm upgrade: provisions a real
                // workspace, seeds it from the live object-store prefix, and
                // hot-swaps in place so shell/git/file tools become available
                // without dropping the conversation (workspace_tier_upgrade.md
                // §4.2 S3 / Phase 2). `/upgrade-workspace vm` is the explicit
                // human-intent trigger for the privileged tier — the server
                // still gates it (can_use_vm + global kill-switch); sandbox is
                // the default.
                const tier = arg.trim().toLowerCase() === 'vm' ? 'vm' : 'sandbox';
                this._sendControl({method: 'upgrade-to-workspace', target_tier: tier});
                this._systemMessage(
                    tier === 'vm'
                        ? 'Provisioning a VM workspace (requires approval), please wait...'
                        : 'Provisioning workspace, please wait...',
                );
                return true;
            }
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
        this._resolvePermission(pending, 'approve');
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
        this._resolvePermission(pending, 'deny');
    }

    private _resolvePermission(
        pending: PermissionRequest | null,
        decision: 'approve' | 'deny',
    ): void {
        const threadId = this.threadId();
        if (threadId && pending?.approvalId) {
            const url =
                `${environment.apiUrl}/persistent/threads/${threadId}` +
                `/approve/${pending.approvalId}`;
            this.http.post(url, {decision}).subscribe({
                error: (err: unknown) => {
                    const status = (err as {status?: number})?.status;
                    if (status === 409) {
                        // Already decided — a stale card from SSE replay or a
                        // double-click (session_silent_failure_audit.md #10).
                        // The permission.resolved event reconciles the card;
                        // just tell the user instead of re-sending over WS.
                        this._systemMessage('This permission request was already decided.');
                        return;
                    }
                    this._sendControl({method: decision, approval_id: pending.approvalId});
                },
            });
            return;
        }
        this._sendControl({method: decision});
    }

    /** Interrupt the current turn — REST POST. */
    async interrupt(): Promise<void> {
        if (this.isInterrupting()) return;
        const tid = this.threadId();
        if (!tid) return;
        this.isInterrupting.set(true);
        try {
            await firstValueFrom(
                this.http.post(`${environment.apiUrl}/persistent/threads/${tid}/interrupt`, {})
            );
        } catch (err: any) {
            // Interrupt failures are rare and the SSE will surface the next
            // turn boundary regardless — log and reset the flag.
            this.isInterrupting.set(false);
            this._clearInterruptFallback();
            console.warn('[persistent-chat] interrupt failed:', err);
            return;
        }
        // On success the agent emits `interrupt.ack` / `turn.completed` over
        // SSE and the handlers reset isInterrupting. Arm a fallback in case
        // that frame never arrives (stalled stream): force a reconnect so the
        // durable turn boundary replays from cursor and clears "Stopping…".
        this._armInterruptFallback();
    }

    /** Arm the one-shot stuck-"Stopping…" fallback (see interrupt()). */
    private _armInterruptFallback(): void {
        this._clearInterruptFallback();
        this.interruptFallbackTimer = setTimeout(() => {
            this.interruptFallbackTimer = null;
            if (this.isInterrupting()) {
                console.warn(
                    '[persistent-chat] interrupt ack not seen within ' +
                    `${INTERRUPT_ACK_TIMEOUT_MS}ms — forcing reconnect to re-sync`
                );
                this.reconnectNow();
            }
        }, INTERRUPT_ACK_TIMEOUT_MS);
    }

    /** Cancel the stuck-"Stopping…" fallback timer if armed. */
    private _clearInterruptFallback(): void {
        if (this.interruptFallbackTimer) {
            clearTimeout(this.interruptFallbackTimer);
            this.interruptFallbackTimer = null;
        }
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

        // Agent-liveness tracking (session_silent_failure_audit.md #8):
        // "Connected" only proves the orchestrator SSE is up. Every frame
        // reaching this dispatcher is agent-origin (orchestrator pings and
        // ws.ping never get here), so its age is a fair proxy for "is the
        // agent producing anything".
        this.agentLastEventAt = now;

        switch (data.method) {
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
                // Running-command snapshot: only act when the key is present so
                // a metadata-only session.state from another channel can't clobber it.
                if ('running_tool' in params) {
                    const rt = params['running_tool'] as Partial<RunningToolInfo> | null;
                    this.runningTool.set(
                        rt && rt.tool ? {id: rt.id ?? '', tool: rt.tool, args: rt.args ?? {}} : null,
                    );
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
                    messageId: (params['message_id'] as string) || undefined,
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
                if (this.runningTool()?.id === ((params['id'] as string) || '')) {
                    this.runningTool.set(null);
                }
                break;

            case 'permission.request': {
                const id = (params['id'] as string) || '';
                const tool = (params['tool'] as string) || '';
                const args = (params['args'] as Record<string, unknown>) || {};
                const approvalId = (params['approval_id'] as string) || undefined;
                this.pendingPermission.set({
                    id,
                    ...(approvalId ? {approvalId} : {}),
                    tool,
                    args,
                });
                this.dispatch({
                    type: 'permission_request',
                    toolUseId: id,
                    tool,
                    args,
                    timestamp: now,
                });
                break;
            }

            case 'permission.resolved': {
                // SSE replay re-delivers permission.request frames; without
                // this matching outcome event a reloading client resurrected
                // an already-decided approval card, whose re-click then
                // 409'd (session_silent_failure_audit.md #10).
                const resolvedId = (params['id'] as string) || '';
                const decision =
                    params['decision'] === 'approved' ? 'approved' : 'denied';
                if (this.pendingPermission()?.id === resolvedId) {
                    this.pendingPermission.set(null);
                }
                if (resolvedId) {
                    this.dispatch({
                        type: 'permission_decision',
                        toolUseId: resolvedId,
                        decision,
                        timestamp: now,
                    });
                }
                break;
            }

            case 'turn.completed': {
                const turnId = String(params['turn_id'] ?? this.conversation().activeAssistantTurnId ?? '');
                if (turnId) {
                    this.dispatch({type: 'turn_completed', turnId, finishedAt: now});
                }
                this.isInterrupting.set(false);
                this.runningTool.set(null);
                // A compaction never outlives its turn — clear a stale block
                // (e.g. the pod died mid-fold and the turn was closed).
                this.compaction.set(null);
                break;
            }

            case 'turn.error': {
                // A failed turn used to leave the assistant bubble spinning
                // forever (no turn.completed on the error path) with only the
                // transient banner as a signal. Close the turn and append a
                // durable line — the matching role='error' history row keeps
                // it across reloads (session_silent_failure_audit.md #2).
                this._closeActiveTurnIfAny('turn_interrupted');
                this.isInterrupting.set(false);
                this.runningTool.set(null);
                this.compaction.set(null);
                this._systemMessage(`⚠ ${this.sanitizeError(params['message'] as string)}`);
                break;
            }

            case 'interrupt.ack':
                this._closeActiveTurnIfAny('turn_interrupted');
                this.isInterrupting.set(false);
                this.runningTool.set(null);
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

            case 'usage.updated': {
                const prev = this.usage();
                const turn = (params['turn'] as number) ?? null;
                const sameTurn = prev !== null && prev.turn === turn;
                this.usage.set({
                    turn,
                    // Latest call's prompt size ≈ current context fill
                    inputTokens: (params['input_tokens'] as number) ?? prev?.inputTokens ?? null,
                    outputTokensTurn:
                        (sameTurn ? prev.outputTokensTurn : 0) +
                        ((params['output_tokens'] as number) ?? 0),
                    reasoningTokensTurn:
                        (sameTurn ? prev.reasoningTokensTurn : 0) +
                        ((params['reasoning_tokens'] as number) ?? 0),
                    ctxLimitTokens:
                        (params['ctx_limit_tokens'] as number) ?? prev?.ctxLimitTokens ?? null,
                });
                break;
            }

            case 'compaction.started': {
                this.compaction.set({
                    trigger: (params['trigger'] as string) ?? 'auto',
                    totalTokens: (params['total_tokens'] as number) ?? null,
                    ctxUsedTokens: (params['ctx_used_tokens'] as number) ?? null,
                    ctxLimitTokens: (params['ctx_limit_tokens'] as number) ?? null,
                    ctxUsedPct: (params['ctx_used_pct'] as number) ?? null,
                    auxLimitTokens: (params['aux_limit_tokens'] as number) ?? null,
                    nPasses: (params['n_passes'] as number) ?? 1,
                    currentPass: 0,
                    firstMsg: null,
                    lastMsg: null,
                    inTokens: null,
                    outTokens: null,
                    attempt: 1,
                    stage: 'summarizing',
                    startedAt: now,
                });
                break;
            }

            case 'compaction.progress': {
                // Replay tolerance: a reload mid-compaction can deliver
                // progress without its started frame — synthesize minimal
                // state instead of dropping the update.
                const prev = this.compaction();
                this.compaction.set({
                    trigger: (params['trigger'] as string) ?? prev?.trigger ?? 'auto',
                    totalTokens: prev?.totalTokens ?? null,
                    ctxUsedTokens: prev?.ctxUsedTokens ?? null,
                    ctxLimitTokens: prev?.ctxLimitTokens ?? null,
                    ctxUsedPct: prev?.ctxUsedPct ?? null,
                    auxLimitTokens: prev?.auxLimitTokens ?? null,
                    nPasses: (params['n_passes'] as number) ?? prev?.nPasses ?? 1,
                    currentPass: (params['pass'] as number) ?? prev?.currentPass ?? 1,
                    firstMsg: (params['first_msg'] as number) ?? null,
                    lastMsg: (params['last_msg'] as number) ?? null,
                    inTokens: (params['in_tokens'] as number) ?? null,
                    outTokens: (params['out_tokens'] as number) ?? null,
                    attempt: (params['attempt'] as number) ?? 1,
                    stage: (params['stage'] as string) ?? 'summarizing',
                    startedAt: prev?.startedAt ?? now,
                });
                break;
            }

            case 'compaction.skipped': {
                // Engine ran but the result wasn't worth adopting (e.g. the
                // summary came out larger than the folded messages). Journaled
                // terminal frame — clears the progress block, including on
                // SSE replay. Manual /compact additionally gets its own
                // summary-less context.compacted for the system line.
                this.compaction.set(null);
                break;
            }

            case 'compaction.failed': {
                this.compaction.set(null);
                const reason = (params['reason'] as string) ?? 'unknown';
                // History is intact by contract (the engine aborts rather than
                // compacting behind a placeholder) — say so explicitly.
                this._systemMessage(
                    `⚠ Context compaction failed (${reason}) — conversation kept intact, will retry later.`,
                );
                break;
            }

            case 'context.compacted': {
                // Compaction finished — clear the live progress block.
                this.compaction.set(null);
                const summary = (params['summary'] as string | null) ?? '';
                if (!summary) {
                    // Manual /compact no-op: nothing was folded and no banner
                    // row was persisted — a transient system line, not a banner.
                    this._systemMessage('Nothing to compact — context is within limits.');
                    break;
                }
                // Show a compaction banner. Stable id (compaction-<turn>) keeps
                // SSE replay idempotent; the reducer replaces rather than dupes.
                const turn = params['turn'];
                const compactionId =
                    turn != null ? `compaction-${turn}` : makeLocalId('compaction');
                this.dispatch({
                    type: 'add_compaction',
                    id: compactionId,
                    summary,
                    timestamp: Date.now(),
                });
                break;
            }

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

            case 'session.suspended':
                // Drift-drain (platform update) suspend. Unlike 'ended', a
                // suspended thread stays live-resumable: the next message
                // restores the workspace on a fresh agent, so keep the
                // composer enabled and don't render the resume card.
                this._systemMessage(
                    (params['message'] as string)
                    || 'Session suspended. Send a message to resume where you left off.',
                );
                this.isWaitingForInput.set(false);
                this.threadStatus.set('suspended');
                break;

            case 'vm_upgrade.needed': {
                // A sandbox session hit a sudo command (vm_upgrade_required
                // freeze). The accept is the SAME /upgrade-workspace command with
                // the `vm` arg: it routes through the unified upgrade handler,
                // which seeds the VM from the sandbox, opens the sudo gate, and
                // persists the tier (workspace_tier_upgrade.md Q8). The old banner
                // pointed at a nonexistent "upgrade button" / `/upgrade` command.
                const cmd = (params['command'] as string) || '';
                const cmdNote = cmd ? ` (\`${cmd}\` needs root)` : '';
                this._systemMessage(
                    `VM upgrade needed: ${(params['reason'] as string) || 'sudo detected'}${cmdNote}. `
                    + `Send /upgrade-workspace vm to move this session onto a VM with sudo `
                    + `(your files carry over).`,
                );
                break;
            }

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

            case 'workspace_upgrade.needed': {
                // The agent called request_workspace_upgrade — offer the upgrade
                // (HITL: a human accepts before anything provisions). The minimal
                // accept path is the /upgrade-workspace slash command, which sends
                // the same upgrade-to-workspace control message. Honor the offered
                // tier (`vm` would need `/upgrade-workspace vm`); the tool only
                // requests `sandbox` today.
                const tier = (params['target_tier'] as string) || 'sandbox';
                const accept = tier === 'vm' ? '/upgrade-workspace vm' : '/upgrade-workspace';
                this._systemMessage(
                    `The agent requested a real workspace: `
                    + `${(params['reason'] as string) || 'shell/git tools needed'}. `
                    + `Send ${accept} to provision a ${tier} `
                    + `(your files carry over).`,
                );
                break;
            }

            case 'workspace_upgrade.started':
                this._systemMessage('Provisioning workspace, please wait...');
                break;

            case 'workspace_upgrade.progress': {
                // Heartbeat during a slow (cold) VM provision so a multi-minute
                // wait isn't a silent black box (workspace_tier_upgrade.md Q7).
                // The agent emits this ~once a minute while polling readiness.
                const elapsed = params['elapsed_s'] as number | undefined;
                const tier = (params['target_tier'] as string) || 'workspace';
                this._systemMessage(
                    typeof elapsed === 'number'
                        ? `Still provisioning the ${tier} workspace (${elapsed}s elapsed)…`
                        : `Still provisioning the ${tier} workspace…`,
                );
                break;
            }

            case 'workspace_upgrade.complete': {
                const seeded = params['seeded_files'] as number | undefined;
                const seededNote = typeof seeded === 'number'
                    ? ` ${seeded} file(s) carried over.`
                    : '';
                const tier = (params['target_tier'] as string) || '';
                const sudoNote = tier === 'vm'
                    ? ' Running on a VM — sudo is now available.'
                    : '';
                this._systemMessage(
                    `Workspace ready — shell, git, and file tools are now available.`
                    + `${sudoNote}${seededNote}`,
                );
                break;
            }

            case 'workspace_upgrade.failed':
                this._systemMessage(
                    `Workspace upgrade failed: ${(params['reason'] as string) || 'unknown error'}`,
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

            case 'workspace_sync.error': {
                const op = (params['op'] as string) || 'sync';
                // The initial cloud->workspace seed failed (degraded:true,
                // op:'initial_pull'): sync is OFF for the whole session — the
                // workspace may be missing files from the cloud and edits will
                // NOT be saved back. That is worse than a per-turn push/pull
                // retry, so surface it as a sticky danger toast + a session-long
                // flag instead of a misleading "will retry next turn" note.
                if (params['degraded'] === true || op === 'initial_pull') {
                    this.cloudSyncDegraded.set(true);
                    this.toast.danger(
                        `Cloud sync could not start for this session. The workspace may be missing files from the cloud, and changes won't be saved back to it. ${this.sanitizeError(params['message'] as string)}`,
                        {duration: 0},
                    );
                } else {
                    const turn = params['turn_id'] as number | undefined;
                    const turnLabel = turn != null ? ` on turn ${turn}` : '';
                    this.toast.warning(
                        `Workspace sync (${op}) failed${turnLabel}. Your changes are in the workspace but not yet saved to the cloud. Will retry on next turn.`,
                    );
                }
                break;
            }

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

        // Clear any transient error left over from the WS reconnect storm
        // during session attach: when the orchestrator polls /ready faster
        // than the agent finishes attaching its session, the agent rejects
        // each WS with an "Agent not ready" frame (persistent_app.py:1489)
        // until attach completes. Those errors are stale the moment we get
        // session.state — keep them on screen and the user sees a red
        // banner contradicting a healthy session.
        this.error.set(null);

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
/**
 * Merge two message lists by `id` (server rows win on conflict), sorted in
 * display order (created_at, then turn_number, then id). Combines the
 * IndexedDB cache with an `?after=` incremental fetch without depending on a
 * cache read-back, so it's correct even when IndexedDB is unavailable.
 */
function mergeMessagesById(a: HistoryMessage[], b: HistoryMessage[]): HistoryMessage[] {
    const byId = new Map<string, HistoryMessage>();
    for (const m of a) byId.set(m.id, m);
    for (const m of b) byId.set(m.id, m);
    return Array.from(byId.values()).sort((x, y) => {
        const c = (x.created_at ?? '').localeCompare(y.created_at ?? '');
        if (c !== 0) return c;
        const t = (x.turn_number ?? 0) - (y.turn_number ?? 0);
        if (t !== 0) return t;
        return x.id.localeCompare(y.id);
    });
}

// Synthetic image-delivery messages ("Image content from tool call <id>:")
// hand a tool's screenshot/page image to a multimodal model
// (src/services/image_content.py + src/persistent_graph.py). The base64 is
// dropped at persist, leaving a bare marker that would otherwise render as a
// user bubble — hide it from the transcript entirely.
const SYNTHETIC_IMAGE_DELIVERY_RE = /^Image content from tool call \S+:\s*$/;

export function historyToTurns(messages: HistoryMessage[]): Turn[] {
    const turns: Turn[] = [];
    const turnByNumber = new Map<number, AssistantTurn>();
    const toolCallById = new Map<string, ToolCallEvent>();
    let lastCompactionSummary: string | null = null;

    for (const m of messages) {
        const isUser = ['human', 'user', 'HumanMessageChunk'].includes(m.role);
        const isAssistant = ['ai', 'assistant', 'AIMessageChunk'].includes(m.role);
        const isTool = m.role === 'tool' || m.role === 'ToolMessageChunk';

        const ts = m.created_at ? Date.parse(m.created_at) || Date.now() : Date.now();

        // Compaction boundary marker (role='summary'). Consecutive identical
        // summaries collapse to one marker (threads written before the
        // run-counter gate carry duplicate rows — the duplicate-banner bug).
        // A marker whose turn is already open renders as an inline event at
        // its true position in the event stream; the turn block anchors at
        // its first row, so a top-level divider would otherwise trail the
        // whole turn's content. Markers between turns stay top-level dividers.
        if (m.role === 'summary') {
            const summaryText = m.content ?? '';
            if (summaryText && summaryText === lastCompactionSummary) continue;
            if (summaryText) lastCompactionSummary = summaryText;
            const owner =
                m.turn_number != null ? turnByNumber.get(m.turn_number) : undefined;
            if (owner) {
                owner.events.push({
                    kind: 'compaction',
                    id: m.id,
                    summary: summaryText,
                    startedAt: ts,
                });
            } else {
                turns.push({
                    kind: 'compaction',
                    id: m.id,
                    summary: summaryText,
                    timestamp: ts,
                });
            }
            continue;
        }

        // Persisted turn failure (role='error') → muted system line, same
        // treatment the live turn.error frame gets
        // (session_silent_failure_audit.md #2).
        if (m.role === 'error') {
            turns.push({
                kind: 'system',
                id: m.id,
                content: `⚠ ${m.content || 'The turn failed.'}`,
                timestamp: ts,
            });
            continue;
        }

        if (!isUser && !isAssistant && !isTool) continue;

        if (isUser) {
            if (SYNTHETIC_IMAGE_DELIVERY_RE.test(m.content || '')) continue;
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
                // The individual AI row id (a turn may group several), keyed so
                // a reasoning frame replayed over this rendered bubble dedupes.
                messageId: m.id,
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
