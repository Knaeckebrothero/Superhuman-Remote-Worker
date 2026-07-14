import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {provideHttpClient} from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import {TestBed} from '@angular/core/testing';
import {CanvasState} from '../models/canvas.model';
import {CanvasService, parseCanvasBroadcastInvalidation} from './canvas.service';
import {PersistentThreadTransportBridge} from './persistent-thread-transport-bridge.service';

function canvasState(
  revision: number,
  overrides: Partial<CanvasState> = {},
): CanvasState {
  return {
    canvas_id: 'main',
    source: {type: 'workspace_file', path: 'output/report.md'},
    title: 'Research report',
    renderer: 'markdown',
    editable: false,
    alt_text: null,
    presentation_revision: revision,
    source_version: 'sha256:abc',
    status: 'unavailable',
    capabilities: {
      can_edit: false,
      can_pop_out: false,
      can_take_control: false,
    },
    updated_at: `2026-07-13T10:00:0${revision}Z`,
    ...overrides,
  };
}

describe('CanvasService', () => {
  let service: CanvasService;
  let transport: PersistentThreadTransportBridge;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        CanvasService,
        PersistentThreadTransportBridge,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    service = TestBed.inject(CanvasService);
    transport = TestBed.inject(PersistentThreadTransportBridge);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify({ignoreCancelled: true});
    vi.useRealTimers();
  });

  it('loads the authoritative state and retains its representation ETag', () => {
    service.selectThread('thread-1');

    const request = http.expectOne((req) =>
      req.url.endsWith('/persistent/threads/thread-1/canvases/main'),
    );
    expect(request.request.method).toBe('GET');
    request.flush(canvasState(2), {
      headers: {ETag: '"canvas:2:representation"'},
    });

    expect(service.state()?.presentation_revision).toBe(2);
    expect(service.stateEtag()).toBe('"canvas:2:representation"');
    expect(service.loadStatus()).toBe('ready');
    expect(service.requestError()).toBeNull();
  });

  it('distinguishes a never-created 204 from a revisioned cleared state', () => {
    service.selectThread('never-created');
    http.expectOne((req) => req.url.includes('/never-created/canvases/main')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    expect(service.state()).toBeNull();
    expect(service.stateEtag()).toBeNull();
    expect(service.loadStatus()).toBe('ready');

    service.selectThread('cleared');
    http.expectOne((req) => req.url.includes('/cleared/canvases/main')).flush(
      canvasState(7, {
        source: null,
        title: null,
        renderer: 'auto',
        source_version: null,
        status: 'cleared',
      }),
      {headers: {ETag: '"canvas:7:cleared"'}},
    );
    expect(service.state()?.status).toBe('cleared');
    expect(service.state()?.presentation_revision).toBe(7);
  });

  it('reconciles a newer invalidation and ignores duplicate or older revisions', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(2));

    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 3},
    });
    const refresh = http.expectOne((req) => req.url.includes('/thread-1/canvases/main'));

    // The same journal frame can be replayed while the authoritative GET is in
    // flight. It must not cause a third request after revision 3 is applied.
    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 3},
    });
    refresh.flush(canvasState(3));
    http.expectNone((req) => req.url.includes('/thread-1/canvases/main'));

    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 2},
    });
    http.expectNone((req) => req.url.includes('/thread-1/canvases/main'));
    expect(service.state()?.presentation_revision).toBe(3);
  });

  it('converges directly to the latest REST revision after missed events', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));

    // Revisions 2-5 were missed; a later invalidation is only a reload hint.
    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 6},
    });
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(8));

    expect(service.state()?.presentation_revision).toBe(8);
  });

  it('queues a newer invalidation that arrives during an in-flight reconciliation', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));

    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 2},
    });
    const firstRefresh = http.expectOne((req) => req.url.includes('/thread-1/canvases/main'));
    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 4},
    });
    firstRefresh.flush(canvasState(2));

    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(4));
    expect(service.state()?.presentation_revision).toBe(4);
  });

  it('bounds a follow-up when REST initially returns below the advertised revision', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));

    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 5},
    });
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(3));
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(5));

    expect(service.state()?.presentation_revision).toBe(5);
  });

  it('follows an in-flight GET after a direct reconcile-required frame', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));

    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 2},
    });
    const firstRefresh = http.expectOne((req) => req.url.includes('/thread-1/canvases/main'));
    transport.forwardEvent('thread-1', {method: 'canvas.reconcile_required'});
    firstRefresh.flush(canvasState(2));

    const recoveryRefresh = http.expectOne((req) =>
      req.url.includes('/thread-1/canvases/main'),
    );
    recoveryRefresh.flush(canvasState(2));
    expect(service.state()?.presentation_revision).toBe(2);
  });

  it('cancels a stale thread request and clears prior state for a root draft', () => {
    service.selectThread('thread-a');
    const staleRequest = http.expectOne((req) => req.url.includes('/thread-a/canvases/main'));

    service.selectThread('thread-b');
    expect(staleRequest.cancelled).toBe(true);
    expect(service.state()).toBeNull();
    http.expectOne((req) => req.url.includes('/thread-b/canvases/main')).flush(canvasState(4));
    expect(service.state()?.presentation_revision).toBe(4);

    service.selectThread(null);
    expect(service.threadId()).toBeNull();
    expect(service.state()).toBeNull();
    expect(service.stateEtag()).toBeNull();
    expect(service.loadStatus()).toBe('idle');
  });

  it('ignores invalidations belonging to another thread', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));

    transport.forwardEvent('thread-2', {
      method: 'canvas.cleared',
      params: {canvas_id: 'main', presentation_revision: 2},
    });

    http.expectNone((req) => req.url.includes('/canvases/main'));
    expect(service.state()?.presentation_revision).toBe(1);
  });

  it('preserves the last authoritative state when a reconciliation fails', () => {
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(
      canvasState(1),
      {headers: {ETag: '"canvas:1:stable"'}},
    );

    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 2},
    });
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(
      {detail: {code: 'canvas_temporarily_unavailable'}},
      {status: 503, statusText: 'Service Unavailable'},
    );

    expect(service.state()?.presentation_revision).toBe(1);
    expect(service.stateEtag()).toBe('"canvas:1:stable"');
    expect(service.loadStatus()).toBe('error');
    expect(service.requestError()).toEqual({
      status: 503,
      code: 'canvas_temporarily_unavailable',
    });
  });

  it.each([401, 403, 404])(
    'clears state and ETag immediately on terminal HTTP %s',
    (status) => {
      service.selectThread('thread-1');
      http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(
        canvasState(1),
        {headers: {ETag: '"canvas:1:authorized"'}},
      );
      expect(service.state()).not.toBeNull();

      transport.forwardEvent('thread-1', {
        method: 'canvas.updated',
        params: {canvas_id: 'main', presentation_revision: 2},
      });
      http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(
        {detail: {code: 'canvas_access_ended'}},
        {status, statusText: 'Terminal Canvas state failure'},
      );

      expect(service.state()).toBeNull();
      expect(service.stateEtag()).toBeNull();
      expect(service.loadStatus()).toBe('error');
      expect(service.requestError()).toEqual({status, code: 'canvas_access_ended'});
    },
  );

  it('reconciles on focus only after the bounded staleness interval', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-13T10:00:00Z'));
    service.selectThread('thread-1');
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));

    window.dispatchEvent(new Event('focus'));
    http.expectNone((req) => req.url.includes('/thread-1/canvases/main'));

    vi.advanceTimersByTime(30_001);
    window.dispatchEvent(new Event('focus'));
    http.expectOne((req) => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));
  });

  it('saves source bytes with both preconditions and applies the response immediately', () => {
    service.selectThread('thread-1');
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(
      canvasState(2, {status: 'ready', editable: true}),
      {headers: {ETag: '"canvas:2:state"'}},
    );
    const sender = vi.fn().mockReturnValue(true);
    transport.attachControlSender(sender);
    let result: unknown;

    service.saveContent({
      contentUrl: '/api/persistent/threads/thread-1/canvases/main/content' +
        '?presentation_revision=2&source_fingerprint=sha256%3Afp' +
        '&source_version=sha256%3Aabc&ngsw-bypass=true',
      contentEtag: '"sha256:abc"',
      presentationRevision: 2,
      content: '# Edited report',
    }).subscribe(value => result = value);

    const save = http.expectOne(req => req.method === 'PUT');
    expect(save.request.headers.get('If-Match')).toBe('"sha256:abc"');
    expect(save.request.headers.get('X-Canvas-Presentation-Revision')).toBe('2');
    expect(save.request.headers.get('Content-Type')).toBe('text/plain; charset=utf-8');
    expect(save.request.body).toBe('# Edited report');
    save.flush(canvasState(3, {
      status: 'ready',
      editable: true,
      source_version: 'sha256:def',
    }), {
      headers: {
        ETag: '"canvas:3:state"',
        'X-Canvas-Content-ETag': '"sha256:def"',
      },
    });

    expect(result).toEqual(expect.objectContaining({
      stateEtag: '"canvas:3:state"',
      contentEtag: '"sha256:def"',
    }));
    expect(service.state()?.presentation_revision).toBe(3);
    expect(service.stateEtag()).toBe('"canvas:3:state"');
    expect(sender).toHaveBeenCalledWith('thread-1', {
      method: 'canvas.source_updated',
      canvas_id: 'main',
      path: 'output/report.md',
      presentation_revision: 3,
      source_version: 'sha256:def',
    });
  });

  it('conditionally adopts current workspace bytes and invalidates the runtime cache', () => {
    service.selectThread('thread-1');
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(
      canvasState(4, {status: 'source_changed'}),
      {headers: {ETag: '"canvas:4:state"'}},
    );
    const sender = vi.fn().mockReturnValue(true);
    transport.attachControlSender(sender);

    service.refreshSource().subscribe();
    const refresh = http.expectOne(req => req.url.endsWith('/canvases/main/refresh'));
    expect(refresh.request.method).toBe('POST');
    expect(refresh.request.headers.get('If-Match')).toBe('"canvas:4:state"');
    refresh.flush(canvasState(5, {
      status: 'ready',
      source_version: 'sha256:fresh',
    }), {
      headers: {
        ETag: '"canvas:5:state"',
        'X-Canvas-Content-ETag': '"sha256:fresh"',
      },
    });

    expect(service.state()?.source_version).toBe('sha256:fresh');
    expect(sender).toHaveBeenCalledWith('thread-1', expect.objectContaining({
      method: 'canvas.source_updated',
      presentation_revision: 5,
      source_version: 'sha256:fresh',
    }));
  });

  it('conditionally rotates a live app origin without requiring a content ETag', () => {
    const app = canvasState(8, {
      source: {type: 'workspace_app', entry_path: '/demo'},
      renderer: 'auto',
      source_version: null,
      status: 'ready',
      capabilities: {
        can_edit: false,
        can_pop_out: true,
        can_take_control: false,
        can_create_viewer_session: true,
      },
    });
    service.selectThread('thread-1');
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(app, {
      headers: {ETag: '"canvas:8:app"'},
    });
    const sender = vi.fn().mockReturnValue(true);
    transport.attachControlSender(sender);

    let result: unknown;
    service.resetOrigin('"canvas:8:app"').subscribe(value => result = value);
    const reset = http.expectOne(req => req.url.endsWith('/canvases/main/reset-origin'));
    expect(reset.request.method).toBe('POST');
    expect(reset.request.body).toBeNull();
    expect(reset.request.headers.get('If-Match')).toBe('"canvas:8:app"');
    reset.flush({...app, presentation_revision: 9}, {
      headers: {ETag: '"canvas:9:fresh-origin"'},
    });

    expect(result).toEqual({
      state: expect.objectContaining({presentation_revision: 9}),
      stateEtag: '"canvas:9:fresh-origin"',
    });
    expect(service.state()?.presentation_revision).toBe(9);
    expect(service.stateEtag()).toBe('"canvas:9:fresh-origin"');
    expect(sender).toHaveBeenCalledWith('thread-1', {
      method: 'canvas.presentation_updated',
      canvas_id: 'main',
      presentation_revision: 9,
    });
  });

  it('refuses to reset app storage without an authoritative state ETag', () => {
    let error: unknown;
    service.resetOrigin().subscribe({error: value => error = value});

    expect(error).toBeInstanceOf(Error);
    http.expectNone(req => req.url.includes('/reset-origin'));
  });

  it('does not notify the runtime from a mutation response older than current state', () => {
    service.selectThread('thread-1');
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(
      canvasState(3, {status: 'ready', editable: true}),
      {headers: {ETag: '"canvas:3:state"'}},
    );
    const sender = vi.fn().mockReturnValue(true);
    transport.attachControlSender(sender);
    const contentUrl = '/api/persistent/threads/thread-1/canvases/main/content' +
      '?presentation_revision=3&source_fingerprint=sha256%3Afp' +
      '&source_version=sha256%3Aabc&ngsw-bypass=true';

    service.saveContent({
      contentUrl,
      contentEtag: '"sha256:abc"',
      presentationRevision: 3,
      content: '# Older completion',
    }).subscribe();
    service.saveContent({
      contentUrl,
      contentEtag: '"sha256:abc"',
      presentationRevision: 3,
      content: '# Newer completion',
    }).subscribe();

    const [older, newer] = http.match(req => req.method === 'PUT');
    newer.flush(canvasState(5, {
      status: 'ready',
      editable: true,
      source_version: 'sha256:newer',
    }), {
      headers: {
        ETag: '"canvas:5:state"',
        'X-Canvas-Content-ETag': '"sha256:newer"',
      },
    });
    older.flush(canvasState(4, {
      status: 'ready',
      editable: true,
      source_version: 'sha256:older',
    }), {
      headers: {
        ETag: '"canvas:4:state"',
        'X-Canvas-Content-ETag': '"sha256:older"',
      },
    });

    expect(service.state()?.presentation_revision).toBe(5);
    expect(sender).toHaveBeenCalledTimes(1);
    expect(sender).toHaveBeenCalledWith('thread-1', expect.objectContaining({
      presentation_revision: 5,
      source_version: 'sha256:newer',
    }));
  });
});

describe('CanvasService cross-tab invalidation', () => {
  it('accepts only a bounded state pointer and never a Canvas state payload', () => {
    expect(parseCanvasBroadcastInvalidation({
      type: 'canvas.presentation_invalidated',
      threadId: 'thread-1',
      canvasId: 'main',
      presentationRevision: 9,
      state: {title: 'must not be trusted'},
    })).toEqual({
      type: 'canvas.presentation_invalidated',
      threadId: 'thread-1',
      canvasId: 'main',
      presentationRevision: 9,
    });
    expect(parseCanvasBroadcastInvalidation({
      type: 'canvas.presentation_invalidated',
      threadId: 'thread-1',
      canvasId: 'other',
      presentationRevision: 9,
    })).toBeNull();
    expect(parseCanvasBroadcastInvalidation({
      type: 'canvas.presentation_invalidated',
      threadId: 'thread-1',
      canvasId: 'main',
      presentationRevision: -1,
    })).toBeNull();
  });

  it('rebroadcasts transport pointers and reconciles received pointers without looping', () => {
    class FakeBroadcastChannel {
      static instances: FakeBroadcastChannel[] = [];
      readonly postMessage = vi.fn();
      readonly close = vi.fn();
      private listener: ((event: MessageEvent<unknown>) => void) | null = null;

      constructor(readonly name: string) {
        FakeBroadcastChannel.instances.push(this);
      }

      addEventListener(_type: string, listener: EventListenerOrEventListenerObject): void {
        if (typeof listener === 'function') {
          this.listener = listener as (event: MessageEvent<unknown>) => void;
        }
      }

      removeEventListener(): void {
        this.listener = null;
      }

      emit(data: unknown): void {
        this.listener?.(new MessageEvent('message', {data}));
      }
    }

    const original = Object.getOwnPropertyDescriptor(window, 'BroadcastChannel');
    Object.defineProperty(window, 'BroadcastChannel', {
      configurable: true,
      value: FakeBroadcastChannel as unknown as typeof BroadcastChannel,
    });
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        CanvasService,
        PersistentThreadTransportBridge,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    const service = TestBed.inject(CanvasService);
    const transport = TestBed.inject(PersistentThreadTransportBridge);
    const http = TestBed.inject(HttpTestingController);
    const channel = FakeBroadcastChannel.instances[0]!;

    service.selectThread('thread-1');
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(canvasState(1));
    transport.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'main', presentation_revision: 2},
    });
    expect(channel.postMessage).toHaveBeenCalledWith({
      type: 'canvas.presentation_invalidated',
      threadId: 'thread-1',
      canvasId: 'main',
      presentationRevision: 2,
    });
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(canvasState(2));

    channel.postMessage.mockClear();
    channel.emit({
      type: 'canvas.presentation_invalidated',
      threadId: 'thread-1',
      canvasId: 'main',
      presentationRevision: 3,
      state: {title: 'ignored'},
    });
    expect(service.state()?.presentation_revision).toBe(2);
    expect(channel.postMessage).not.toHaveBeenCalled();
    http.expectOne(req => req.url.includes('/thread-1/canvases/main')).flush(canvasState(3));
    expect(service.state()?.presentation_revision).toBe(3);

    http.verify();
    TestBed.resetTestingModule();
    if (original) Object.defineProperty(window, 'BroadcastChannel', original);
    else Reflect.deleteProperty(window, 'BroadcastChannel');
  });
});
