import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import {
  DestroyRef,
  Injectable,
  InjectionToken,
  Signal,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { environment } from '../../core/environment';
import { CanvasState, MAIN_CANVAS_ID } from '../../core/models/canvas.model';

export const CANVAS_AWARENESS_RENEW_MS = 5_000;
const CANVAS_AWARENESS_RETRY_MS = 5_000;
const CANVAS_AWARENESS_STORAGE_ID = 'srw.canvas.awareness.id.v1';
const CANVAS_AWARENESS_STORAGE_SEQUENCE = 'srw.canvas.awareness.sequence.v1';
const CANVAS_AWARENESS_POPOUT_CONTEXT = 'srw.canvas.awareness.popout.v1';
const MAX_AWARENESS_EDITORS = 256;

export interface CanvasAwarenessEventSource {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

export type CanvasAwarenessEventSourceFactory = (
  url: string,
  init: EventSourceInit,
) => CanvasAwarenessEventSource;

export const CANVAS_AWARENESS_EVENT_SOURCE_FACTORY =
  new InjectionToken<CanvasAwarenessEventSourceFactory>('CANVAS_AWARENESS_EVENT_SOURCE_FACTORY', {
    factory: () => (url, init) => new EventSource(url, init),
  });

export const CANVAS_AWARENESS_SESSION_STORAGE = new InjectionToken<Storage | null>(
  'CANVAS_AWARENESS_SESSION_STORAGE',
  {
    factory: () => {
      if (typeof sessionStorage === 'undefined') return null;
      try {
        // Access itself can throw in privacy-restricted browsing contexts.
        sessionStorage.getItem(CANVAS_AWARENESS_STORAGE_ID);
        return sessionStorage;
      } catch {
        return null;
      }
    },
  },
);

export interface CanvasAwarenessIdentity {
  readonly path: string;
  readonly presentationRevision: number;
  readonly sourceVersion: string;
}

export interface CanvasRemoteEditor extends CanvasAwarenessIdentity {
  readonly senderId: string;
  readonly editingSessionId: string;
  readonly sequence: number;
  readonly ttlMs: number;
}

interface CanvasAwarenessScope extends CanvasAwarenessIdentity {
  readonly threadId: string;
}

interface CanvasAwarenessMutation {
  readonly scope: CanvasAwarenessScope;
  readonly sequence: number;
  readonly state: 'editing' | 'idle';
}

interface CanvasAwarenessMutationResponse {
  readonly applied: boolean;
  readonly sender_id: string;
  readonly sequence: number;
  readonly state: 'editing' | 'idle';
  readonly expires_at: string;
}

interface CanvasAwarenessWireEditor {
  readonly sender_id: string;
  readonly editing_session_id: string;
  readonly path: string;
  readonly presentation_revision: number;
  readonly source_version: string;
  readonly sequence: number;
  readonly ttl_ms: number;
}

interface CanvasAwarenessWireSnapshot {
  readonly canvas_id: typeof MAIN_CANVAS_ID;
  readonly editors: readonly CanvasAwarenessWireEditor[];
}

/**
 * Pane-local, lane-free Canvas editing awareness.
 *
 * The durable Canvas pointer remains in CanvasService. This controller owns
 * only the courtesy editor TTL: owner-gated REST writes plus a dedicated,
 * unjournaled named-event stream. It deliberately has no dependency on the
 * persistent chat/control WebSocket, so the authenticated pop-out and a
 * socketless stateless thread use the exact same path.
 */
@Injectable()
export class CanvasAwarenessController {
  private readonly http = inject(HttpClient);
  private readonly eventSourceFactory = inject(CANVAS_AWARENESS_EVENT_SOURCE_FACTORY);
  private readonly storage = inject(CANVAS_AWARENESS_SESSION_STORAGE);
  private readonly destroyRef = inject(DestroyRef);

  private readonly editorsSignal = signal<readonly CanvasRemoteEditor[]>([]);
  readonly remoteEditors: Signal<readonly CanvasRemoteEditor[]> = this.editorsSignal.asReadonly();
  readonly remoteEditing = computed(() => this.editorsSignal().length > 0);

  private editingSessionId: string;
  private sequence: number;
  private scope: CanvasAwarenessScope | null = null;
  private active = false;
  private wantsEditing = false;
  private stream: CanvasAwarenessEventSource | null = null;
  private streamGeneration = 0;
  private request: Subscription | null = null;
  private inFlight: CanvasAwarenessMutation | null = null;
  private pending: CanvasAwarenessMutation[] = [];
  private renewTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private expiryTimer: ReturnType<typeof setTimeout> | null = null;
  private editorDeadlines = new Map<string, number>();
  private destroyed = false;

  constructor() {
    const persistedId = this.readStorage(CANVAS_AWARENESS_STORAGE_ID);
    this.editingSessionId = isEditingSessionId(persistedId)
      ? persistedId
      : createEditingSessionId();
    this.writeStorage(CANVAS_AWARENESS_STORAGE_ID, this.editingSessionId);
    this.sequence = parseStoredSequence(this.readStorage(CANVAS_AWARENESS_STORAGE_SEQUENCE));

    this.destroyRef.onDestroy(() => this.destroy());
  }

  /** Bind awareness to the pane's retained editor snapshot. */
  sync(active: boolean, threadId: string | null, state: CanvasState | null, popout = false): void {
    this.prepareContext(popout);
    const nextScope = awarenessScope(threadId, state);
    const targetChanged = !sameScope(this.scope, nextScope);
    const previousScope = this.scope;

    if ((!active || !nextScope) && this.wantsEditing) {
      this.wantsEditing = false;
      this.stopRenewTimer();
      if (previousScope) this.enqueue(previousScope, 'idle');
    }

    this.active = active;
    this.scope = nextScope;

    if (targetChanged) {
      this.closeStream();
      this.applySnapshot([]);

      if (this.wantsEditing && nextScope) {
        // Same-thread content saves advance the presentation identity. A
        // higher-sequence editing row replaces the old identity atomically;
        // no transient idle edge is necessary.
        if (!previousScope || previousScope.threadId === nextScope.threadId) {
          this.enqueue(nextScope, 'editing');
        } else {
          this.enqueue(previousScope, 'idle');
          this.enqueue(nextScope, 'editing');
        }
      }
    }

    if (active && nextScope) this.openStream(nextScope.threadId);
    else this.closeStream();
  }

  /** Start one focus epoch and renew it until blur/inactivation. */
  startEditing(): void {
    if (this.destroyed || !this.active || !this.scope) return;
    if (!this.wantsEditing) {
      this.wantsEditing = true;
      this.enqueue(this.scope, 'editing');
    }
    this.scheduleRenew();
  }

  /** Write a monotonic tombstone for the current focus epoch. */
  stopEditing(): void {
    this.stopRenewTimer();
    if (!this.wantsEditing) return;
    this.wantsEditing = false;
    if (this.scope) this.enqueue(this.scope, 'idle');
  }

  private openStream(threadId: string): void {
    if (this.stream || this.destroyed) return;
    const generation = ++this.streamGeneration;
    const url = `${this.awarenessBaseUrl(threadId)}/stream?ngsw-bypass=true`;
    let stream: CanvasAwarenessEventSource;
    try {
      stream = this.eventSourceFactory(url, { withCredentials: true });
    } catch {
      // SSR and locked-down browser contexts may not expose EventSource. A
      // missing courtesy stream must not break Canvas itself or its REST save.
      return;
    }
    this.stream = stream;
    stream.addEventListener('canvas_awareness', (event) => {
      if (
        this.destroyed ||
        this.stream !== stream ||
        generation !== this.streamGeneration ||
        this.scope?.threadId !== threadId
      )
        return;
      const snapshot = parseCanvasAwarenessSnapshot(event.data);
      if (!snapshot) return;
      this.applyWireSnapshot(snapshot);
    });
  }

  private closeStream(): void {
    this.streamGeneration += 1;
    const stream = this.stream;
    this.stream = null;
    stream?.close();
  }

  private applyWireSnapshot(snapshot: CanvasAwarenessWireSnapshot): void {
    const scope = this.scope;
    if (!scope || snapshot.canvas_id !== MAIN_CANVAS_ID) return;
    const editors: CanvasRemoteEditor[] = [];
    const seen = new Set<string>();
    for (const editor of snapshot.editors) {
      if (
        editor.editing_session_id === this.editingSessionId ||
        editor.path !== scope.path ||
        editor.presentation_revision !== scope.presentationRevision ||
        editor.source_version !== scope.sourceVersion
      )
        continue;
      const key = `${editor.sender_id}\u0000${editor.editing_session_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      editors.push({
        senderId: editor.sender_id,
        editingSessionId: editor.editing_session_id,
        path: editor.path,
        presentationRevision: editor.presentation_revision,
        sourceVersion: editor.source_version,
        sequence: editor.sequence,
        ttlMs: editor.ttl_ms,
      });
    }
    this.applySnapshot(editors);
  }

  private applySnapshot(editors: readonly CanvasRemoteEditor[]): void {
    if (this.expiryTimer) clearTimeout(this.expiryTimer);
    this.expiryTimer = null;
    const now = Date.now();
    this.editorDeadlines = new Map(
      editors.map((editor) => [remoteEditorKey(editor), now + editor.ttlMs]),
    );
    this.editorsSignal.set([...editors]);
    this.scheduleExpiry();
  }

  private scheduleExpiry(): void {
    if (this.expiryTimer || this.editorDeadlines.size === 0) return;
    const earliest = Math.min(...this.editorDeadlines.values());
    const now = Date.now();
    this.expiryTimer = setTimeout(
      () => {
        this.expiryTimer = null;
        const now = Date.now();
        const retained = this.editorsSignal().filter((editor) => {
          const deadline = this.editorDeadlines.get(remoteEditorKey(editor));
          if (deadline !== undefined && deadline > now) return true;
          this.editorDeadlines.delete(remoteEditorKey(editor));
          return false;
        });
        if (retained.length !== this.editorsSignal().length) {
          this.editorsSignal.set(retained);
        }
        this.scheduleExpiry();
      },
      Math.max(0, earliest - now),
    );
  }

  private enqueue(scope: CanvasAwarenessScope, state: 'editing' | 'idle'): void {
    if (this.destroyed) return;
    const mutation: CanvasAwarenessMutation = {
      scope,
      state,
      sequence: this.nextSequence(),
    };
    const key = mutationKey(mutation);
    // Only the newest not-yet-sent state for one row matters. An in-flight
    // predecessor is retained; the higher sequence deterministically wins.
    this.pending = this.pending.filter((item) => mutationKey(item) !== key);
    this.pending.push(mutation);
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.pump();
  }

  private pump(): void {
    if (this.destroyed || this.request || this.retryTimer) return;
    const mutation = this.pending.shift();
    if (!mutation) return;
    this.inFlight = mutation;
    // Occupy the single-flight slot before subscribe(). Angular HTTP settles
    // asynchronously, but tests and future adapters may return a synchronous
    // Observable. Without the placeholder, a synchronous completion can clear
    // the slot and recursively pump before the old Subscription is assigned.
    this.request = new Subscription();
    let settledSynchronously = false;
    const subscription = this.http
      .put<CanvasAwarenessMutationResponse>(this.mutationUrl(mutation.scope.threadId), {
        sequence: mutation.sequence,
        state: mutation.state,
        path: mutation.scope.path,
        presentation_revision: mutation.scope.presentationRevision,
        source_version: mutation.scope.sourceVersion,
      })
      .subscribe({
        next: (response) => this.handleMutationResponse(mutation, response),
        error: (error) => {
          settledSynchronously = true;
          this.handleMutationError(mutation, error);
        },
        complete: () => {
          settledSynchronously = true;
          this.finishMutation(mutation);
        },
      });
    if (!settledSynchronously && this.inFlight === mutation) {
      this.request = subscription;
    } else if (!subscription.closed) {
      subscription.unsubscribe();
    }
  }

  private handleMutationResponse(
    mutation: CanvasAwarenessMutation,
    response: CanvasAwarenessMutationResponse,
  ): void {
    if (!isMutationResponse(response) || response.sequence < mutation.sequence) return;
    this.adoptServerSequence(response.sequence);

    // A prior page instance with the same sessionStorage identity may have
    // committed a newer tombstone. Reassert this page's current intent with a
    // sequence above server truth instead of waiting a full renewal interval.
    if (
      response.sequence > mutation.sequence &&
      this.scope &&
      mutationKey(mutation) === mutationKey({ scope: this.scope }) &&
      response.state !== (this.wantsEditing ? 'editing' : 'idle')
    ) {
      this.enqueue(this.scope, this.wantsEditing ? 'editing' : 'idle');
    }
  }

  private handleMutationError(mutation: CanvasAwarenessMutation, error: unknown): void {
    this.request = null;
    this.inFlight = null;
    const status = error instanceof HttpErrorResponse ? error.status : 0;
    const terminal = [401, 403, 404, 422].includes(status);
    if (terminal || status === 409) {
      if (terminal && this.scope && mutationKey(mutation) === mutationKey({ scope: this.scope })) {
        this.wantsEditing = false;
        this.stopRenewTimer();
        this.closeStream();
        this.applySnapshot([]);
      }
      this.pump();
      return;
    }

    // Ambiguous network/5xx outcome: retry the exact sequence unless a newer
    // queued state for this row already supersedes it.
    if (!this.pending.some((item) => mutationKey(item) === mutationKey(mutation))) {
      this.pending.unshift(mutation);
    }
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.pump();
    }, CANVAS_AWARENESS_RETRY_MS);
  }

  private finishMutation(mutation: CanvasAwarenessMutation): void {
    if (this.inFlight !== mutation) return;
    this.request = null;
    this.inFlight = null;
    this.pump();
  }

  private scheduleRenew(): void {
    if (this.renewTimer || !this.wantsEditing) return;
    this.renewTimer = setTimeout(() => {
      this.renewTimer = null;
      if (!this.wantsEditing || !this.active || !this.scope) return;
      // Preserve an ambiguous request's exact sequence while its retry is
      // pending. The retry and renewal timers intentionally share the 5s
      // cadence; the renewal simply schedules the following heartbeat.
      if (this.retryTimer) {
        this.scheduleRenew();
        return;
      }
      this.enqueue(this.scope, 'editing');
      this.scheduleRenew();
    }, CANVAS_AWARENESS_RENEW_MS);
  }

  private stopRenewTimer(): void {
    if (this.renewTimer) clearTimeout(this.renewTimer);
    this.renewTimer = null;
  }

  private nextSequence(): number {
    if (this.sequence >= Number.MAX_SAFE_INTEGER) {
      // Impossibly long-lived tab: keep the protocol valid. A fresh identity
      // is safer than wrapping a sequence, but rotating mid-focus would echo
      // the old row as a remote editor; fail closed until page reload.
      return this.sequence;
    }
    this.sequence += 1;
    this.writeStorage(CANVAS_AWARENESS_STORAGE_SEQUENCE, String(this.sequence));
    return this.sequence;
  }

  private adoptServerSequence(sequence: number): void {
    if (sequence <= this.sequence) return;
    this.sequence = sequence;
    this.writeStorage(CANVAS_AWARENESS_STORAGE_SEQUENCE, String(sequence));
  }

  private prepareContext(popout: boolean): void {
    if (!popout) return;
    const popoutContextId = this.readStorage(CANVAS_AWARENESS_POPOUT_CONTEXT);
    if (popoutContextId === this.editingSessionId) return;
    if (this.stream || this.request || this.pending.length > 0 || this.wantsEditing) {
      // CanvasPane passes its popout input on the first sync, before opening a
      // stream or writing a lease. Refuse an unsafe mid-session identity swap
      // if a future caller violates that ordering.
      return;
    }

    // window.open clones sessionStorage from its opener. Rotate once in the
    // authenticated popout so it cannot collapse the opener's editor into the
    // same DB row, then retain the marker across popout reloads. Ordinary
    // browser Duplicate Tab may also clone storage; that bounded courtesy-UX
    // collision is accepted to preserve hard-reload self filtering.
    this.editingSessionId = createEditingSessionId();
    this.sequence = 0;
    this.writeStorage(CANVAS_AWARENESS_STORAGE_ID, this.editingSessionId);
    this.writeStorage(CANVAS_AWARENESS_STORAGE_SEQUENCE, '0');
    this.writeStorage(CANVAS_AWARENESS_POPOUT_CONTEXT, this.editingSessionId);
  }

  private awarenessBaseUrl(threadId: string): string {
    return (
      `${environment.apiUrl}/persistent/threads/${encodeURIComponent(threadId)}` +
      `/canvases/${MAIN_CANVAS_ID}/awareness`
    );
  }

  private mutationUrl(threadId: string): string {
    return `${this.awarenessBaseUrl(threadId)}/${encodeURIComponent(this.editingSessionId)}`;
  }

  private readStorage(key: string): string | null {
    try {
      return this.storage?.getItem(key) ?? null;
    } catch {
      return null;
    }
  }

  private writeStorage(key: string, value: string): void {
    try {
      this.storage?.setItem(key, value);
    } catch {
      // Memory identity/sequence remains valid for this page lifetime.
    }
  }

  private destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stopRenewTimer();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.expiryTimer) clearTimeout(this.expiryTimer);
    this.retryTimer = null;
    this.expiryTimer = null;
    this.request?.unsubscribe();
    this.request = null;
    this.inFlight = null;
    this.pending = [];
    this.closeStream();
    this.editorDeadlines.clear();
    this.editorsSignal.set([]);
  }
}

export function parseCanvasAwarenessSnapshot(raw: string): CanvasAwarenessWireSnapshot | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value) || value['canvas_id'] !== MAIN_CANVAS_ID) return null;
  const editors = value['editors'];
  if (!Array.isArray(editors) || editors.length > MAX_AWARENESS_EDITORS) return null;
  const parsed: CanvasAwarenessWireEditor[] = [];
  for (const editor of editors) {
    if (!isWireEditor(editor)) return null;
    parsed.push({
      sender_id: editor['sender_id'],
      editing_session_id: editor['editing_session_id'],
      path: editor['path'],
      presentation_revision: editor['presentation_revision'],
      source_version: editor['source_version'],
      sequence: editor['sequence'],
      ttl_ms: editor['ttl_ms'],
    });
  }
  return { canvas_id: MAIN_CANVAS_ID, editors: parsed };
}

function awarenessScope(
  threadId: string | null,
  state: CanvasState | null,
): CanvasAwarenessScope | null {
  const source = state?.source;
  if (
    !threadId ||
    !state ||
    source?.type !== 'workspace_file' ||
    typeof source.path !== 'string' ||
    !source.path ||
    !Number.isSafeInteger(state.presentation_revision) ||
    state.presentation_revision < 1 ||
    typeof state.source_version !== 'string' ||
    !state.source_version
  )
    return null;
  return {
    threadId,
    path: source.path,
    presentationRevision: state.presentation_revision,
    sourceVersion: state.source_version,
  };
}

function sameScope(left: CanvasAwarenessScope | null, right: CanvasAwarenessScope | null): boolean {
  return (
    left === right ||
    !!(
      left &&
      right &&
      left.threadId === right.threadId &&
      left.path === right.path &&
      left.presentationRevision === right.presentationRevision &&
      left.sourceVersion === right.sourceVersion
    )
  );
}

function mutationKey(mutation: Pick<CanvasAwarenessMutation, 'scope'>): string {
  return mutation.scope.threadId;
}

function remoteEditorKey(editor: CanvasRemoteEditor): string {
  return `${editor.senderId}\u0000${editor.editingSessionId}`;
}

function isMutationResponse(value: unknown): value is CanvasAwarenessMutationResponse {
  return (
    isRecord(value) &&
    typeof value['applied'] === 'boolean' &&
    typeof value['sender_id'] === 'string' &&
    value['sender_id'].length > 0 &&
    value['sender_id'].length <= 128 &&
    isSafePositiveInteger(value['sequence']) &&
    (value['state'] === 'editing' || value['state'] === 'idle') &&
    typeof value['expires_at'] === 'string' &&
    Number.isFinite(Date.parse(value['expires_at']))
  );
}

function isWireEditor(value: unknown): value is CanvasAwarenessWireEditor {
  return (
    isRecord(value) &&
    typeof value['sender_id'] === 'string' &&
    value['sender_id'].length > 0 &&
    value['sender_id'].length <= 128 &&
    isEditingSessionId(value['editing_session_id']) &&
    typeof value['path'] === 'string' &&
    value['path'].length > 0 &&
    value['path'].length <= 4096 &&
    isSafePositiveInteger(value['presentation_revision']) &&
    typeof value['source_version'] === 'string' &&
    /^sha256:[0-9a-f]{64}$/.test(value['source_version']) &&
    isSafePositiveInteger(value['sequence']) &&
    isSafePositiveInteger(value['ttl_ms']) &&
    value['ttl_ms'] <= 60_000
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isSafeNonnegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isSafePositiveInteger(value: unknown): value is number {
  return isSafeNonnegativeInteger(value) && value > 0;
}

function isEditingSessionId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{8,128}$/.test(value);
}

function parseStoredSequence(value: string | null): number {
  if (!value || !/^\d+$/.test(value)) return 0;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
}

function createEditingSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  }
  return `canvas-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}
