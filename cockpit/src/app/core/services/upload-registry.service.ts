/**
 * Eager upload: the transfer starts when a file is ATTACHED, not when the
 * message is sent (knowledge-base/knowledge/features/session_attachment_send_flow.md §5.4). Most
 * users attach first and type second, so by the time they hit Enter the bytes
 * are often already in the workspace and the send is instant.
 *
 * Why a root-provided service and not more surface on PersistentChatService:
 *
 *  - `ChatPageComponent` is destroyed on navigation, so an upload owned by the
 *    component dies with a route change the user did not intend as a cancel.
 *    `providedIn: 'root'` outlives it, exactly like `PersistentChatService`.
 *  - The upload it owns is *uncommitted*. Everything on the outbox is a send
 *    the user has already committed to; mixing the two lifetimes into one
 *    already-4600-line service is how the "is this cancellable?" question stops
 *    having an answer.
 *  - It keeps the registry ignorant of chat state. Whether a thread can accept
 *    an eager upload at all (thread exists, session ready, tier not `none`) is
 *    a chat-state question, so `PersistentChatService` decides it and this
 *    service is told. No circular injection, and the preconditions live where
 *    the signals they read do.
 *
 * The seam with the send path is deliberately narrow: `adopt()` returns an
 * `Observable<ThreadUploadEvent>` shaped exactly like `uploadOneToThread`'s, so
 * the outbox's upload stage awaits an already-running transfer with the same
 * code that starts a new one. Every guard around that await is unchanged.
 *
 * It is also the ONLY place a thread upload is issued from, which is what lets
 * spec §5.3's two limits live in one function (`_gatedUpload`): a per-filename
 * FIFO barrier and a global concurrency ceiling. Both the eager path and the
 * send path's fallback go through it, so neither can slip a request past them.
 */
import {inject, Injectable} from '@angular/core';
import {
    defer,
    finalize,
    firstValueFrom,
    from,
    Observable,
    ReplaySubject,
    Subscription,
    switchMap,
} from 'rxjs';
import {ApiService} from './api.service';
import {FilePreview, ThreadUploadedFile, ThreadUploadEvent} from '../models/file.model';
import {topLevelUploadTargets} from './upload-stage';

/**
 * How many upload requests may be on the wire at once (spec §5.3).
 *
 * The server's virtual-tier semaphore is 4 and is **shared across all users**
 * (`MAX_CONCURRENT_VIRTUAL_UPLOADS`, `thread_uploads.py`), and every in-flight
 * request is a fully-buffered body on both ends. One request per attached file
 * with no ceiling — which is what shipped — lets a 10-file selection open ten.
 */
export const MAX_CONCURRENT_UPLOADS = 2;

/**
 * The error an aborted eager upload terminates with.
 *
 * Angular reports a user abort and a dead network identically as `status: 0`,
 * so a cancelled upload that surfaced through the normal error path would read
 * as *"Network error — check your connection"* — the misleading message from
 * the service-worker incident (knowledge-history/done/cockpit_service_worker_breaks_file_
 * uploads.md). Intent is recorded in `cancelled` BEFORE the abort, and this
 * type is what any awaiting consumer sees instead of an HttpErrorResponse.
 */
export class UploadCancelledError extends Error {
    constructor(readonly previewId: string) {
        super(`Upload cancelled: ${previewId}`);
        this.name = 'UploadCancelledError';
    }
}

export function isUploadCancelled(err: unknown): err is UploadCancelledError {
    return err instanceof UploadCancelledError;
}

interface EagerUpload {
    previewId: string;
    /** The thread the bytes are going to. Adoption from any other thread is
     *  refused — resolving into a foreign workspace is the failure mode the
     *  whole thread-switch cleanup exists to prevent. */
    threadId: string;
    file: File;
    /** ReplaySubject(1) so a late adopter sees the terminal `done` (or the
     *  latest progress) rather than hanging forever on a stream that already
     *  said everything it had to say. */
    events$: ReplaySubject<ThreadUploadEvent>;
    sub?: Subscription;
    /** Latest bytes-on-the-wire, straight off the progress events. Not a
     *  signal: nothing renders these, they only answer "has the body left?". */
    loaded: number;
    total: number | null;
    status: 'uploading' | 'done';
    resolved: ThreadUploadedFile[];
    /** True once the send path has taken it over. An adopted upload is no
     *  longer cancellable here: the user committed to it. */
    adopted: boolean;
    /** Cancelled while the body was already in — delete it once it lands and
     *  we know the name the server gave it. */
    deleteOnArrival: boolean;
}

/** Barrier key: one thread's one requested filename. A space is an
 *  unambiguous separator here because the thread id is a UUID, and it keeps
 *  this file TEXT: a NUL byte in the template made git classify the whole
 *  module as binary, which hid it from `git diff` and `git grep` entirely. */
function barrierKey(threadId: string, name: string): string {
    return `${threadId} ${name}`;
}

@Injectable({providedIn: 'root'})
export class UploadRegistryService {
    private readonly api = inject(ApiService);

    /** Keyed by `FilePreview.id` — a key that exists before the server path
     *  does, and the same one the outbox's `PendingUpload` uses. */
    private readonly entries = new Map<string, EagerUpload>();

    /**
     * Preview ids the user cancelled. Written BEFORE the unsubscribe that will
     * surface as a `status: 0` error, and read before anything looks at that
     * status. This is the whole reason a cancel does not read as an outage.
     *
     * Never pruned within a page's lifetime: one short string per cancelled
     * attachment, and a stale entry can only ever be consulted for a preview id
     * that will never be uploaded again (ids are unique per attach).
     */
    private readonly cancelled = new Set<string>();

    /**
     * One FIFO chain per `threadId + requested filename`, covering **both**
     * uploads and DELETEs of that name.
     *
     * Two independent races share this one fix, and both come from the backend
     * having no upload idempotency:
     *
     *  - **Re-upload racing a DELETE.** Eager upload widens the window between
     *    "remove the chip" and "attach it again"; a DELETE still in flight when
     *    the re-upload claims the name could remove the file that just replaced
     *    it. Waiting also lets the file reclaim its clean name instead of `_1`.
     *  - **Two same-named uploads truncating each other.** The dedupe key is
     *    `name|size|lastModified`, so two *different* files called `report.pdf`
     *    both attach. Concurrently, each one lists `uploads/` before either has
     *    written (`thread_uploads.py:763-769`), both claim `report.pdf`, and the
     *    second `sftp.open(..., "wb")` truncates the first. Serializing them
     *    makes the second list *after* the first wrote, so it takes `report_1`.
     *    The old batched POST resolved names inside one listing and could not
     *    hit this; per-file requests reintroduced it.
     */
    private readonly nameBarrier = new Map<string, Promise<void>>();

    /** Requests currently on the wire, and the FIFO of gates waiting for one of
     *  the {@link MAX_CONCURRENT_UPLOADS} slots to come free. */
    private activeUploads = 0;
    private readonly slotWaiters: (() => void)[] = [];

    /**
     * Begin uploading an attached file. Idempotent per preview id.
     *
     * The caller owns the preconditions (§5.4: a thread exists, the session is
     * ready, the tier is not `none`) — if they don't hold, simply don't call
     * this and the file uploads at flush time as it always did. The deferred
     * path is the fallback and is never removed.
     */
    start(threadId: string, preview: FilePreview): void {
        if (!preview.file) return;
        if (this.entries.has(preview.id)) return;

        const entry: EagerUpload = {
            previewId: preview.id,
            threadId,
            file: preview.file,
            events$: new ReplaySubject<ThreadUploadEvent>(1),
            loaded: 0,
            total: null,
            status: 'uploading',
            resolved: [],
            adopted: false,
            deleteOnArrival: false,
        };
        this.entries.set(preview.id, entry);

        entry.sub = this._gatedUpload(threadId, entry.file).subscribe({
            next: (ev) => {
                if (ev.kind === 'progress') {
                    entry.loaded = ev.loaded;
                    entry.total = ev.total;
                } else {
                    entry.status = 'done';
                    entry.resolved = ev.files;
                }
                entry.events$.next(ev);
            },
            error: (err) => {
                // Intent first. Reading `err.status` before this check is the
                // bug: abort and offline are both 0.
                this.entries.delete(preview.id);
                entry.events$.error(
                    this.cancelled.has(preview.id) ? new UploadCancelledError(preview.id) : err,
                );
            },
            complete: () => {
                entry.events$.complete();
                if (entry.deleteOnArrival) {
                    // Cancelled after the body was already in: the server
                    // finished the write regardless, so take it back now that
                    // we know what it called the file.
                    this.entries.delete(preview.id);
                    this._deleteUploaded(entry);
                } else if (entry.adopted) {
                    // The outbox has what it needs; nothing here can act on it
                    // any more.
                    this.entries.delete(preview.id);
                }
                // A completed, un-adopted entry stays: it is a finished upload
                // waiting to be adopted by a send — or deleted by a chip
                // removal.
            },
        });
    }

    /**
     * Hand the send path whatever is already running (or already finished) for
     * this preview, or a fresh request when there is nothing to adopt.
     *
     * Takes `threadId` and `file` as well as the id because the fallback lives
     * here: keeping it inside the registry is what lets the outbox's upload
     * stage stay a single expression whose surrounding guards never learn that
     * eager upload exists.
     */
    adopt(threadId: string, previewId: string, file: File): Observable<ThreadUploadEvent> {
        const entry = this.entries.get(previewId);
        if (!entry || entry.threadId !== threadId || entry.deleteOnArrival) {
            // Gated like every other request: the deferred path is a fallback,
            // not an exemption. Issuing it raw would let a flush-time upload
            // race an eager one for the same name — the truncation the barrier
            // exists to prevent — and would ignore the concurrency ceiling.
            return this._gatedUpload(threadId, file);
        }
        entry.adopted = true;
        if (entry.status === 'done') {
            // The common case — the upload finished while the user was still
            // typing — and the one that leaks if this is missing. `complete`
            // has ALREADY run, so its `adopted` branch will never fire again to
            // release this entry, and cancel/abortAll skip adopted entries by
            // design. Without this delete the map retains the File (and its
            // blob, up to 100MB) for the whole life of the page, once per sent
            // attachment. The map holds *cancellability*, not data: the
            // ReplaySubject we return keeps replaying `done` regardless.
            this.entries.delete(previewId);
        }
        return entry.events$.asObservable();
    }

    /**
     * How many uploads the registry is still holding — and so how many `File`
     * handles are still reachable from it.
     *
     * Retention is a correctness property here, not a curiosity: an entry that
     * outlives the send that adopted it pins a blob of up to 100MB until the
     * page is reloaded. Exposed read-only so that invariant can be asserted
     * directly rather than inferred from side effects.
     */
    get trackedCount(): number {
        return this.entries.size;
    }

    /** True when this preview's upload was aborted by the user. Checked before
     *  any error handler reads a status. */
    wasCancelled(previewId: string): boolean {
        return this.cancelled.has(previewId);
    }

    /**
     * The user removed the chip. Abort if the bytes are still moving; delete
     * what already landed.
     *
     * Cancel is only honest while the transfer is in progress. Once the request
     * body has fully arrived the server completes the write no matter what the
     * client does (`asyncio.to_thread` work is uncancellable), and aborting
     * then would ALSO discard the server-assigned name — the only handle that
     * can address the file for deletion. So a body that is already out is left
     * to land and deleted afterwards.
     */
    cancel(previewId: string): void {
        const entry = this.entries.get(previewId);
        if (!entry || entry.adopted || entry.deleteOnArrival) return;
        // Explicit intent, recorded before anything can fail as a status 0.
        this.cancelled.add(previewId);

        if (entry.status === 'done') {
            this.entries.delete(previewId);
            entry.events$.error(new UploadCancelledError(previewId));
            this._deleteUploaded(entry);
            return;
        }
        if (entry.total != null && entry.total > 0 && entry.loaded >= entry.total) {
            entry.deleteOnArrival = true;
            return; // stays in the map so `complete` can fire the DELETE
        }
        this.entries.delete(previewId);
        entry.sub?.unsubscribe();
        entry.events$.error(new UploadCancelledError(previewId));
    }

    /**
     * Every thread transition: `connect()` to a different thread and
     * `enterDraftSession()`. Chips do not follow the user between threads, so
     * neither may their bytes — an upload that resolved after a switch would
     * land in the wrong workspace or patch a foreign queue.
     *
     * Adopted uploads are deliberately left alone: they belong to a committed
     * send, and the flush's own thread guard already drops their resolution.
     */
    abortAll(): void {
        for (const previewId of [...this.entries.keys()]) this.cancel(previewId);
    }

    /**
     * The single point at which an upload request is issued.
     *
     * Wraps `uploadOneToThread` in the two limits spec §5.3 asks for — a FIFO
     * barrier per requested filename, then a global concurrency ceiling — and
     * releases both on completion, failure **or unsubscribe** (`finalize`), so
     * a cancelled chip never wedges the name or leaks a slot.
     *
     * Through ApiService → HttpClient, never a raw XHR/fetch: the auth
     * interceptor's `ngsw-bypass: 1` is what stops the service worker
     * corrupting the multipart body and killing upload progress.
     *
     * `defer` matters: the gate is claimed at SUBSCRIBE time. Claiming it when
     * the observable is merely constructed would hold the name for a caller
     * that never subscribes.
     */
    private _gatedUpload(threadId: string, file: File): Observable<ThreadUploadEvent> {
        return defer(() => {
            const gate = this._enterGate(threadId, file.name);
            const request = this.api.uploadOneToThread(threadId, file);
            return (
                gate.wait ? from(gate.wait).pipe(switchMap(() => request)) : request
            ).pipe(finalize(() => gate.release()));
        });
    }

    /**
     * Claim a place in this filename's chain and, once it is ours, a
     * concurrency slot.
     *
     * ORDER IS LOAD-BEARING: barrier first, slot second. Taking the scarce slot
     * before waiting on the barrier would let a blocked upload sit on a slot
     * that the upload it is waiting for needs — a deadlock with two same-named
     * files and a ceiling of two. Waiting slotless cannot deadlock: whatever
     * holds the slots is a request already on the wire.
     *
     * Returns `wait: null` when the name is free and a slot is available, so an
     * ordinary attach still issues its request synchronously rather than a
     * microtask later.
     */
    private _enterGate(
        threadId: string,
        name: string,
    ): {wait: Promise<void> | null; release: () => void} {
        const key = barrierKey(threadId, name);

        // Chain: we run after everything already queued on this key, and
        // anything queued after us runs after `mine` resolves.
        let releaseBarrier!: () => void;
        const mine = new Promise<void>((resolve) => (releaseBarrier = resolve));
        const previous = this.nameBarrier.get(key);
        const chained = (previous ?? Promise.resolve()).then(() => mine);
        this.nameBarrier.set(key, chained);
        void chained.then(() => {
            if (this.nameBarrier.get(key) === chained) this.nameBarrier.delete(key);
        });

        const state = {slot: false, released: false};
        const release = () => {
            if (state.released) return;
            state.released = true;
            if (state.slot) {
                state.slot = false;
                this._releaseSlot();
            }
            releaseBarrier();
            // Drop the key SYNCHRONOUSLY when we are the tail of the chain —
            // waiting for `chained`'s own `.then` would leave a spent barrier
            // in the map for a microtask, and the very next attach would defer
            // behind a request that is already over. Nobody is chained behind
            // the tail, so this can never skip a waiter.
            if (this.nameBarrier.get(key) === chained) this.nameBarrier.delete(key);
        };

        if (previous === undefined) {
            const slotWait = this._acquireSlot();
            if (slotWait === null) {
                state.slot = true;
                return {wait: null, release}; // wholly unconstrained: go now
            }
            return {wait: slotWait.then(() => this._settleSlot(state)), release};
        }
        return {
            wait: previous.then(async () => {
                const slotWait = this._acquireSlot();
                if (slotWait) await slotWait;
                this._settleSlot(state);
            }),
            release,
        };
    }

    /** A slot has just been granted: keep it, or hand it straight back when the
     *  subscription was torn down while we queued for it. */
    private _settleSlot(state: {slot: boolean; released: boolean}): void {
        if (state.released) this._releaseSlot();
        else state.slot = true;
    }

    /** Take a slot, or queue for one. Null means "taken, synchronously" — the
     *  caller holds it either way once the returned promise settles. */
    private _acquireSlot(): Promise<void> | null {
        if (this.activeUploads < MAX_CONCURRENT_UPLOADS) {
            this.activeUploads += 1;
            return null;
        }
        return new Promise<void>((resolve) => {
            this.slotWaiters.push(() => {
                this.activeUploads += 1;
                resolve();
            });
        });
    }

    /** Give a slot back and hand it to the longest-waiting gate, if any. */
    private _releaseSlot(): void {
        this.activeUploads -= 1;
        this.slotWaiters.shift()?.();
    }

    /** Delete what an upload actually stored, and hold the barrier that stops a
     *  re-upload of the same requested name from racing it. */
    private _deleteUploaded(entry: EagerUpload): void {
        const key = barrierKey(entry.threadId, entry.file.name);
        const targets = topLevelUploadTargets(entry.resolved.map((f) => f.name));
        if (targets.length === 0) return;

        // CHAIN onto whatever is already queued under this key — an upload or
        // an earlier DELETE — never replace it. The key is the name a re-upload
        // would REQUEST, while the targets are the names the server ASSIGNED —
        // so two chips for same-named but different files (stored as `a.pdf`
        // and `a_1.pdf`, both keyed `t1 a.pdf`) share a key. Overwriting left
        // the first DELETE unbarriered, and a re-attached `a.pdf` could then be
        // uploaded and deleted by that older, still-in-flight DELETE —
        // precisely the race the barrier exists to close.
        const previous = this.nameBarrier.get(key) ?? Promise.resolve();
        const done = previous
            .then(() =>
                Promise.all(
                    targets.map((target) =>
                        // Swallowed on purpose: the user asked to remove a
                        // chip, and a failed cleanup is not something they can
                        // act on. Worst case is one orphaned file, which is
                        // what the pre-Task-10 world had for every removal.
                        firstValueFrom(this.api.deleteThreadUpload(entry.threadId, target)).then(
                            () => undefined,
                            () => undefined,
                        ),
                    ),
                ),
            )
            .then(() => undefined);

        this.nameBarrier.set(key, done);
        void done.then(() => {
            if (this.nameBarrier.get(key) === done) this.nameBarrier.delete(key);
        });
    }
}
