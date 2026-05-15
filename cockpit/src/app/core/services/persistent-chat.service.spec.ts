import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {Injector, NgZone, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {PersistentChatService} from './persistent-chat.service';
import {ApiService} from './api.service';
import {IndexedDbService} from './indexed-db.service';
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
    };

    // NgZone stub: just run callbacks synchronously. Tests don't depend on
    // change-detection scheduling, only on signal mutations being observed.
    const mockZone: any = {
        run: <T>(fn: () => T) => fn(),
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

    const injector = Injector.create({
        providers: [
            {provide: HttpClient, useValue: mockHttp},
            {provide: ApiService, useValue: mockApi},
            {provide: IndexedDbService, useValue: mockCache},
            {provide: NgZone, useValue: mockZone},
        ],
    });

    const service = runInInjectionContext(injector, () => new PersistentChatService());
    return {service, mockHttp, mockApi, mockCache, sseInstances, wsInstances};
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

    it('surfaces error frames via sanitized error signal', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {method: 'error', params: {message: 'something broke'}}, '1:1');
        expect(service.error()).toContain('something broke');
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

    it('/compact slash command sends compact + adds a system turn', async () => {
        const ctx = await readySession();
        await ctx.service.sendMessage('/compact recent edits');
        const sent = ctx.wsInstances[0].send.mock.calls.map((c: any) => JSON.parse(c[0]));
        expect(sent).toContainEqual({method: 'compact', focus: 'recent edits'});
        const systemTurns = ctx.service.turns().filter(isSystemTurn);
        expect(systemTurns.slice(-1)[0].content).toMatch(/Compacting/);
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

    it('dispatches status frames from the control WS into startupPhase', async () => {
        const {service, wsInstances} = await readySession();
        expect(service.startupPhase()).toBeNull();

        fireWsFrame(wsInstances[0], {
            method: 'status',
            params: {phase: 'provisioning', elapsed_s: 2, timeout_s: 300},
        });
        expect(service.startupPhase()).toBe('provisioning');

        fireWsFrame(wsInstances[0], {method: 'status', params: {phase: 'booting'}});
        expect(service.startupPhase()).toBe('booting');

        fireWsFrame(wsInstances[0], {method: 'status', params: {phase: 'connecting'}});
        expect(service.startupPhase()).toBe('connecting');
    });

    it('drops non-status frames on the control WS to avoid double-dispatch with SSE', async () => {
        const {service, wsInstances} = await readySession();
        const turnsBefore = service.turns().length;

        // turn.started would create a streaming turn if dispatched.
        fireWsFrame(wsInstances[0], {method: 'turn.started', params: {turn_id: 99}});
        expect(service.currentStreamingTurn()).toBeNull();

        // token would attach text to the streaming turn if dispatched.
        fireWsFrame(wsInstances[0], {method: 'token', params: {content: 'should-be-dropped'}});
        expect(service.currentStreamingTurn()).toBeNull();

        // ready would flip sessionReady if dispatched — leave that to SSE.
        fireWsFrame(wsInstances[0], {method: 'ready', params: {}});
        expect(service.sessionReady()).toBe(false);

        expect(service.turns().length).toBe(turnsBefore);
    });

    it('silently ignores malformed WS frames', async () => {
        const {service, wsInstances} = await readySession();
        wsInstances[0].onmessage?.({data: 'not-json{'} as MessageEvent);
        expect(service.startupPhase()).toBeNull();
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
