import {HttpErrorResponse} from '@angular/common/http';
import {computed, DestroyRef, inject, Injectable, signal} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {Subscription} from 'rxjs';
import {CanvasMutationResponse, CanvasState, MAIN_CANVAS_ID} from '../../core/models/canvas.model';
import {CanvasService} from '../../core/services/canvas.service';
import {
  CanvasAwarenessEvent,
  PersistentThreadTransportBridge,
} from '../../core/services/persistent-thread-transport-bridge.service';
import {canvasSourceKey, CanvasTrustedRenderer, resolveCanvasContentUrl, selectCanvasRenderer} from './canvas-rendering';

export type CanvasEditConflict =
  | 'content_changed'
  | 'presentation_changed'
  | 'replaced'
  | 'cleared'
  | 'busy'
  | 'precondition_required'
  | 'save_failed';

export type CanvasEditSaveStatus = 'idle' | 'saving' | 'saved' | 'error';

interface CanvasEditSnapshot {
  readonly state: CanvasState;
  readonly sourceKey: string;
  readonly path: string;
  readonly renderer: Exclude<CanvasTrustedRenderer, 'image' | 'unsupported'>;
  readonly contentUrl: string;
  readonly contentEtag: string;
  readonly content: string;
}

const EDITING_RENEW_INTERVAL_MS = 5_000;

/** Pane-local optimistic edit state. Dirty buffers never become Canvas state. */
@Injectable()
export class CanvasEditController {
  private readonly canvas = inject(CanvasService);
  private readonly transport = inject(PersistentThreadTransportBridge);
  private readonly destroyRef = inject(DestroyRef);

  readonly editMode = signal(false);
  readonly editorMounted = signal(false);
  readonly buffer = signal('');
  readonly saveStatus = signal<CanvasEditSaveStatus>('idle');
  readonly conflict = signal<CanvasEditConflict | null>(null);
  readonly refreshPending = signal(false);
  readonly remoteEditing = signal(false);
  readonly sessionState = signal<CanvasState | null>(null);
  readonly sessionPath = computed(() => {
    const source = this.sessionState()?.source;
    return source?.type === 'workspace_file' && typeof source.path === 'string'
      ? source.path
      : null;
  });
  readonly sessionRenderer = computed(() => selectCanvasRenderer(this.sessionState()));
  readonly hasSession = computed(() => this.snapshot() !== null);
  readonly dirty = computed(() => {
    const snapshot = this.snapshot();
    return snapshot !== null && this.buffer() !== snapshot.content;
  });
  readonly canEnterEdit = computed(() => {
    const snapshot = this.snapshot();
    if (!snapshot) return false;
    if (this.dirty()) return true;
    const current = this.canvas.state();
    return !!(
      current?.editable &&
      current.capabilities.can_edit &&
      canvasSourceKey(current) === snapshot.sourceKey
    );
  });
  readonly canSave = computed(() =>
    this.dirty() &&
    this.saveStatus() !== 'saving' &&
    !isBlockingConflict(this.conflict()),
  );

  private readonly snapshot = signal<CanvasEditSnapshot | null>(null);
  private pendingRemote: CanvasEditSnapshot | null = null;
  private selectedThread: string | null = null;
  private active = false;
  private focused = false;
  private awarenessActive = false;
  private awarenessTimer: ReturnType<typeof setInterval> | null = null;
  private saveRequest: Subscription | null = null;
  private refreshRequest: Subscription | null = null;
  private savedTimer: ReturnType<typeof setTimeout> | null = null;
  private bufferGeneration = 0;
  private adoptAfterRefreshGeneration: number | null = null;
  private readonly editingSessionId = createEditingSessionId();
  private readonly remoteEditors = new Map<string, {
    event: CanvasAwarenessEvent;
    timer: ReturnType<typeof setTimeout>;
  }>();

  constructor() {
    this.transport.canvasAwareness$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(event => this.handleAwareness(event));
    this.destroyRef.onDestroy(() => {
      this.sendIdle();
      this.cancelRequests();
      if (this.savedTimer) clearTimeout(this.savedTimer);
      this.clearRemoteEditors();
    });
  }

  sync(
    active: boolean,
    threadId: string | null,
    state: CanvasState | null,
    displayState: CanvasState | null,
    content: string,
    contentEtag: string | null,
    contentReady: boolean,
    sourceChanged: boolean,
  ): void {
    if (threadId !== this.selectedThread) {
      this.sendIdle();
      this.cancelRequests();
      this.resetSession();
      this.selectedThread = threadId;
    }
    this.active = active;
    if (!active) this.sendIdle();

    if (
      this.snapshot() &&
      !state &&
      [401, 403, 404].includes(this.canvas.requestError()?.status ?? 0)
    ) {
      this.resetSession();
      return;
    }

    const existingSnapshot = this.snapshot();
    if (existingSnapshot && !this.dirty()) {
      const currentKey = canvasSourceKey(state);
      const editingRevoked = !!state &&
        state.presentation_revision !== existingSnapshot.state.presentation_revision &&
        (!state.editable || !state.capabilities.can_edit);
      if (
        !state?.source ||
        state.status === 'cleared' ||
        currentKey !== existingSnapshot.sourceKey ||
        editingRevoked
      ) {
        this.resetSession();
        return;
      }
    }

    if (this.snapshot() && sourceChanged) this.conflict.set('content_changed');
    this.pruneRemoteEditors();

    const snapshot = this.snapshot();
    if (snapshot && this.dirty()) {
      const currentKey = canvasSourceKey(state);
      if (!state?.source || state.status === 'cleared') {
        this.conflict.set('cleared');
      } else if (currentKey !== snapshot.sourceKey) {
        this.conflict.set('replaced');
      } else if (state.presentation_revision !== snapshot.state.presentation_revision) {
        this.conflict.set('presentation_changed');
      }
    }

    if (!contentReady || !displayState || !contentEtag) return;
    const candidate = snapshotFrom(displayState, content, contentEtag);
    if (!candidate) return;

    const currentSnapshot = this.snapshot();
    if (!currentSnapshot) {
      this.adopt(candidate);
      return;
    }
    if (sameSnapshot(candidate, currentSnapshot)) return;

    if (this.adoptAfterRefreshGeneration !== null) {
      const mayDiscard =
        this.adoptAfterRefreshGeneration === this.bufferGeneration;
      this.adoptAfterRefreshGeneration = null;
      this.refreshPending.set(false);
      if (mayDiscard) {
        this.adopt(candidate);
      } else {
        this.pendingRemote = candidate;
        this.conflict.set(
          candidate.sourceKey === currentSnapshot.sourceKey
            ? 'content_changed'
            : 'replaced',
        );
      }
      return;
    }

    if (this.dirty()) {
      this.pendingRemote = candidate;
      this.conflict.set(
        candidate.sourceKey === currentSnapshot.sourceKey
          ? 'presentation_changed'
          : 'replaced',
      );
      return;
    }

    this.adopt(candidate);
  }

  enterEdit(): void {
    if (!this.canEnterEdit()) return;
    this.editorMounted.set(true);
    this.editMode.set(true);
  }

  showPreview(): void {
    this.editMode.set(false);
    this.sendIdle();
  }

  updateBuffer(value: string): void {
    if (!this.snapshot()) return;
    this.buffer.set(value);
    this.bufferGeneration += 1;
    if (this.saveStatus() !== 'saving') this.saveStatus.set('idle');
    if (this.focused) this.sendEditing();
  }

  editorFocused(): void {
    this.focused = true;
    if (this.active) this.sendEditing();
  }

  editorBlurred(): void {
    this.focused = false;
    this.sendIdle();
  }

  save(): void {
    const snapshot = this.snapshot();
    if (!snapshot || !this.canSave()) return;
    this.saveRequest?.unsubscribe();
    if (this.savedTimer) {
      clearTimeout(this.savedTimer);
      this.savedTimer = null;
    }
    const submittedContent = this.buffer();
    this.saveStatus.set('saving');
    this.conflict.set(null);
    this.saveRequest = this.canvas.saveContent({
      contentUrl: snapshot.contentUrl,
      contentEtag: snapshot.contentEtag,
      presentationRevision: snapshot.state.presentation_revision,
      content: submittedContent,
    }).subscribe({
      next: response => this.applySave(response, submittedContent),
      error: (error: unknown) => this.handleSaveError(error),
    });
  }

  /**
   * Discard the local buffer in favor of an already-loaded replacement, or
   * conditionally adopt the current workspace bytes when the file drifted
   * outside the Canvas coordinator.
   */
  loadCurrentVersion(): void {
    if (this.pendingRemote) {
      this.adopt(this.pendingRemote);
      return;
    }
    if (this.conflict() === 'cleared' || this.conflict() === 'replaced') {
      this.resetSession();
      return;
    }
    const current = this.canvas.state();
    if (!current?.editable || !current.capabilities.can_edit) {
      this.resetSession();
      this.refreshPresentedSource();
      return;
    }
    this.refreshRequest?.unsubscribe();
    this.refreshPending.set(true);
    this.adoptAfterRefreshGeneration = this.bufferGeneration;
    this.refreshRequest = this.canvas.refreshSource().subscribe({
      error: (error: unknown) => {
        this.adoptAfterRefreshGeneration = null;
        this.refreshPending.set(false);
        const requestError = canvasEditError(error);
        this.conflict.set(
          requestError.status === 412 ? 'presentation_changed' : 'save_failed',
        );
        if (requestError.status === 412) this.canvas.reconcile();
      },
    });
  }

  /** Trusted refresh action for a read-only presentation whose bytes drifted. */
  refreshPresentedSource(): void {
    this.refreshRequest?.unsubscribe();
    this.refreshPending.set(true);
    this.refreshRequest = this.canvas.refreshSource().subscribe({
      next: () => this.refreshPending.set(false),
      error: () => {
        this.refreshPending.set(false);
        this.canvas.reconcile();
      },
    });
  }

  keepEditing(): void {
    this.editorMounted.set(true);
    this.editMode.set(true);
  }

  private applySave(response: CanvasMutationResponse, submittedContent: string): void {
    const current = this.canvas.state();
    const baseline = this.snapshot();
    if (
      current &&
      current.presentation_revision > response.state.presentation_revision
    ) {
      const currentKey = canvasSourceKey(current);
      this.saveStatus.set('error');
      if (!current.source || current.status === 'cleared') {
        this.conflict.set('cleared');
      } else if (baseline && currentKey !== baseline.sourceKey) {
        this.conflict.set('replaced');
      } else {
        this.conflict.set('presentation_changed');
      }
      // The save committed, but a later presentation already won. Preserve
      // the user's bytes against the old baseline and converge the preview.
      this.canvas.reconcile();
      return;
    }
    const source = response.state.source;
    const contentUrl = resolveCanvasContentUrl(response.state.content_url);
    const renderer = selectCanvasRenderer(response.state);
    if (
      source?.type !== 'workspace_file' ||
      typeof source.path !== 'string' ||
      !contentUrl ||
      (renderer !== 'markdown' && renderer !== 'text' && renderer !== 'html')
    ) {
      this.saveStatus.set('error');
      this.conflict.set('save_failed');
      return;
    }
    this.snapshot.set({
      state: response.state,
      sourceKey: canvasSourceKey(response.state)!,
      path: source.path,
      renderer,
      contentUrl,
      contentEtag: response.contentEtag,
      content: submittedContent,
    });
    this.pendingRemote = null;
    this.sessionState.set(response.state);
    this.conflict.set(null);
    if (this.buffer() === submittedContent) {
      this.saveStatus.set('saved');
      this.savedTimer = setTimeout(() => {
        if (this.saveStatus() === 'saved') this.saveStatus.set('idle');
        this.savedTimer = null;
      }, 2_000);
    } else {
      // A late Monaco/composition change is newer than the committed bytes.
      this.saveStatus.set('idle');
    }
    if (this.focused) this.sendEditing();
  }

  private handleSaveError(error: unknown): void {
    const requestError = canvasEditError(error);
    this.saveStatus.set('error');
    if (requestError.status === 412) {
      this.conflict.set('content_changed');
    } else if (requestError.status === 409) {
      this.conflict.set(conflictFromCode(requestError.code));
    } else if (requestError.status === 423) {
      this.conflict.set('busy');
    } else if (requestError.status === 428) {
      this.conflict.set('precondition_required');
    } else {
      this.conflict.set('save_failed');
    }
    // A transport/5xx failure can be an uncertain outcome: workspace bytes may
    // have changed before the Canvas-row transaction failed. Preserve the
    // buffer, but always reconcile authoritative state before a retry.
    this.canvas.reconcile();
  }

  private adopt(snapshot: CanvasEditSnapshot): void {
    this.snapshot.set(snapshot);
    this.pendingRemote = null;
    this.buffer.set(snapshot.content);
    this.sessionState.set(snapshot.state);
    this.conflict.set(null);
    this.saveStatus.set('idle');
    if (this.savedTimer) {
      clearTimeout(this.savedTimer);
      this.savedTimer = null;
    }
    this.pruneRemoteEditors();
  }

  private resetSession(): void {
    this.sendIdle();
    this.snapshot.set(null);
    this.pendingRemote = null;
    this.editMode.set(false);
    this.editorMounted.set(false);
    this.buffer.set('');
    this.sessionState.set(null);
    this.conflict.set(null);
    this.saveStatus.set('idle');
    if (this.savedTimer) {
      clearTimeout(this.savedTimer);
      this.savedTimer = null;
    }
    this.refreshPending.set(false);
    this.adoptAfterRefreshGeneration = null;
    this.bufferGeneration = 0;
    this.clearRemoteEditors();
  }

  private sendEditing(): void {
    const snapshot = this.snapshot();
    const threadId = this.selectedThread;
    if (!this.active || !this.focused || !snapshot || !threadId || !snapshot.state.source_version) {
      return;
    }
    this.transport.sendCanvasControl(threadId, {
      method: 'canvas.user_editing',
      canvas_id: MAIN_CANVAS_ID,
      path: snapshot.path,
      presentation_revision: snapshot.state.presentation_revision,
      source_version: snapshot.state.source_version,
      editing_session_id: this.editingSessionId,
    });
    this.awarenessActive = true;
    if (!this.awarenessTimer) {
      this.awarenessTimer = setInterval(() => this.sendEditing(), EDITING_RENEW_INTERVAL_MS);
    }
  }

  private sendIdle(): void {
    if (this.awarenessTimer) clearInterval(this.awarenessTimer);
    this.awarenessTimer = null;
    if (!this.awarenessActive) return;
    const snapshot = this.snapshot();
    const threadId = this.selectedThread;
    this.awarenessActive = false;
    if (!snapshot || !threadId || !snapshot.state.source_version) return;
    this.transport.sendCanvasControl(threadId, {
      method: 'canvas.user_idle',
      canvas_id: MAIN_CANVAS_ID,
      path: snapshot.path,
      presentation_revision: snapshot.state.presentation_revision,
      source_version: snapshot.state.source_version,
      editing_session_id: this.editingSessionId,
    });
  }

  private handleAwareness(event: CanvasAwarenessEvent): void {
    if (
      event.threadId !== this.selectedThread ||
      event.editingSessionId === this.editingSessionId
    ) {
      return;
    }
    if (event.method === 'canvas.user_idle') {
      const key = awarenessKey(event);
      const remote = this.remoteEditors.get(key);
      if (remote && sameAwarenessIdentity(remote.event, event)) this.removeRemoteEditor(key);
      return;
    }
    if (!this.awarenessMatchesSnapshot(event)) return;
    const key = awarenessKey(event);
    this.removeRemoteEditor(key);
    const timer = setTimeout(
      () => this.removeRemoteEditor(key),
      event.ttlMs ?? 15_000,
    );
    this.remoteEditors.set(key, {event, timer});
    this.remoteEditing.set(true);
  }

  private awarenessMatchesSnapshot(event: CanvasAwarenessEvent): boolean {
    const snapshot = this.snapshot();
    const current = this.canvas.state();
    const currentSource = current?.source;
    return !!(
      snapshot &&
      event.path === snapshot.path &&
      event.presentationRevision === snapshot.state.presentation_revision &&
      event.sourceVersion === snapshot.state.source_version &&
      currentSource?.type === 'workspace_file' &&
      typeof currentSource.path === 'string' &&
      currentSource.path === event.path &&
      current?.presentation_revision === event.presentationRevision &&
      current.source_version === event.sourceVersion
    );
  }

  private pruneRemoteEditors(): void {
    for (const [id, remote] of this.remoteEditors) {
      if (!this.awarenessMatchesSnapshot(remote.event)) this.removeRemoteEditor(id);
    }
  }

  private removeRemoteEditor(id: string): void {
    const remote = this.remoteEditors.get(id);
    if (remote) clearTimeout(remote.timer);
    this.remoteEditors.delete(id);
    this.remoteEditing.set(this.remoteEditors.size > 0);
  }

  private clearRemoteEditors(): void {
    for (const remote of this.remoteEditors.values()) clearTimeout(remote.timer);
    this.remoteEditors.clear();
    this.remoteEditing.set(false);
  }

  private cancelRequests(): void {
    this.saveRequest?.unsubscribe();
    this.saveRequest = null;
    this.refreshRequest?.unsubscribe();
    this.refreshRequest = null;
  }
}

function snapshotFrom(
  state: CanvasState,
  content: string,
  contentEtag: string,
): CanvasEditSnapshot | null {
  const source = state.source;
  const renderer = selectCanvasRenderer(state);
  const sourceKey = canvasSourceKey(state);
  const contentUrl = resolveCanvasContentUrl(state.content_url);
  if (
    source?.type !== 'workspace_file' ||
    typeof source.path !== 'string' ||
    !state.editable ||
    !state.capabilities.can_edit ||
    !sourceKey ||
    !contentUrl ||
    (renderer !== 'markdown' && renderer !== 'text' && renderer !== 'html')
  ) {
    return null;
  }
  return {state, sourceKey, path: source.path, renderer, contentUrl, contentEtag, content};
}

function sameSnapshot(left: CanvasEditSnapshot, right: CanvasEditSnapshot): boolean {
  return left.sourceKey === right.sourceKey &&
    left.state.presentation_revision === right.state.presentation_revision &&
    left.contentEtag === right.contentEtag &&
    left.content === right.content;
}

function conflictFromCode(code: string | null): CanvasEditConflict {
  switch (code) {
    case 'canvas_replaced':
      return 'replaced';
    case 'canvas_cleared':
      return 'cleared';
    case 'canvas_presentation_changed':
      return 'presentation_changed';
    default:
      return 'save_failed';
  }
}

function isBlockingConflict(conflict: CanvasEditConflict | null): boolean {
  return conflict === 'content_changed' ||
    conflict === 'presentation_changed' ||
    conflict === 'replaced' ||
    conflict === 'cleared' ||
    conflict === 'precondition_required';
}

function canvasEditError(error: unknown): {status: number | null; code: string | null} {
  if (!(error instanceof HttpErrorResponse)) return {status: null, code: null};
  const detail = error.error?.detail;
  return {
    status: error.status || null,
    code: typeof detail === 'object' && detail !== null && typeof detail.code === 'string'
      ? detail.code
      : null,
  };
}

function awarenessKey(event: CanvasAwarenessEvent): string {
  return `${event.senderId}\u0000${event.editingSessionId}`;
}

function sameAwarenessIdentity(left: CanvasAwarenessEvent, right: CanvasAwarenessEvent): boolean {
  return left.path === right.path &&
    left.presentationRevision === right.presentationRevision &&
    left.sourceVersion === right.sourceVersion;
}

function createEditingSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  }
  // Presence IDs are only echo-suppression hints, never authorization or user
  // identity. This deterministic SSR fallback is replaced in every browser.
  return 'canvas-editor-server';
}
