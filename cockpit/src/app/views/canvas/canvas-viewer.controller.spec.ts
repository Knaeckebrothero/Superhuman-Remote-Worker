import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {TestBed} from '@angular/core/testing';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {environment} from '../../core/environment';
import {CanvasState} from '../../core/models/canvas.model';
import {CanvasService} from '../../core/services/canvas.service';
import {CanvasViewerController} from './canvas-viewer.controller';
import {CANVAS_BOOTSTRAP_CHANNEL, CANVAS_BOOTSTRAP_VERSION} from './canvas-viewer-protocol';

const VIEWER_SUFFIX = '.canvas.userland.test';
const ORIGIN = `https://7f2640cb-8584-4ab1-a68e-95b2c9274419${VIEWER_SUFFIX}`;
const ATTACHMENT_ONE = '54f4fd56-69d8-46c8-8ab7-a3349af0d784';
const ATTACHMENT_TWO = '5ce49cf9-72e4-4e50-aa4f-f972f63e934d';
const BRIDGE_NONCE = 'b'.repeat(43);
const CHALLENGE = 'c'.repeat(43);
const READY_RECEIPT = 'r'.repeat(43);
const EXCHANGE_CODE = 'e'.repeat(43);

function appState(revision: number, overrides: Partial<CanvasState> = {}): CanvasState {
  return {
    canvas_id: 'main',
    source: {type: 'workspace_app', entry_path: '/'},
    title: 'Prototype',
    renderer: 'auto',
    editable: false,
    alt_text: null,
    presentation_revision: revision,
    source_version: null,
    status: 'ready',
    capabilities: {
      can_edit: false,
      can_pop_out: false,
      can_take_control: false,
      can_create_viewer_session: true,
    },
    updated_at: `2026-07-13T10:00:0${revision}Z`,
    ...overrides,
  };
}

function attachment(attachmentId = ATTACHMENT_ONE) {
  const now = Date.now();
  return {
    attachment_id: attachmentId,
    origin: ORIGIN,
    bootstrap_url: `${ORIGIN}/_canvas/bootstrap?attachment_id=${attachmentId}`,
    bridge_nonce: BRIDGE_NONCE,
    bootstrap_expires_at: new Date(now + 30_000).toISOString(),
    renew_after: new Date(now + 60_000).toISOString(),
    expires_at: new Date(now + 120_000).toISOString(),
  };
}

function challenge(overrides: Record<string, unknown> = {}) {
  return {
    channel: CANVAS_BOOTSTRAP_CHANNEL,
    version: CANVAS_BOOTSTRAP_VERSION,
    type: 'challenge',
    attachment_id: ATTACHMENT_ONE,
    challenge: CHALLENGE,
    ready_receipt: READY_RECEIPT,
    ...overrides,
  };
}

function ready(overrides: Record<string, unknown> = {}) {
  return {...challenge(), type: 'ready', ...overrides};
}

function frameEvent(
  source: WindowProxy,
  data: unknown,
  origin = ORIGIN,
): MessageEvent<unknown> {
  return {source, data, origin} as MessageEvent<unknown>;
}

function frameWindow(): WindowProxy & {postMessage: ReturnType<typeof vi.fn>} {
  return {postMessage: vi.fn()} as unknown as WindowProxy & {
    postMessage: ReturnType<typeof vi.fn>;
  };
}

function renewal() {
  const now = Date.now();
  return {
    renew_after: new Date(now + 60_000).toISOString(),
    expires_at: new Date(now + 120_000).toISOString(),
  };
}

describe('Canvas live-app viewer lifecycle', () => {
  let controller: CanvasViewerController;
  let http: HttpTestingController;
  let canvas: {reconcile: ReturnType<typeof vi.fn>};
  let originalSuffix: string | null;

  beforeEach(() => {
    originalSuffix = environment.canvasViewerHostSuffix;
    environment.canvasViewerHostSuffix = VIEWER_SUFFIX;
    canvas = {reconcile: vi.fn()};
    TestBed.configureTestingModule({
      providers: [
        CanvasViewerController,
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: CanvasService, useValue: canvas},
      ],
    });
    controller = TestBed.inject(CanvasViewerController);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify({ignoreCancelled: true});
    TestBed.resetTestingModule();
    environment.canvasViewerHostSuffix = originalSuffix;
    vi.useRealTimers();
  });

  it('stays dark without the positive viewer capability', () => {
    const state = appState(1, {
      capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    });
    controller.syncPresentation(true, 'thread-1', state, '"canvas:1"');

    http.expectNone(() => true);
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerStatus()).toBe('idle');
  });

  it('creates with If-Match and tears down the attachment when closed', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    const create = http.expectOne(request => request.url.endsWith('/view-attachments'));
    expect(create.request.method).toBe('POST');
    expect(create.request.body).toBeNull();
    expect(create.request.headers.get('If-Match')).toBe('"canvas:1"');
    create.flush(attachment());

    expect(controller.frameUrl()).not.toBeNull();
    expect(controller.frameOrigin()).toBe(ORIGIN);
    expect(controller.viewerStatus()).toBe('loading');

    controller.syncPresentation(false, 'thread-1', appState(1), '"canvas:1"');
    expect(controller.frameUrl()).toBeNull();
    const close = http.expectOne(request => request.url.endsWith(`/view-attachments/${ATTACHMENT_ONE}`));
    expect(close.request.method).toBe('DELETE');
    close.flush(null);
    expect(controller.viewerStatus()).toBe('idle');
  });

  it('rejects an off-suffix bootstrap before crossing the resource URL boundary', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush({
      ...attachment(),
      origin: 'https://canvas.evil.test',
      bootstrap_url: `https://canvas.evil.test/_canvas/bootstrap?attachment_id=${ATTACHMENT_ONE}`,
    });

    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.viewerErrorCode()).toBe('invalid_view_attachment');
    http.expectOne(request => request.url.endsWith(`/view-attachments/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('surfaces the typed unsupported-browser response for trusted fallback UX', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(
      {detail: {code: 'canvas_browser_unsupported'}},
      {status: 409, statusText: 'Conflict'},
    );

    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.viewerErrorCode()).toBe('canvas_browser_unsupported');
  });

  it('retries the isolated attachment flow without creating a top-level URL', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(
      {detail: {code: 'canvas_viewer_unavailable'}},
      {status: 503, statusText: 'Service Unavailable'},
    );
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.frameUrl()).toBeNull();

    controller.retry();
    const retry = http.expectOne(request => request.url.endsWith('/view-attachments'));
    expect(retry.request.method).toBe('POST');
    expect(retry.request.headers.get('If-Match')).toBe('"canvas:1"');
    retry.flush(attachment());
    expect(controller.frameUrl()).not.toBeNull();

    controller.syncPresentation(false, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('does not re-attempt a failed create while the reconciled state is unchanged', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(
      {detail: {code: 'canvas_precondition_failed'}},
      {status: 412, statusText: 'Precondition Failed'},
    );
    expect(canvas.reconcile).toHaveBeenCalledTimes(1);
    expect(controller.viewerStatus()).toBe('error');

    // The forced reconcile lands on the same state, which re-runs the pane's
    // presentation effect. Re-attempting here is what turns one rejected
    // precondition into an unbounded request storm.
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');

    http.expectNone(request => request.url.endsWith('/view-attachments'));
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.viewerErrorCode()).toBe('canvas_viewer_create_failed');
  });

  it('re-attempts as soon as the reconciled state carries a new ETag', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(
      {detail: {code: 'canvas_precondition_failed'}},
      {status: 412, statusText: 'Precondition Failed'},
    );

    controller.syncPresentation(true, 'thread-1', appState(2), '"canvas:2"');

    const retry = http.expectOne(request => request.url.endsWith('/view-attachments'));
    expect(retry.request.headers.get('If-Match')).toBe('"canvas:2"');
    retry.flush(attachment());
    expect(controller.frameUrl()).not.toBeNull();

    controller.syncPresentation(false, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('authorizes only an exact frame challenge and becomes ready only on the receipt', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = frameWindow();
    const otherFrame = frameWindow();
    controller.bindFrame(frame);

    controller.handleFrameMessage(frameEvent(otherFrame, challenge()));
    controller.handleFrameMessage(frameEvent(frame, challenge(), 'https://other.test'));
    controller.handleFrameMessage(frameEvent(frame, challenge({extra: true})));
    http.expectNone(request => request.url.endsWith('/authorize'));

    controller.handleFrameMessage(frameEvent(frame, challenge()));
    const authorize = http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}/authorize`));
    expect(authorize.request.method).toBe('POST');
    expect(authorize.request.body).toEqual({
      challenge: CHALLENGE,
      ready_receipt: READY_RECEIPT,
      bridge_nonce: BRIDGE_NONCE,
    });
    authorize.flush({
      challenge: CHALLENGE,
      ready_receipt: READY_RECEIPT,
      exchange_code: EXCHANGE_CODE,
      expires_at: new Date(Date.now() + 10_000).toISOString(),
    });

    expect(frame.postMessage).toHaveBeenCalledOnce();
    expect(frame.postMessage).toHaveBeenCalledWith({
      channel: CANVAS_BOOTSTRAP_CHANNEL,
      version: CANVAS_BOOTSTRAP_VERSION,
      type: 'authorize',
      attachment_id: ATTACHMENT_ONE,
      challenge: CHALLENGE,
      exchange_code: EXCHANGE_CODE,
    }, ORIGIN);
    expect(controller.viewerStatus()).toBe('loading');

    controller.handleFrameMessage(frameEvent(frame, ready({ready_receipt: 'x'.repeat(43)})));
    expect(controller.viewerStatus()).toBe('loading');
    controller.handleFrameMessage(frameEvent(frame, ready()));
    expect(controller.viewerStatus()).toBe('ready');

    controller.syncPresentation(false, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('rejects mismatched authorization echoes without posting a credential', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = frameWindow();
    controller.bindFrame(frame);
    controller.handleFrameMessage(frameEvent(frame, challenge()));
    http.expectOne(request => request.url.endsWith('/authorize')).flush({
      challenge: 'd'.repeat(43),
      ready_receipt: READY_RECEIPT,
      exchange_code: EXCHANGE_CODE,
      expires_at: new Date(Date.now() + 10_000).toISOString(),
    });

    expect(frame.postMessage).not.toHaveBeenCalled();
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerErrorCode()).toBe('invalid_view_attachment_authorization');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('honors a bootstrap error only when the active correlation tuple matches', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = frameWindow();
    controller.bindFrame(frame);
    controller.handleFrameMessage(frameEvent(frame, challenge()));
    const authorize = http.expectOne(request => request.url.endsWith('/authorize'));

    controller.handleFrameMessage(frameEvent(frame, {
      ...challenge(),
      type: 'error',
      code: 'exchange_failed',
      ready_receipt: 'x'.repeat(43),
    }));
    expect(controller.frameUrl()).not.toBeNull();

    controller.handleFrameMessage(frameEvent(frame, {
      ...challenge(),
      type: 'error',
      code: 'exchange_failed',
    }));
    expect(authorize.cancelled).toBe(true);
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerErrorCode()).toBe('canvas_viewer_bootstrap_failed');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('surfaces a missing partitioned bootstrap cookie as browser storage unavailable', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = frameWindow();
    controller.bindFrame(frame);
    controller.handleFrameMessage(frameEvent(frame, challenge()));
    const authorize = http.expectOne(request => request.url.endsWith('/authorize'));

    controller.handleFrameMessage(frameEvent(frame, {
      ...challenge(),
      type: 'error',
      code: 'canvas_browser_storage_unavailable',
    }));

    expect(authorize.cancelled).toBe(true);
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerErrorCode()).toBe('canvas_browser_storage_unavailable');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('cancels an in-flight authorization when the presentation is torn down', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = frameWindow();
    controller.bindFrame(frame);
    controller.handleFrameMessage(frameEvent(frame, challenge()));
    const authorize = http.expectOne(request => request.url.endsWith('/authorize'));

    controller.syncPresentation(false, 'thread-1', appState(1), '"canvas:1"');
    expect(authorize.cancelled).toBe(true);
    expect(controller.viewerStatus()).toBe('idle');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('ignores a stale frame detach but closes the attachment when its frame disappears', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = frameWindow();
    controller.bindFrame(frame);

    controller.unbindFrame(frameWindow());
    expect(controller.frameUrl()).not.toBeNull();
    controller.unbindFrame(frame);

    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerErrorCode()).toBe('canvas_viewer_frame_detached');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('closes a frame which never completes before its bootstrap deadline', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-13T10:00:00Z'));
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush({
      ...attachment(),
      bootstrap_expires_at: new Date(Date.now() + 1_000).toISOString(),
    });

    vi.advanceTimersByTime(1_000);
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerErrorCode()).toBe('canvas_viewer_bootstrap_expired');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('renews a same-source presentation without replacing the mounted frame', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = controller.frameUrl();

    controller.syncPresentation(true, 'thread-1', appState(2), '"canvas:2"');
    const renew = http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}/renew`));
    expect(renew.request.method).toBe('POST');
    expect(renew.request.body).toBeNull();
    renew.flush(renewal());

    expect(controller.frameUrl()).toBe(frame);
    expect(controller.viewerStatus()).toBe('loading');

    controller.syncPresentation(false, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('keeps hard expiry armed while a scheduled renewal is stalled', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-13T10:00:00Z'));
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush({
      ...attachment(),
      renew_after: new Date(Date.now() + 1_000).toISOString(),
      expires_at: new Date(Date.now() + 2_000).toISOString(),
      bootstrap_expires_at: new Date(Date.now() + 2_000).toISOString(),
    });

    vi.advanceTimersByTime(1_000);
    const renew = http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}/renew`));
    expect(controller.viewerStatus()).toBe('renewing');

    vi.advanceTimersByTime(1_000);
    expect(renew.cancelled).toBe(true);
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.viewerErrorCode()).toBe('canvas_viewer_expired');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });

  it('replaces a stale same-path attachment only after renewal rejects it', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const oldFrame = controller.frameUrl();

    controller.syncPresentation(true, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}/renew`)).flush(
      {detail: {code: 'canvas_attachment_stale'}},
      {status: 409, statusText: 'Conflict'},
    );
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(
      attachment(ATTACHMENT_TWO),
    );

    expect(controller.frameUrl()).not.toBe(oldFrame);
    expect(controller.viewerStatus()).toBe('loading');

    controller.syncPresentation(false, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_TWO}`)).flush(null);
  });

  it('tears down immediately on an unavailable state', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());

    controller.syncPresentation(
      true,
      'thread-1',
      appState(1, {status: 'unavailable'}),
      '"canvas:1:unavailable"',
    );
    expect(controller.frameUrl()).toBeNull();
    http.expectOne(request => request.url.endsWith(`/${ATTACHMENT_ONE}`)).flush(null);
  });
});
