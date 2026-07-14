import {describe, expect, it, vi} from 'vitest';
import {PersistentThreadTransportBridge} from './persistent-thread-transport-bridge.service';

describe('PersistentThreadTransportBridge', () => {
  it('exposes decoded events without journal metadata and narrows Canvas invalidations', () => {
    const bridge = new PersistentThreadTransportBridge();
    const allEvents: unknown[] = [];
    const canvasEvents: unknown[] = [];
    bridge.events$.subscribe((event) => allEvents.push(event));
    bridge.canvasInvalidations$.subscribe((event) => canvasEvents.push(event));

    bridge.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {
        canvas_id: 'main',
        presentation_revision: 4,
        source_type: 'workspace_file',
        updated_at: '2026-07-13T10:00:00Z',
        _seq: [2, 9],
      },
    });

    expect(allEvents).toEqual([
      {
        threadId: 'thread-1',
        method: 'canvas.updated',
        params: {
          canvas_id: 'main',
          presentation_revision: 4,
          source_type: 'workspace_file',
          updated_at: '2026-07-13T10:00:00Z',
        },
      },
    ]);
    expect(canvasEvents).toEqual([
      {
        threadId: 'thread-1',
        method: 'canvas.updated',
        canvasId: 'main',
        presentationRevision: 4,
        sourceType: 'workspace_file',
        updatedAt: '2026-07-13T10:00:00Z',
      },
    ]);
  });

  it('turns a revisionless terminal frame into an unconditional main-Canvas reconcile', () => {
    const bridge = new PersistentThreadTransportBridge();
    const canvasEvents: unknown[] = [];
    bridge.canvasInvalidations$.subscribe((event) => canvasEvents.push(event));

    bridge.forwardEvent('thread-1', {method: 'canvas.reconcile_required'});

    expect(canvasEvents).toEqual([
      {
        threadId: 'thread-1',
        method: 'canvas.reconcile_required',
        canvasId: 'main',
        presentationRevision: null,
        sourceType: null,
        updatedAt: null,
      },
    ]);
  });

  it('does not route a future named Canvas into v1 main state', () => {
    const bridge = new PersistentThreadTransportBridge();
    const canvasEvents: unknown[] = [];
    bridge.canvasInvalidations$.subscribe((event) => canvasEvents.push(event));

    bridge.forwardEvent('thread-1', {
      method: 'canvas.updated',
      params: {canvas_id: 'secondary', presentation_revision: 1},
    });

    expect(canvasEvents).toEqual([]);
  });

  it('exposes validated Canvas editing awareness without treating it as state invalidation', () => {
    const bridge = new PersistentThreadTransportBridge();
    const awareness: unknown[] = [];
    const invalidations: unknown[] = [];
    bridge.canvasAwareness$.subscribe(event => awareness.push(event));
    bridge.canvasInvalidations$.subscribe(event => invalidations.push(event));

    bridge.forwardEvent('thread-1', {
      method: 'canvas.user_editing',
      params: {
        canvas_id: 'main',
        path: 'output/report.md',
        presentation_revision: 5,
        source_version: 'sha256:abc',
        editing_session_id: 'remote-tab-123',
        sender_id: 'socket-1',
        ttl_ms: 15000,
      },
    });

    expect(awareness).toEqual([{
      threadId: 'thread-1',
      method: 'canvas.user_editing',
      canvasId: 'main',
      path: 'output/report.md',
      presentationRevision: 5,
      sourceVersion: 'sha256:abc',
      editingSessionId: 'remote-tab-123',
      senderId: 'socket-1',
      ttlMs: 15000,
    }]);
    expect(invalidations).toEqual([]);

    bridge.forwardEvent('thread-1', {
      method: 'canvas.user_idle',
      params: {
        canvas_id: 'secondary',
        path: 'output/report.md',
        presentation_revision: 5,
        source_version: 'sha256:abc',
        editing_session_id: 'remote-tab-123',
        sender_id: 'socket-1',
      },
    });
    expect(awareness).toHaveLength(1);
  });

  it('accepts a disconnect idle without a meaningless TTL', () => {
    const bridge = new PersistentThreadTransportBridge();
    const awareness: unknown[] = [];
    bridge.canvasAwareness$.subscribe(event => awareness.push(event));

    bridge.forwardEvent('thread-1', {
      method: 'canvas.user_idle',
      params: {
        canvas_id: 'main',
        path: 'output/report.md',
        presentation_revision: 5,
        source_version: 'sha256:abc',
        editing_session_id: 'remote-tab-123',
        sender_id: 'socket-1',
      },
    });

    expect(awareness).toEqual([expect.objectContaining({
      method: 'canvas.user_idle',
      ttlMs: null,
    })]);
  });

  it('delegates only while the current transport owner is attached', () => {
    const bridge = new PersistentThreadTransportBridge();
    const sender = vi.fn().mockReturnValue(true);
    const detach = bridge.attachControlSender(sender);
    const control = {
      method: 'canvas.source_updated' as const,
      canvas_id: 'main' as const,
      path: 'output/report.md',
      presentation_revision: 5,
      source_version: 'sha256:abc',
    };

    expect(bridge.sendCanvasControl('thread-1', control)).toBe(true);
    expect(sender).toHaveBeenCalledWith('thread-1', control);

    const presentationControl = {
      method: 'canvas.presentation_updated' as const,
      canvas_id: 'main' as const,
      presentation_revision: 6,
    };
    expect(bridge.sendCanvasControl('thread-1', presentationControl)).toBe(true);
    expect(sender).toHaveBeenCalledWith('thread-1', presentationControl);

    detach();
    expect(bridge.sendCanvasControl('thread-1', control)).toBe(false);
  });
});
