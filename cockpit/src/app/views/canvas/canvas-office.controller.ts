import {HttpClient, HttpErrorResponse} from '@angular/common/http';
import {DestroyRef, Injectable, inject, signal} from '@angular/core';
import {firstValueFrom, Subscription} from 'rxjs';
import {
  CanvasOfficeSession,
  CanvasState,
  MAIN_CANVAS_ID,
} from '../../core/models/canvas.model';
import {environment} from '../../core/environment';
import {canvasSourceKey, selectCanvasRenderer} from './canvas-rendering';

export type CanvasOfficeStatus = 'idle' | 'loading' | 'ready' | 'error';

interface DesiredOfficeSession {
  readonly threadId: string;
  readonly sourceKey: string;
  readonly revision: number;
  readonly stateEtag: string;
  readonly editable: boolean;
}

/** Pane-local lifecycle for a Collabora launch token and frame. */
@Injectable()
export class CanvasOfficeController {
  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  readonly session = signal<CanvasOfficeSession | null>(null);
  readonly officeOrigin = signal<string | null>(environment.canvasOfficeOrigin);
  readonly officeStatus = signal<CanvasOfficeStatus>('idle');
  readonly officeErrorCode = signal<string | null>(null);
  readonly modified = signal(false);
  readonly conflictCode = signal<string | null>(null);

  private desired: DesiredOfficeSession | null = null;
  private request: Subscription | null = null;
  private requestToken: symbol | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => this.reset());
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
      this.reset();
      return;
    }

    const origin = environment.canvasOfficeOrigin;
    this.officeOrigin.set(origin);
    if (!isCanonicalOrigin(origin)) {
      this.desired = desired;
      this.resetRequestAndSession();
      this.fail('canvas_office_not_configured');
      return;
    }

    if (
      this.desired &&
      sameOfficeDocument(this.desired, desired) &&
      (this.request !== null || this.session() !== null)
    ) {
      // A source revision bump belongs to the mounted editor's version-restore
      // protocol. Keep its WindowProxy and token session intact while exposing
      // the latest precondition to renewal or a fallback remount.
      this.desired = desired;
      return;
    }

    this.resetRequestAndSession();
    this.desired = desired;
    this.mint(desired, origin);
  }

  markDocumentLoaded(): void {
    if (!this.session() || !this.desired) return;
    this.officeStatus.set('ready');
    this.officeErrorCode.set(null);
  }

  markModified(modified: boolean): void {
    this.modified.set(modified);
  }

  markConflict(code: string): void {
    this.conflictCode.set(code);
  }

  readonly refreshToken = async (): Promise<CanvasOfficeSession | null> => {
    const desired = this.desired;
    const origin = this.officeOrigin();
    if (!desired || !isCanonicalOrigin(origin)) return null;
    try {
      const response = await firstValueFrom(
        this.http.post<CanvasOfficeSession>(
          officeSessionUrl(desired.threadId),
          null,
          {headers: {'If-Match': desired.stateEtag}},
        ),
      );
      if (!this.desired || !sameOfficeDocument(this.desired, desired)) return null;
      return parseOfficeSession(response, origin);
    } catch {
      return null;
    }
  };

  readonly reloadSession = (): void => {
    const desired = this.desired;
    const origin = this.officeOrigin();
    if (!desired || !isCanonicalOrigin(origin)) {
      this.fail('canvas_office_session_failed');
      return;
    }
    this.conflictCode.set(null);
    this.modified.set(false);
    this.resetRequestAndSession();
    this.desired = desired;
    this.mint(desired, origin);
  };

  retry(): void {
    if (this.officeStatus() === 'loading') return;
    this.reloadSession();
  }

  private toDesired(
    active: boolean,
    threadId: string | null,
    state: CanvasState | null,
    stateEtag: string | null,
  ): DesiredOfficeSession | null {
    if (
      !active ||
      !threadId ||
      !stateEtag ||
      state?.source?.type !== 'workspace_file' ||
      state.renderer !== 'office' ||
      state.capabilities.can_view_office !== true ||
      (state.editable && state.capabilities.can_edit !== true) ||
      state.status !== 'ready' ||
      selectCanvasRenderer(state) !== 'office'
    ) {
      return null;
    }
    const sourceKey = canvasSourceKey(state);
    return sourceKey
      ? {
          threadId,
          sourceKey,
          revision: state.presentation_revision,
          stateEtag,
          editable: state.editable,
        }
      : null;
  }

  private mint(desired: DesiredOfficeSession, origin: string): void {
    const token = Symbol('canvas-office-session');
    this.requestToken = token;
    this.officeStatus.set('loading');
    this.officeErrorCode.set(null);
    const request = this.http.post<CanvasOfficeSession>(
      officeSessionUrl(desired.threadId),
      null,
      {headers: {'If-Match': desired.stateEtag}},
    ).subscribe({
      next: response => {
        if (!this.isCurrent(token, desired)) return;
        const session = parseOfficeSession(response, origin);
        if (!session) {
          this.cancelRequest();
          this.session.set(null);
          this.fail('invalid_office_session');
          return;
        }
        this.session.set(session);
        this.officeStatus.set('loading');
        this.officeErrorCode.set(null);
      },
      error: error => this.handleError(token, error),
      complete: () => this.completeRequest(token),
    });
    if (this.requestToken === token) this.request = request;
    else request.unsubscribe();
  }

  private handleError(token: symbol, error: unknown): void {
    if (this.requestToken !== token) return;
    const code = officeErrorCode(error);
    this.cancelRequest();
    this.session.set(null);
    this.fail(code ?? 'canvas_office_session_failed');
  }

  private isCurrent(token: symbol, desired: DesiredOfficeSession): boolean {
    return this.requestToken === token &&
      this.desired !== null &&
      sameOfficeDocument(this.desired, desired);
  }

  private completeRequest(token: symbol): void {
    if (this.requestToken !== token) return;
    this.requestToken = null;
    this.request = null;
  }

  private cancelRequest(): void {
    this.requestToken = null;
    this.request?.unsubscribe();
    this.request = null;
  }

  private resetRequestAndSession(): void {
    this.cancelRequest();
    this.session.set(null);
  }

  private reset(): void {
    this.desired = null;
    this.resetRequestAndSession();
    this.officeStatus.set('idle');
    this.officeErrorCode.set(null);
    this.modified.set(false);
    this.conflictCode.set(null);
  }

  private fail(code: string): void {
    this.officeStatus.set('error');
    this.officeErrorCode.set(code);
  }
}

function sameOfficeDocument(
  left: DesiredOfficeSession,
  right: DesiredOfficeSession,
): boolean {
  return left.threadId === right.threadId &&
    left.sourceKey === right.sourceKey &&
    left.editable === right.editable;
}

function officeSessionUrl(threadId: string): string {
  return (
    `${environment.apiUrl}/persistent/threads/${encodeURIComponent(threadId)}` +
    `/canvases/${MAIN_CANVAS_ID}/office-session`
  );
}

function parseOfficeSession(
  value: unknown,
  expectedOrigin: string,
): CanvasOfficeSession | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  if (
    keys.length !== 4 ||
    !['WOPISrc', 'access_token', 'access_token_ttl', 'urlsrc']
      .every((key, index) => keys[index] === key) ||
    typeof record['urlsrc'] !== 'string' ||
    typeof record['WOPISrc'] !== 'string' ||
    typeof record['access_token'] !== 'string' ||
    !record['access_token'] ||
    record['access_token'].length > 16_384 ||
    typeof record['access_token_ttl'] !== 'number' ||
    !Number.isSafeInteger(record['access_token_ttl']) ||
    record['access_token_ttl'] < 1_000_000_000_000
  ) {
    return null;
  }

  try {
    const action = new URL(record['urlsrc']);
    const wopiSource = new URL(record['WOPISrc']);
    if (
      action.origin !== expectedOrigin ||
      action.username ||
      action.password ||
      action.hash ||
      !action.pathname.endsWith('/cool.html') ||
      action.searchParams.has('WOPISrc') ||
      (wopiSource.protocol !== 'https:' && wopiSource.protocol !== 'http:') ||
      wopiSource.username ||
      wopiSource.password ||
      wopiSource.search ||
      wopiSource.hash ||
      !/^\/wopi\/files\/[^/]+$/.test(wopiSource.pathname)
    ) {
      return null;
    }
  } catch {
    return null;
  }

  return {
    urlsrc: record['urlsrc'],
    WOPISrc: record['WOPISrc'],
    access_token: record['access_token'],
    access_token_ttl: record['access_token_ttl'],
  };
}

function isCanonicalOrigin(value: string | null): value is string {
  if (!value) return false;
  try {
    const parsed = new URL(value);
    return (
      parsed.origin === value &&
      parsed.pathname === '/' &&
      !parsed.search &&
      !parsed.hash &&
      !parsed.username &&
      !parsed.password &&
      (parsed.protocol === 'https:' || parsed.protocol === 'http:')
    );
  } catch {
    return false;
  }
}

function officeErrorCode(error: unknown): string | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  const detail = error.error?.detail;
  if (typeof detail === 'object' && detail !== null && typeof detail.code === 'string') {
    return detail.code;
  }
  return typeof detail === 'string' ? detail : null;
}
