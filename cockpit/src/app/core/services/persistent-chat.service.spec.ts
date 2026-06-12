import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {NgZone, signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {PersistentChatService} from './persistent-chat.service';
import {ApiService} from './api.service';
import {IndexedDbService} from './indexed-db.service';
import {NotificationService} from './notification.service';
import {AppToastService} from '../../ui/toast';
import {
    AssistantTurn,
    isAssistantTurn,
    isSystemTurn,
    isToolCall,
    isUserTurn,
    TextEvent,
    ToolCallEvent,
    UserTurn,
} from '../models/turn.model';

// ---------------------------------------------------------------------------
// Test scaffolding
// ---------------------------------------------------------------------------

/**
 * EventSource mock — captures the URL, exposes hooks for triggering open,
 * message, gone_beyond_horizon, and error events, and tracks readyState
 * transitions so tests can exercise the transient-vs-terminal error split.
 */
interface MockEventSource {
    url: string;
    readyState: number;
    close: ReturnType<typeof vi.fn>;
    onopen: ((e: any) => void) | null;
    onmessage: ((e: MessageEvent) => void) | null;
    onerror: ((e: any) => void) | null;
    listeners: Record<string, ((e: any) => void)[]>;
}

function createMockEventSource(): MockEventSource {
    return {
        url: '',
        readyState: 0, // CONNECTING
        close: vi.fn(),
        onopen: null,
        onmessage: null,
        onerror: null,
        listeners: {},
    };
}

function fireSseOpen(es: MockEventSource): void {
    es.readyState = 1; // OPEN
    es.onopen?.({});
}

function fireSseMessage(es: MockEventSource, frame: Record<string, unknown>, lastEventId = ''): void {
    es.onmessage?.({
        data: JSON.stringify(frame),
        lastEventId,
    } as MessageEvent);
}

function fireSseNamedEvent(es: MockEventSource, name: string, frame: Record<string, unknown>, lastEventId = ''): void {
    const handlers = es.listeners[name] || [];
    handlers.forEach((h) =>
        h({data: JSON.stringify(frame), lastEventId} as MessageEvent),
    );
}

function fireSseTransientError(es: MockEventSource): void {
    // Browser is retrying — readyState moves back to CONNECTING.
    es.readyState = 0;
    es.onerror?.({});
}

function fireSseTerminalError(es: MockEventSource): void {
    es.readyState = 2; // CLOSED
    es.onerror?.({});
}

/**
 * WebSocket mock for the control plane.
 */
function createMockWs() {
    const ws: any = {
        readyState: 1, // OPEN
        send: vi.fn(),
        close: vi.fn(),
        onopen: null,
        onmessage: null,
        onclose: null,
        onerror: null,
        addEventListener: vi.fn((event: string, cb: any) => {
            if (event === 'open') ws._openCb = cb;
        }),
        removeEventListener: vi.fn(),
    };
    return ws;
}

/**
 * Build the service in a minimal injection context. Returns the spies the
 * tests assert against, plus a hook to capture every constructed
 * EventSource so handlers can be driven by tests.
 */
function createService(opts: {
    cursor?: {epoch: number; seq: number} | null;
} = {}) {
    const mockHttp: any = {
        get: vi.fn().mockReturnValue(of({messages: [], total: 0})),
        post: vi.fn().mockReturnValue(of({})),
        delete: vi.fn().mockReturnValue(of({})),
    };

    const mockApi: any = {
        uploadToThread: vi.fn().mockReturnValue(of({thread_id: 't', files: []})),
        humanizeUploadError: vi.fn().mockReturnValue('upload failed'),
    };

    const mockCache: any = {
        getThreadCursor: vi.fn().mockResolvedValue(
            opts.cursor ?? null,
        ),
        setThreadCursor: vi.fn().mockResolvedValue(undefined),
        deleteThreadCursor: vi.fn().mockResolvedValue(undefined),
        getThreadMessages: vi.fn().mockResolvedValue([]),
        getNewestCachedCreatedAt: vi.fn().mockResolvedValue(null),
        upsertThreadMessages: vi.fn().mockResolvedValue(undefined),
        clearThreadMessages: vi.fn().mockResolvedValue(undefined),
    };

    // NgZone stub: just run callbacks synchronously. Tests don't depend on
    // change-detection scheduling, only on signal mutations being observed.
    // NOTE: we don't provide this as the NgZone token because Angular's
    // ChangeDetectionScheduler subscribes to NgZone's onStable/onMicrotaskEmpty
    // streams. TestBed's default NgZoneNoop satisfies that contract; this stub
    // is unused in the TestBed configuration but kept for reference.
    const mockZone: any = {
        run: <T>(fn: () => T) => fn(),
    };
    void mockZone;

    const mockToast: any = {
        show: vi.fn(),
        info: vi.fn(),
        success: vi.fn(),
        warning: vi.fn(),
        danger: vi.fn(),
        dismiss: vi.fn(),
        dismissAll: vi.fn(),
    };

    const sseInstances: MockEventSource[] = [];

    function MockEventSourceCtor(this: any, url: string, _init?: EventSourceInit) {
        const es = createMockEventSource();
        es.url = url;
        // Patch addEventListener so the service's gone_beyond_horizon
        // subscription becomes a fireable handler in tests.
        (es as any).addEventListener = (name: string, cb: (e: any) => void) => {
            (es.listeners[name] ||= []).push(cb);
        };
        sseInstances.push(es);
        return es as any;
    }
    (MockEventSourceCtor as any).CONNECTING = 0;
    (MockEventSourceCtor as any).OPEN = 1;
    (MockEventSourceCtor as any).CLOSED = 2;
    (globalThis as any).EventSource = MockEventSourceCtor;

    const wsInstances: any[] = [];
    function MockWebSocketCtor(this: any, url: string) {
        const ws = createMockWs();
        ws.url = url;
        wsInstances.push(ws);
        return ws as any;
    }
    (MockWebSocketCtor as any).OPEN = 1;
    (MockWebSocketCtor as any).CONNECTING = 0;
    (globalThis as any).WebSocket = MockWebSocketCtor;

    // Minimal NotificationService stub — just the lifecycleEvent signal
    // the PersistentChatService constructor effect reads. Tests fire phase
    // transitions by setting this signal directly.
    const mockNotifications: any = {
        lifecycleEvent: signal<{thread_id: string; state: string; reason?: string} | null>(null),
    };

    // TestBed gives us the ChangeDetectionScheduler that effect() needs.
    // Manual Injector.create() doesn't wire that up. We let TestBed use
    // its default NgZone — the scheduler subscribes to its lifecycle
    // streams, and a thin stub would break that subscription.
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
        providers: [
            {provide: HttpClient, useValue: mockHttp},
            {provide: ApiService, useValue: mockApi},
            {provide: IndexedDbService, useValue: mockCache},
            {provide: AppToastService, useValue: mockToast},
            {provide: NotificationService, useValue: mockNotifications},
            PersistentChatService,
        ],
    });
    const service = TestBed.inject(PersistentChatService);
    return {
        service,
        mockHttp,
        mockApi,
        mockCache,
        sseInstances,
        wsInstances,
        notifications: mockNotifications,
    };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PersistentChatService — initial state', () => {
    it('starts disconnected with default signals', () => {
        const {service} = createService();
        expect(service.connectionState()).toBe('disconnected');
        expect(service.isConnected()).toBe(false);
        expect(service.threadId()).toBeNull();
        expect(service.turns()).toEqual([]);
        expect(service.currentStreamingTurn()).toBeNull();
        expect(service.isStreaming()).toBe(false);
        expect(service.historyLoaded()).toBe(false);
        expect(service.permissionMode()).toBe('supervised');
        expect(service.narrationMode()).toBe('auto');
        expect(service.reconnectAttempt()).toBe(0);
        expect(service.reconnectGaveUp()).toBe(false);
        expect(service.cloudSyncDegraded()).toBe(false);
    });
});

describe('PersistentChatService — render windowing', () => {
    function makeTurns(n: number) {
        return Array.from({length: n}, (_, i) => ({
            kind: 'user' as const,
            id: `u${i}`,
            content: `m${i}`,
            timestamp: i,
        }));
    }

    function seed(service: PersistentChatService, n: number): void {
        service.conversation.set({
            threadId: 't-window',
            turns: makeTurns(n),
            activeAssistantTurnId: null,
        });
    }

    it('renders all turns when under the window size', () => {
        const {service} = createService();
        seed(service, 30);
        expect(service.visibleTurns().length).toBe(30);
        expect(service.hasOlderTurns()).toBe(false);
    });

    it('renders only the most recent window when over the size', () => {
        const {service} = createService();
        seed(service, 120);
        const visible = service.visibleTurns();
        expect(visible.length).toBe(50);
        expect(visible[0].id).toBe('u70'); // slice(-50) of 120
        expect(visible[49].id).toBe('u119');
        expect(service.hasOlderTurns()).toBe(true);
    });

    it('loadOlderTurns widens the window by the step, capped at length', () => {
        const {service} = createService();
        seed(service, 120);
        service.loadOlderTurns();
        expect(service.visibleTurns().length).toBe(100);
        expect(service.hasOlderTurns()).toBe(true);
        service.loadOlderTurns(); // 150 → capped at 120
        expect(service.visibleTurns().length).toBe(120);
        expect(service.hasOlderTurns()).toBe(false);
    });

    it('growWindow anchors the visible top by the delta', () => {
        const {service} = createService();
        seed(service, 120);
        const topBefore = service.visibleTurns()[0].id; // u70
        seed(service, 123); // 3 more turns present
        service.growWindow(3);
        const visible = service.visibleTurns();
        expect(visible.length).toBe(53);
        expect(visible[0].id).toBe(topBefore); // visible top unchanged
    });

    it('resetWindow re-bounds to the default window', () => {
        const {service} = createService();
        seed(service, 120);
        service.loadOlderTurns();
        expect(service.visibleTurns().length).toBe(100);
        service.resetWindow();
        expect(service.visibleTurns().length).toBe(50);
    });
});

describe('PersistentChatService — message cache (loadHistory)', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('full-loads when nothing is cached (no ?after=) and caches the result', async () => {
        const {service, mockHttp, mockCache} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.includes('/messages')) {
                return of({
                    messages: [
                        {id: 'm1', role: 'human', content: 'hi', tool_calls: null, turn_number: 1, created_at: '2026-05-15T08:00:00Z'},
                    ],
                    total: 1,
                });
            }
            return of({status: 'active', total_turns: 1});
        });

        await service.connect('thread-nocache');

        const msgUrls = mockHttp.get.mock.calls
            .map((c: any) => c[0] as string)
            .filter((u: string) => u.includes('/messages'));
        expect(msgUrls.length).toBeGreaterThan(0);
        expect(msgUrls.every((u: string) => !u.includes('after='))).toBe(true);
        expect(mockCache.upsertThreadMessages).toHaveBeenCalled(); // cached for next time
        expect(service.turns().length).toBe(1);
    });

    it('paints cached history first, then refreshes incrementally via ?after=', async () => {
        const {service, mockHttp, mockCache} = createService();
        mockCache.getThreadMessages.mockResolvedValue([
            {id: 'm1', threadId: 'thread-cache', role: 'human', content: 'old', tool_calls: null, turn_number: 1, created_at: '2026-05-15T08:00:00Z'},
        ]);
        mockHttp.get.mockImplementation((url: string) => {
            if (url.includes('/messages')) {
                // Server returns one NEW message after the cached cursor.
                return of({
                    messages: [
                        {id: 'm2', role: 'ai', content: 'new reply', tool_calls: null, turn_number: 2, created_at: '2026-05-15T08:01:00Z'},
                    ],
                    total: 2,
                });
            }
            return of({status: 'active', total_turns: 2});
        });

        await service.connect('thread-cache');

        const msgUrls = mockHttp.get.mock.calls
            .map((c: any) => c[0] as string)
            .filter((u: string) => u.includes('/messages'));
        expect(msgUrls.some((u: string) => u.includes('after='))).toBe(true);
        expect(mockCache.upsertThreadMessages).toHaveBeenCalled();
        // Merged cached + fetched: user (m1) then assistant (m2).
        const turns = service.turns();
        expect(turns.length).toBe(2);
        expect(turns[0].kind).toBe('user');
        expect(turns[1].kind).toBe('assistant');
    });

    it('renders a role="summary" history row as a compaction banner', async () => {
        const {service, mockHttp} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.includes('/messages')) {
                return of({
                    messages: [
                        {id: 'u1', role: 'human', content: 'hi', tool_calls: null, turn_number: 1, created_at: '2026-05-15T08:00:00Z'},
                        {id: 's1', role: 'summary', content: 'We discussed X and Y.', tool_calls: null, turn_number: 2, created_at: '2026-05-15T08:01:00Z'},
                    ],
                    total: 2,
                });
            }
            return of({status: 'active', total_turns: 2});
        });

        await service.connect('thread-summary');

        const banner = service.turns().find((t: {kind: string}) => t.kind === 'compaction');
        expect(banner).toBeTruthy();
        expect((banner as {summary: string}).summary).toBe('We discussed X and Y.');
    });

    it('renders a mid-turn summary row as an inline event at its position', async () => {
        // The assistant turn anchors at its first row; a summary row that
        // falls inside the turn must render IN the event stream, not as a
        // top-level divider trailing the whole turn's content.
        const {service, mockHttp} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.includes('/messages')) {
                return of({
                    messages: [
                        {id: 'u1', role: 'human', content: 'go', tool_calls: null, turn_number: 5, created_at: '2026-05-15T08:00:00Z'},
                        {id: 'a1', role: 'ai', content: 'working on it', tool_calls: null, turn_number: 5, created_at: '2026-05-15T08:00:10Z'},
                        {id: 's1', role: 'summary', content: 'recap text', tool_calls: null, turn_number: 5, created_at: '2026-05-15T08:00:20Z'},
                        {id: 'a2', role: 'ai', content: 'final answer', tool_calls: null, turn_number: 5, created_at: '2026-05-15T08:00:30Z'},
                    ],
                    total: 4,
                });
            }
            return of({status: 'active', total_turns: 5});
        });

        await service.connect('thread-midturn-summary');

        const turns = service.turns();
        expect(turns.some((t: {kind: string}) => t.kind === 'compaction')).toBe(false);
        const assistant = turns.find((t: {kind: string}) => t.kind === 'assistant') as any;
        expect(assistant).toBeTruthy();
        const kinds = assistant.events.map((e: {kind: string}) => e.kind);
        expect(kinds).toEqual(['text', 'compaction', 'text']);
        expect(assistant.events[1].summary).toBe('recap text');
    });

    it('collapses consecutive duplicate summary rows into one banner', async () => {
        // Threads written before the run-counter gate carry repeated
        // role='summary' rows with identical content (duplicate-banner bug).
        const {service, mockHttp} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.includes('/messages')) {
                return of({
                    messages: [
                        {id: 'u1', role: 'human', content: 'hi', tool_calls: null, turn_number: 1, created_at: '2026-05-15T08:00:00Z'},
                        {id: 's1', role: 'summary', content: 'same recap', tool_calls: null, turn_number: 2, created_at: '2026-05-15T08:01:00Z'},
                        {id: 's2', role: 'summary', content: 'same recap', tool_calls: null, turn_number: 2, created_at: '2026-05-15T08:01:30Z'},
                        {id: 's3', role: 'summary', content: 'same recap', tool_calls: null, turn_number: 2, created_at: '2026-05-15T08:02:00Z'},
                    ],
                    total: 4,
                });
            }
            return of({status: 'active', total_turns: 2});
        });

        await service.connect('thread-dup-summaries');

        const banners = service.turns().filter((t: {kind: string}) => t.kind === 'compaction');
        expect(banners.length).toBe(1);
    });
});

describe('PersistentChatService — connect()', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('loads transcript history + thread meta via REST before opening SSE', async () => {
        const {service, mockHttp, sseInstances} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.endsWith('/messages')) {
                return of({messages: [], total: 0});
            }
            return of({status: 'active', title: 'My session', total_turns: 0});
        });

        await service.connect('thread-A');

        // Both REST endpoints were called.
        const urls = mockHttp.get.mock.calls.map((c: any) => c[0]);
        expect(urls.some((u: string) => u.endsWith('/persistent/threads/thread-A/messages'))).toBe(true);
        expect(urls.some((u: string) => u.endsWith('/persistent/threads/thread-A'))).toBe(true);
        // Then SSE.
        expect(sseInstances).toHaveLength(1);
        expect(sseInstances[0].url).toContain('/persistent/threads/thread-A/stream');
        expect(service.historyLoaded()).toBe(true);
    });

    it('rehydrates thinking + tool results from history (migration 0011)', async () => {
        const {service, mockHttp} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.endsWith('/messages')) {
                return of({
                    messages: [
                        {
                            id: 'u1',
                            role: 'human',
                            content: 'Read the file',
                            tool_calls: null,
                            turn_number: 1,
                            created_at: '2026-05-15T08:00:00Z',
                        },
                        {
                            id: 'a1',
                            role: 'ai',
                            content: null,
                            tool_calls: [{name: 'read_file', args: {path: 'x'}, id: 'tc-1'}],
                            turn_number: 1,
                            thinking: 'I should read the file first.',
                            created_at: '2026-05-15T08:00:01Z',
                        },
                        {
                            id: 't1',
                            role: 'tool',
                            content: 'file contents here',
                            tool_calls: null,
                            turn_number: 1,
                            tool_call_id: 'tc-1',
                            created_at: '2026-05-15T08:00:02Z',
                        },
                        {
                            id: 'a2',
                            role: 'ai',
                            content: 'Here is what I found.',
                            tool_calls: null,
                            turn_number: 1,
                            created_at: '2026-05-15T08:00:03Z',
                        },
                    ],
                    total: 4,
                });
            }
            return of({status: 'active', total_turns: 1});
        });

        await service.connect('thread-hist');

        const turns = service.turns();
        // user turn + one collapsed assistant turn
        const assistant = turns.find(isAssistantTurn) as AssistantTurn;
        expect(assistant).toBeDefined();
        // Events arrive in order: thought, tool_call (now with result), text
        const kinds = assistant.events.map((e) => e.kind);
        expect(kinds).toEqual(['thought', 'tool_call', 'text']);
        const tool = assistant.events.find(isToolCall) as ToolCallEvent;
        expect(tool.result).toBe('file contents here');
        expect(tool.status).toBe('completed');
        expect(tool.resultStatus).toBe('ok');
        const thought = assistant.events[0] as any;
        expect(thought.content).toBe('I should read the file first.');
        // Keyed by the AI row id so a replayed reasoning frame for the same
        // message dedupes against this rendered bubble.
        expect(thought.messageId).toBe('a1');
    });

    it('dedupes a replayed thinking frame against the rendered history bubble', async () => {
        // docs/issues/persistent_chat_reasoning_after_answer_and_replay_duplication.md
        // After a cold connect paints the completed turn, the SSE replay cursor
        // can re-emit the trailing reasoning frame (gemma journals it after the
        // token run). It must not spawn a second `recovered:` thought bubble.
        const {service, mockHttp, sseInstances} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.endsWith('/messages')) {
                return of({
                    messages: [
                        {
                            id: 'a1',
                            role: 'ai',
                            content: 'The answer is 42.',
                            tool_calls: null,
                            turn_number: 1,
                            thinking: 'Let me reason about this.',
                            created_at: '2026-05-15T08:00:01Z',
                        },
                    ],
                    total: 1,
                });
            }
            return of({status: 'active', total_turns: 1});
        });

        await service.connect('thread-replay');
        fireSseOpen(sseInstances[0]);

        // Replay re-emits just the reasoning frame, keyed to the same row id.
        fireSseMessage(
            sseInstances[0],
            {method: 'thinking', params: {content: 'Let me reason about this.', message_id: 'a1'}},
            '1:1',
        );

        const assistantTurns = service.turns().filter(isAssistantTurn) as AssistantTurn[];
        expect(assistantTurns).toHaveLength(1);
        expect(assistantTurns[0].recovered).not.toBe(true);
        const thoughts = assistantTurns[0].events.filter((e) => e.kind === 'thought');
        expect(thoughts).toHaveLength(1);
    });

    it('passes the cached cursor as ?last_event_id=<epoch>:<seq> on initial open', async () => {
        const {service, mockHttp, sseInstances} = createService({
            cursor: {epoch: 7, seq: 42, threadId: 'thread-B', updatedAt: ''} as any,
        });
        mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );

        await service.connect('thread-B');

        expect(sseInstances[0].url).toContain('last_event_id=7%3A42');
    });

    it('opens SSE without ?last_event_id= when no cursor is cached', async () => {
        const {service, mockHttp, sseInstances} = createService({cursor: null});
        mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );

        await service.connect('thread-C');

        expect(sseInstances[0].url).not.toContain('last_event_id');
    });

    it('does not open SSE for ended threads — shows resume card instead', async () => {
        const {service, mockHttp, sseInstances} = createService();
        mockHttp.get.mockImplementation((url: string) => {
            if (url.endsWith('/messages')) return of({messages: [], total: 0});
            return of({status: 'ended', total_turns: 0});
        });

        await service.connect('thread-ended');

        expect(sseInstances).toHaveLength(0);
        expect(service.connectionState()).toBe('disconnected');
        expect(service.threadStatus()).toBe('ended');
    });

    it('flips connectionState to connected on SSE open', async () => {
        const {service, mockHttp, sseInstances} = createService();
        mockHttp.get.mockImplementation(() => of({status: 'active', total_turns: 0, messages: [], total: 0}));

        await service.connect('thread-D');
        expect(service.connectionState()).toBe('connecting');

        fireSseOpen(sseInstances[0]);
        expect(service.connectionState()).toBe('connected');
        expect(service.isConnected()).toBe(true);
        expect(service.error()).toBeNull();
    });
});

describe('PersistentChatService — createAndConnect()', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('clears prior session turns + threadId synchronously, before the POST resolves', async () => {
        const {service, mockHttp} = createService();

        // Seed a prior session so the bug reproduces: connect to thread-A,
        // then create a new thread. Without the synchronous reset in
        // createAndConnect(), thread-A's turns would still be visible
        // throughout the (potentially multi-second) POST + boot phase.
        mockHttp.get.mockImplementation((url: string) => {
            if (url.endsWith('/messages')) {
                return of({
                    messages: [
                        {
                            id: 'u-prev',
                            role: 'human',
                            content: 'Hello from thread A',
                            tool_calls: null,
                            turn_number: 1,
                            created_at: '2026-05-15T08:00:00Z',
                        },
                    ],
                    total: 1,
                });
            }
            return of({status: 'active', title: 'Old session', total_turns: 1});
        });
        await service.connect('thread-A');
        expect(service.turns().length).toBeGreaterThan(0);
        expect(service.threadId()).toBe('thread-A');

        // Make the POST hang so we can observe the state during the await.
        let resolvePost: (v: any) => void = () => {};
        mockHttp.post.mockReturnValue({
            subscribe(observer: any) {
                resolvePost = (v) => {
                    observer.next(v);
                    observer.complete();
                };
                return {unsubscribe: () => {}};
            },
        });

        const promise = service.createAndConnect({config_name: 'scholar'});

        // Synchronous part of createAndConnect must have already cleared
        // the prior session's content. Without the fix, turns would still
        // contain thread-A's user message.
        expect(service.turns()).toEqual([]);
        expect(service.threadId()).toBeNull();
        expect(service.isCreating()).toBe(true);
        expect(service.startupPhase()).toBe('creating');

        // Let the POST resolve so the test cleans up.
        mockHttp.get.mockImplementation((url: string) => {
            if (url.endsWith('/messages')) return of({messages: [], total: 0});
            return of({status: 'active', total_turns: 0});
        });
        resolvePost({thread_id: 'thread-new'});
        await promise;
        expect(service.threadId()).toBe('thread-new');
    });
});

describe('PersistentChatService — SSE event dispatch', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    async function setup() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-X');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);
        return {...ctx, es};
    }

    it('appends token frames into the active turn as a streaming TextEvent', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'turn.started', params: {turn_id: 1}}, '1:1');
        fireSseMessage(es, {method: 'token', params: {content: 'Hello '}}, '1:2');
        fireSseMessage(es, {method: 'token', params: {content: 'world'}}, '1:3');
        const turn = service.currentStreamingTurn();
        expect(turn).not.toBeNull();
        const text = turn!.events.find((e) => e.kind === 'text') as TextEvent;
        expect(text.content).toBe('Hello world');
        expect(text.status).toBe('streaming');
        expect(service.isStreaming()).toBe(true);
    });

    it('handles turn.completed by closing the active turn and clearing the streaming flag', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'turn.started', params: {turn_id: 1}}, '1:1');
        fireSseMessage(es, {method: 'token', params: {content: 'done'}}, '1:2');
        fireSseMessage(es, {method: 'turn.completed', params: {turn_id: 1}}, '1:3');
        expect(service.isStreaming()).toBe(false);
        expect(service.currentStreamingTurn()).toBeNull();
        const assistantTurns = service.turns().filter(isAssistantTurn);
        const last = assistantTurns[assistantTurns.length - 1] as AssistantTurn;
        expect(last.status).toBe('done');
        const text = last.events.find((e) => e.kind === 'text') as TextEvent;
        expect(text.content).toBe('done');
        expect(text.status).toBe('done');
    });

    it('sets pendingPermission on permission.request', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'permission.request',
            params: {id: 'tc-1', tool: 'run_command', args: {cmd: 'ls'}},
        }, '1:1');
        expect(service.pendingPermission()).toEqual({
            id: 'tc-1',
            tool: 'run_command',
            args: {cmd: 'ls'},
        });
    });

    it('keeps approval_id from permission.request for durable approval', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'permission.request',
            params: {
                id: 'tc-1',
                approval_id: 'approval-1',
                tool: 'run_command',
                args: {cmd: 'ls'},
            },
        }, '1:1');
        expect(service.pendingPermission()).toEqual({
            id: 'tc-1',
            approvalId: 'approval-1',
            tool: 'run_command',
            args: {cmd: 'ls'},
        });
    });

    it('promotes ready event to sessionReady=true and flushes a pending message', async () => {
        const {service, es, mockHttp} = await setup();
        // Stuff a pending message as if user typed while session wasn't ready.
        (service as any).pendingMessage.set('queued msg');

        fireSseMessage(es, {method: 'ready', params: {}}, '1:1');

        expect(service.sessionReady()).toBe(true);
        expect(service.pendingMessage()).toBeNull();
        // pendingMessage flushed via POST /input.
        const postCalls = mockHttp.post.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/persistent/threads/thread-X/input'),
        );
        expect(postCalls.length).toBeGreaterThanOrEqual(1);
    });

    it('flips threadStatus to ended on session.ended event', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'session.ended', params: {}}, '1:1');
        expect(service.threadStatus()).toBe('ended');
    });

    it('flips threadStatus to suspended (not ended) on session.suspended', async () => {
        const {service, es} = await setup();
        fireSseMessage(
            es,
            {
                method: 'session.suspended',
                params: {message: 'Session suspended for a platform update.'},
            },
            '1:1',
        );
        // Suspended threads stay live-resumable — the composer must remain
        // enabled (no 'ended' resume card) and the next send wakes the session.
        expect(service.threadStatus()).toBe('suspended');
        expect(service.isWaitingForInput()).toBe(false);
    });

    it('surfaces error frames via sanitized error signal', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'error', params: {message: 'something broke'}}, '1:1');
        expect(service.error()).toContain('something broke');
    });

    it('marks cloudSyncDegraded on a degraded workspace_sync.error (initial pull)', async () => {
        const {service, es} = await setup();
        expect(service.cloudSyncDegraded()).toBe(false);
        fireSseMessage(
            es,
            {
                method: 'workspace_sync.error',
                params: {
                    op: 'initial_pull',
                    turn_id: 0,
                    message: 'token exchange 400',
                    degraded: true,
                },
            },
            '1:1',
        );
        // Sticky, session-long: the initial cloud->workspace seed failed, so
        // sync is OFF for the whole session (not a per-turn retry).
        expect(service.cloudSyncDegraded()).toBe(true);
    });

    it('does NOT mark cloudSyncDegraded for a retryable per-turn workspace_sync.error', async () => {
        const {service, es} = await setup();
        fireSseMessage(
            es,
            {
                method: 'workspace_sync.error',
                params: {op: 'push', turn_id: 3, message: 'transient 502'},
            },
            '1:1',
        );
        // Turn-loop push/pull failures retry next turn; not a degraded session.
        expect(service.cloudSyncDegraded()).toBe(false);
    });
});

describe('PersistentChatService — cursor persistence', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('saves the cursor to IndexedDB on each event with a parseable lastEventId', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-cur');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);

        fireSseMessage(es, {method: 'token', params: {content: 'x'}}, '5:101');
        expect(ctx.mockCache.setThreadCursor).toHaveBeenCalledWith('thread-cur', 5, 101);

        fireSseMessage(es, {method: 'token', params: {content: 'y'}}, '5:102');
        expect(ctx.mockCache.setThreadCursor).toHaveBeenLastCalledWith('thread-cur', 5, 102);
    });

    it('ignores malformed lastEventId values silently', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-mal');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);

        ctx.mockCache.setThreadCursor.mockClear();
        fireSseMessage(es, {method: 'token', params: {content: 'x'}}, '');     // empty
        fireSseMessage(es, {method: 'token', params: {content: 'y'}}, 'nope'); // no colon
        fireSseMessage(es, {method: 'token', params: {content: 'z'}}, 'a:b');  // NaN
        expect(ctx.mockCache.setThreadCursor).not.toHaveBeenCalled();
    });
});

describe('PersistentChatService — gone_beyond_horizon recovery', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('drops cursor, reloads history, reopens stream', async () => {
        const ctx = createService({cursor: {epoch: 1, seq: 5, threadId: 'thread-g', updatedAt: ''} as any});
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-g');
        const firstSse = ctx.sseInstances[0];
        fireSseOpen(firstSse);

        // Now simulate gone_beyond_horizon.
        fireSseNamedEvent(firstSse, 'gone_beyond_horizon', {
            method: 'gone_beyond_horizon',
            params: {epoch: 2, server_seq: 0, reason: 'epoch_mismatch'},
        }, '2:0');

        // The handler chain awaits: deleteCursor → loadHistory → openSse →
        // getThreadCursor. setTimeout(0) lets all queued microtasks drain.
        await new Promise((r) => setTimeout(r, 0));

        expect(ctx.mockCache.deleteThreadCursor).toHaveBeenCalledWith('thread-g');
        expect(firstSse.close).toHaveBeenCalled();
        // A second SSE was opened.
        expect(ctx.sseInstances.length).toBeGreaterThanOrEqual(2);
        // History was reloaded — find an additional GET /messages call after
        // the initial one.
        const historyCalls = ctx.mockHttp.get.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/persistent/threads/thread-g/messages'),
        );
        expect(historyCalls.length).toBeGreaterThanOrEqual(2);
    });
});

describe('PersistentChatService — SSE error handling', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('bumps reconnectAttempt on transient onerror (browser retrying)', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-tr');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);

        fireSseTransientError(es);
        expect(ctx.service.reconnectAttempt()).toBe(1);
        expect(ctx.service.connectionState()).toBe('connecting');
        expect(ctx.service.reconnectGaveUp()).toBe(false);

        fireSseTransientError(es);
        expect(ctx.service.reconnectAttempt()).toBe(2);
    });

    it('sets reconnectGaveUp + error state on terminal CLOSED', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-term');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);

        fireSseTerminalError(es);
        expect(ctx.service.connectionState()).toBe('error');
        expect(ctx.service.reconnectGaveUp()).toBe(true);
    });

    it('reconnectNow() drops the existing SSE and opens a fresh one', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-rn');
        const first = ctx.sseInstances[0];
        fireSseOpen(first);
        fireSseTerminalError(first);
        expect(ctx.service.reconnectGaveUp()).toBe(true);

        ctx.service.reconnectNow();
        // _openSse awaits cache.getThreadCursor — drain microtasks.
        await new Promise((r) => setTimeout(r, 0));

        expect(first.close).toHaveBeenCalled();
        expect(ctx.sseInstances.length).toBe(2);
        expect(ctx.service.reconnectGaveUp()).toBe(false);
        expect(ctx.service.reconnectAttempt()).toBe(0);
    });
});

describe('PersistentChatService — SSE liveness watchdog', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('force-reopens the SSE when no event arrives for > 45s', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-wd');
        // Drain the _openSse microtask chain (await getThreadCursor) so the
        // first EventSource has been constructed.
        await vi.advanceTimersByTimeAsync(0);

        const first = ctx.sseInstances[0];
        fireSseOpen(first);
        expect(ctx.sseInstances.length).toBe(1);

        // 50s of silence — past the 45s watchdog threshold.
        await vi.advanceTimersByTimeAsync(50_000);

        expect(first.close).toHaveBeenCalled();
        expect(ctx.sseInstances.length).toBe(2);
        expect(ctx.service.connectionState()).toBe('connecting');
        expect(ctx.service.reconnectAttempt()).toBeGreaterThan(0);
    });

    it('treats a typed `ping` event as liveness and does not trip the watchdog', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-ping');
        await vi.advanceTimersByTimeAsync(0);

        const es = ctx.sseInstances[0];
        fireSseOpen(es);

        // 30s of silence, then a ping, then another 30s. Total 60s elapsed,
        // but only 30s since the most recent ping — under the 45s threshold.
        await vi.advanceTimersByTimeAsync(30_000);
        fireSseNamedEvent(es, 'ping', {});
        await vi.advanceTimersByTimeAsync(30_000);

        expect(es.close).not.toHaveBeenCalled();
        expect(ctx.sseInstances.length).toBe(1);
    });

    it('treats a regular onmessage frame as liveness', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-msg');
        await vi.advanceTimersByTimeAsync(0);

        const es = ctx.sseInstances[0];
        fireSseOpen(es);

        await vi.advanceTimersByTimeAsync(40_000);
        fireSseMessage(es, {method: 'thinking', params: {text: 'still here'}}, '1:1');
        await vi.advanceTimersByTimeAsync(40_000);

        expect(es.close).not.toHaveBeenCalled();
        expect(ctx.sseInstances.length).toBe(1);
    });

    it('stops the watchdog on disconnect()', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-dc');
        await vi.advanceTimersByTimeAsync(0);

        fireSseOpen(ctx.sseInstances[0]);

        ctx.service.disconnect();
        await vi.advanceTimersByTimeAsync(60_000);

        // Disconnect intentionally torn down — no auto-reopen.
        expect(ctx.sseInstances.length).toBe(1);
    });

    it('does not start a watchdog on terminal CLOSED', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-cl');
        await vi.advanceTimersByTimeAsync(0);

        const first = ctx.sseInstances[0];
        fireSseOpen(first);
        fireSseTerminalError(first);
        expect(ctx.service.reconnectGaveUp()).toBe(true);

        // After terminal close, no further reopens should happen no matter
        // how much time passes.
        await vi.advanceTimersByTimeAsync(120_000);
        expect(ctx.sseInstances.length).toBe(1);
    });
});

describe('PersistentChatService — REST sends', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    async function readySession() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-r');
        fireSseOpen(ctx.sseInstances[0]);
        // Drive the agent ready event so sendMessage POSTs immediately.
        fireSseMessage(ctx.sseInstances[0], {method: 'ready', params: {}}, '1:1');
        ctx.mockHttp.post.mockClear();
        return ctx;
    }

    it('sendMessage POSTs to /input with the user content', async () => {
        const ctx = await readySession();
        await ctx.service.sendMessage('hello');
        const calls = ctx.mockHttp.post.mock.calls;
        const inputCall = calls.find((c: any) =>
            String(c[0]).endsWith('/persistent/threads/thread-r/input'),
        );
        expect(inputCall).toBeDefined();
        expect(inputCall![1]).toEqual({content: 'hello'});
        // Local optimistic UserTurn added.
        const userTurns = ctx.service.turns().filter(isUserTurn);
        const last = userTurns[userTurns.length - 1] as UserTurn;
        expect(last.content).toBe('hello');
    });

    it('sendMessage queues content if session is not yet ready', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-q');
        fireSseOpen(ctx.sseInstances[0]);
        // No 'ready' event yet → sessionReady is false.

        await ctx.service.sendMessage('queued');
        // No POST.
        const inputCalls = ctx.mockHttp.post.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/persistent/threads/thread-q/input'),
        );
        expect(inputCalls).toHaveLength(0);
        expect(ctx.service.pendingMessage()).toBe('queued');
    });

    it('sendMessage on 409 conflict silently no-ops (server has the turn)', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockReturnValue(throwError(() => ({status: 409})));
        await ctx.service.sendMessage('dup');
        // No error surfaced — 409 is a race we accept.
        expect(ctx.service.error()).toBeNull();
    });

    it('rolls back the optimistic bubble and resolves false on a hard POST failure', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockReturnValue(
            throwError(() => ({status: 500, error: {detail: 'boom'}})),
        );
        const ok = await ctx.service.sendMessage('will fail');
        expect(ok).toBe(false);
        // The optimistic UserTurn is removed so the composer can restore the
        // draft for retry; the error banner explains why.
        const stillThere = ctx.service
            .turns()
            .some((t) => isUserTurn(t) && t.content === 'will fail');
        expect(stillThere).toBe(false);
        expect(ctx.service.error()).not.toBeNull();
    });

    it('keeps the bubble and resolves true on a 409 dup', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockReturnValue(throwError(() => ({status: 409})));
        const ok = await ctx.service.sendMessage('dup');
        expect(ok).toBe(true);
        const present = ctx.service
            .turns()
            .some((t) => isUserTurn(t) && t.content === 'dup');
        expect(present).toBe(true);
        expect(ctx.service.error()).toBeNull();
    });

    it('interrupt POSTs to /interrupt', async () => {
        const ctx = await readySession();
        await ctx.service.interrupt();
        const calls = ctx.mockHttp.post.mock.calls;
        const intCall = calls.find((c: any) =>
            String(c[0]).endsWith('/persistent/threads/thread-r/interrupt'),
        );
        expect(intCall).toBeDefined();
        expect(ctx.service.isInterrupting()).toBe(true);
    });
});

describe('PersistentChatService — resume re-validation (visibility/online)', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    async function connectOpen(threadId: string) {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect(threadId);
        await new Promise((r) => setTimeout(r, 0));
        fireSseOpen(ctx.sseInstances[0]);
        return ctx;
    }

    it('force-reopens a stale SSE when the tab regains liveness (online event)', async () => {
        const ctx = await connectOpen('thread-resume-stale');
        const first = ctx.sseInstances[0];
        // Simulate the watchdog having been frozen while the tab was
        // backgrounded: the last SSE event is now past the 45s threshold.
        (ctx.service as any).sseLastEventAt = Date.now() - 60_000;

        window.dispatchEvent(new Event('online'));
        await new Promise((r) => setTimeout(r, 0));

        expect(first.close).toHaveBeenCalled();
        expect(ctx.sseInstances.length).toBe(2);
        expect(ctx.service.connectionState()).toBe('connecting');
    });

    it('does not tear down a healthy SSE on resume', async () => {
        const ctx = await connectOpen('thread-resume-fresh');
        const first = ctx.sseInstances[0];
        // Fresh liveness — an event arrived just now.
        (ctx.service as any).sseLastEventAt = Date.now();

        window.dispatchEvent(new Event('online'));
        await new Promise((r) => setTimeout(r, 0));

        expect(first.close).not.toHaveBeenCalled();
        expect(ctx.sseInstances.length).toBe(1);
    });

    it('is a no-op when there is no active thread', async () => {
        const ctx = createService();
        // No connect() — threadId is null.
        window.dispatchEvent(new Event('online'));
        await new Promise((r) => setTimeout(r, 0));
        expect(ctx.sseInstances.length).toBe(0);
    });

    it('re-ensures the control WS when the SSE recovers (slaved liveness)', async () => {
        const ctx = await connectOpen('thread-ws-slave');
        expect(ctx.wsInstances.length).toBe(1);

        // Simulate the control WS having silently died — on a real drop the
        // readyState moves to CLOSED, but no onclose fires to reconnect it.
        (ctx.service as any).controlWs.readyState = 3; // CLOSED

        // Force an SSE reconnect; firing open on the new stream is the
        // "wasReconnecting" path, which re-ensures the control WS.
        ctx.service.reconnectNow();
        await new Promise((r) => setTimeout(r, 0));
        fireSseOpen(ctx.sseInstances[1]);
        await new Promise((r) => setTimeout(r, 0));

        expect(ctx.wsInstances.length).toBe(2);
    });
});

describe('PersistentChatService — control WS (slash commands + permissions)', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    async function readySession() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-c');
        fireSseOpen(ctx.sseInstances[0]);
        fireSseMessage(ctx.sseInstances[0], {method: 'ready', params: {}}, '1:1');
        // Clear the send spy so test sees only its own calls.
        ctx.wsInstances[0].send.mockClear();
        return ctx;
    }

    it('approve() sends {method: "approve"} over the control WS', async () => {
        const ctx = await readySession();
        // Stage a pending permission so approve() has something to clear.
        (ctx.service as any).pendingPermission.set({
            id: 'tc-1', tool: 'run_command', args: {},
        });

        ctx.service.approve();
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'approve'});
        expect(ctx.service.pendingPermission()).toBeNull();
    });

    it('approve() resolves durable approval requests through REST', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockClear();
        ctx.wsInstances[0].send.mockClear();
        (ctx.service as any).pendingPermission.set({
            id: 'tc-rest',
            approvalId: 'approval-1',
            tool: 'run_command',
            args: {},
        });

        ctx.service.approve();

        expect(ctx.mockHttp.post).toHaveBeenCalledWith(
            expect.stringContaining('/persistent/threads/thread-c/approve/approval-1'),
            {decision: 'approve'},
        );
        expect(ctx.wsInstances[0].send).not.toHaveBeenCalled();
        expect(ctx.service.pendingPermission()).toBeNull();
    });

    it('deny() resolves durable approval requests through REST', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockClear();
        ctx.wsInstances[0].send.mockClear();
        (ctx.service as any).pendingPermission.set({
            id: 'tc-deny-rest',
            approvalId: 'approval-2',
            tool: 'run_command',
            args: {},
        });

        ctx.service.deny();

        expect(ctx.mockHttp.post).toHaveBeenCalledWith(
            expect.stringContaining('/persistent/threads/thread-c/approve/approval-2'),
            {decision: 'deny'},
        );
        expect(ctx.wsInstances[0].send).not.toHaveBeenCalled();
        expect(ctx.service.pendingPermission()).toBeNull();
    });

    it('deny() sends {method: "deny"} and seeds the denied tool call in the active turn', async () => {
        const ctx = await readySession();
        // Real permission.request always fires inside a turn — set that up.
        fireSseMessage(ctx.sseInstances[0], {method: 'turn.started', params: {turn_id: 1}}, '1:2');
        (ctx.service as any).pendingPermission.set({
            id: 'tc-2', tool: 'rm_rf', args: {path: '/'},
        });

        ctx.service.deny();
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'deny'});
        const turn = ctx.service.currentStreamingTurn()!;
        const denied = turn.events.filter(isToolCall).find((tc) => tc.id === 'tc-2') as ToolCallEvent;
        expect(denied).toBeDefined();
        expect(denied.status).toBe('denied');
        expect(denied.decision).toBe('denied');
    });

    it('/compact slash command sends compact without a local echo', async () => {
        // No "Compacting context..." system turn: the agent's
        // compaction.started/progress frames drive the live progress block,
        // and a no-op answers with a summary-less context.compacted.
        const ctx = await readySession();
        await ctx.service.sendMessage('/compact recent edits');
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'compact', focus: 'recent edits'});
        const systemTurns = ctx.service.turns().filter(isSystemTurn);
        expect(systemTurns.some((t) => /Compacting/.test(String(t.content)))).toBe(false);
    });

    it('/done sends archive', async () => {
        const ctx = await readySession();
        await ctx.service.sendMessage('/done');
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'archive'});
    });

    it('/undo sends undo', async () => {
        const ctx = await readySession();
        await ctx.service.sendMessage('/undo');
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'undo'});
    });

    it('setMode mutates the signal and sends mode.set', async () => {
        const ctx = await readySession();
        ctx.service.setMode('auto_accept');
        expect(ctx.service.permissionMode()).toBe('auto_accept');
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'mode.set', mode: 'auto_accept'});
    });

    it('setNarrationMode mutates the signal and sends narration.set', async () => {
        const ctx = await readySession();
        ctx.service.setNarrationMode('silent');
        expect(ctx.service.narrationMode()).toBe('silent');
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'narration.set', mode: 'silent'});
    });

    it('updateConfig forwards the config object over the WS', async () => {
        const ctx = await readySession();
        ctx.service.updateConfig({model: 'claude-sonnet-4-6', temperature: 0.3});
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({
            method: 'config.update',
            config: {model: 'claude-sonnet-4-6', temperature: 0.3},
        });
    });
});

describe('PersistentChatService — control WS frame filtering', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    async function readySession() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-status');
        fireSseOpen(ctx.sseInstances[0]);
        return ctx;
    }

    function fireWsFrame(ws: any, frame: Record<string, unknown>): void {
        ws.onmessage?.({data: JSON.stringify(frame)} as MessageEvent);
    }

    it('drops _seq-stamped frames on the control WS to avoid double-dispatch with SSE', async () => {
        const {service, wsInstances} = await readySession();
        const turnsBefore = service.turns().length;

        // Broadcast events carry params._seq = [epoch, seq] from the agent's
        // _broadcast() — SSE will redeliver them, so the WS copy is discarded.
        fireWsFrame(wsInstances[0], {
            method: 'turn.started',
            params: {turn_id: 99, _seq: [0, 12]},
        });
        expect(service.currentStreamingTurn()).toBeNull();

        fireWsFrame(wsInstances[0], {
            method: 'token',
            params: {content: 'should-be-dropped', _seq: [0, 13]},
        });
        expect(service.currentStreamingTurn()).toBeNull();

        fireWsFrame(wsInstances[0], {method: 'ready', params: {_seq: [0, 14]}});
        expect(service.sessionReady()).toBe(false);

        expect(service.turns().length).toBe(turnsBefore);
    });

    it('processes session.state from the control WS (WS-direct, no _seq)', async () => {
        // Regression: reconnect to an idle session whose cached SSE cursor sits
        // past the most recent `ready` event. The agent's session.state welcome
        // frame is the only thing that arrives over the WS, and it must flip
        // sessionReady so the UI clears the "Establishing connection" card.
        const {service, wsInstances} = await readySession();
        expect(service.sessionReady()).toBe(false);

        fireWsFrame(wsInstances[0], {
            method: 'session.state',
            params: {
                thread_id: 'thread-status',
                permission_mode: 'manual',
                narration_mode: 'verbose',
                turn_count: 1,
                model: 'claude-opus-4-7',
                temperature: 0.5,
            },
        });

        expect(service.sessionReady()).toBe(true);
        expect(service.permissionMode()).toBe('manual');
        expect(service.modelName()).toBe('claude-opus-4-7');
    });

    it('clears a stale "Agent not ready" error once session.state arrives', async () => {
        // Regression: during the WS reconnect storm at session attach the agent
        // rejects each /ws/chat with an `error: Agent not ready` frame until
        // attach completes. The eventual session.state must wipe the stale
        // banner so the UI doesn't show a red error contradicting a healthy
        // session.
        const {service, wsInstances} = await readySession();

        fireWsFrame(wsInstances[0], {
            method: 'error',
            params: {message: 'Agent not ready'},
        });
        expect(service.error()).toBe('Agent not ready');

        fireWsFrame(wsInstances[0], {
            method: 'session.state',
            params: {thread_id: 'thread-status'},
        });
        expect(service.error()).toBeNull();
        expect(service.sessionReady()).toBe(true);
    });

    it('silently ignores malformed WS frames', async () => {
        const {service, wsInstances} = await readySession();
        wsInstances[0].onmessage?.({data: 'not-json{'} as MessageEvent);
        expect(service.startupPhase()).toBeNull();
    });
});

describe('PersistentChatService — direct session WS (prepare + connection)', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    /** Build a URL-aware GET mock that handles all the endpoints connect() hits. */
    function connectGetMock(opts: {
        connectionResponses?: any[];  // array of values/errors to return on successive /connection calls
        threadMeta?: Record<string, unknown>;
        messages?: any[];
    } = {}) {
        const connectionResponses = opts.connectionResponses ?? [
            of({state: 'ready', ws_url: 'wss://api.example.com/p/t/ws?t=jwt', token: 'jwt', expires_at: 0}),
        ];
        let connectionCallIdx = 0;
        return (url: string) => {
            if (url.includes('/api/sessions/') && url.endsWith('/connection')) {
                const r = connectionResponses[Math.min(connectionCallIdx, connectionResponses.length - 1)];
                connectionCallIdx += 1;
                return r;
            }
            if (url.endsWith('/messages')) {
                return of({messages: opts.messages ?? [], total: 0});
            }
            return of(opts.threadMeta ?? {status: 'active', total_turns: 0});
        };
    }

    it('warm reconnect (already bound): GETs /connection, opens WS at returned ws_url, skips /prepare', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(
            connectGetMock({
                connectionResponses: [
                    of({
                        state: 'ready',
                        ws_url: 'wss://api.example.com/p/t1/ws?t=tok-warm',
                        token: 'tok-warm',
                        expires_at: 0,
                    }),
                ],
            }),
        );

        await ctx.service.connect('t1');

        // GET /api/sessions/t1/connection was called.
        const getUrls = ctx.mockHttp.get.mock.calls.map((c: any) => c[0]);
        expect(getUrls.some((u: string) => u.endsWith('/api/sessions/t1/connection'))).toBe(true);

        // POST /api/sessions/t1/prepare was NOT called.
        const prepareCalls = ctx.mockHttp.post.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/api/sessions/t1/prepare'),
        );
        expect(prepareCalls).toHaveLength(0);

        // WebSocket was opened at the URL returned by /connection.
        expect(ctx.wsInstances).toHaveLength(1);
        expect(ctx.wsInstances[0].url).toBe('wss://api.example.com/p/t1/ws?t=tok-warm');
        expect(ctx.service.sessionReady()).toBe(true);
    });

    it('cold start (425 → prepare → poll /connection until ready): WS opens at final ws_url', async () => {
        const ctx = createService();

        // /connection: 425, then 425 again (still booting), then 200.
        ctx.mockHttp.get.mockImplementation(
            connectGetMock({
                connectionResponses: [
                    throwError(() => ({status: 425})),
                    throwError(() => ({status: 425})),
                    of({
                        state: 'ready',
                        ws_url: 'wss://api.example.com/p/t2/ws?t=tok-cold',
                        token: 'tok-cold',
                        expires_at: 0,
                    }),
                ],
            }),
        );

        ctx.mockHttp.post.mockImplementation((url: string) => {
            if (url.endsWith('/api/sessions/t2/prepare')) {
                return of({state: 'provisioning'});
            }
            return of({});
        });

        await ctx.service.connect('t2');

        // POST /prepare was called exactly once.
        const prepareCalls = ctx.mockHttp.post.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/api/sessions/t2/prepare'),
        );
        expect(prepareCalls).toHaveLength(1);

        // GET /connection was called until it succeeded.
        const connCalls = ctx.mockHttp.get.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/api/sessions/t2/connection'),
        );
        expect(connCalls.length).toBeGreaterThanOrEqual(2);

        // WS opened at the URL returned by the successful /connection.
        const sessionWs = ctx.wsInstances.find((ws) =>
            String(ws.url || '').includes('wss://api.example.com/p/t2/ws'),
        );
        expect(sessionWs).toBeDefined();
        expect(sessionWs.url).toBe('wss://api.example.com/p/t2/ws?t=tok-cold');
        expect(ctx.service.sessionReady()).toBe(true);

        // No transient SSE on /notifications/events is opened — phase
        // signals come from the always-on NotificationService feed (owned
        // by the app shell), not from a per-connect listener.
        const lifecycleEs = ctx.sseInstances.find((es) =>
            es.url.includes('/notifications/events'),
        );
        expect(lifecycleEs).toBeUndefined();
    });

    it('NotificationService lifecycle events update startupPhase for the active thread', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(
            connectGetMock({
                connectionResponses: [
                    of({
                        state: 'ready',
                        ws_url: 'wss://api.example.com/p/t-life/ws?t=tok',
                        token: 'tok',
                        expires_at: 0,
                    }),
                ],
            }),
        );
        await ctx.service.connect('t-life');

        // The constructor effect filters on threadId() — confirm the
        // active thread matches before firing events.
        expect(ctx.service.threadId()).toBe('t-life');
        ctx.service.sessionReady.set(false);
        ctx.service.startupPhase.set(null);

        ctx.notifications.lifecycleEvent.set({
            thread_id: 't-life',
            state: 'provisioning',
        });
        // Flush the constructor effect so it observes the signal change.
        TestBed.tick();
        expect(ctx.service.startupPhase()).toBe('provisioning');

        ctx.notifications.lifecycleEvent.set({
            thread_id: 't-life',
            state: 'booting',
        });
        TestBed.tick();
        expect(ctx.service.startupPhase()).toBe('booting');

        ctx.notifications.lifecycleEvent.set({
            thread_id: 't-life',
            state: 'ready',
        });
        TestBed.tick();
        // 'ready' from the server means agent is session-ready — the
        // cockpit now opens the WS, which is the "connecting" phase
        // client-side.
        expect(ctx.service.startupPhase()).toBe('connecting');

        // Events for a different thread are ignored.
        ctx.notifications.lifecycleEvent.set({
            thread_id: 'other-thread',
            state: 'provisioning',
        });
        TestBed.tick();
        expect(ctx.service.startupPhase()).toBe('connecting');
    });

    it('WS close code 4401 re-fetches /connection and reopens WS with the fresh token', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(
            connectGetMock({
                connectionResponses: [
                    of({
                        state: 'ready',
                        ws_url: 'wss://api.example.com/p/t3/ws?t=tok-A',
                        token: 'tok-A',
                        expires_at: 0,
                    }),
                    of({
                        state: 'ready',
                        ws_url: 'wss://api.example.com/p/t3/ws?t=tok-B',
                        token: 'tok-B',
                        expires_at: 0,
                    }),
                ],
            }),
        );

        await ctx.service.connect('t3');

        // The first WS should be at the first ws_url.
        expect(ctx.wsInstances).toHaveLength(1);
        expect(ctx.wsInstances[0].url).toBe('wss://api.example.com/p/t3/ws?t=tok-A');

        // Fire a 4401 close on the first WS.
        ctx.wsInstances[0].onclose?.({code: 4401, reason: 'token expired'} as CloseEvent);

        // Let microtasks flush so the re-fetch + WS reopen happens.
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        // /connection was called again (twice total: initial + post-4401 refresh).
        const connCalls = ctx.mockHttp.get.mock.calls.filter((c: any) =>
            String(c[0]).endsWith('/api/sessions/t3/connection'),
        );
        expect(connCalls.length).toBeGreaterThanOrEqual(2);

        // A new WS was opened at the refreshed ws_url.
        expect(ctx.wsInstances.length).toBeGreaterThanOrEqual(2);
        const secondWs = ctx.wsInstances[ctx.wsInstances.length - 1];
        expect(secondWs.url).toBe('wss://api.example.com/p/t3/ws?t=tok-B');
    });
});

describe('PersistentChatService — disconnect()', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('closes both SSE and control WS, resets signals', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-d');
        fireSseOpen(ctx.sseInstances[0]);

        ctx.service.disconnect();

        expect(ctx.sseInstances[0].close).toHaveBeenCalled();
        expect(ctx.wsInstances[0].close).toHaveBeenCalledWith(1000);
        expect(ctx.service.connectionState()).toBe('disconnected');
        expect(ctx.service.sessionReady()).toBe(false);
    });
});

describe('PersistentChatService — endSession()', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('DELETEs the thread, tears down SSE/WS, resets state', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-e');
        fireSseOpen(ctx.sseInstances[0]);

        await ctx.service.endSession();

        const deleteCalls = ctx.mockHttp.delete.mock.calls;
        expect(deleteCalls.length).toBe(1);
        expect(deleteCalls[0][0]).toContain('/persistent/threads/thread-e');
        expect(deleteCalls[0][0]).not.toContain('permanent=true');
        expect(ctx.sseInstances[0].close).toHaveBeenCalled();
        expect(ctx.wsInstances[0].close).toHaveBeenCalledWith(1000);
        expect(ctx.service.connectionState()).toBe('disconnected');
        expect(ctx.service.sessionReady()).toBe(false);
    });

    it('tears down locally even if DELETE fails', async () => {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-f');
        fireSseOpen(ctx.sseInstances[0]);

        ctx.mockHttp.delete.mockImplementation(() => throwError(() => new Error('boom')));

        await expect(ctx.service.endSession()).rejects.toThrow('boom');
        expect(ctx.service.connectionState()).toBe('disconnected');
        expect(ctx.sseInstances[0].close).toHaveBeenCalled();
    });

    it('skips DELETE when no thread is connected', async () => {
        const ctx = createService();
        await ctx.service.endSession();
        expect(ctx.mockHttp.delete).not.toHaveBeenCalled();
        expect(ctx.service.connectionState()).toBe('disconnected');
    });
});

describe('PersistentChatService — attachments', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
    });

    afterEach(() => {
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    it('addAttachments appends, removeAttachment drops by id, clearAttachments empties', () => {
        const {service} = createService();
        const a: any = {id: '1', file: {} as File, name: 'a.png'};
        const b: any = {id: '2', file: {} as File, name: 'b.png'};
        service.addAttachments([a, b]);
        expect(service.pendingAttachments()).toHaveLength(2);
        service.removeAttachment('1');
        expect(service.pendingAttachments()).toEqual([b]);
        service.clearAttachments();
        expect(service.pendingAttachments()).toEqual([]);
    });
});

describe('PersistentChatService — interrupt self-healing', () => {
    let originalEs: any;
    let originalWs: any;

    beforeEach(() => {
        originalEs = (globalThis as any).EventSource;
        originalWs = (globalThis as any).WebSocket;
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
        (globalThis as any).EventSource = originalEs;
        (globalThis as any).WebSocket = originalWs;
        vi.clearAllMocks();
    });

    // Connect, open the SSE, and start a turn so isStreaming() is true and
    // "Stopping…" is meaningful.
    async function setupStreaming() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-int');
        await vi.advanceTimersByTimeAsync(0); // drain _openSse microtasks
        const es = ctx.sseInstances[0];
        fireSseOpen(es);
        fireSseMessage(es, {method: 'turn.started', params: {turn_id: 1}}, '1:1');
        return {...ctx, es};
    }

    it('forces a reconnect if the interrupt ack never arrives', async () => {
        const {service} = await setupStreaming();
        const reconnectSpy = vi
            .spyOn(service, 'reconnectNow')
            .mockImplementation(() => {});

        await service.interrupt();
        expect(service.isInterrupting()).toBe(true);
        expect(reconnectSpy).not.toHaveBeenCalled();

        // No interrupt.ack / turn.completed arrives — fallback fires at ~8s and
        // forces a replay-from-cursor reconnect to re-sync.
        await vi.advanceTimersByTimeAsync(8_001);

        expect(reconnectSpy).toHaveBeenCalledTimes(1);
    });

    it('does not reconnect when the turn boundary arrives in time', async () => {
        const {service, es} = await setupStreaming();
        const reconnectSpy = vi
            .spyOn(service, 'reconnectNow')
            .mockImplementation(() => {});

        await service.interrupt();
        // turn.completed lands promptly and clears "Stopping…".
        fireSseMessage(es, {method: 'turn.completed', params: {turn_id: 1}}, '1:9');
        expect(service.isInterrupting()).toBe(false);

        await vi.advanceTimersByTimeAsync(8_001);
        expect(reconnectSpy).not.toHaveBeenCalled();
    });

    it('clears a stuck isInterrupting when the connection drops (invariant)', async () => {
        const {service} = await setupStreaming();

        await service.interrupt();
        expect(service.isInterrupting()).toBe(true);
        expect(service.isStreaming()).toBe(true);

        // disconnect() closes the active turn but does not explicitly reset
        // isInterrupting — the invariant effect (not streaming ⇒ not stopping)
        // is what must clear it.
        service.disconnect();
        TestBed.tick();

        expect(service.isStreaming()).toBe(false);
        expect(service.isInterrupting()).toBe(false);
    });
});

describe('PersistentChatService — compaction progress frames', () => {
    afterEach(() => vi.clearAllMocks());

    async function setup() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-X');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);
        return {...ctx, es};
    }

    it('builds compaction state from started + progress frames', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'compaction.started',
            params: {
                trigger: 'auto', total_tokens: 951_682, ctx_used_tokens: 951_682,
                ctx_limit_tokens: 1_047_576, ctx_used_pct: 91,
                aux_limit_tokens: 131_072, n_passes: 10,
                plan: [{pass: 1, first_msg: 1, last_msg: 112, tokens: 98_000}],
            },
        }, '1:1');
        expect(service.compaction()).not.toBeNull();
        expect(service.compaction()!.nPasses).toBe(10);
        expect(service.compaction()!.currentPass).toBe(0); // planning

        fireSseMessage(es, {
            method: 'compaction.progress',
            params: {
                pass: 4, n_passes: 10, first_msg: 113, last_msg: 141,
                in_tokens: 38_000, out_tokens: 2_500, stage: 'summarizing', attempt: 1,
            },
        }, '1:2');
        const comp = service.compaction()!;
        expect(comp.currentPass).toBe(4);
        expect(comp.firstMsg).toBe(113);
        expect(comp.outTokens).toBe(2_500);
        // started-frame fields survive progress updates
        expect(comp.ctxUsedPct).toBe(91);
    });

    it('synthesizes state from a replayed progress frame without started (reload mid-fold)', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'compaction.progress',
            params: {pass: 7, n_passes: 10, first_msg: 500, last_msg: 540, in_tokens: 40_000, attempt: 2},
        }, '1:1');
        const comp = service.compaction()!;
        expect(comp.currentPass).toBe(7);
        expect(comp.nPasses).toBe(10);
        expect(comp.attempt).toBe(2);
    });

    it('clears compaction state on context.compacted (success path)', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'compaction.started', params: {n_passes: 2}}, '1:1');
        expect(service.compaction()).not.toBeNull();
        fireSseMessage(es, {
            method: 'context.compacted',
            params: {before: 100, after: 12, trigger: 'auto', summary: 'did things', turn: 3},
        }, '1:2');
        expect(service.compaction()).toBeNull();
    });

    it('clears compaction state and surfaces a system line on compaction.failed', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'compaction.started', params: {n_passes: 3}}, '1:1');
        fireSseMessage(es, {
            method: 'compaction.failed',
            params: {reason: 'aux_unavailable', pass: 2, n_passes: 3, kept_messages: true},
        }, '1:2');
        expect(service.compaction()).toBeNull();
        const sys = service.turns().filter((t: any) => t.kind === 'system');
        expect(sys.some((t: any) => String(t.content).includes('aux_unavailable'))).toBe(true);
    });

    it('clears stale compaction state when the turn ends', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'turn.started', params: {turn_id: 1}}, '1:1');
        fireSseMessage(es, {method: 'compaction.started', params: {n_passes: 5}}, '1:2');
        fireSseMessage(es, {method: 'turn.completed', params: {turn_id: 1}}, '1:3');
        expect(service.compaction()).toBeNull();
    });

    it('clears the progress block on compaction.skipped', async () => {
        // Engine ran but the size guard rejected the summary — the journaled
        // terminal frame must clear the block (incl. on SSE replay).
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'compaction.started', params: {n_passes: 1, trigger: 'manual'}}, '1:1');
        expect(service.compaction()).not.toBeNull();
        fireSseMessage(es, {
            method: 'compaction.skipped',
            params: {trigger: 'manual', reason: 'summary_not_smaller'},
        }, '1:2');
        expect(service.compaction()).toBeNull();
    });

    it('uses the trigger carried by a replayed progress frame (no started)', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'compaction.progress',
            params: {trigger: 'manual', pass: 1, n_passes: 1, first_msg: 1, last_msg: 34, in_tokens: 1176, out_tokens: null, attempt: 1, stage: 'summarizing'},
        }, '1:1');
        expect(service.compaction()!.trigger).toBe('manual');
    });

    it('renders a summary-less context.compacted as a system line, not a banner', async () => {
        // Manual /compact no-op: the agent answers with summary=null and
        // persists no row — the UI must not add an (empty) banner.
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'compaction.started', params: {n_passes: 1, trigger: 'manual'}}, '1:1');
        fireSseMessage(es, {
            method: 'context.compacted',
            params: {before: 10, after: 10, trigger: 'manual', summary: null, turn: 2},
        }, '1:2');
        expect(service.compaction()).toBeNull();
        expect(service.turns().some((t: any) => t.kind === 'compaction')).toBe(false);
        const sys = service.turns().filter((t: any) => t.kind === 'system');
        expect(sys.some((t: any) => String(t.content).includes('Nothing to compact'))).toBe(true);
    });
});

describe('PersistentChatService — usage.updated telemetry', () => {
    afterEach(() => vi.clearAllMocks());

    async function setup() {
        const ctx = createService();
        ctx.mockHttp.get.mockImplementation(() =>
            of({status: 'active', total_turns: 0, messages: [], total: 0}),
        );
        await ctx.service.connect('thread-X');
        const es = ctx.sseInstances[0];
        fireSseOpen(es);
        return {...ctx, es};
    }

    it('accumulates output/reasoning within a turn, latest input wins', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'usage.updated',
            params: {turn: 1, input_tokens: 10_000, output_tokens: 500, reasoning_tokens: 200, ctx_limit_tokens: 128_000},
        }, '1:1');
        fireSseMessage(es, {
            method: 'usage.updated',
            params: {turn: 1, input_tokens: 12_000, output_tokens: 700, reasoning_tokens: 100},
        }, '1:2');
        const u = service.usage()!;
        expect(u.inputTokens).toBe(12_000);
        expect(u.outputTokensTurn).toBe(1_200);
        expect(u.reasoningTokensTurn).toBe(300);
        expect(u.ctxLimitTokens).toBe(128_000);
    });

    it('resets per-turn accumulators when the turn changes', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'usage.updated',
            params: {turn: 1, input_tokens: 10_000, output_tokens: 500, ctx_limit_tokens: 128_000},
        }, '1:1');
        fireSseMessage(es, {
            method: 'usage.updated',
            params: {turn: 2, input_tokens: 11_000, output_tokens: 50},
        }, '1:2');
        const u = service.usage()!;
        expect(u.turn).toBe(2);
        expect(u.outputTokensTurn).toBe(50);
        // limit carried over from the earlier frame
        expect(u.ctxLimitTokens).toBe(128_000);
    });
});
