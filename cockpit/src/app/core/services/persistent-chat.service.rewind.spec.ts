/**
 * Task 8 — cockpit service surface for session rewind
 * (docs/features/session_rewind.md).
 *
 * rewind()/summarizeUpTo() only touch _sendControl, so this harness skips the
 * EventSource/WebSocket constructor mocks and connect() flow that
 * persistent-chat.service.spec.ts / .outbox.spec.ts need: it builds the
 * service through the same TestBed provider set, then assigns `controlWs`
 * directly the way "PersistentChatService — control frame delivery across a
 * reconnect" does in the main spec, and asserts on the mock socket's `send`
 * calls.
 */
import {describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {HttpClient} from '@angular/common/http';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {PersistentChatService} from './persistent-chat.service';
import {ApiService} from './api.service';
import {CapabilitiesService} from './capabilities.service';
import {IndexedDbService} from './indexed-db.service';
import {NotificationService} from './notification.service';
import {AppToastService} from '../../ui/toast';

function createService() {
    const mockHttp: any = {
        get: vi.fn().mockReturnValue(of({status: 'active', total_turns: 0, messages: [], total: 0})),
        post: vi.fn().mockReturnValue(of({})),
        patch: vi.fn().mockReturnValue(of({status: 'updated'})),
        delete: vi.fn().mockReturnValue(of({})),
    };
    const mockApi: any = {
        uploadToThread: vi.fn().mockReturnValue(of({thread_id: 't', files: []})),
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
        show: vi.fn(),
        info: vi.fn(),
        success: vi.fn(),
        warning: vi.fn(),
        danger: vi.fn(),
        dismiss: vi.fn(),
        dismissAll: vi.fn(),
    };
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
    return {service, mockHttp, mockCache};
}

/** A control WebSocket that is already OPEN — _sendControl writes straight
 *  through it instead of queueing on controlOutbox. */
function createMockWs() {
    return {
        readyState: WebSocket.OPEN,
        send: vi.fn(),
        close: vi.fn(),
        onopen: null,
        onmessage: null,
        onclose: null,
        onerror: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
    } as any;
}

function framesOn(ws: any): Record<string, unknown>[] {
    return ws.send.mock.calls.map((c: any) => JSON.parse(c[0]));
}

describe('PersistentChatService rewind', () => {
    it('sends a flat rewind frame with a request_id and flags in-flight', () => {
        const {service} = createService();
        const live = createMockWs();
        service.threadId.set('thread-rw');
        (service as any).controlWs = live;

        const requestId = service.rewind('row-1', 'conversation');

        const frame = framesOn(live)[0];
        expect(frame.method).toBe('rewind');
        expect(frame.message_id).toBe('row-1');
        expect(frame.mode).toBe('conversation');
        expect(frame.request_id).toBe(requestId);
        expect(frame.params).toBeUndefined();
        expect(service.rewindInFlight()).toBe(true);
    });

    it('summarizeUpTo rides the compact verb with a boundary', () => {
        const {service} = createService();
        const live = createMockWs();
        service.threadId.set('thread-rw');
        (service as any).controlWs = live;

        service.summarizeUpTo('row-2');

        const frame = framesOn(live)[0];
        expect(frame.method).toBe('compact');
        expect(frame.boundary_message_id).toBe('row-2');
    });
});
