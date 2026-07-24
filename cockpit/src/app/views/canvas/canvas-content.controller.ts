import {HttpClient, HttpErrorResponse} from '@angular/common/http';
import {DestroyRef, Injectable, inject, signal} from '@angular/core';
import {Subscription} from 'rxjs';
import {CanvasState} from '../../core/models/canvas.model';
import {CanvasService} from '../../core/services/canvas.service';
import {
  CanvasTrustedRenderer,
  canvasSourceKey,
  resolveCanvasContentUrl,
  selectCanvasRenderer,
} from './canvas-rendering';

export type CanvasContentStatus = 'idle' | 'loading' | 'ready' | 'error' | 'source_changed';

/**
 * Pane-local lifecycle for authorized file bytes. Kept separate from layout so
 * state/content races and Blob revocation are testable without rendering the
 * component tree (the repo's Vitest JIT does not resolve external styleUrls).
 */
@Injectable()
export class CanvasContentController {
  private readonly http = inject(HttpClient);
  private readonly canvas = inject(CanvasService);
  private readonly destroyRef = inject(DestroyRef);

  readonly displayRenderer = signal<CanvasTrustedRenderer>('unsupported');
  readonly displayState = signal<CanvasState | null>(null);
  readonly displaySourceKey = signal<string | null>(null);
  readonly textContent = signal('');
  readonly contentEtag = signal<string | null>(null);
  readonly imageUrl = signal<string | null>(null);
  readonly contentStatus = signal<CanvasContentStatus>('idle');
  readonly contentErrorCode = signal<string | null>(null);

  private request: Subscription | null = null;
  private requestToken: symbol | null = null;
  private loadedContentUrl: string | null = null;
  private displayedVisualKey: string | null = null;
  private imageObjectUrl: string | null = null;
  private active = false;

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.cancelRequest();
      this.revokeImageObjectUrl();
    });
  }

  syncPresentation(
    active: boolean,
    _threadId: string | null,
    state: CanvasState | null,
    readScrollTop: () => number = () => 0,
    restoreScrollTop: (value: number) => void = () => undefined,
  ): void {
    this.active = active;
    if (!active || !state?.source || state.status === 'cleared') {
      this.clearVisual();
      return;
    }

    const renderer = selectCanvasRenderer(state);
    const sourceKey = canvasSourceKey(state);
    const visualKey = sourceKey ? `${sourceKey}:${renderer}` : null;
    const sameVisual = visualKey !== null && visualKey === this.displayedVisualKey;

    if (state.status !== 'ready') {
      this.cancelRequest();
      if (!sameVisual) this.clearVisual();
      if (state.status === 'source_changed') this.contentStatus.set('source_changed');
      return;
    }

    // Live apps and Office own separate session lifecycles, not Canvas content
    // bytes. Office binaries must only flow through Collabora's WOPI GetFile.
    if (
      renderer === 'app' ||
      renderer === 'office' ||
      renderer === 'unsupported' ||
      !sourceKey ||
      !visualKey
    ) {
      this.clearVisual();
      return;
    }

    const resolvedUrl = resolveCanvasContentUrl(state.content_url);
    if (!resolvedUrl) {
      if (!sameVisual) this.clearVisual();
      this.contentStatus.set('error');
      this.contentErrorCode.set('invalid_content_url');
      return;
    }

    if (renderer === 'image') {
      this.loadImage(state, sourceKey, visualKey, resolvedUrl, sameVisual);
      return;
    }

    if (sameVisual && this.loadedContentUrl === resolvedUrl && this.contentStatus() === 'ready') {
      this.displayState.set(state);
      return;
    }

    const preservedScroll = sameVisual ? readScrollTop() : 0;
    this.cancelRequest();
    if (!sameVisual) this.clearVisual(false);
    this.contentStatus.set('loading');
    this.contentErrorCode.set(null);

    const token = Symbol('canvas-content-request');
    this.requestToken = token;
    const request = this.http.get(resolvedUrl, {
      responseType: 'text',
      observe: 'response',
    }).subscribe({
      next: response => {
        if (!this.isCurrentRequest(token, state, resolvedUrl)) return;
        this.displayRenderer.set(renderer);
        this.displayState.set(state);
        this.displaySourceKey.set(sourceKey);
        this.displayedVisualKey = visualKey;
        this.loadedContentUrl = resolvedUrl;
        this.textContent.set(response.body ?? '');
        this.contentEtag.set(response.headers.get('ETag'));
        this.imageUrl.set(null);
        this.contentStatus.set('ready');
        this.contentErrorCode.set(null);
        if (sameVisual) queueMicrotask(() => restoreScrollTop(preservedScroll));
      },
      error: (error: unknown) => this.handleContentError(token, state, sameVisual, error),
      complete: () => this.completeRequest(token),
    });
    this.retainRequest(token, request);
  }

  private loadImage(
    state: CanvasState,
    sourceKey: string,
    visualKey: string,
    resolvedUrl: string,
    sameVisual: boolean,
  ): void {
    if (sameVisual && this.loadedContentUrl === resolvedUrl && this.contentStatus() === 'ready') {
      this.displayState.set(state);
      return;
    }
    this.cancelRequest();
    if (!sameVisual) this.clearVisual(false);
    this.contentStatus.set('loading');
    this.contentErrorCode.set(null);

    const token = Symbol('canvas-image-request');
    this.requestToken = token;
    const request = this.http.get(resolvedUrl, {responseType: 'blob'}).subscribe({
      next: blob => {
        if (!this.isCurrentRequest(token, state, resolvedUrl)) return;
        const objectUrl = URL.createObjectURL(blob);
        this.revokeImageObjectUrl();
        this.imageObjectUrl = objectUrl;
        this.imageUrl.set(objectUrl);
        this.displayRenderer.set('image');
        this.displayState.set(state);
        this.displaySourceKey.set(sourceKey);
        this.displayedVisualKey = visualKey;
        this.loadedContentUrl = resolvedUrl;
        this.textContent.set('');
        this.contentEtag.set(null);
        this.contentStatus.set('ready');
        this.contentErrorCode.set(null);
      },
      error: (error: unknown) => this.handleContentError(token, state, sameVisual, error),
      complete: () => this.completeRequest(token),
    });
    this.retainRequest(token, request);
  }

  private retainRequest(token: symbol, request: Subscription): void {
    if (this.requestToken === token) this.request = request;
    else request.unsubscribe();
  }

  private completeRequest(token: symbol): void {
    if (this.requestToken !== token) return;
    this.requestToken = null;
    this.request = null;
  }

  private isCurrentRequest(token: symbol, state: CanvasState, url: string): boolean {
    return (
      this.requestToken === token &&
      this.active &&
      this.canvas.state() === state &&
      resolveCanvasContentUrl(state.content_url) === url
    );
  }

  private handleContentError(
    token: symbol,
    state: CanvasState,
    sameVisual: boolean,
    error: unknown,
  ): void {
    if (this.requestToken !== token || this.canvas.state() !== state) return;
    this.requestToken = null;
    this.request = null;
    const httpError = error instanceof HttpErrorResponse ? error : null;
    this.contentErrorCode.set(extractErrorCode(httpError));

    if (httpError?.status === 409) {
      this.contentStatus.set('source_changed');
      this.canvas.reconcile();
    } else if (httpError && [401, 403, 404].includes(httpError.status)) {
      this.clearVisual();
      this.contentStatus.set('error');
    } else {
      if (!sameVisual) this.clearVisual(false);
      this.contentStatus.set('error');
    }
  }

  private cancelRequest(): void {
    this.requestToken = null;
    this.request?.unsubscribe();
    this.request = null;
  }

  private clearVisual(cancel = true): void {
    if (cancel) this.cancelRequest();
    this.displayRenderer.set('unsupported');
    this.displayState.set(null);
    this.displaySourceKey.set(null);
    this.textContent.set('');
    this.contentEtag.set(null);
    this.revokeImageObjectUrl();
    this.imageUrl.set(null);
    this.contentStatus.set('idle');
    this.contentErrorCode.set(null);
    this.loadedContentUrl = null;
    this.displayedVisualKey = null;
  }

  private revokeImageObjectUrl(): void {
    if (!this.imageObjectUrl) return;
    URL.revokeObjectURL(this.imageObjectUrl);
    this.imageObjectUrl = null;
  }
}

function extractErrorCode(error: HttpErrorResponse | null): string | null {
  const detail = error?.error?.detail;
  if (typeof detail === 'object' && detail !== null && typeof detail.code === 'string') {
    return detail.code;
  }
  if (typeof detail === 'string') return detail;
  return null;
}
