import {HttpClient, HttpErrorResponse} from '@angular/common/http';
import {DestroyRef, Injectable, inject, signal} from '@angular/core';
import {DomSanitizer, SafeResourceUrl} from '@angular/platform-browser';
import {Subscription} from 'rxjs';
import {
  CanvasState,
  CanvasViewAttachment,
  CanvasViewAttachmentRenewal,
  MAIN_CANVAS_ID,
} from '../../core/models/canvas.model';
import {environment} from '../../core/environment';
import {CanvasService} from '../../core/services/canvas.service';
import {canvasSourceKey, resolveCanvasViewerBootstrapUrl} from './canvas-rendering';

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
  expiresAt: number;
  renewAfter: number;
}

const ATTACHMENT_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
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
  private attachment: ActiveAttachment | null = null;
  private request: Subscription | null = null;
  private requestToken: symbol | null = null;
  private requestDesired: DesiredViewer | null = null;
  private renewTimer: ReturnType<typeof setTimeout> | null = null;
  private expiryTimer: ReturnType<typeof setTimeout> | null = null;

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
    this.cancelRequest();
    this.createAttachment(desired);
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
    const bootstrapUrl = resolveCanvasViewerBootstrapUrl(
      response?.bootstrap_url,
      response?.origin,
    );
    if (!validAttachmentId(response?.attachment_id) || !timing || !bootstrapUrl) {
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
      ...timing,
    };
    this.frameUrl.set(safeBootstrapUrl);
    this.frameOrigin.set(response.origin);
    this.viewerStatus.set('ready');
    this.viewerErrorCode.set(null);
    this.scheduleTimers(this.attachment);
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
    this.attachment = {...attachment, ...desired, ...timing};
    this.viewerStatus.set('ready');
    this.viewerErrorCode.set(null);
    this.scheduleTimers(this.attachment);
  }

  private handleCreateError(token: symbol, error: unknown): void {
    if (this.requestToken !== token) return;
    const status = httpStatus(error);
    this.cancelRequest();
    if (status === 412 || status === 401 || status === 403 || status === 404) {
      this.canvas.reconcile();
    }
    this.fail('canvas_viewer_create_failed');
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
    this.attachment = null;
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
    this.clearTimers();
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
        this.clearTimers();
        this.attachment = null;
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
    this.clearTimers();
    const attachment = this.attachment;
    this.attachment = null;
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

  private clearTimers(): void {
    this.clearRenewTimer();
    if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
    this.expiryTimer = null;
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
  return typeof value === 'string' && ATTACHMENT_ID_PATTERN.test(value);
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
