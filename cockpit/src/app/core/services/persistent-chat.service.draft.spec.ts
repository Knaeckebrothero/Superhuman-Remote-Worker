/**
 * Instant-landing draft sessions (knowledge-base/knowledge/features/instant_landing_session.md):
 * `/` shows an open composer with no thread; the first send creates the
 * session with a reviewed body (title, default project, connector selection) and
 * the queued message rides the outbox into the new thread. The orchestrator
 * resolves all other settings from the owner's defaults — the client sends
 * no model / permission mode / workspace backend.
 */
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {draftTitleFrom, PersistentChatService} from './persistent-chat.service';
import {ApiService} from './api.service';
import {CapabilitiesService} from './capabilities.service';
import {IndexedDbService} from './indexed-db.service';
import {NotificationService} from './notification.service';
import {AppToastService} from '../../ui/toast';
import {isUserTurn} from '../models/turn.model';

// --- Minimal transport mocks (mirrors the outbox spec's scaffolding) ---

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

/** Let queued microtasks + a macrotask boundary drain. */
const flushTick = () => new Promise((r) => setTimeout(r, 0));

const isThreadsCreate = (url: unknown) => String(url).endsWith('/persistent/threads');
const isInput = (url: unknown) => String(url).endsWith('/input');

function createService(opts: {
    projects?: any[];
    eligible?: any[];
    createFails?: boolean;
    policyAvailable?: boolean;
} = {}) {
    const mockHttp: any = {
        get: vi.fn().mockImplementation((url: string) => {
            if (url.includes('/projects')) return of(opts.projects ?? []);
            return of({status: 'active', total_turns: 0, messages: [], total: 0});
        }),
        post: vi.fn().mockImplementation((url: string) => {
            if (isThreadsCreate(url)) {
                return opts.createFails
                    ? throwError(() => new Error('create failed'))
                    : of({thread_id: 't-new'});
            }
            return of({});
        }),
        patch: vi.fn().mockReturnValue(of({status: 'updated'})),
        delete: vi.fn().mockReturnValue(of({})),
    };
    const mockApi: any = {
        uploadOneToThread: vi.fn().mockReturnValue(of({kind: 'done', files: []})),
        deleteThreadUpload: vi.fn().mockReturnValue(of(undefined)),
        humanizeUploadError: vi.fn().mockReturnValue('upload failed'),
        getEligibleDatasources: vi.fn().mockReturnValue(of(opts.eligible ?? [])),
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
        cloudDiffStagedEvent: signal(null),
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
        providers: [
            {provide: HttpClient, useValue: mockHttp},
            {provide: ApiService, useValue: mockApi},
            {
                provide: CapabilitiesService,
                useValue: {
                    datasourceScopeAutoAttachAvailable: () => opts.policyAvailable ?? true,
                    datasourceScopeAutoAttachAvailability$: of(opts.policyAvailable ?? true),
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
    return {service, mockHttp, mockApi, sseInstances};
}

const createPosts = (ctx: {mockHttp: any}) =>
    ctx.mockHttp.post.mock.calls.filter((c: any) => isThreadsCreate(c[0]));
const inputPosts = (ctx: {mockHttp: any}) =>
    ctx.mockHttp.post.mock.calls.filter((c: any) => isInput(c[0]));

describe('draftTitleFrom', () => {
    it('uses the first line, collapsed and trimmed', () => {
        expect(draftTitleFrom('  fix the   login bug\nplease')).toBe('fix the login bug');
    });

    it('falls back for empty input', () => {
        expect(draftTitleFrom('   \n\n')).toBe('Untitled Session');
    });

    it('caps long messages at a word boundary with an ellipsis', () => {
        const long = 'word '.repeat(30).trim();
        const title = draftTitleFrom(long);
        expect(title.length).toBeLessThanOrEqual(61);
        expect(title.endsWith('…')).toBe(true);
    });
});

describe('PersistentChatService — instant-landing draft sessions', () => {
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

    it('enterDraftSession opens an empty composable state — nothing created', async () => {
        const ctx = createService();
        ctx.service.enterDraftSession();
        await flushTick();

        expect(ctx.service.isDraftSession()).toBe(true);
        expect(ctx.service.threadId()).toBeNull();
        expect(ctx.service.turns()).toEqual([]);
        expect(createPosts(ctx).length).toBe(0); // landing creates nothing
    });

    it('first send creates the thread with a minimal body and carries the message', async () => {
        const ctx = createService({
            projects: [{id: 'p-def', is_default: true}, {id: 'p2'}],
            eligible: [
                {id: 'auto', default_selected: true},
                {id: 'manual', default_selected: false},
            ],
        });
        ctx.service.enterDraftSession();
        await flushTick();

        await ctx.service.sendMessage('fix the login bug');
        await flushTick();

        const creates = createPosts(ctx);
        expect(creates.length).toBe(1);
        expect(creates[0][1]).toEqual({
            title: 'fix the login bug',
            project_ids: ['p-def'],
            datasource_ids: ['auto'],
        });
        expect(ctx.service.isDraftSession()).toBe(false);
        expect(ctx.service.threadId()).toBe('t-new');
        // The message is queued (not posted) until the session goes ready…
        expect(ctx.service.outbox().map((i) => i.displayContent)).toEqual(['fix the login bug']);
        expect(inputPosts(ctx).length).toBe(0);
        // …and the optimistic bubble survived the create/connect reset.
        expect(ctx.service.turns().filter(isUserTurn).length).toBe(1);

        // Ready → the carried outbox flushes into the new thread.
        fireSseOpen(ctx.sseInstances[0]);
        fireSseMessage(ctx.sseInstances[0], {method: 'ready', params: {}}, '1:1');
        await flushTick();
        expect(inputPosts(ctx).length).toBe(1);
        expect(ctx.service.outbox().length).toBe(0);
    });

    it('selects no connector defaults while the rollout capability is unavailable', async () => {
        const ctx = createService({
            policyAvailable: false,
            projects: [{id: 'p-def', is_default: true}],
            eligible: [{id: 'auto', default_selected: true}],
        });
        ctx.service.enterDraftSession();
        await flushTick();

        expect(ctx.mockApi.getEligibleDatasources).not.toHaveBeenCalled();
        expect(ctx.service.draftDatasourceIds()).toEqual([]);
    });

    it('omits project_ids when there is no default project', async () => {
        const ctx = createService({projects: [{id: 'p2'}]});
        ctx.service.enterDraftSession();
        await flushTick();

        await ctx.service.sendMessage('hello');
        await flushTick();

        expect(createPosts(ctx)[0][1]).toEqual({title: 'hello', datasource_ids: []});
    });

    it('lets the user opt out of reviewed draft defaults', async () => {
        const ctx = createService({eligible: [{id: 'auto', default_selected: true}]});
        ctx.service.enterDraftSession();
        await flushTick();
        expect(ctx.service.draftDatasourceIds()).toEqual(['auto']);

        ctx.service.setDraftConnectorsEnabled(false);
        await ctx.service.sendMessage('no credentials');
        await flushTick();

        expect(createPosts(ctx)[0][1]).toEqual({
            title: 'no credentials',
            datasource_ids: [],
        });
    });

    it('a pasted screenshot on the landing page creates the session, then uploads', async () => {
        // Pre-Task-4 this was an unrecoverable dead end: onPaste attaches with
        // no connection guard, sendMessage hit the upload block first,
        // threadId() was null, and it bailed with 'Cannot upload: no active
        // thread' BEFORE _createFromDraftSession — so the composer was stuck
        // with a chip it could neither send nor recover from.
        const ctx = createService({eligible: []});
        ctx.service.enterDraftSession();
        await flushTick();

        const shot = new File(['x'], 'pasted-image.png', {type: 'image/png'});
        ctx.service.addAttachments([{
            id: 'p1', file: shot, name: 'pasted-image.png', size: shot.size,
            mimeType: 'image/png', uploadStatus: 'pending',
        } as any]);

        const ok = await ctx.service.sendMessage('what is this?');
        await flushTick();

        expect(ok).toBe(true);
        expect(ctx.service.attachmentError()).toBeNull();
        expect(createPosts(ctx).length).toBe(1);
        expect(ctx.service.threadId()).toBe('t-new');
        // Composer cleared; the file rides the queued item instead.
        expect(ctx.service.pendingAttachments()).toEqual([]);
        expect(ctx.service.outbox()[0].pendingFiles?.map((f) => f.name)).toEqual([
            'pasted-image.png',
        ]);
        // Still no upload — there is no ready workspace yet.
        expect(ctx.mockApi.uploadOneToThread).not.toHaveBeenCalled();

        // Ready → the upload runs against the thread that now exists, then
        // the message goes out with the hint naming the stored file.
        ctx.mockApi.uploadOneToThread.mockReturnValue(
            of({
                kind: 'done',
                files: [{name: 'pasted-image.png', size: shot.size, mime_type: 'image/png', path: 'uploads/pasted-image.png'}],
            }),
        );
        fireSseOpen(ctx.sseInstances[0]);
        fireSseMessage(ctx.sseInstances[0], {method: 'ready', params: {}}, '1:1');
        await flushTick();

        expect(ctx.mockApi.uploadOneToThread).toHaveBeenCalledWith('t-new', shot);
        expect(inputPosts(ctx).length).toBe(1);
        expect(inputPosts(ctx)[0][1].content).toBe(
            'what is this?\n\n[Attached files in uploads/: pasted-image.png]',
        );
        expect(ctx.service.outbox().length).toBe(0);
    });

    it('a second send while the create is in flight queues without a second create', async () => {
        const ctx = createService();
        ctx.service.enterDraftSession();
        await flushTick();

        await ctx.service.sendMessage('one');
        await ctx.service.sendMessage('two');
        await flushTick();

        expect(createPosts(ctx).length).toBe(1);
        expect(ctx.service.outbox().map((i) => i.displayContent)).toEqual(['one', 'two']);
    });

    it('re-enters draft (outbox intact) when the create fails, so send retries it', async () => {
        const ctx = createService({createFails: true});
        ctx.service.enterDraftSession();
        await flushTick();

        await ctx.service.sendMessage('hello');
        await flushTick();

        expect(ctx.service.isDraftSession()).toBe(true);
        expect(ctx.service.outbox().map((i) => i.displayContent)).toEqual(['hello']);
        // The optimistic bubble was re-shown on the error state.
        expect(ctx.service.turns().filter(isUserTurn).length).toBe(1);
    });

    it('connecting to a real thread leaves draft mode', async () => {
        const ctx = createService();
        ctx.service.enterDraftSession();
        await flushTick();

        await ctx.service.connect('t-existing');
        expect(ctx.service.isDraftSession()).toBe(false);
    });
});
