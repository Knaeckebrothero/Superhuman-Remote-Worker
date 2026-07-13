import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {TestBed} from '@angular/core/testing';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {environment} from '../../core/environment';
import {CanvasState} from '../../core/models/canvas.model';
import {CanvasService} from '../../core/services/canvas.service';
import {CanvasViewerController} from './canvas-viewer.controller';

const VIEWER_SUFFIX = '.canvas.userland.test';
const ORIGIN = `https://7f2640cb-8584-4ab1-a68e-95b2c9274419${VIEWER_SUFFIX}`;

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

function attachment(attachmentId = 'attachment_one') {
  const now = Date.now();
  return {
    attachment_id: attachmentId,
    origin: ORIGIN,
    bootstrap_url: `${ORIGIN}/_canvas/bootstrap?token=${'t'.repeat(43)}`,
    renew_after: new Date(now + 60_000).toISOString(),
    expires_at: new Date(now + 120_000).toISOString(),
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
    expect(controller.viewerStatus()).toBe('ready');

    controller.syncPresentation(false, 'thread-1', appState(1), '"canvas:1"');
    expect(controller.frameUrl()).toBeNull();
    const close = http.expectOne(request => request.url.endsWith('/view-attachments/attachment_one'));
    expect(close.request.method).toBe('DELETE');
    close.flush(null);
    expect(controller.viewerStatus()).toBe('idle');
  });

  it('rejects an off-suffix bootstrap before crossing the resource URL boundary', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush({
      ...attachment(),
      origin: 'https://canvas.evil.test',
      bootstrap_url: 'https://canvas.evil.test/_canvas/bootstrap?token=secret',
    });

    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.viewerErrorCode()).toBe('invalid_view_attachment');
    http.expectOne(request => request.url.endsWith('/view-attachments/attachment_one')).flush(null);
  });

  it('renews a same-source presentation without replacing the mounted frame', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const frame = controller.frameUrl();

    controller.syncPresentation(true, 'thread-1', appState(2), '"canvas:2"');
    const renew = http.expectOne(request => request.url.endsWith('/attachment_one/renew'));
    expect(renew.request.method).toBe('POST');
    expect(renew.request.body).toBeNull();
    renew.flush(renewal());

    expect(controller.frameUrl()).toBe(frame);
    expect(controller.viewerStatus()).toBe('ready');

    controller.syncPresentation(false, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith('/attachment_one')).flush(null);
  });

  it('keeps hard expiry armed while a scheduled renewal is stalled', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-13T10:00:00Z'));
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush({
      ...attachment(),
      renew_after: new Date(Date.now() + 1_000).toISOString(),
      expires_at: new Date(Date.now() + 2_000).toISOString(),
    });

    vi.advanceTimersByTime(1_000);
    const renew = http.expectOne(request => request.url.endsWith('/attachment_one/renew'));
    expect(controller.viewerStatus()).toBe('renewing');

    vi.advanceTimersByTime(1_000);
    expect(renew.cancelled).toBe(true);
    expect(controller.frameUrl()).toBeNull();
    expect(controller.viewerStatus()).toBe('error');
    expect(controller.viewerErrorCode()).toBe('canvas_viewer_expired');
    http.expectOne(request => request.url.endsWith('/attachment_one')).flush(null);
  });

  it('replaces a stale same-path attachment only after renewal rejects it', () => {
    controller.syncPresentation(true, 'thread-1', appState(1), '"canvas:1"');
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(attachment());
    const oldFrame = controller.frameUrl();

    controller.syncPresentation(true, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith('/attachment_one/renew')).flush(
      {detail: {code: 'canvas_attachment_stale'}},
      {status: 409, statusText: 'Conflict'},
    );
    http.expectOne(request => request.url.endsWith('/attachment_one')).flush(null);
    http.expectOne(request => request.url.endsWith('/view-attachments')).flush(
      attachment('attachment_two'),
    );

    expect(controller.frameUrl()).not.toBe(oldFrame);
    expect(controller.viewerStatus()).toBe('ready');

    controller.syncPresentation(false, 'thread-1', appState(2), '"canvas:2"');
    http.expectOne(request => request.url.endsWith('/attachment_two')).flush(null);
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
    http.expectOne(request => request.url.endsWith('/attachment_one')).flush(null);
  });
});
