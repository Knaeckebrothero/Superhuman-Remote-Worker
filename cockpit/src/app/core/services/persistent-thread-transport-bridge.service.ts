import {Injectable, signal} from '@angular/core';
import {filter, map, Observable, Subject} from 'rxjs';
import {CanvasId, MAIN_CANVAS_ID} from '../models/canvas.model';

export interface PersistentThreadTransportEvent {
  readonly threadId: string;
  readonly method: string;
  readonly params: Readonly<Record<string, unknown>>;
}

export type CanvasInvalidationMethod =
  | 'canvas.updated'
  | 'canvas.cleared'
  | 'canvas.source_updated'
  | 'canvas.reconcile_required';

export interface CanvasInvalidation {
  readonly threadId: string;
  readonly method: CanvasInvalidationMethod;
  readonly canvasId: CanvasId;
  /** Null means "reconcile unconditionally" rather than trusting the frame. */
  readonly presentationRevision: number | null;
  readonly sourceType: string | null;
  readonly updatedAt: string | null;
}

/** Slice-2 control shape; the bridge exists in Slice 0 so transport ownership
 * does not have to move when conditional editing lands. */
export interface CanvasSourceUpdatedControl {
  readonly method: 'canvas.source_updated';
  readonly canvas_id: CanvasId;
  readonly path: string;
  readonly presentation_revision: number;
  readonly source_version: string;
}

/** A committed non-file Canvas mutation which other views must reconcile. */
export interface CanvasPresentationUpdatedControl {
  readonly method: 'canvas.presentation_updated';
  readonly canvas_id: CanvasId;
  readonly presentation_revision: number;
}

export interface CanvasUserEditingControl {
  readonly method: 'canvas.user_editing';
  readonly canvas_id: CanvasId;
  readonly path: string;
  readonly presentation_revision: number;
  readonly source_version: string;
  readonly editing_session_id: string;
}

export interface CanvasUserIdleControl {
  readonly method: 'canvas.user_idle';
  readonly canvas_id: CanvasId;
  readonly path: string;
  readonly presentation_revision: number;
  readonly source_version: string;
  readonly editing_session_id: string;
}

export type CanvasControl =
  | CanvasSourceUpdatedControl
  | CanvasPresentationUpdatedControl
  | CanvasUserEditingControl
  | CanvasUserIdleControl;

type CanvasControlSender = (threadId: string, control: CanvasControl) => boolean;

const CANVAS_METHODS = new Set<CanvasInvalidationMethod>([
  'canvas.updated',
  'canvas.cleared',
  'canvas.source_updated',
  'canvas.reconcile_required',
]);

export interface CanvasAwarenessEvent {
  readonly threadId: string;
  readonly method: 'canvas.user_editing' | 'canvas.user_idle';
  readonly canvasId: CanvasId;
  readonly path: string;
  readonly presentationRevision: number;
  readonly sourceVersion: string;
  readonly editingSessionId: string;
  readonly senderId: string;
  readonly ttlMs: number | null;
}

const CANVAS_AWARENESS_METHODS = new Set(['canvas.user_editing', 'canvas.user_idle']);

/**
 * Read-only seam over the transport still owned by PersistentChatService.
 *
 * This service intentionally owns neither an EventSource nor a WebSocket. The
 * chat transport forwards already-decoded events here and installs the one
 * narrow Canvas control sender. Canvas state remains in CanvasService.
 */
@Injectable({providedIn: 'root'})
export class PersistentThreadTransportBridge {
  private readonly eventSubject = new Subject<PersistentThreadTransportEvent>();
  private controlSender: CanvasControlSender | null = null;
  private controlSenderToken: symbol | null = null;

  readonly events$: Observable<PersistentThreadTransportEvent> =
    this.eventSubject.asObservable();

  /**
   * Whether the active thread's assistant turn is currently streaming.
   * Mirrored here by the chat transport owner so Canvas consumers (the
   * shared-browser baton automation) never depend on the chat service.
   */
  private readonly agentTurnActiveSignal = signal(false);
  readonly agentTurnActive = this.agentTurnActiveSignal.asReadonly();

  /** @internal Called only by the existing persistent-thread transport owner. */
  setAgentTurnActive(active: boolean): void {
    this.agentTurnActiveSignal.set(active);
  }

  readonly canvasInvalidations$: Observable<CanvasInvalidation> = this.events$.pipe(
    map((event) => this.asCanvasInvalidation(event)),
    filter((event): event is CanvasInvalidation => event !== null),
  );

  readonly canvasAwareness$: Observable<CanvasAwarenessEvent> = this.events$.pipe(
    map(event => this.asCanvasAwareness(event)),
    filter((event): event is CanvasAwarenessEvent => event !== null),
  );

  /** @internal Called only by the existing persistent-thread transport owner. */
  forwardEvent(
    threadId: string,
    frame: {readonly method: string; readonly params?: Record<string, unknown>},
  ): void {
    if (!threadId || typeof frame.method !== 'string' || !frame.method) return;

    // `_seq` is journal transport metadata, not part of the consumer contract.
    const params = {...(frame.params ?? {})};
    delete params['_seq'];
    this.eventSubject.next({threadId, method: frame.method, params});
  }

  /**
   * Install the control callback without transferring WebSocket lifecycle to
   * the bridge. The returned cleanup cannot detach a newer owner.
   */
  attachControlSender(sender: CanvasControlSender): () => void {
    const token = Symbol('persistent-thread-transport-owner');
    this.controlSenderToken = token;
    this.controlSender = sender;
    return () => {
      if (this.controlSenderToken !== token) return;
      this.controlSenderToken = null;
      this.controlSender = null;
    };
  }

  /** False means there is no matching live thread transport to accept it. */
  sendCanvasControl(threadId: string, control: CanvasControl): boolean {
    return this.controlSender?.(threadId, control) ?? false;
  }

  private asCanvasInvalidation(
    event: PersistentThreadTransportEvent,
  ): CanvasInvalidation | null {
    if (!CANVAS_METHODS.has(event.method as CanvasInvalidationMethod)) return null;

    const rawCanvasId = event.params['canvas_id'];
    // All ordinary invalidations must identify `main`. The terminal direct
    // recovery frame may omit it because v1 has only one Canvas.
    if (
      rawCanvasId !== MAIN_CANVAS_ID &&
      !(event.method === 'canvas.reconcile_required' && rawCanvasId == null)
    ) {
      return null;
    }

    const rawRevision = event.params['presentation_revision'];
    const presentationRevision =
      typeof rawRevision === 'number' &&
      Number.isSafeInteger(rawRevision) &&
      rawRevision >= 0
        ? rawRevision
        : null;
    const rawSourceType = event.params['source_type'];
    const rawUpdatedAt = event.params['updated_at'];

    return {
      threadId: event.threadId,
      method: event.method as CanvasInvalidationMethod,
      canvasId: MAIN_CANVAS_ID,
      presentationRevision,
      sourceType: typeof rawSourceType === 'string' ? rawSourceType : null,
      updatedAt: typeof rawUpdatedAt === 'string' ? rawUpdatedAt : null,
    };
  }

  private asCanvasAwareness(
    event: PersistentThreadTransportEvent,
  ): CanvasAwarenessEvent | null {
    if (!CANVAS_AWARENESS_METHODS.has(event.method)) return null;
    const canvasId = event.params['canvas_id'];
    const path = event.params['path'];
    const revision = event.params['presentation_revision'];
    const sourceVersion = event.params['source_version'];
    const editingSessionId = event.params['editing_session_id'];
    const senderId = event.params['sender_id'];
    const ttlMs = event.params['ttl_ms'];
    if (
      canvasId !== MAIN_CANVAS_ID ||
      typeof path !== 'string' ||
      !path ||
      typeof revision !== 'number' ||
      !Number.isSafeInteger(revision) ||
      revision < 1 ||
      typeof sourceVersion !== 'string' ||
      !sourceVersion ||
      typeof editingSessionId !== 'string' ||
      !editingSessionId ||
      editingSessionId.length > 128 ||
      typeof senderId !== 'string' ||
      !senderId ||
      senderId.length > 128 ||
      (event.method === 'canvas.user_editing' && !isAwarenessTtl(ttlMs)) ||
      (event.method === 'canvas.user_idle' && ttlMs !== undefined && !isAwarenessTtl(ttlMs))
    ) {
      return null;
    }
    return {
      threadId: event.threadId,
      method: event.method as CanvasAwarenessEvent['method'],
      canvasId: MAIN_CANVAS_ID,
      path,
      presentationRevision: revision,
      sourceVersion,
      editingSessionId,
      senderId,
      ttlMs: typeof ttlMs === 'number' ? ttlMs : null,
    };
  }
}

function isAwarenessTtl(value: unknown): value is number {
  return typeof value === 'number' &&
    Number.isSafeInteger(value) &&
    value >= 1_000 &&
    value <= 60_000;
}
