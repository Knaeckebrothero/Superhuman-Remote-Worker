import {HttpErrorResponse} from '@angular/common/http';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {of, Subject, throwError} from 'rxjs';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {CanvasMutationResponse, CanvasState} from '../../core/models/canvas.model';
import {CanvasService} from '../../core/services/canvas.service';
import {CanvasAwarenessController} from './canvas-awareness.controller';
import {CanvasEditController} from './canvas-edit.controller';

function contentUrl(revision: number, version = `sha256:${revision}`): string {
  return '/api/persistent/threads/thread-1/canvases/main/content' +
    `?presentation_revision=${revision}` +
    '&source_fingerprint=sha256%3Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' +
    `&source_version=${encodeURIComponent(version)}&ngsw-bypass=true`;
}

function editableState(
  revision: number,
  overrides: Partial<CanvasState> = {},
): CanvasState {
  const version = `sha256:${revision}`;
  return {
    canvas_id: 'main',
    source: {type: 'workspace_file', path: 'output/report.md'},
    title: 'Report',
    renderer: 'markdown',
    editable: true,
    alt_text: null,
    presentation_revision: revision,
    source_version: version,
    content_url: contentUrl(revision, version),
    status: 'ready',
    capabilities: {can_edit: true, can_pop_out: false, can_take_control: false},
    updated_at: `2026-07-13T10:00:0${revision}Z`,
    ...overrides,
  };
}

function mutation(state: CanvasState, contentEtag: string): CanvasMutationResponse {
  return {state, stateEtag: `"canvas:${state.presentation_revision}:state"`, contentEtag};
}

describe('CanvasEditController', () => {
  let controller: CanvasEditController;
  let awareness: {
    remoteEditing: ReturnType<typeof signal<boolean>>;
    sync: ReturnType<typeof vi.fn>;
    startEditing: ReturnType<typeof vi.fn>;
    stopEditing: ReturnType<typeof vi.fn>;
  };
  let canvas: {
    state: ReturnType<typeof signal<CanvasState | null>>;
    requestError: ReturnType<typeof signal<{status: number | null; code: string | null} | null>>;
    saveContent: ReturnType<typeof vi.fn>;
    refreshSource: ReturnType<typeof vi.fn>;
    reconcile: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    canvas = {
      state: signal<CanvasState | null>(editableState(1)),
      requestError: signal(null),
      saveContent: vi.fn(),
      refreshSource: vi.fn(),
      reconcile: vi.fn(),
    };
    awareness = {
      remoteEditing: signal(false),
      sync: vi.fn(),
      startEditing: vi.fn(),
      stopEditing: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        CanvasEditController,
        {provide: CanvasService, useValue: canvas},
        {provide: CanvasAwarenessController, useValue: awareness},
      ],
    });
    controller = TestBed.inject(CanvasEditController);
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('preserves a dirty buffer across republish, replacement, clear, and pane hiding', () => {
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    expect(controller.editorMounted()).toBe(true);
    controller.updateBuffer('# Local edit');

    const republished = editableState(2);
    canvas.state.set(republished);
    sync(republished, '# Agent edit', '"sha256:2"');
    expect(controller.buffer()).toBe('# Local edit');
    expect(controller.conflict()).toBe('presentation_changed');
    expect(controller.sessionState()?.presentation_revision).toBe(1);

    controller.sync(
      false,
      'thread-1',
      republished,
      null,
      '',
      null,
      false,
      false,
    );
    expect(controller.buffer()).toBe('# Local edit');
    expect(controller.dirty()).toBe(true);
    expect(controller.editorMounted()).toBe(true);

    const replacement = editableState(3, {
      source: {type: 'workspace_file', path: 'output/other.md'},
      title: 'Other file',
    });
    canvas.state.set(replacement);
    sync(replacement, '# Replacement', '"sha256:3"');
    expect(controller.conflict()).toBe('replaced');
    expect(controller.sessionPath()).toBe('output/report.md');

    const cleared = editableState(4, {
      source: null,
      title: null,
      renderer: 'auto',
      editable: false,
      source_version: null,
      content_url: null,
      status: 'cleared',
      capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    });
    canvas.state.set(cleared);
    sync(cleared, '', null, false);
    expect(controller.conflict()).toBe('cleared');
    expect(controller.buffer()).toBe('# Local edit');

    controller.sync(true, 'thread-2', null, null, '', null, false, false);
    expect(controller.hasSession()).toBe(false);
    expect(controller.buffer()).toBe('');
  });

  it('saves the captured bytes/revision and advances the clean baseline on success', () => {
    const next = editableState(2, {
      source_version: 'sha256:saved',
      content_url: contentUrl(2, 'sha256:saved'),
    });
    canvas.saveContent.mockReturnValue(of(mutation(next, '"sha256:saved"')));
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    controller.updateBuffer('# User edit');

    controller.save();

    expect(canvas.saveContent).toHaveBeenCalledWith({
      contentUrl: expect.stringContaining('presentation_revision=1'),
      contentEtag: '"sha256:1"',
      presentationRevision: 1,
      content: '# User edit',
    });
    expect(controller.saveStatus()).toBe('saved');
    expect(controller.dirty()).toBe(false);
    expect(controller.sessionState()?.presentation_revision).toBe(2);
  });

  it('preserves local bytes when a later presentation wins before save completion', () => {
    const saved = editableState(2, {
      source_version: 'sha256:saved',
      content_url: contentUrl(2, 'sha256:saved'),
    });
    canvas.saveContent.mockReturnValue(of(mutation(saved, '"sha256:saved"')));
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    controller.updateBuffer('# User edit');
    canvas.state.set(editableState(3));

    controller.save();

    expect(controller.saveStatus()).toBe('error');
    expect(controller.conflict()).toBe('presentation_changed');
    expect(controller.buffer()).toBe('# User edit');
    expect(controller.dirty()).toBe(true);
    expect(controller.sessionState()?.presentation_revision).toBe(1);
    expect(canvas.reconcile).toHaveBeenCalledOnce();
  });

  it('keeps an edit arriving during save dirty against the submitted baseline', () => {
    const response = new Subject<CanvasMutationResponse>();
    const saved = editableState(2, {
      source_version: 'sha256:submitted',
      content_url: contentUrl(2, 'sha256:submitted'),
    });
    canvas.saveContent.mockReturnValue(response);
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    controller.updateBuffer('# Submitted');

    controller.save();
    controller.updateBuffer('# Later composition');
    expect(controller.saveStatus()).toBe('saving');

    response.next(mutation(saved, '"sha256:submitted"'));
    response.complete();

    expect(canvas.saveContent).toHaveBeenCalledWith(expect.objectContaining({
      content: '# Submitted',
    }));
    expect(controller.sessionState()?.presentation_revision).toBe(2);
    expect(controller.buffer()).toBe('# Later composition');
    expect(controller.dirty()).toBe(true);
    expect(controller.saveStatus()).toBe('idle');
  });

  it('keeps local bytes and distinguishes stale content from presentation replacement', () => {
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    controller.updateBuffer('# User edit');
    canvas.saveContent.mockReturnValue(throwError(() => new HttpErrorResponse({
      status: 412,
      error: {detail: {code: 'canvas_content_precondition_failed'}},
    })));

    controller.save();

    expect(controller.conflict()).toBe('content_changed');
    expect(controller.buffer()).toBe('# User edit');
    expect(canvas.reconcile).toHaveBeenCalledOnce();

    canvas.saveContent.mockReturnValue(throwError(() => new HttpErrorResponse({
      status: 409,
      error: {detail: {code: 'canvas_replaced'}},
    })));
    controller.keepEditing();
    // A trusted state update clears only the transient error; the dirty bytes
    // remain the precondition source for this second request.
    controller.conflict.set(null);
    controller.save();
    expect(controller.conflict()).toBe('replaced');
    expect(controller.buffer()).toBe('# User edit');
  });

  it('conditionally adopts current workspace bytes without overwriting before GET completes', () => {
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    controller.updateBuffer('# Local');
    controller.sync(
      true,
      'thread-1',
      editableState(1, {status: 'source_changed'}),
      editableState(1),
      '# Original',
      '"sha256:1"',
      false,
      true,
    );
    const refreshed = editableState(2, {
      source_version: 'sha256:fresh',
      content_url: contentUrl(2, 'sha256:fresh'),
    });
    canvas.refreshSource.mockReturnValue(of(mutation(refreshed, '"sha256:fresh"')));

    controller.loadCurrentVersion();
    expect(controller.buffer()).toBe('# Local');
    expect(controller.refreshPending()).toBe(true);

    canvas.state.set(refreshed);
    sync(refreshed, '# Workspace version', '"sha256:fresh"');
    expect(controller.buffer()).toBe('# Workspace version');
    expect(controller.dirty()).toBe(false);
    expect(controller.conflict()).toBeNull();
    expect(controller.refreshPending()).toBe(false);
  });

  it('does not discard an edit made after load-current refresh starts', () => {
    const response = new Subject<CanvasMutationResponse>();
    const refreshed = editableState(2, {
      source_version: 'sha256:fresh',
      content_url: contentUrl(2, 'sha256:fresh'),
    });
    canvas.refreshSource.mockReturnValue(response);
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.enterEdit();
    controller.updateBuffer('# Local before refresh');
    controller.conflict.set('content_changed');

    controller.loadCurrentVersion();
    controller.updateBuffer('# Local after refresh click');
    response.next(mutation(refreshed, '"sha256:fresh"'));
    response.complete();
    canvas.state.set(refreshed);
    sync(refreshed, '# Workspace version', '"sha256:fresh"');

    expect(controller.buffer()).toBe('# Local after refresh click');
    expect(controller.dirty()).toBe(true);
    expect(controller.conflict()).toBe('content_changed');
    expect(controller.refreshPending()).toBe(false);

    // A second explicit destructive action adopts the already-loaded bytes.
    controller.loadCurrentVersion();
    expect(controller.buffer()).toBe('# Workspace version');
    expect(controller.dirty()).toBe(false);
  });

  it('offers the same conditional workspace refresh for read-only source drift', () => {
    const refreshed = editableState(2, {
      editable: false,
      capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    });
    canvas.refreshSource.mockReturnValue(of(mutation(refreshed, '"sha256:2"')));

    controller.refreshPresentedSource();

    expect(canvas.refreshSource).toHaveBeenCalledOnce();
    expect(controller.refreshPending()).toBe(false);
  });

  it('delegates focus and retained Canvas identity to lane-free awareness', () => {
    const state = editableState(1);
    sync(state, '# Original', '"sha256:1"');
    expect(awareness.sync).toHaveBeenLastCalledWith(
      true,
      'thread-1',
      state,
      false,
    );
    awareness.startEditing.mockClear();
    awareness.stopEditing.mockClear();

    controller.editorFocused();
    expect(awareness.startEditing).toHaveBeenCalledOnce();
    controller.editorBlurred();
    expect(awareness.stopEditing).toHaveBeenCalledOnce();

    awareness.remoteEditing.set(true);
    expect(controller.remoteEditing()).toBe(true);

    controller.sync(
      true,
      'thread-1',
      state,
      state,
      '# Original',
      '"sha256:1"',
      true,
      false,
      true,
    );
    expect(awareness.sync).toHaveBeenLastCalledWith(true, 'thread-1', state, true);
  });

  it('tears down authorized dirty bytes on terminal access loss', () => {
    sync(editableState(1), '# Original', '"sha256:1"');
    controller.updateBuffer('# Private local edit');
    canvas.requestError.set({status: 403, code: 'canvas_access_ended'});
    canvas.state.set(null);

    controller.sync(true, 'thread-1', null, null, '', null, false, false);

    expect(controller.hasSession()).toBe(false);
    expect(controller.buffer()).toBe('');
  });

  it('does not retain a clean editor session after clear or edit-capability revocation', () => {
    sync(editableState(1), '# Original', '"sha256:1"');
    const cleared = editableState(2, {
      source: null,
      renderer: 'auto',
      editable: false,
      source_version: null,
      content_url: null,
      status: 'cleared',
      capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    });
    canvas.state.set(cleared);
    sync(cleared, '', null, false);
    expect(controller.hasSession()).toBe(false);

    const restored = editableState(3);
    canvas.state.set(restored);
    sync(restored, '# Restored', '"sha256:3"');
    const revoked = editableState(4, {
      editable: false,
      capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    });
    canvas.state.set(revoked);
    sync(revoked, '# Restored', '"sha256:4"');
    expect(controller.hasSession()).toBe(false);
  });

  function sync(
    state: CanvasState,
    content: string,
    etag: string | null,
    ready = true,
  ): void {
    controller.sync(true, 'thread-1', state, state, content, etag, ready, false);
  }
});
