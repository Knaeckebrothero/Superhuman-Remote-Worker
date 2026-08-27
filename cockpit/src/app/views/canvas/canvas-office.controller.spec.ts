import {provideHttpClient} from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import {TestBed} from '@angular/core/testing';
import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import {CanvasState} from '../../core/models/canvas.model';
import {environment} from '../../core/environment';
import {CanvasOfficeController} from './canvas-office.controller';

const OFFICE_ORIGIN = 'https://office.example.test';

function officeState(revision = 1, editable = false): CanvasState {
  return {
    canvas_id: 'main',
    source: {type: 'workspace_file', path: 'output/report.docx'},
    title: 'Report',
    renderer: 'office',
    editable,
    alt_text: null,
    presentation_revision: revision,
    source_version: `sha256:${revision}`,
    status: 'ready',
    capabilities: {
      can_edit: editable,
      can_pop_out: true,
      can_take_control: false,
      can_view_office: true,
    },
    updated_at: '2026-07-24T10:00:00Z',
  };
}

describe('Canvas Office session lifecycle', () => {
  let controller: CanvasOfficeController;
  let http: HttpTestingController;
  let previousOrigin: string | null;

  beforeEach(() => {
    previousOrigin = environment.canvasOfficeOrigin;
    environment.canvasOfficeOrigin = OFFICE_ORIGIN;
    TestBed.configureTestingModule({
      providers: [
        CanvasOfficeController,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    controller = TestBed.inject(CanvasOfficeController);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify({ignoreCancelled: true});
    environment.canvasOfficeOrigin = previousOrigin;
    TestBed.resetTestingModule();
  });

  it('mints against the exact state and becomes ready only after the handshake', () => {
    const state = officeState();
    controller.syncPresentation(true, 'thread-1', state, '"canvas:1:office"');

    const request = http.expectOne(
      `${environment.apiUrl}/persistent/threads/thread-1/canvases/main/office-session`,
    );
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toBeNull();
    expect(request.request.headers.get('If-Match')).toBe('"canvas:1:office"');
    request.flush({
      urlsrc: `${OFFICE_ORIGIN}/browser/version/cool.html?`,
      WOPISrc: 'http://srw-orchestrator:8085/wopi/files/abc123',
      access_token: 'signed-token',
      access_token_ttl: 1_721_858_400_000,
    });

    expect(controller.officeStatus()).toBe('loading');
    expect(controller.session()?.access_token).toBe('signed-token');
    controller.markDocumentLoaded();
    expect(controller.officeStatus()).toBe('ready');

    controller.syncPresentation(false, 'thread-1', state, '"canvas:1:office"');
    expect(controller.session()).toBeNull();
    expect(controller.officeStatus()).toBe('idle');
  });

  it('fails closed on an unconfigured origin or a non-exact response', () => {
    environment.canvasOfficeOrigin = null;
    controller.syncPresentation(true, 'thread-1', officeState(), '"canvas:1:office"');
    http.expectNone(() => true);
    expect(controller.officeErrorCode()).toBe('canvas_office_not_configured');

    environment.canvasOfficeOrigin = OFFICE_ORIGIN;
    controller.syncPresentation(true, 'thread-1', officeState(2), '"canvas:2:office"');
    http.expectOne(request => request.url.endsWith('/office-session')).flush({
      urlsrc: `${OFFICE_ORIGIN}/browser/version/cool.html?`,
      WOPISrc: 'http://srw-orchestrator:8085/wopi/files/abc123',
      access_token: 'signed-token',
      access_token_ttl: 1_721_858_400_000,
      unexpected: true,
    });
    expect(controller.session()).toBeNull();
    expect(controller.officeErrorCode()).toBe('invalid_office_session');
  });

  it('keeps an editable frame mounted across revisions and renews against the latest state', async () => {
    const initial = officeState(1, true);
    controller.syncPresentation(true, 'thread-1', initial, '"canvas:1:office"');
    http.expectOne(request => request.url.endsWith('/office-session')).flush({
      urlsrc: `${OFFICE_ORIGIN}/browser/version/cool.html?`,
      WOPISrc: 'http://srw-orchestrator:8085/wopi/files/abc123',
      access_token: 'write-token',
      access_token_ttl: 1_721_858_400_000,
    });

    controller.syncPresentation(
      true,
      'thread-1',
      officeState(2, true),
      '"canvas:2:office"',
    );
    http.expectNone(() => true);
    expect(controller.session()?.access_token).toBe('write-token');

    const renewal = controller.refreshToken();
    const request = http.expectOne(request => request.url.endsWith('/office-session'));
    expect(request.request.headers.get('If-Match')).toBe('"canvas:2:office"');
    request.flush({
      urlsrc: `${OFFICE_ORIGIN}/browser/version/cool.html?`,
      WOPISrc: 'http://srw-orchestrator:8085/wopi/files/abc123',
      access_token: 'renewed-write-token',
      access_token_ttl: 1_721_858_999_000,
    });
    await expect(renewal).resolves.toMatchObject({
      access_token: 'renewed-write-token',
    });
  });

  it('rejects an editable Office state without the positive edit capability', () => {
    const state = {
      ...officeState(1, true),
      capabilities: {
        ...officeState(1, true).capabilities,
        can_edit: false,
      },
    };
    controller.syncPresentation(true, 'thread-1', state, '"canvas:1:office"');

    http.expectNone(() => true);
    expect(controller.session()).toBeNull();
    expect(controller.officeStatus()).toBe('idle');
  });
});
