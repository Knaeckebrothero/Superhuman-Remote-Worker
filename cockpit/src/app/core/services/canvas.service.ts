import {HttpClient, HttpErrorResponse, HttpResponse} from '@angular/common/http';
import {DestroyRef, inject, Injectable, signal} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {map, Observable, Subscription, tap, throwError} from 'rxjs';
import {
  CanvasContentSaveRequest,
  CanvasLoadStatus,
  CanvasMutationResponse,
  CanvasRequestError,
  CanvasState,
  MAIN_CANVAS_ID,
} from '../models/canvas.model';
import {environment} from '../environment';
import {
  CanvasInvalidation,
  PersistentThreadTransportBridge,
} from './persistent-thread-transport-bridge.service';

/** Avoid a REST request on every focus bounce while still repairing missed SSE. */
export const CANVAS_RECONCILE_STALE_MS = 30_000;

/**
 * Authoritative Dynamic Canvas state for the selected persistent thread.
 *
 * Transport events are invalidations only. Every accepted invalidation is
 * reconciled through the REST representation (and its ETag); event payloads
 * never become Canvas state directly.
 */
@Injectable({providedIn: 'root'})
export class CanvasService {
  private readonly http = inject(HttpClient);
  private readonly transport = inject(PersistentThreadTransportBridge);
  private readonly destroyRef = inject(DestroyRef);

  readonly threadId = signal<string | null>(null);
  readonly state = signal<CanvasState | null>(null);
  readonly stateEtag = signal<string | null>(null);
  readonly loadStatus = signal<CanvasLoadStatus>('idle');
  readonly requestError = signal<CanvasRequestError | null>(null);

  private requestSubscription: Subscription | null = null;
  private activeRequestToken: symbol | null = null;
  private activeTargetRevision: number | null = null;
  private activeTargetFollowUpAllowed = true;
  private pendingRevision: number | null = null;
  private pendingRevisionFollowUpAllowed = false;
  private pendingForcedReconcile = false;
  private lastSuccessfulReconcileAt = 0;

  constructor() {
    this.transport.canvasInvalidations$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((event) => this.handleInvalidation(event));

    if (typeof window !== 'undefined' && typeof document !== 'undefined') {
      const reconcileIfVisible = () => {
        if (document.visibilityState !== 'visible') return;
        this.reconcileIfStale();
      };
      window.addEventListener('focus', reconcileIfVisible);
      document.addEventListener('visibilitychange', reconcileIfVisible);
      this.destroyRef.onDestroy(() => {
        window.removeEventListener('focus', reconcileIfVisible);
        document.removeEventListener('visibilitychange', reconcileIfVisible);
      });
    }

    this.destroyRef.onDestroy(() => this.cancelRequest());
  }

  /**
   * Select the route's thread. A null draft selection synchronously clears all
   * prior-thread state and cannot fall back to the last viewed Canvas.
   */
  selectThread(threadId: string | null): void {
    if (this.threadId() === threadId) {
      if (threadId) this.reconcileIfStale();
      return;
    }

    this.cancelRequest();
    this.pendingRevision = null;
    this.pendingRevisionFollowUpAllowed = false;
    this.pendingForcedReconcile = false;
    this.lastSuccessfulReconcileAt = 0;
    this.threadId.set(threadId);
    this.state.set(null);
    this.stateEtag.set(null);
    this.requestError.set(null);

    if (!threadId) {
      this.loadStatus.set('idle');
      return;
    }

    this.startReconcile(null);
  }

  /** Force an authoritative reload, coalescing with a request already running. */
  reconcile(): void {
    if (!this.threadId()) return;
    if (this.activeRequestToken) {
      this.pendingForcedReconcile = true;
      return;
    }
    this.startReconcile(null);
  }

  /**
   * Save UTF-8 source bytes through the server-issued content pointer.
   *
   * The caller must supply the ETag captured with those exact bytes and the
   * presentation revision they were loaded under. The backend revalidates both
   * before reading the body and returns the new authoritative state.
   */
  saveContent(request: CanvasContentSaveRequest): Observable<CanvasMutationResponse> {
    const threadId = this.threadId();
    if (!threadId) {
      return throwError(() => new Error('Cannot save Canvas content without a thread'));
    }
    return this.http.put<CanvasState>(request.contentUrl, request.content, {
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        'If-Match': request.contentEtag,
        'X-Canvas-Presentation-Revision': String(request.presentationRevision),
      },
      observe: 'response',
    }).pipe(
      map(response => this.parseMutationResponse(response)),
      tap(response => this.applyMutationResponse(threadId, response, true)),
    );
  }

  /** Adopt the current workspace bytes after an out-of-band source change. */
  refreshSource(): Observable<CanvasMutationResponse> {
    const threadId = this.threadId();
    const stateEtag = this.stateEtag();
    if (!threadId || !stateEtag) {
      return throwError(() => new Error('Cannot refresh Canvas without current state'));
    }
    const url =
      `${environment.apiUrl}/persistent/threads/${encodeURIComponent(threadId)}` +
      `/canvases/${MAIN_CANVAS_ID}/refresh`;
    return this.http.post<CanvasState>(url, null, {
      headers: {'If-Match': stateEtag},
      observe: 'response',
    }).pipe(
      map(response => this.parseMutationResponse(response)),
      tap(response => this.applyMutationResponse(threadId, response, true)),
    );
  }

  private handleInvalidation(event: CanvasInvalidation): void {
    if (event.threadId !== this.threadId() || event.canvasId !== MAIN_CANVAS_ID) return;

    const appliedRevision = this.state()?.presentation_revision ?? 0;
    if (
      event.presentationRevision !== null &&
      event.presentationRevision <= appliedRevision
    ) {
      return;
    }

    if (this.activeRequestToken) {
      if (event.presentationRevision === null) {
        // A direct reconcile_required frame exists specifically because the
        // durable event write failed. Ensure a request that may have started
        // before the state transition is followed by another authoritative GET.
        this.pendingForcedReconcile = true;
      } else {
        this.queuePendingRevision(event.presentationRevision, true);
      }
      return;
    }

    this.startReconcile(event.presentationRevision);
  }

  private reconcileIfStale(): void {
    if (!this.threadId() || this.activeRequestToken) return;
    if (
      this.lastSuccessfulReconcileAt > 0 &&
      Date.now() - this.lastSuccessfulReconcileAt < CANVAS_RECONCILE_STALE_MS
    ) {
      return;
    }
    this.startReconcile(null);
  }

  private startReconcile(
    targetRevision: number | null,
    targetFollowUpAllowed = true,
  ): void {
    const threadId = this.threadId();
    if (!threadId || this.activeRequestToken) return;

    const token = Symbol('canvas-state-request');
    this.activeRequestToken = token;
    this.activeTargetRevision = targetRevision;
    this.activeTargetFollowUpAllowed = targetFollowUpAllowed;
    this.loadStatus.set('loading');
    this.requestError.set(null);

    const url =
      `${environment.apiUrl}/persistent/threads/${encodeURIComponent(threadId)}` +
      `/canvases/${MAIN_CANVAS_ID}`;
    const subscription = this.http
      .get<CanvasState>(url, {observe: 'response'})
      .subscribe({
        next: (response) => this.applyResponse(token, threadId, response),
        error: (error: unknown) => this.failRequest(token, threadId, error),
        complete: () => this.completeRequest(token, threadId),
      });

    // HttpClient is asynchronous in production, but this guard also handles a
    // synchronous test double which completes during subscribe().
    if (this.activeRequestToken === token) {
      this.requestSubscription = subscription;
    } else {
      subscription.unsubscribe();
    }
  }

  private applyResponse(
    token: symbol,
    threadId: string,
    response: HttpResponse<CanvasState>,
  ): void {
    if (!this.isCurrentRequest(token, threadId)) return;

    if (response.status === 204 || response.body === null) {
      this.state.set(null);
      this.stateEtag.set(null);
      this.queueTargetFollowUpIfNeeded(0);
    } else if (isCanvasState(response.body)) {
      const currentRevision = this.state()?.presentation_revision ?? 0;
      if (response.body.presentation_revision >= currentRevision) {
        this.state.set(response.body);
        this.stateEtag.set(response.headers.get('ETag'));
      }
      this.queueTargetFollowUpIfNeeded(response.body.presentation_revision);
    } else {
      this.requestError.set({status: response.status, code: 'invalid_canvas_state'});
      this.loadStatus.set('error');
      return;
    }

    this.lastSuccessfulReconcileAt = Date.now();
    this.requestError.set(null);
    this.loadStatus.set('ready');
  }

  private failRequest(token: symbol, threadId: string, error: unknown): void {
    if (!this.isCurrentRequest(token, threadId)) return;
    this.activeRequestToken = null;
    this.activeTargetRevision = null;
    this.activeTargetFollowUpAllowed = true;
    this.requestSubscription = null;
    this.loadStatus.set('error');
    const requestError = toCanvasRequestError(error);
    this.requestError.set(requestError);

    if (isTerminalCanvasStateFailure(requestError.status)) {
      // Authorization loss and a missing thread are teardown boundaries, not
      // transient overlays. Do not leave bytes or an ETag from a state the
      // current browser may no longer inspect, and do not immediately replay a
      // queued invalidation against the same terminal response.
      this.state.set(null);
      this.stateEtag.set(null);
      this.pendingRevision = null;
      this.pendingRevisionFollowUpAllowed = false;
      this.pendingForcedReconcile = false;
      this.lastSuccessfulReconcileAt = Date.now();
      return;
    }

    this.drainPendingReconcile();
  }

  private completeRequest(token: symbol, threadId: string): void {
    if (!this.isCurrentRequest(token, threadId)) return;
    this.activeRequestToken = null;
    this.activeTargetRevision = null;
    this.activeTargetFollowUpAllowed = true;
    this.requestSubscription = null;
    this.drainPendingReconcile();
  }

  private drainPendingReconcile(): void {
    if (!this.threadId() || this.activeRequestToken) return;

    const appliedRevision = this.state()?.presentation_revision ?? 0;
    const pendingRevision = this.pendingRevision;
    const pendingRevisionFollowUpAllowed = this.pendingRevisionFollowUpAllowed;
    const force = this.pendingForcedReconcile;
    this.pendingRevision = null;
    this.pendingRevisionFollowUpAllowed = false;
    this.pendingForcedReconcile = false;

    if (force) {
      this.startReconcile(null);
    } else if (pendingRevision !== null && pendingRevision > appliedRevision) {
      this.startReconcile(pendingRevision, pendingRevisionFollowUpAllowed);
    }
  }

  private cancelRequest(): void {
    this.activeRequestToken = null;
    this.activeTargetRevision = null;
    this.activeTargetFollowUpAllowed = true;
    this.requestSubscription?.unsubscribe();
    this.requestSubscription = null;
  }

  private isCurrentRequest(token: symbol, threadId: string): boolean {
    return this.activeRequestToken === token && this.threadId() === threadId;
  }

  private queueTargetFollowUpIfNeeded(receivedRevision: number): void {
    const targetRevision = this.activeTargetRevision;
    if (
      targetRevision === null ||
      receivedRevision >= targetRevision ||
      !this.activeTargetFollowUpAllowed
    ) {
      return;
    }
    // One bounded follow-up protects against a GET that raced a committed
    // invalidation through a lagging read path. A second below-floor response
    // waits for the normal focus/reconnect reconciliation instead of looping.
    this.queuePendingRevision(targetRevision, false);
  }

  private queuePendingRevision(revision: number, allowTargetFollowUp: boolean): void {
    if (this.pendingRevision === null || revision > this.pendingRevision) {
      this.pendingRevision = revision;
      this.pendingRevisionFollowUpAllowed = allowTargetFollowUp;
    } else if (revision === this.pendingRevision) {
      this.pendingRevisionFollowUpAllowed ||= allowTargetFollowUp;
    }
  }

  private parseMutationResponse(response: HttpResponse<CanvasState>): CanvasMutationResponse {
    if (!isCanvasState(response.body)) {
      throw new Error('Canvas mutation returned invalid state');
    }
    const stateEtag = response.headers.get('ETag');
    const contentEtag = response.headers.get('X-Canvas-Content-ETag');
    const expectedContentEtag = response.body.source_version
      ? `"${response.body.source_version}"`
      : null;
    if (!stateEtag || !contentEtag || contentEtag !== expectedContentEtag) {
      throw new Error('Canvas mutation returned invalid precondition metadata');
    }
    return {state: response.body, stateEtag, contentEtag};
  }

  private applyMutationResponse(
    threadId: string,
    response: CanvasMutationResponse,
    notifyRuntime: boolean,
  ): void {
    if (this.threadId() !== threadId) return;
    const currentRevision = this.state()?.presentation_revision ?? 0;
    if (response.state.presentation_revision < currentRevision) return;
    this.state.set(response.state);
    this.stateEtag.set(response.stateEtag);
    this.loadStatus.set('ready');
    this.requestError.set(null);
    this.lastSuccessfulReconcileAt = Date.now();

    const source = response.state.source;
    if (
      !notifyRuntime ||
      source?.type !== 'workspace_file' ||
      typeof source.path !== 'string' ||
      !response.state.source_version
    ) {
      return;
    }
    this.transport.sendCanvasControl(threadId, {
      method: 'canvas.source_updated',
      canvas_id: MAIN_CANVAS_ID,
      path: source.path,
      presentation_revision: response.state.presentation_revision,
      source_version: response.state.source_version,
    });
  }
}

const CANVAS_RENDERERS = new Set(['auto', 'markdown', 'text', 'html', 'image']);
const CANVAS_STATUSES = new Set([
  'ready',
  'starting',
  'source_changed',
  'unavailable',
  'ended',
  'error',
  'cleared',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isCanvasState(value: unknown): value is CanvasState {
  if (!isRecord(value)) return false;
  const source = value['source'];
  const capabilities = value['capabilities'];
  return (
    value['canvas_id'] === MAIN_CANVAS_ID &&
    (source === null || (isRecord(source) && typeof source['type'] === 'string')) &&
    (value['title'] === null || typeof value['title'] === 'string') &&
    typeof value['renderer'] === 'string' &&
    CANVAS_RENDERERS.has(value['renderer']) &&
    typeof value['editable'] === 'boolean' &&
    (value['alt_text'] === null || typeof value['alt_text'] === 'string') &&
    typeof value['presentation_revision'] === 'number' &&
    Number.isSafeInteger(value['presentation_revision']) &&
    value['presentation_revision'] >= 0 &&
    (value['source_version'] === null || typeof value['source_version'] === 'string') &&
    (value['content_url'] === undefined ||
      value['content_url'] === null ||
      typeof value['content_url'] === 'string') &&
    typeof value['status'] === 'string' &&
    CANVAS_STATUSES.has(value['status']) &&
    isRecord(capabilities) &&
    typeof capabilities['can_edit'] === 'boolean' &&
    typeof capabilities['can_pop_out'] === 'boolean' &&
    typeof capabilities['can_take_control'] === 'boolean' &&
    typeof value['updated_at'] === 'string'
  );
}

function toCanvasRequestError(error: unknown): CanvasRequestError {
  if (!(error instanceof HttpErrorResponse)) return {status: null, code: null};
  const detail = isRecord(error.error) ? error.error['detail'] : null;
  const code =
    isRecord(detail) && typeof detail['code'] === 'string'
      ? detail['code']
      : isRecord(error.error) && typeof error.error['code'] === 'string'
        ? error.error['code']
        : null;
  return {status: error.status || null, code};
}

function isTerminalCanvasStateFailure(status: number | null): boolean {
  return status === 401 || status === 403 || status === 404;
}
