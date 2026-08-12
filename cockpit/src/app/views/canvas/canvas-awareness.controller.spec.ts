import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CanvasState } from '../../core/models/canvas.model';
import {
  CANVAS_AWARENESS_EVENT_SOURCE_FACTORY,
  CANVAS_AWARENESS_RENEW_MS,
  CANVAS_AWARENESS_SESSION_STORAGE,
  CanvasAwarenessController,
  CanvasAwarenessEventSource,
} from './canvas-awareness.controller';

const SOURCE_VERSION = `sha256:${'a'.repeat(64)}`;
const TAB_ID = 'canvas-tab-one-1234';

class FakeEventSource implements CanvasAwarenessEventSource {
  readonly listeners = new Map<string, (event: MessageEvent<string>) => void>();
  readonly close = vi.fn();

  constructor(
    readonly url: string,
    readonly init: EventSourceInit,
  ) {}

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, listener);
  }

  emit(type: string, data: unknown): void {
    this.listeners.get(type)?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
}

function editableState(revision = 4, overrides: Partial<CanvasState> = {}): CanvasState {
  return {
    canvas_id: 'main',
    source: { type: 'workspace_file', path: 'output/report.md' },
    title: 'Report',
    renderer: 'markdown',
    editable: true,
    alt_text: null,
    presentation_revision: revision,
    source_version: SOURCE_VERSION,
    status: 'ready',
    capabilities: { can_edit: true, can_pop_out: true, can_take_control: false },
    updated_at: '2026-08-10T10:00:00Z',
    ...overrides,
  };
}

function memoryStorage(initial: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(initial));
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
}

describe('CanvasAwarenessController', () => {
  let controller: CanvasAwarenessController;
  let http: HttpTestingController;
  let sources: FakeEventSource[];
  let storage: Storage;

  beforeEach(() => {
    vi.useFakeTimers();
    storage = memoryStorage({
      'srw.canvas.awareness.id.v1': TAB_ID,
      'srw.canvas.awareness.sequence.v1': '10',
    });
    ({ controller, http, sources } = createHarness(storage));
  });

  afterEach(() => {
    http.verify({ ignoreCancelled: true });
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });

  it('opens the exact owner-gated named-event stream independent of chat transport', () => {
    controller.sync(true, 'thread/one', editableState());

    expect(sources).toHaveLength(1);
    expect(sources[0].url).toBe(
      'http://localhost:8085/api/persistent/threads/thread%2Fone' +
        '/canvases/main/awareness/stream?ngsw-bypass=true',
    );
    expect(sources[0].url).not.toContain(TAB_ID);
    expect(sources[0].init).toEqual({ withCredentials: true });
    expect(sources[0].listeners.has('canvas_awareness')).toBe(true);
  });

  it('atomically replaces exact remote editors and filters this reloaded tab', () => {
    controller.sync(true, 'thread-1', editableState());
    sources[0].emit('canvas_awareness', {
      canvas_id: 'main',
      editors: [
        wireEditor({ editing_session_id: TAB_ID, sender_id: 'self' }),
        wireEditor({ editing_session_id: 'remote-tab-123', sender_id: 'remote' }),
        wireEditor({
          editing_session_id: 'other-source-1',
          sender_id: 'stale',
          presentation_revision: 3,
        }),
      ],
    });

    expect(controller.remoteEditors()).toEqual([
      expect.objectContaining({
        senderId: 'remote',
        editingSessionId: 'remote-tab-123',
        presentationRevision: 4,
      }),
    ]);
    expect(controller.remoteEditing()).toBe(true);

    sources[0].emit('canvas_awareness', { canvas_id: 'main', editors: [] });
    expect(controller.remoteEditors()).toEqual([]);
    expect(controller.remoteEditing()).toBe(false);
  });

  it('expires a stale snapshot locally if the dedicated stream is interrupted', () => {
    controller.sync(true, 'thread-1', editableState());
    sources[0].emit('canvas_awareness', {
      canvas_id: 'main',
      editors: [wireEditor({ ttl_ms: 1_500 })],
    });
    expect(controller.remoteEditing()).toBe(true);

    vi.advanceTimersByTime(1_500);

    expect(controller.remoteEditing()).toBe(false);
  });

  it('serializes a tombstone and later refocus so the higher sequence wins', () => {
    controller.sync(true, 'thread-1', editableState());
    controller.startEditing();
    const editing = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(editing.request.method).toBe('PUT');
    expect(editing.request.url).toBe(
      `http://localhost:8085/api/persistent/threads/thread-1/canvases/main/awareness/${TAB_ID}`,
    );
    expect(editing.request.body).toEqual({
      sequence: 11,
      state: 'editing',
      path: 'output/report.md',
      presentation_revision: 4,
      source_version: SOURCE_VERSION,
    });
    editing.flush(mutationResponse(11, 'editing'));

    controller.stopEditing();
    const idle = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(idle.request.body['sequence']).toBe(12);
    expect(idle.request.body['state']).toBe('idle');

    // Refocus while the tombstone is still in flight. No concurrent PUT is
    // issued; the higher editing sequence waits behind it.
    controller.startEditing();
    http.expectNone((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    idle.flush(mutationResponse(12, 'idle'));

    const refocused = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(refocused.request.body['sequence']).toBe(13);
    expect(refocused.request.body['state']).toBe('editing');
    refocused.flush(mutationResponse(13, 'editing'));

    controller.stopEditing();
    http
      .expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`))
      .flush(mutationResponse(14, 'idle'));
  });

  it('adopts a server high-water mark before reasserting current intent', () => {
    controller.sync(true, 'thread-1', editableState());
    controller.startEditing();
    const stale = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    stale.flush({ ...mutationResponse(20, 'idle'), applied: false });

    const reasserted = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(reasserted.request.body['sequence']).toBe(21);
    expect(reasserted.request.body['state']).toBe('editing');
    reasserted.flush(mutationResponse(21, 'editing'));

    controller.stopEditing();
    http
      .expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`))
      .flush(mutationResponse(22, 'idle'));
  });

  it('renews every five seconds without overlapping an in-flight write', () => {
    controller.sync(true, 'thread-1', editableState());
    controller.startEditing();
    const first = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));

    vi.advanceTimersByTime(CANVAS_AWARENESS_RENEW_MS);
    http.expectNone((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    first.flush(mutationResponse(11, 'editing'));

    const renewal = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(renewal.request.body['sequence']).toBe(12);
    expect(renewal.request.body['state']).toBe('editing');
    renewal.flush(mutationResponse(12, 'editing'));

    controller.stopEditing();
    http
      .expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`))
      .flush(mutationResponse(13, 'idle'));
  });

  it('retries an ambiguous failure with the exact sequence before renewing', () => {
    controller.sync(true, 'thread-1', editableState());
    controller.startEditing();
    const first = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(first.request.body['sequence']).toBe(11);
    first.flush(null, { status: 503, statusText: 'Service Unavailable' });

    vi.advanceTimersByTime(CANVAS_AWARENESS_RENEW_MS);

    const retry = http.expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`));
    expect(retry.request.body['sequence']).toBe(11);
    expect(retry.request.body['state']).toBe('editing');
    retry.flush(mutationResponse(11, 'editing'));

    controller.stopEditing();
    http
      .expectOne((request) => request.url.endsWith(`/awareness/${TAB_ID}`))
      .flush(mutationResponse(12, 'idle'));
  });

  it('does not let terminal failure for the old thread cancel the new thread', () => {
    controller.sync(true, 'thread-1', editableState());
    controller.startEditing();
    const oldEditing = http.expectOne((request) => request.url.includes('/thread-1/'));

    controller.sync(true, 'thread-2', editableState());
    oldEditing.flush(null, { status: 403, statusText: 'Forbidden' });

    const oldIdle = http.expectOne((request) => request.url.includes('/thread-1/'));
    expect(oldIdle.request.body['state']).toBe('idle');
    oldIdle.flush(null, { status: 403, statusText: 'Forbidden' });

    const newEditing = http.expectOne((request) => request.url.includes('/thread-2/'));
    expect(newEditing.request.body['state']).toBe('editing');
    newEditing.flush(mutationResponse(13, 'editing'));

    controller.stopEditing();
    http
      .expectOne((request) => request.url.includes('/thread-2/'))
      .flush(mutationResponse(14, 'idle'));
  });

  it('rotates cloned opener identity once and reuses it across popout reload', () => {
    controller.sync(true, 'thread-1', editableState(), true);
    controller.startEditing();
    const first = http.expectOne((request) => request.url.includes('/awareness/'));
    const rotatedId = decodeURIComponent(first.request.url.split('/').at(-1) ?? '');
    expect(rotatedId).not.toBe(TAB_ID);
    expect(first.request.body['sequence']).toBe(1);
    expect(storage.getItem('srw.canvas.awareness.id.v1')).toBe(rotatedId);
    expect(storage.getItem('srw.canvas.awareness.popout.v1')).toBe(rotatedId);
    first.flush(mutationResponse(1, 'editing'));

    http.verify();
    TestBed.resetTestingModule();
    ({ controller, http, sources } = createHarness(storage));

    controller.sync(true, 'thread-1', editableState(), true);
    controller.startEditing();
    const afterReload = http.expectOne((request) => request.url.includes('/awareness/'));
    expect(decodeURIComponent(afterReload.request.url.split('/').at(-1) ?? '')).toBe(rotatedId);
    expect(afterReload.request.body['sequence']).toBe(2);
    afterReload.flush(mutationResponse(2, 'editing'));
  });

  it('closes a superseded stream and ignores its late named snapshot', () => {
    controller.sync(true, 'thread-1', editableState());
    const stale = sources[0];

    controller.sync(true, 'thread-2', editableState());
    expect(stale.close).toHaveBeenCalledOnce();
    expect(sources).toHaveLength(2);

    stale.emit('canvas_awareness', {
      canvas_id: 'main',
      editors: [wireEditor()],
    });
    expect(controller.remoteEditors()).toEqual([]);
  });
});

function createHarness(storage: Storage): {
  controller: CanvasAwarenessController;
  http: HttpTestingController;
  sources: FakeEventSource[];
} {
  const sources: FakeEventSource[] = [];
  TestBed.configureTestingModule({
    providers: [
      CanvasAwarenessController,
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: CANVAS_AWARENESS_SESSION_STORAGE, useValue: storage },
      {
        provide: CANVAS_AWARENESS_EVENT_SOURCE_FACTORY,
        useValue: (url: string, init: EventSourceInit) => {
          const source = new FakeEventSource(url, init);
          sources.push(source);
          return source;
        },
      },
    ],
  });
  return {
    controller: TestBed.inject(CanvasAwarenessController),
    http: TestBed.inject(HttpTestingController),
    sources,
  };
}

function wireEditor(
  overrides: Partial<{
    sender_id: string;
    editing_session_id: string;
    path: string;
    presentation_revision: number;
    source_version: string;
    sequence: number;
    ttl_ms: number;
  }> = {},
) {
  return {
    sender_id: 'remote-sender',
    editing_session_id: 'remote-tab-123',
    path: 'output/report.md',
    presentation_revision: 4,
    source_version: SOURCE_VERSION,
    sequence: 8,
    ttl_ms: 15_000,
    ...overrides,
  };
}

function mutationResponse(sequence: number, state: 'editing' | 'idle') {
  return {
    applied: true,
    sender_id: 'server-sender',
    sequence,
    state,
    expires_at: '2026-08-10T10:00:15Z',
  };
}
