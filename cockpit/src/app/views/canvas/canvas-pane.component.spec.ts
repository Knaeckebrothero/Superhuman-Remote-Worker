import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {CanvasState} from '../../core/models/canvas.model';
import {CanvasService} from '../../core/services/canvas.service';
import {CanvasContentController} from './canvas-content.controller';

function contentUrl(revision: number): string {
  return (
    '/api/persistent/threads/thread-1/canvases/main/content' +
    `?presentation_revision=${revision}&source_fingerprint=fp&source_version=sha256%3A${revision}` +
    '&ngsw-bypass=true'
  );
}

function canvasState(
  revision: number,
  renderer: CanvasState['renderer'] = 'markdown',
  overrides: Partial<CanvasState> = {},
): CanvasState {
  return {
    canvas_id: 'main',
    source: {type: 'workspace_file', path: 'output/report.md'},
    title: 'Report',
    renderer,
    editable: false,
    alt_text: renderer === 'image' ? 'A chart' : null,
    presentation_revision: revision,
    source_version: `sha256:${revision}`,
    content_url: contentUrl(revision),
    status: 'ready',
    capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    updated_at: `2026-07-13T10:00:0${revision}Z`,
    ...overrides,
  };
}

describe('Canvas pane content lifecycle', () => {
  let component: CanvasContentController;
  let viewport: HTMLElement;
  let http: HttpTestingController;
  let canvas: {
    threadId: ReturnType<typeof signal<string | null>>;
    state: ReturnType<typeof signal<CanvasState | null>>;
    loadStatus: ReturnType<typeof signal<'idle' | 'loading' | 'ready' | 'error'>>;
    reconcile: ReturnType<typeof vi.fn>;
  };
  let originalCreateObjectUrl: PropertyDescriptor | undefined;
  let originalRevokeObjectUrl: PropertyDescriptor | undefined;
  let createObjectUrl: ReturnType<typeof vi.fn>;
  let revokeObjectUrl: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    canvas = {
      threadId: signal<string | null>('thread-1'),
      state: signal<CanvasState | null>(null),
      loadStatus: signal<'idle' | 'loading' | 'ready' | 'error'>('ready'),
      reconcile: vi.fn(),
    };
    originalCreateObjectUrl = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
    originalRevokeObjectUrl = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
    createObjectUrl = vi.fn().mockReturnValueOnce('blob:canvas-one').mockReturnValueOnce('blob:canvas-two');
    revokeObjectUrl = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', {configurable: true, value: createObjectUrl});
    Object.defineProperty(URL, 'revokeObjectURL', {configurable: true, value: revokeObjectUrl});

    TestBed.configureTestingModule({
      providers: [
        CanvasContentController,
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: CanvasService, useValue: canvas},
      ],
    });
    component = TestBed.inject(CanvasContentController);
    viewport = document.createElement('div');
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http?.verify({ignoreCancelled: true});
    if (originalCreateObjectUrl) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectUrl);
    else delete (URL as unknown as {createObjectURL?: unknown}).createObjectURL;
    if (originalRevokeObjectUrl) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectUrl);
    else delete (URL as unknown as {revokeObjectURL?: unknown}).revokeObjectURL;
    TestBed.resetTestingModule();
  });

  it('loads text through the protected URL and preserves it across source drift', () => {
    canvas.state.set(canvasState(1));
    sync();
    http.expectOne(request => request.url.includes('/canvases/main/content')).flush(
      '# Version one',
      {headers: {ETag: '"sha256:1"'}},
    );

    expect(component.textContent()).toBe('# Version one');
    expect(component.contentEtag()).toBe('"sha256:1"');
    expect(component.contentStatus()).toBe('ready');

    canvas.state.set(canvasState(2, 'markdown', {
      status: 'source_changed',
      content_url: null,
      source_version: 'sha256:drifted',
    }));
    sync();

    http.expectNone(request => request.url.includes('/canvases/main/content'));
    expect(component.textContent()).toBe('# Version one');
    expect(component.contentStatus()).toBe('source_changed');
  });

  it('cancels an old content load and clears bytes when the selected thread changes', () => {
    canvas.state.set(canvasState(1, 'text'));
    sync();
    const stale = http.expectOne(request => request.url.includes('/canvases/main/content'));

    canvas.threadId.set('thread-2');
    canvas.state.set(null);
    sync();

    expect(stale.cancelled).toBe(true);
    expect(component.textContent()).toBe('');
    expect(component.contentStatus()).toBe('idle');
  });

  it('cancels a raced content revision and applies only the newest response', () => {
    canvas.state.set(canvasState(1, 'text'));
    sync();
    const stale = http.expectOne(request => request.url.includes('presentation_revision=1'));

    canvas.state.set(canvasState(2, 'text'));
    sync();
    expect(stale.cancelled).toBe(true);
    http.expectOne(request => request.url.includes('presentation_revision=2')).flush('newest');

    expect(component.textContent()).toBe('newest');
    expect(component.displayState()?.presentation_revision).toBe(2);
  });

  it('rejects a non-server or incomplete content pointer without issuing a request', () => {
    canvas.state.set(canvasState(1, 'text', {content_url: 'https://evil.test/content'}));
    sync();

    http.expectNone(() => true);
    expect(component.contentStatus()).toBe('error');
    expect(component.contentErrorCode()).toBe('invalid_content_url');
    expect(component.displayRenderer()).toBe('unsupported');
    expect(component.imageUrl()).toBeNull();
  });

  it('preserves scroll while a same-source text presentation refreshes', async () => {
    canvas.state.set(canvasState(1, 'text'));
    sync();
    http.expectOne(request => request.url.includes('presentation_revision=1')).flush('version one');
    viewport.scrollTop = 137;

    canvas.state.set(canvasState(2, 'text'));
    sync();
    expect(component.textContent()).toBe('version one');
    http.expectOne(request => request.url.includes('presentation_revision=2')).flush('version two');
    await Promise.resolve();

    expect(component.textContent()).toBe('version two');
    expect(viewport.scrollTop).toBe(137);
  });

  it('publishes raster blobs only after success and preserves the prior image on 409', () => {
    canvas.state.set(canvasState(1, 'image'));
    sync();
    http.expectOne(request => request.url.includes('presentation_revision=1'))
      .flush(new Blob(['one'], {type: 'image/png'}));

    expect(component.imageUrl()).toBe('blob:canvas-one');
    expect(createObjectUrl).toHaveBeenCalledOnce();
    expect(revokeObjectUrl).not.toHaveBeenCalled();

    canvas.state.set(canvasState(2, 'image'));
    sync();
    http.expectOne(request => request.url.includes('presentation_revision=2')).flush(
      new Blob([JSON.stringify({detail: {code: 'canvas_source_changed'}})], {type: 'application/json'}),
      {status: 409, statusText: 'Conflict'},
    );

    expect(component.imageUrl()).toBe('blob:canvas-one');
    expect(component.contentStatus()).toBe('source_changed');
    expect(revokeObjectUrl).not.toHaveBeenCalled();
    expect(canvas.reconcile).toHaveBeenCalledOnce();
  });

  it('revokes raster object URLs on replacement and authorization loss', () => {
    canvas.state.set(canvasState(1, 'image'));
    sync();
    http.expectOne(request => request.url.includes('presentation_revision=1'))
      .flush(new Blob(['one'], {type: 'image/png'}));

    canvas.state.set(canvasState(2, 'image'));
    sync();
    http.expectOne(request => request.url.includes('presentation_revision=2'))
      .flush(new Blob(['two'], {type: 'image/png'}));
    expect(component.imageUrl()).toBe('blob:canvas-two');
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:canvas-one');

    canvas.state.set(canvasState(3, 'image'));
    sync();
    http.expectOne(request => request.url.includes('presentation_revision=3')).flush(
      new Blob([JSON.stringify({detail: {code: 'forbidden'}})], {type: 'application/json'}),
      {status: 403, statusText: 'Forbidden'},
    );
    expect(component.imageUrl()).toBeNull();
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:canvas-two');
  });

  function sync(): void {
    component.syncPresentation(
      true,
      canvas.threadId(),
      canvas.state(),
      () => viewport.scrollTop,
      value => {
        viewport.scrollTop = value;
      },
    );
  }
});
