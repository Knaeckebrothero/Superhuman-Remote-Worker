/**
 * Slice 3 — eager upload on attach
 * (knowledge-base/knowledge/features/session_attachment_send_flow.md §5.4).
 *
 * The registry is the only thing that owns an upload started before the user
 * has committed to sending, so these tests pin the four properties that make
 * that safe: exactly one request per file, adoption instead of a second
 * request, cancellation that is *honest* about bytes that already landed, and
 * a DELETE that can never race a re-upload of the same name.
 */
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {Observable, Subscriber} from 'rxjs';
import {ApiService} from './api.service';
import {
    isUploadCancelled,
    MAX_CONCURRENT_UPLOADS,
    UploadCancelledError,
    UploadRegistryService,
} from './upload-registry.service';
import {FilePreview, FileType, ThreadUploadEvent, UploadStatus} from '../models/file.model';

/** Let queued microtasks drain (the delete barrier is promise-ordered). */
const tick = () => new Promise((r) => setTimeout(r, 0));

interface UploadRec {
    threadId: string;
    file: File;
    /** Emit a progress/done event. */
    emit: (ev: ThreadUploadEvent) => void;
    /** Terminate the request normally (server answered). */
    finish: () => void;
    /** Terminate the request with a transport/HTTP error. */
    fail: (err: unknown) => void;
    /** True only when the SUBSCRIBER tore the request down — i.e. a real abort.
     *  RxJS runs the teardown on normal completion too, so "finalized" and
     *  "aborted" have to be told apart or every finished upload reads as
     *  cancelled. */
    aborted: boolean;
}
interface DeleteRec {
    threadId: string;
    path: string;
    settle: () => void;
}

/**
 * An ApiService whose upload is a real cold Observable: subscribing records the
 * request, unsubscribing flips `aborted`. That is the only way to prove the
 * cancel actually reaches the transport rather than merely being forgotten
 * about locally — a mock returning `of(...)` cannot express an abort at all.
 */
function makeApi() {
    const uploads: UploadRec[] = [];
    const deletes: DeleteRec[] = [];
    const api = {
        uploadOneToThread: vi.fn(
            (threadId: string, file: File) =>
                new Observable<ThreadUploadEvent>((sub: Subscriber<ThreadUploadEvent>) => {
                    let settled = false;
                    const rec: UploadRec = {
                        threadId,
                        file,
                        emit: (ev) => sub.next(ev),
                        finish: () => {
                            settled = true;
                            sub.complete();
                        },
                        fail: (err) => {
                            settled = true;
                            sub.error(err);
                        },
                        aborted: false,
                    };
                    uploads.push(rec);
                    return () => {
                        rec.aborted = !settled;
                    };
                }),
        ),
        deleteThreadUpload: vi.fn(
            (threadId: string, path: string) =>
                new Observable<void>((sub) => {
                    deletes.push({
                        threadId,
                        path,
                        settle: () => {
                            sub.next(undefined);
                            sub.complete();
                        },
                    });
                }),
        ),
        humanizeUploadError: vi.fn(() => 'upload failed'),
    };
    return {api, uploads, deletes};
}

function preview(name: string, id = `p-${name}`, lastModified = 111): FilePreview {
    return {
        id,
        file: new File(['x'], name, {lastModified}),
        name,
        size: 1,
        sizeFormatted: '1 B',
        type: FileType.DOCUMENT,
        mimeType: 'application/pdf',
        uploadStatus: UploadStatus.PENDING,
    };
}

const done = (...files: {name: string; size: number; mime_type: string; path: string}[]):
    ThreadUploadEvent => ({kind: 'done', files});

const uploaded = (name: string) => ({
    name,
    size: 1,
    mime_type: 'application/pdf',
    path: `uploads/${name}`,
});

function setup() {
    const {api, uploads, deletes} = makeApi();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
        providers: [{provide: ApiService, useValue: api}, UploadRegistryService],
    });
    return {registry: TestBed.inject(UploadRegistryService), api, uploads, deletes};
}

describe('UploadRegistryService', () => {
    let ctx: ReturnType<typeof setup>;

    beforeEach(() => {
        ctx = setup();
    });

    // --- start ------------------------------------------------------------

    it('starts exactly one request per attached file', () => {
        ctx.registry.start('t1', preview('a.pdf'));
        ctx.registry.start('t1', preview('b.pdf'));

        expect(ctx.uploads.map((u) => u.file.name)).toEqual(['a.pdf', 'b.pdf']);
    });

    it('never starts a second request for a preview it is already uploading', () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.registry.start('t1', p);

        expect(ctx.uploads.length).toBe(1);
    });

    // --- adoption ---------------------------------------------------------

    it('adopts an in-flight upload instead of starting a second one', async () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        expect(ctx.uploads.length).toBe(1);

        const events: ThreadUploadEvent[] = [];
        ctx.registry.adopt('t1', p.id, p.file).subscribe((e) => events.push(e));

        // Still one request: the send stage awaits what is already running.
        expect(ctx.uploads.length).toBe(1);

        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();
        expect(events).toEqual([done(uploaded('a.pdf'))]);
    });

    it('replays a COMPLETED eager upload to a late adopter', () => {
        // The common case: the user attaches, types for ten seconds, then
        // sends. Re-uploading a success is a permanent duplicate — the backend
        // resolves the collision with a `_1` suffix and nothing cleans it up.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();

        const events: ThreadUploadEvent[] = [];
        ctx.registry.adopt('t1', p.id, p.file).subscribe((e) => events.push(e));

        expect(ctx.uploads.length).toBe(1);
        expect(events).toEqual([done(uploaded('a.pdf'))]);
    });

    it('releases a completed upload the moment the send adopts it', async () => {
        // The leak this closes: `complete` had already run by adoption time, so
        // its `adopted` branch could never fire again, and cancel/abortAll skip
        // adopted entries by design. The entry — and the File's blob behind it,
        // up to 100MB — stayed reachable for the life of the page, once per
        // sent attachment.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();
        expect(ctx.registry.trackedCount).toBe(1); // held for the send to adopt

        const events: ThreadUploadEvent[] = [];
        ctx.registry.adopt('t1', p.id, p.file).subscribe((e) => events.push(e));

        expect(ctx.registry.trackedCount).toBe(0);
        // Released, but not lost: the map holds cancellability, not data.
        expect(events).toEqual([done(uploaded('a.pdf'))]);
    });

    it('releases an in-flight upload once it is adopted and lands', () => {
        // The other ordering — adopted while transferring, so `complete` is the
        // one that has to let go.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.registry.adopt('t1', p.id, p.file).subscribe({error: () => undefined});
        expect(ctx.registry.trackedCount).toBe(1);

        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();

        expect(ctx.registry.trackedCount).toBe(0);
    });

    it('holds an unsent completed upload, because it is still cancellable', () => {
        // The one entry that SHOULD be retained: attached, uploaded, not yet
        // sent. Removing the chip still has to be able to delete it.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();

        expect(ctx.registry.trackedCount).toBe(1);
        ctx.registry.cancel(p.id);
        expect(ctx.registry.trackedCount).toBe(0);
    });

    it('does not adopt an entry belonging to another thread', () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);

        ctx.registry.adopt('t2', p.id, p.file).subscribe();

        expect(ctx.uploads.length).toBe(2);
        expect(ctx.uploads[1].threadId).toBe('t2');
    });

    it('falls back to a fresh request when the eager attempt already failed', () => {
        // The deferred path is the fallback and is never removed: an eager
        // failure must leave no trace that could stall the send.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].fail({status: 503});

        ctx.registry.adopt('t1', p.id, p.file).subscribe({error: () => undefined});

        expect(ctx.uploads.length).toBe(2);
    });

    // --- cancel -----------------------------------------------------------

    it('cancel before completion aborts the transfer and issues no DELETE', () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit({kind: 'progress', loaded: 10, total: 100});

        ctx.registry.cancel(p.id);

        expect(ctx.uploads[0].aborted).toBe(true);
        expect(ctx.deletes.length).toBe(0);
    });

    it('records a cancel as intent, so it can never read as "Network error"', () => {
        // Angular reports a user abort and a dead network identically as
        // status 0. Intent has to be tracked explicitly and checked BEFORE
        // anything reads `status`, or removing a chip surfaces the misleading
        // message from the service-worker incident.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);

        ctx.registry.cancel(p.id);

        expect(ctx.registry.wasCancelled(p.id)).toBe(true);
        // A file that was never eagerly started (the deferred path) has no
        // cancellation to report, so the send stage must not mistake it for one.
        expect(ctx.registry.wasCancelled('never-started')).toBe(false);
        expect(ctx.api.humanizeUploadError).not.toHaveBeenCalled();
    });

    it('distinguishes a cancel from an HTTP-shaped failure', () => {
        // The terminal value a cancelled stream carries. Adoption is exclusive,
        // so a subscribed stream cannot currently see this — it exists so the
        // subject always terminates (no consumer can hang on an aborted
        // upload) and so the distinction survives if that ever changes.
        expect(isUploadCancelled(new UploadCancelledError('p1'))).toBe(true);
        expect(isUploadCancelled({status: 0})).toBe(false);
        expect(isUploadCancelled(undefined)).toBe(false);
    });

    it('cancel AFTER completion deletes the file the server actually stored', async () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        // The server renamed it (`_1` collision suffix) — the DELETE has to
        // name what landed, not what we asked for.
        ctx.uploads[0].emit(done(uploaded('a_1.pdf')));
        ctx.uploads[0].finish();

        ctx.registry.cancel(p.id);
        await tick();

        expect(ctx.deletes).toHaveLength(1);
        expect(ctx.deletes[0]).toMatchObject({threadId: 't1', path: 'a_1.pdf'});
        expect(ctx.uploads[0].aborted).toBe(false); // nothing left to abort
    });

    it('cancel once the body has fully arrived lets it land, then deletes it', async () => {
        // asyncio.to_thread work is uncancellable: once the request body is in,
        // the server finishes the write whatever the client does. Aborting here
        // would ALSO throw away the server-assigned name — the only thing that
        // can address the file for deletion. So: let it land, then delete.
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit({kind: 'progress', loaded: 100, total: 100});

        ctx.registry.cancel(p.id);
        expect(ctx.uploads[0].aborted).toBe(false);
        expect(ctx.deletes.length).toBe(0);

        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();
        await tick();

        expect(ctx.deletes.map((d) => d.path)).toEqual(['a.pdf']);
    });

    it('deletes a zip as one subtree rather than one call per member', async () => {
        const p = preview('bundle.zip');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit(
            done(uploaded('bundle/a.txt'), uploaded('bundle/sub/b.txt')),
        );
        ctx.uploads[0].finish();

        ctx.registry.cancel(p.id);
        await tick();

        expect(ctx.deletes.map((d) => d.path)).toEqual(['bundle']);
    });

    it('leaves an ADOPTED upload alone — the outbox owns it once the user sends', () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.registry.adopt('t1', p.id, p.file).subscribe({error: () => undefined});

        ctx.registry.cancel(p.id);

        expect(ctx.uploads[0].aborted).toBe(false);
    });

    // --- re-upload ordering ------------------------------------------------

    it('holds a re-upload of the same name until its DELETE has landed', async () => {
        // The backend has no upload idempotency and eager upload widens the
        // window: a DELETE still in flight when the re-upload claims a name
        // could remove the file that just replaced it. Serialize on the client.
        const first = preview('a.pdf', 'p1');
        ctx.registry.start('t1', first);
        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();
        ctx.registry.cancel(first.id);
        await tick();
        expect(ctx.deletes).toHaveLength(1);

        // Same file re-attached (a new chip, so a new preview id).
        ctx.registry.start('t1', preview('a.pdf', 'p2'));
        await tick();
        expect(ctx.uploads).toHaveLength(1); // still waiting on the DELETE

        ctx.deletes[0].settle();
        await tick();
        expect(ctx.uploads).toHaveLength(2);
    });

    it('barriers BOTH DELETEs when two share a requested name', async () => {
        // The key is the name a re-upload would REQUEST; the targets are the
        // names the server ASSIGNED. Two chips for same-named but different
        // files land as `a.pdf` and `a_1.pdf` and share the key `t1 a.pdf`.
        // Overwriting the map entry left the first DELETE unbarriered, so a
        // re-attached `a.pdf` could be uploaded and then deleted by that older
        // DELETE — exactly the race the barrier exists to close.
        const first = preview('a.pdf', 'p1');
        const second = preview('a.pdf', 'p2', 222); // different lastModified
        ctx.registry.start('t1', first);
        ctx.registry.start('t1', second);
        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();
        // The second is barriered behind the first (same requested name), so
        // it only reaches the wire once the first has landed.
        await tick();
        ctx.uploads[1].emit(done(uploaded('a_1.pdf')));
        ctx.uploads[1].finish();

        ctx.registry.cancel(first.id);
        ctx.registry.cancel(second.id);
        await tick();
        expect(ctx.deletes.map((d) => d.path)).toEqual(['a.pdf']); // chained

        // A re-attach must not start while EITHER delete is outstanding.
        ctx.registry.start('t1', preview('a.pdf', 'p3'));
        await tick();
        expect(ctx.uploads).toHaveLength(2);

        ctx.deletes[0].settle();
        await tick();
        expect(ctx.deletes.map((d) => d.path)).toEqual(['a.pdf', 'a_1.pdf']);
        expect(ctx.uploads).toHaveLength(2); // still barriered by the second

        ctx.deletes[1].settle();
        await tick();
        expect(ctx.uploads).toHaveLength(3);
    });

    it('a pending DELETE for a different name does not hold up an upload', async () => {
        const first = preview('a.pdf', 'p1');
        ctx.registry.start('t1', first);
        ctx.uploads[0].emit(done(uploaded('a.pdf')));
        ctx.uploads[0].finish();
        ctx.registry.cancel(first.id);
        await tick();

        ctx.registry.start('t1', preview('b.pdf', 'p2'));
        await tick();

        expect(ctx.uploads).toHaveLength(2);
    });

    it('a cancel that never reached the server issues no DELETE when it fails', async () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.uploads[0].emit({kind: 'progress', loaded: 100, total: 100});
        ctx.registry.cancel(p.id);

        ctx.uploads[0].fail({status: 0});
        await tick();

        expect(ctx.deletes).toHaveLength(0);
    });

    // --- abortAll ----------------------------------------------------------

    it('abortAll aborts what is in flight and deletes what already landed', async () => {
        const inflight = preview('a.pdf', 'p1');
        const landed = preview('b.pdf', 'p2');
        ctx.registry.start('t1', inflight);
        ctx.registry.start('t1', landed);
        ctx.uploads[1].emit(done(uploaded('b.pdf')));
        ctx.uploads[1].finish();

        ctx.registry.abortAll();
        await tick();

        expect(ctx.uploads[0].aborted).toBe(true);
        expect(ctx.deletes.map((d) => d.path)).toEqual(['b.pdf']);
    });

    it('abortAll leaves adopted uploads running', () => {
        const p = preview('a.pdf');
        ctx.registry.start('t1', p);
        ctx.registry.adopt('t1', p.id, p.file).subscribe({error: () => undefined});

        ctx.registry.abortAll();

        expect(ctx.uploads[0].aborted).toBe(false);
    });

    it('does not start an upload for a preview with no File handle', () => {
        ctx.registry.start('t1', {...preview('a.pdf'), file: undefined as unknown as File});
        expect(ctx.uploads).toHaveLength(0);
    });

    // --- bounded concurrency + the same-name barrier (spec §5.3) ------------

    describe('request gating', () => {
        it(`never puts more than ${MAX_CONCURRENT_UPLOADS} requests on the wire`, async () => {
            // Spec §5.3 asks for a ceiling of 2; the shipped code started one
            // unbounded request per attached file. The server's own virtual-tier
            // semaphore is 4 and shared across ALL users, and every request is a
            // fully-buffered body on both ends.
            for (let i = 0; i < 5; i++) ctx.registry.start('t1', preview(`f${i}.pdf`, `p${i}`));
            await tick();

            expect(ctx.uploads).toHaveLength(MAX_CONCURRENT_UPLOADS);
            expect(ctx.uploads.map((u) => u.file.name)).toEqual(['f0.pdf', 'f1.pdf']);
        });

        it('admits the next queued file the moment a slot frees, in order', async () => {
            for (let i = 0; i < 4; i++) ctx.registry.start('t1', preview(`f${i}.pdf`, `p${i}`));
            await tick();

            ctx.uploads[0].emit(done(uploaded('f0.pdf')));
            ctx.uploads[0].finish();
            await tick();
            expect(ctx.uploads.map((u) => u.file.name)).toEqual([
                'f0.pdf',
                'f1.pdf',
                'f2.pdf',
            ]);

            ctx.uploads[1].emit(done(uploaded('f1.pdf')));
            ctx.uploads[1].finish();
            await tick();
            expect(ctx.uploads).toHaveLength(4);
        });

        it('gives the slot back when a queued upload is cancelled before it starts', async () => {
            // A gate that leaked its slot on cancel would strangle the queue
            // after two removed chips and never recover.
            ctx.registry.start('t1', preview('f0.pdf', 'p0'));
            ctx.registry.start('t1', preview('f1.pdf', 'p1'));
            ctx.registry.start('t1', preview('f2.pdf', 'p2'));
            await tick();
            expect(ctx.uploads).toHaveLength(2);

            ctx.registry.cancel('p0');
            await tick();

            expect(ctx.uploads.map((u) => u.file.name)).toEqual([
                'f0.pdf',
                'f1.pdf',
                'f2.pdf',
            ]);
        });

        it('serializes two same-named files so the second cannot truncate the first', async () => {
            // THE DATA-LOSS CASE. The dedupe key is name|size|lastModified, so
            // two *different* files called a.pdf both attach. Run concurrently,
            // each lists uploads/ before either has written
            // (thread_uploads.py:763-769), both claim `a.pdf`, and the second
            // write truncates the first. The old batched POST resolved names
            // inside ONE listing and could not hit this.
            const first = preview('a.pdf', 'p1');
            const second = preview('a.pdf', 'p2', 222);
            ctx.registry.start('t1', first);
            ctx.registry.start('t1', second);
            await tick();

            // Second is held, even though a concurrency slot is free.
            expect(ctx.uploads).toHaveLength(1);

            ctx.uploads[0].emit(done(uploaded('a.pdf')));
            ctx.uploads[0].finish();
            await tick();

            // It lists AFTER the first landed, so the server hands it `a_1.pdf`.
            expect(ctx.uploads).toHaveLength(2);
            expect(ctx.uploads[1].file.name).toBe('a.pdf');
        });

        it('releases the name even when the first same-named upload fails', async () => {
            const first = preview('a.pdf', 'p1');
            ctx.registry.start('t1', first);
            ctx.registry.start('t1', preview('a.pdf', 'p2', 222));
            await tick();
            expect(ctx.uploads).toHaveLength(1);

            ctx.uploads[0].fail({status: 503});
            await tick();

            expect(ctx.uploads).toHaveLength(2);
        });

        it('does not hold up a different name behind a slow one', async () => {
            ctx.registry.start('t1', preview('a.pdf', 'p1'));
            ctx.registry.start('t1', preview('b.pdf', 'p2'));
            await tick();

            expect(ctx.uploads).toHaveLength(2);
        });

        it('does not hold up the same name in a DIFFERENT thread', async () => {
            // Collisions are resolved per workspace; the key has to carry the
            // thread or two sessions would needlessly serialize.
            ctx.registry.start('t1', preview('a.pdf', 'p1'));
            ctx.registry.start('t2', preview('a.pdf', 'p2'));
            await tick();

            expect(ctx.uploads).toHaveLength(2);
        });

        it("the send path's fallback request is gated too", async () => {
            // adopt()'s fallback is the deferred upload path. Issuing it raw
            // would let a flush-time upload race an eager one for the same name
            // — the truncation this barrier exists to prevent.
            ctx.registry.start('t1', preview('a.pdf', 'p1'));
            await tick();
            expect(ctx.uploads).toHaveLength(1);

            // A different preview id, so nothing to adopt: fresh request.
            ctx.registry
                .adopt('t1', 'never-started', new File(['y'], 'a.pdf'))
                .subscribe({error: () => undefined});
            await tick();

            expect(ctx.uploads).toHaveLength(1); // barriered behind the eager one

            ctx.uploads[0].emit(done(uploaded('a.pdf')));
            ctx.uploads[0].finish();
            await tick();

            expect(ctx.uploads).toHaveLength(2);
        });
    });
});
