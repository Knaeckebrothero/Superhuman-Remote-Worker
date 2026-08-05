import {HttpClient, HttpErrorResponse} from '@angular/common/http';
import {DestroyRef, Injectable, inject, signal} from '@angular/core';
import {DomSanitizer, SafeResourceUrl} from '@angular/platform-browser';
import {Subscription} from 'rxjs';
import {
  CanvasState,
  CanvasViewAttachment,
  CanvasViewAttachmentRenewal,
  CanvasViewAuthorization,
  MAIN_CANVAS_ID,
} from '../../core/models/canvas.model';
import {environment} from '../../core/environment';
import {CanvasService} from '../../core/services/canvas.service';
import {canvasSourceKey, resolveCanvasViewerBootstrapUrl} from './canvas-rendering';
import {
  CanvasBootstrapChallengeMessage,
  CanvasBootstrapErrorMessage,
  CanvasBootstrapReadyMessage,
  canvasBootstrapAuthorizeMessage,
  isCanonicalCanvasUuid,
  isCanvasOpaqueValue,
  parseCanvasBootstrapMessage,
  parseCanvasViewAuthorization,
} from './canvas-viewer-protocol';

export type CanvasViewerStatus = 'idle' | 'loading' | 'ready' | 'renewing' | 'error';

interface DesiredViewer {
  threadId: string;
  sourceKey: string;
  revision: number;
  stateEtag: string;
}

interface ActiveAttachment extends DesiredViewer {
  attachmentId: string;
  origin: string;
  safeBootstrapUrl: SafeResourceUrl;
  bridgeNonce: string | null;
  bootstrapExpiresAt: number;
  expiresAt: number;
  renewAfter: number;
  handshakePhase: HandshakePhase;
  challenge: string | null;
  readyReceipt: string | null;
}

type HandshakePhase =
  | 'awaiting_frame'
  | 'awaiting_challenge'
  | 'authorizing'
  | 'awaiting_ready'
  | 'complete';

const MIN_RENEW_DELAY_MS = 1_000;

/** Pane-local lifecycle for one isolated live-app iframe attachment. */
@Injectable()
export class CanvasViewerController {
  private readonly http = inject(HttpClient);
  private readonly canvas = inject(CanvasService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly destroyRef = inject(DestroyRef);

  readonly frameUrl = signal<SafeResourceUrl | null>(null);
  readonly frameOrigin = signal<string | null>(null);
  readonly viewerStatus = signal<CanvasViewerStatus>('idle');
  readonly viewerErrorCode = signal<string | null>(null);

  private desired: DesiredViewer | null = null;
  private failedDesired: DesiredViewer | null = null;
  private attachment: ActiveAttachment | null = null;
  private request: Subscription | null = null;
  private requestToken: symbol | null = null;
  private requestDesired: DesiredViewer | null = null;
  private authorizationRequest: Subscription | null = null;
  private authorizationToken: symbol | null = null;
  private frameWindow: WindowProxy | null = null;
  private renewTimer: ReturnType<typeof setTimeout> | null = null;
  private expiryTimer: ReturnType<typeof setTimeout> | null = null;
  private handshakeTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => this.reset(true));
  }

  syncPresentation(
    active: boolean,
    threadId: string | null,
    state: CanvasState | null,
    stateEtag: string | null,
  ): void {
    const desired = this.toDesired(active, threadId, state, stateEtag);
    if (!desired) {
      this.desired = null;
      this.reset(true);
      return;
    }
    this.desired = desired;

    const attachment = this.attachment;
    if (attachment) {
      if (!sameViewer(attachment, desired)) {
        this.reset(true);
        this.desired = desired;
        this.createAttachment(desired);
      } else if (attachment.revision !== desired.revision) {
        this.renewAttachment(desired);
      }
      return;
    }

    if (this.request && this.requestDesired && sameDesired(this.requestDesired, desired)) return;
    // One rejected create must not become a request storm. This effect re-runs
    // for every reconciled state — including the unchanged state that the
    // forced reconcile after a failure brings back — so a create that already
    // failed for exactly this state waits for a state that actually differs,
    // or for the explicit retry.
    if (this.failedDesired && sameDesired(this.failedDesired, desired)) return;
    this.cancelRequest();
    this.createAttachment(desired);
  }

  /** Retry the current trusted attachment flow without ever opening the app top-level. */
  retry(): void {
    const desired = this.desired;
    if (!desired || this.viewerStatus() === 'loading' || this.viewerStatus() === 'renewing') {
      return;
    }
    this.reset(true);
    this.desired = desired;
    this.createAttachment(desired);
  }

  /** Bind the exact WindowProxy before the renderer mounts the remote URL. */
  bindFrame(frame: WindowProxy): void {
    const attachment = this.attachment;
    if (!attachment) return;
    if (this.frameWindow && this.frameWindow !== frame) return;
    this.frameWindow = frame;
    if (attachment.handshakePhase === 'awaiting_frame') {
      attachment.handshakePhase = 'awaiting_challenge';
    }
  }

  /** Ignore a stale renderer teardown after a replacement has already mounted. */
  unbindFrame(frame: WindowProxy): void {
    if (this.frameWindow !== frame) return;
    this.frameWindow = null;
    if (this.attachment) {
      this.failActiveAttachment('canvas_viewer_frame_detached');
    }
  }

  /** Receive only events pre-filtered by the renderer's exact WindowProxy. */
  handleFrameMessage(event: MessageEvent<unknown>): void {
    const attachment = this.attachment;
    if (
      !attachment ||
      attachment.handshakePhase === 'complete' ||
      !this.frameWindow ||
      event.source !== this.frameWindow ||
      event.origin !== attachment.origin
    ) {
      return;
    }
    const message = parseCanvasBootstrapMessage(event.data);
    if (!message || message.attachment_id !== attachment.attachmentId) return;

    switch (message.type) {
      case 'challenge':
        this.handleChallenge(attachment, message);
        break;
      case 'ready':
        this.handleReady(attachment, message);
        break;
      case 'error':
        this.handleBootstrapError(attachment, message);
        break;
    }
  }

  private toDesired(
    active: boolean,
    threadId: string | null,
    state: CanvasState | null,
    stateEtag: string | null,
  ): DesiredViewer | null {
    if (
      !active ||
      !threadId ||
      !stateEtag ||
      state?.source?.type !== 'workspace_app' ||
      state.capabilities.can_create_viewer_session !== true ||
      (state.status !== 'ready' && state.status !== 'starting')
    ) {
      return null;
    }
    const sourceKey = canvasSourceKey(state);
    return sourceKey
      ? {threadId, sourceKey, revision: state.presentation_revision, stateEtag}
      : null;
  }

  private createAttachment(desired: DesiredViewer): void {
    if (!environment.canvasViewerHostSuffix) {
      this.fail('canvas_viewer_not_configured');
      return;
    }
    const token = Symbol('canvas-view-attachment-create');
    this.requestToken = token;
    this.requestDesired = desired;
    this.viewerStatus.set('loading');
    this.viewerErrorCode.set(null);
    const request = this.http.post<CanvasViewAttachment>(
      this.attachmentsUrl(desired.threadId),
      null,
      {headers: {'If-Match': desired.stateEtag}},
    ).subscribe({
      next: response => this.applyCreatedAttachment(token, desired, response),
      error: error => this.handleCreateError(token, error),
      complete: () => this.completeRequest(token),
    });
    this.retainRequest(token, request);
  }

  private applyCreatedAttachment(
    token: symbol,
    desired: DesiredViewer,
    response: CanvasViewAttachment,
  ): void {
    if (!this.isCurrentRequest(token, desired)) {
      if (validAttachmentId(response?.attachment_id)) {
        this.closeRemote(desired.threadId, response.attachment_id);
      }
      return;
    }
    const timing = parseTiming(response);
    const bootstrapExpiresAt = parseFutureTimestamp(response?.bootstrap_expires_at);
    const bootstrapUrl = resolveCanvasViewerBootstrapUrl(
      response?.bootstrap_url,
      response?.origin,
      response?.attachment_id,
    );
    if (
      !validAttachmentId(response?.attachment_id) ||
      !timing ||
      !bootstrapExpiresAt ||
      bootstrapExpiresAt > timing.expiresAt ||
      !isCanvasOpaqueValue(response?.bridge_nonce) ||
      !bootstrapUrl
    ) {
      if (validAttachmentId(response?.attachment_id)) {
        this.closeRemote(desired.threadId, response.attachment_id);
      }
      this.cancelRequest();
      this.fail('invalid_view_attachment');
      return;
    }

    const safeBootstrapUrl = this.sanitizer.bypassSecurityTrustResourceUrl(bootstrapUrl);
    this.attachment = {
      ...desired,
      attachmentId: response.attachment_id,
      origin: response.origin,
      safeBootstrapUrl,
      bridgeNonce: response.bridge_nonce,
      bootstrapExpiresAt,
      handshakePhase: 'awaiting_frame',
      challenge: null,
      readyReceipt: null,
      ...timing,
    };
    this.frameUrl.set(safeBootstrapUrl);
    this.frameOrigin.set(response.origin);
    this.viewerStatus.set('loading');
    this.viewerErrorCode.set(null);
    this.scheduleTimers(this.attachment);
    this.scheduleHandshakeTimer(this.attachment);
  }

  private handleChallenge(
    attachment: ActiveAttachment,
    message: CanvasBootstrapChallengeMessage,
  ): void {
    if (attachment.handshakePhase !== 'awaiting_challenge' || !attachment.bridgeNonce) {
      return;
    }
    attachment.challenge = message.challenge;
    attachment.readyReceipt = message.ready_receipt;
    attachment.handshakePhase = 'authorizing';

    const token = Symbol('canvas-view-attachment-authorize');
    this.authorizationToken = token;
    const request = this.http.post<CanvasViewAuthorization>(
      `${this.attachmentsUrl(attachment.threadId)}/${encodeURIComponent(attachment.attachmentId)}/authorize`,
      {
        challenge: message.challenge,
        ready_receipt: message.ready_receipt,
        bridge_nonce: attachment.bridgeNonce,
      },
    ).subscribe({
      next: response => this.applyAuthorization(token, attachment, response),
      error: error => this.handleAuthorizationError(token, attachment, error),
      complete: () => this.completeAuthorization(token),
    });
    this.retainAuthorization(token, request);
  }

  private applyAuthorization(
    token: symbol,
    attachment: ActiveAttachment,
    response: CanvasViewAuthorization,
  ): void {
    if (
      this.authorizationToken !== token ||
      this.attachment !== attachment ||
      attachment.handshakePhase !== 'authorizing'
    ) {
      return;
    }
    const authorization = parseCanvasViewAuthorization(response);
    const authorizationExpiresAt = parseFutureTimestamp(authorization?.expires_at);
    if (
      !authorization ||
      !authorizationExpiresAt ||
      authorizationExpiresAt > attachment.bootstrapExpiresAt ||
      authorization.challenge !== attachment.challenge ||
      authorization.ready_receipt !== attachment.readyReceipt ||
      !this.frameWindow
    ) {
      this.cancelAuthorization();
      this.failActiveAttachment('invalid_view_attachment_authorization');
      return;
    }

    attachment.handshakePhase = 'awaiting_ready';
    // The bridge proof has served its only purpose. Never retain it for the
    // lifetime of the untrusted application document.
    attachment.bridgeNonce = null;
    try {
      this.frameWindow.postMessage(
        canvasBootstrapAuthorizeMessage(
          attachment.attachmentId,
          authorization.challenge,
          authorization.exchange_code,
        ),
        attachment.origin,
      );
    } catch {
      this.cancelAuthorization();
      this.failActiveAttachment('canvas_viewer_authorize_failed');
    }
  }

  private handleReady(
    attachment: ActiveAttachment,
    message: CanvasBootstrapReadyMessage,
  ): void {
    if (
      attachment.handshakePhase !== 'awaiting_ready' ||
      message.challenge !== attachment.challenge ||
      message.ready_receipt !== attachment.readyReceipt
    ) {
      return;
    }
    this.cancelAuthorization();
    this.clearHandshakeTimer();
    attachment.handshakePhase = 'complete';
    attachment.bridgeNonce = null;
    attachment.challenge = null;
    attachment.readyReceipt = null;
    this.viewerStatus.set('ready');
    this.viewerErrorCode.set(null);
  }

  private handleBootstrapError(
    attachment: ActiveAttachment,
    message: CanvasBootstrapErrorMessage,
  ): void {
    // Error messages are not trusted merely because they use the Canvas
    // channel. They may end this handshake only after matching every value
    // from the active gateway challenge.
    if (
      !attachment.challenge ||
      !attachment.readyReceipt ||
      message.challenge !== attachment.challenge ||
      message.ready_receipt !== attachment.readyReceipt
    ) {
      return;
    }
    this.failActiveAttachment(
      message.code === 'canvas_browser_storage_unavailable'
        ? 'canvas_browser_storage_unavailable'
        : 'canvas_viewer_bootstrap_failed',
    );
  }

  private handleAuthorizationError(
    token: symbol,
    attachment: ActiveAttachment,
    error: unknown,
  ): void {
    if (this.authorizationToken !== token || this.attachment !== attachment) return;
    const status = httpStatus(error);
    this.cancelAuthorization();
    if (status === 401 || status === 403 || status === 404 || status === 409 || status === 410) {
      this.canvas.reconcile();
    }
    this.failActiveAttachment('canvas_viewer_authorize_failed');
  }

  private renewAttachment(desired: DesiredViewer): void {
    const attachment = this.attachment;
    if (!attachment || !sameViewer(attachment, desired)) return;
    if (this.request && this.requestDesired && sameDesired(this.requestDesired, desired)) return;
    this.cancelRequest();
    // Keep the hard-expiry timer alive while renewal is in flight. A stalled
    // renewal must never leave an expired bootstrap mounted indefinitely.
    this.clearRenewTimer();

    const token = Symbol('canvas-view-attachment-renew');
    this.requestToken = token;
    this.requestDesired = desired;
    this.viewerStatus.set('renewing');
    const request = this.http.post<CanvasViewAttachmentRenewal>(
      `${this.attachmentsUrl(desired.threadId)}/${encodeURIComponent(attachment.attachmentId)}/renew`,
      null,
    ).subscribe({
      next: response => this.applyRenewal(token, desired, attachment, response),
      error: error => this.handleRenewError(token, desired, attachment, error),
      complete: () => this.completeRequest(token),
    });
    this.retainRequest(token, request);
  }

  private applyRenewal(
    token: symbol,
    desired: DesiredViewer,
    attachment: ActiveAttachment,
    response: CanvasViewAttachmentRenewal,
  ): void {
    if (!this.isCurrentRequest(token, desired) || this.attachment !== attachment) return;
    const timing = parseTiming(response);
    if (!timing) {
      this.handleRenewFailure(desired, attachment, 'invalid_view_attachment_renewal', false);
      return;
    }
    Object.assign(attachment, desired, timing);
    this.viewerStatus.set(
      attachment.handshakePhase === 'complete' ? 'ready' : 'loading',
    );
    this.viewerErrorCode.set(null);
    this.scheduleTimers(attachment);
  }

  private handleCreateError(token: symbol, error: unknown): void {
    if (this.requestToken !== token) return;
    const failed = this.requestDesired;
    const status = httpStatus(error);
    const code = httpErrorCode(error);
    this.cancelRequest();
    this.failedDesired = failed;
    if (status === 412 || status === 401 || status === 403 || status === 404) {
      this.canvas.reconcile();
    }
    this.fail(
      code === 'canvas_browser_unsupported'
        ? 'canvas_browser_unsupported'
        : 'canvas_viewer_create_failed',
    );
  }

  private handleRenewError(
    token: symbol,
    desired: DesiredViewer,
    attachment: ActiveAttachment,
    error: unknown,
  ): void {
    if (this.requestToken !== token || this.attachment !== attachment) return;
    const status = httpStatus(error);
    if (status === 412 || status === 401 || status === 403 || status === 404) {
      this.canvas.reconcile();
    }
    this.handleRenewFailure(
      desired,
      attachment,
      'canvas_viewer_renew_failed',
      status === 404 || status === 409 || status === 410 || status === 412,
    );
  }

  private handleRenewFailure(
    desired: DesiredViewer,
    attachment: ActiveAttachment,
    code: string,
    recreate: boolean,
  ): void {
    this.cancelRequest();
    if (this.attachment !== attachment) return;
    this.cancelAuthorization();
    this.attachment = null;
    this.frameWindow = null;
    this.clearTimers();
    this.frameUrl.set(null);
    this.frameOrigin.set(null);
    this.closeRemote(attachment.threadId, attachment.attachmentId);
    this.fail(code);
    if (recreate && this.desired && sameDesired(this.desired, desired)) {
      this.createAttachment(this.desired);
    }
  }

  private scheduleTimers(attachment: ActiveAttachment): void {
    this.clearLeaseTimers();
    const now = Date.now();
    this.renewTimer = setTimeout(
      () => {
        if (this.attachment !== attachment || !this.desired) return;
        this.renewAttachment(this.desired);
      },
      Math.max(MIN_RENEW_DELAY_MS, attachment.renewAfter - now),
    );
    this.expiryTimer = setTimeout(
      () => {
        if (this.attachment !== attachment) return;
        this.cancelRequest();
        this.cancelAuthorization();
        this.clearTimers();
        this.attachment = null;
        this.frameWindow = null;
        this.frameUrl.set(null);
        this.frameOrigin.set(null);
        this.closeRemote(attachment.threadId, attachment.attachmentId);
        this.fail('canvas_viewer_expired');
      },
      Math.max(MIN_RENEW_DELAY_MS, attachment.expiresAt - now),
    );
  }

  private reset(closeRemote: boolean): void {
    this.cancelRequest();
    this.cancelAuthorization();
    this.clearTimers();
    this.failedDesired = null;
    const attachment = this.attachment;
    this.attachment = null;
    this.frameWindow = null;
    this.frameUrl.set(null);
    this.frameOrigin.set(null);
    this.viewerStatus.set('idle');
    this.viewerErrorCode.set(null);
    if (closeRemote && attachment) {
      this.closeRemote(attachment.threadId, attachment.attachmentId);
    }
  }

  private fail(code: string): void {
    this.viewerStatus.set('error');
    this.viewerErrorCode.set(code);
  }

  private failActiveAttachment(code: string): void {
    const attachment = this.attachment;
    if (!attachment) {
      this.fail(code);
      return;
    }
    this.cancelRequest();
    this.cancelAuthorization();
    this.clearTimers();
    this.attachment = null;
    this.frameWindow = null;
    this.frameUrl.set(null);
    this.frameOrigin.set(null);
    this.closeRemote(attachment.threadId, attachment.attachmentId);
    this.fail(code);
  }

  private attachmentsUrl(threadId: string): string {
    return (
      `${environment.apiUrl}/persistent/threads/${encodeURIComponent(threadId)}` +
      `/canvases/${MAIN_CANVAS_ID}/view-attachments`
    );
  }

  private closeRemote(threadId: string, attachmentId: string): void {
    this.http.delete(
      `${this.attachmentsUrl(threadId)}/${encodeURIComponent(attachmentId)}`,
    ).subscribe({error: () => undefined});
  }

  private retainRequest(token: symbol, request: Subscription): void {
    if (this.requestToken === token) this.request = request;
    else request.unsubscribe();
  }

  private completeRequest(token: symbol): void {
    if (this.requestToken !== token) return;
    this.requestToken = null;
    this.requestDesired = null;
    this.request = null;
  }

  private cancelRequest(): void {
    this.requestToken = null;
    this.requestDesired = null;
    this.request?.unsubscribe();
    this.request = null;
  }

  private retainAuthorization(token: symbol, request: Subscription): void {
    if (this.authorizationToken === token) this.authorizationRequest = request;
    else request.unsubscribe();
  }

  private completeAuthorization(token: symbol): void {
    if (this.authorizationToken !== token) return;
    this.authorizationToken = null;
    this.authorizationRequest = null;
  }

  private cancelAuthorization(): void {
    this.authorizationToken = null;
    this.authorizationRequest?.unsubscribe();
    this.authorizationRequest = null;
  }

  private clearTimers(): void {
    this.clearLeaseTimers();
    this.clearHandshakeTimer();
  }

  private clearLeaseTimers(): void {
    this.clearRenewTimer();
    if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
    this.expiryTimer = null;
  }

  private scheduleHandshakeTimer(attachment: ActiveAttachment): void {
    this.clearHandshakeTimer();
    this.handshakeTimer = setTimeout(
      () => {
        if (this.attachment !== attachment || attachment.handshakePhase === 'complete') return;
        this.failActiveAttachment('canvas_viewer_bootstrap_expired');
      },
      Math.max(MIN_RENEW_DELAY_MS, attachment.bootstrapExpiresAt - Date.now()),
    );
  }

  private clearHandshakeTimer(): void {
    if (this.handshakeTimer !== null) clearTimeout(this.handshakeTimer);
    this.handshakeTimer = null;
  }

  private clearRenewTimer(): void {
    if (this.renewTimer !== null) clearTimeout(this.renewTimer);
    this.renewTimer = null;
  }

  private isCurrentRequest(token: symbol, desired: DesiredViewer): boolean {
    return this.requestToken === token &&
      this.desired !== null &&
      sameDesired(this.desired, desired);
  }
}

function sameViewer(left: DesiredViewer, right: DesiredViewer): boolean {
  return left.threadId === right.threadId && left.sourceKey === right.sourceKey;
}

function sameDesired(left: DesiredViewer, right: DesiredViewer): boolean {
  return sameViewer(left, right) &&
    left.revision === right.revision &&
    left.stateEtag === right.stateEtag;
}

function validAttachmentId(value: unknown): value is string {
  return isCanonicalCanvasUuid(value);
}

function parseTiming(
  response: CanvasViewAttachment | CanvasViewAttachmentRenewal | null | undefined,
): Pick<ActiveAttachment, 'expiresAt' | 'renewAfter'> | null {
  if (!response || typeof response.expires_at !== 'string' || typeof response.renew_after !== 'string') {
    return null;
  }
  const expiresAt = Date.parse(response.expires_at);
  const renewAfter = Date.parse(response.renew_after);
  if (
    !Number.isFinite(expiresAt) ||
    !Number.isFinite(renewAfter) ||
    expiresAt <= Date.now() ||
    renewAfter >= expiresAt
  ) {
    return null;
  }
  return {expiresAt, renewAfter};
}

function httpStatus(error: unknown): number | null {
  return error instanceof HttpErrorResponse ? error.status || null : null;
}

function httpErrorCode(error: unknown): string | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  const payload = error.error;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const detail = (payload as Record<string, unknown>)['detail'];
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
  const code = (detail as Record<string, unknown>)['code'];
  return typeof code === 'string' ? code : null;
}

function parseFutureTimestamp(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && timestamp > Date.now() ? timestamp : null;
}
