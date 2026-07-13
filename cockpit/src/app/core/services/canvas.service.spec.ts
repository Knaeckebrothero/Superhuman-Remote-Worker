import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {provideHttpClient} from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import {TestBed} from '@angular/core/testing';
import {CanvasState} from '../models/canvas.model';
import {CanvasService} from './canvas.service';
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
});
