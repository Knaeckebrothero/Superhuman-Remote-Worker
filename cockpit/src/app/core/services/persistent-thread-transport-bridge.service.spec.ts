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

    detach();
    expect(bridge.sendCanvasControl('thread-1', control)).toBe(false);
  });
});
