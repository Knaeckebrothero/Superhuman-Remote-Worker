/**
 * Phase 3 — the outbox: queued sends are user intent, not transport state.
 * (docs/features/session_reliability_and_transport_simplification.md)
 *
 * These tests cover the categorical fix for the "Creating thread" send-swallow:
 * a send committed by the user survives disconnect/reconnect/thread-creation,
 * flushes FIFO with one POST in flight, and never silently double-sends or
 * cross-mutates another thread's queue.
 */
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {HttpClient} from '@angular/common/http';
import {of, throwError, Subject} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {PersistentChatService} from './persistent-chat.service';
import {ApiService} from './api.service';
import {CapabilitiesService} from './capabilities.service';
import {IndexedDbService} from './indexed-db.service';
import {NotificationService} from './notification.service';
import {AppToastService} from '../../ui/toast';
import {isUserTurn, UserTurn} from '../models/turn.model';

// --- Minimal transport mocks (mirrors the main service spec's scaffolding) ---

interface MockEventSource {
    url: string;
    readyState: number;
    close: ReturnType<typeof vi.fn>;
    onopen: ((e: any) => void) | null;
    onmessage: ((e: MessageEvent) => void) | null;
    onerror: ((e: any) => void) | null;
    listeners: Record<string, ((e: any) => void)[]>;
}

function fireSseOpen(es: MockEventSource): void {
    es.readyState = 1;
    es.onopen?.({});
}

function fireSseMessage(es: MockEventSource, frame: Record<string, unknown>, lastEventId = ''): void {
    es.onmessage?.({data: JSON.stringify(frame), lastEventId} as MessageEvent);
}

function fireSseNamedEvent(es: MockEventSource, name: string, frame: Record<string, unknown>): void {
    (es.listeners[name] || []).forEach((h) => h({data: JSON.stringify(frame), lastEventId: ''} as MessageEvent));
}

/** Let queued microtasks + a macrotask boundary drain (flush POSTs resolve). */
const flushTick = () => new Promise((r) => setTimeout(r, 0));

const INPUT = '/input';
const isInput = (url: unknown) => String(url).endsWith(INPUT);

function createService() {
    const mockHttp: any = {
        get: vi.fn().mockReturnValue(of({status: 'active', total_turns: 0, messages: [], total: 0})),
        post: vi.fn().mockReturnValue(of({})),
        patch: vi.fn().mockReturnValue(of({status: 'updated'})),
        delete: vi.fn().mockReturnValue(of({})),
    };
    const mockApi: any = {
        uploadOneToThread: vi.fn().mockReturnValue(of([])),
        humanizeUploadError: vi.fn().mockReturnValue('upload failed'),
    };
    const mockCache: any = {
        getThreadCursor: vi.fn().mockResolvedValue(null),
        setThreadCursor: vi.fn().mockResolvedValue(undefined),
        deleteThreadCursor: vi.fn().mockResolvedValue(undefined),
        getThreadMessages: vi.fn().mockResolvedValue([]),
        getNewestCachedCreatedAt: vi.fn().mockResolvedValue(null),
        upsertThreadMessages: vi.fn().mockResolvedValue(undefined),
        clearThreadMessages: vi.fn().mockResolvedValue(undefined),
    };
    const mockToast: any = {
        show: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn(), danger: vi.fn(),
        dismiss: vi.fn(), dismissAll: vi.fn(),
    };

    const sseInstances: MockEventSource[] = [];
    function MockEventSourceCtor(this: any, url: string) {
        const es: MockEventSource = {
            url, readyState: 0, close: vi.fn(),
            onopen: null, onmessage: null, onerror: null, listeners: {},
        };
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

    function MockWebSocketCtor(this: any, url: string) {
        return {
            url, readyState: 1, send: vi.fn(), close: vi.fn(),
            onopen: null, onmessage: null, onclose: null, onerror: null,
            addEventListener: vi.fn(), removeEventListener: vi.fn(),
        } as any;
    }
    (MockWebSocketCtor as any).OPEN = 1;
    (MockWebSocketCtor as any).CONNECTING = 0;
    (globalThis as any).WebSocket = MockWebSocketCtor;

    const mockNotifications: any = {
        lifecycleEvent: signal<{thread_id: string; state: string; reason?: string} | null>(null),
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
        providers: [
            {provide: HttpClient, useValue: mockHttp},
            {provide: ApiService, useValue: mockApi},
            {
                provide: CapabilitiesService,
                useValue: {
                    datasourceScopeAutoAttachAvailable: () => true,
                    datasourceScopeAutoAttachAvailability$: of(true),
                },
            },
            {provide: IndexedDbService, useValue: mockCache},
            {provide: AppToastService, useValue: mockToast},
            {provide: NotificationService, useValue: mockNotifications},
            {provide: TranslocoService, useValue: {translate: (k: string) => k}},
            PersistentChatService,
        ],
    });
    const service = TestBed.inject(PersistentChatService);
    return {service, mockHttp, mockApi, mockCache, sseInstances};
}

/** Connect + drive the session to ready so the outbox flushes immediately. */
async function readySession(threadId = 'thread-a') {
    const ctx = createService();
    await ctx.service.connect(threadId);
    fireSseOpen(ctx.sseInstances[0]);
    fireSseMessage(ctx.sseInstances[0], {method: 'ready', params: {}}, '1:1');
    ctx.mockHttp.post.mockClear();
    return ctx;
}

const inputPosts = (ctx: {mockHttp: any}) =>
    ctx.mockHttp.post.mock.calls.filter((c: any) => isInput(c[0]));

describe('PersistentChatService — Phase 3 outbox', () => {
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

    // --- 1. N sends while not ready ---------------------------------------

    it('queues N sends while not ready — N items, N bubbles, no overwrite', async () => {
        const ctx = createService();
        await ctx.service.connect('t1'); // no 'ready' frame → not ready
        await ctx.service.sendMessage('one');
        await ctx.service.sendMessage('two');
        await ctx.service.sendMessage('three');

        expect(ctx.service.outbox().map((i) => i.content)).toEqual(['one', 'two', 'three']);
        const bubbles = ctx.service.turns().filter(isUserTurn).map((t) => (t as UserTurn).content);
        expect(bubbles).toEqual(['one', 'two', 'three']);
        expect(inputPosts(ctx).length).toBe(0); // nothing POSTed while not ready
    });

    // --- 2. ownership: disconnect keeps, thread-switch clears, carry re-adds -

    it('disconnect() preserves the outbox; connect(other) clears it', async () => {
        const ctx = createService();
        await ctx.service.connect('t1');
        await ctx.service.sendMessage('keep me');
        expect(ctx.service.outbox().length).toBe(1);

        ctx.service.disconnect();
        expect(ctx.service.outbox().length).toBe(1); // survives transport teardown

        await ctx.service.connect('t2'); // genuine thread switch
        expect(ctx.service.outbox().length).toBe(0);
    });

    it('connect(id, {carryOutbox}) re-dispatches queued bubbles after load_history', async () => {
        const ctx = createService();
        (ctx.service as any).outbox.set([
            {localId: 'user-c', content: 'hi', displayContent: 'hi', attempts: 0},
        ]);
        await ctx.service.connect('t-new', {carryOutbox: true});

        const bubbles = ctx.service.turns().filter(isUserTurn).map((t) => (t as UserTurn).content);
        expect(bubbles).toContain('hi');
        expect(ctx.service.outbox().length).toBe(1); // still queued (not ready)
    });

    // --- 3. FIFO, single POST in flight -----------------------------------

    it('flushes FIFO with exactly one POST in flight', async () => {
        const ctx = await readySession();
        const subjects: Subject<any>[] = [];
        ctx.mockHttp.post.mockImplementation((url: string) => {
            if (isInput(url)) {
                const s = new Subject<any>();
                subjects.push(s);
                return s;
            }
            return of({});
        });

        await ctx.service.sendMessage('a');
        await ctx.service.sendMessage('b');
        await ctx.service.sendMessage('c');

        expect(ctx.service.outbox().length).toBe(3);
        expect(subjects.length).toBe(1); // only the head is in flight

        subjects[0].next({});
        subjects[0].complete();
        await flushTick();
        expect(subjects.length).toBe(2);
        expect(ctx.service.outbox().map((i) => i.content)).toEqual(['b', 'c']);

        subjects[1].next({});
        subjects[1].complete();
        await flushTick();
        subjects[2].next({});
        subjects[2].complete();
        await flushTick();
        expect(ctx.service.outbox().length).toBe(0);
    });

    // --- 4. flush outcomes: 409 / 503 / 404 -------------------------------

    it('409 removes the item but keeps the bubble', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockReturnValue(throwError(() => ({status: 409})));
        await ctx.service.sendMessage('dup');
        await flushTick();

        expect(ctx.service.outbox().length).toBe(0);
        expect(ctx.service.turns().some((t) => isUserTurn(t) && t.content === 'dup')).toBe(true);
    });

    it('503 keeps item + bubble + banner, no timer retry; next send retriggers', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockReturnValue(throwError(() => ({status: 503, error: {detail: 'busy'}})));
        await ctx.service.sendMessage('later');
        await flushTick();

        expect(ctx.service.outbox().length).toBe(1);
        expect(ctx.service.turns().some((t) => isUserTurn(t) && t.content === 'later')).toBe(true);
        expect(ctx.service.error()).not.toBeNull();

        // No auto-retry: another tick issues no further POST on its own.
        const before = inputPosts(ctx).length;
        await flushTick();
        expect(inputPosts(ctx).length).toBe(before);

        // Next send retriggers the flush (POST succeeds now) → both drain FIFO.
        ctx.mockHttp.post.mockReturnValue(of({}));
        await ctx.service.sendMessage('again');
        await flushTick();
        expect(ctx.service.outbox().length).toBe(0);
    });

    it('404 drains the outbox and rolls back its bubbles', async () => {
        const ctx = await readySession();
        ctx.mockHttp.post.mockReturnValue(throwError(() => ({status: 404})));
        await ctx.service.sendMessage('gone');
        await flushTick();

        expect(ctx.service.outbox().length).toBe(0);
        expect(ctx.service.turns().some((t) => isUserTurn(t) && t.content === 'gone')).toBe(false);
    });

    // --- 5. thread switch mid-POST → resolution dropped -------------------

    it('drops a POST resolution after a thread switch (no cross-thread mutation)', async () => {
        const ctx = await readySession('t1');
        const s = new Subject<any>();
        ctx.mockHttp.post.mockImplementation((url: string) => (isInput(url) ? s : of({})));

        await ctx.service.sendMessage('from-t1'); // POST in flight (deferred)
        expect(ctx.service.outbox().length).toBe(1);

        await ctx.service.connect('t2'); // switch clears the outbox
        await ctx.service.sendMessage('from-t2'); // t2 not ready → queued
        const t2Len = ctx.service.outbox().length;
        expect(t2Len).toBe(1);

        // The stale t1 POST resolves now — must NOT touch t2's queue.
        s.next({});
        s.complete();
        await flushTick();
        expect(ctx.service.outbox().length).toBe(t2Len);
        expect(ctx.service.outbox()[0].content).toBe('from-t2');
    });

    // --- 6. single-flight + horizon re-dispatch ---------------------------

    it('concurrent _flushOutbox calls stay single-flight', async () => {
        const ctx = await readySession();
        const subjects: Subject<any>[] = [];
        ctx.mockHttp.post.mockImplementation((url: string) => {
            if (isInput(url)) {
                const s = new Subject<any>();
                subjects.push(s);
                return s;
            }
            return of({});
        });
        (ctx.service as any).outbox.set([
            {localId: 'u1', content: 'x', displayContent: 'x', attempts: 0},
        ]);

        (ctx.service as any)._flushOutbox();
        (ctx.service as any)._flushOutbox();

        expect(subjects.length).toBe(1); // second call is a no-op
    });

    it('horizon reload re-dispatches an unflushed bubble once, skipping the in-flight head', async () => {
        const ctx = await readySession('t1');
        const s = new Subject<any>();
        ctx.mockHttp.post.mockImplementation((url: string) => (isInput(url) ? s : of({})));

        await ctx.service.sendMessage('head'); // POST(head) deferred → in flight
        await ctx.service.sendMessage('second'); // queued behind
        expect(ctx.service.outbox().length).toBe(2);

        // gone_beyond_horizon → loadHistory (empty) then re-dispatch bubbles.
        fireSseNamedEvent(ctx.sseInstances[0], 'gone_beyond_horizon', {
            params: {epoch: 2, server_seq: 0},
        });
        await flushTick();
        await flushTick();

        const bubbles = ctx.service.turns().filter(isUserTurn).map((t) => (t as UserTurn).content);
        // 'second' re-dispatched exactly once; the in-flight 'head' skipped
        // (its row may already be in the reloaded history).
        expect(bubbles.filter((c) => c === 'second').length).toBe(1);
        expect(bubbles).not.toContain('head');
    });
});
