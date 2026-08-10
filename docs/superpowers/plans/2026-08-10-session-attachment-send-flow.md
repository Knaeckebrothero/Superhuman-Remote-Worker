# Session Attachment Send Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a message with attachments appear in the transcript the instant the user hits Enter, with the composer fully cleared, while the upload continues visibly in the background.

**Architecture:** The upload stops being a precondition of `sendMessage` and becomes stage 0 of the existing send outbox. The outbox already models queued-not-yet-accepted, single-flight, retryable-vs-terminal, rollback and retry/discard — the upload inherits all of it. Uploads split from one batched multipart POST into one request per file, which keeps each request under the 100 MB Cloudflare body cap and makes per-file progress, cancel and retry expressible. Slice 3 adds eager upload on attach plus the delete endpoint that makes cancellation honest.

**Tech Stack:** Angular 21 (standalone + signals, inline templates), RxJS, Vitest 4 + jsdom, Playwright 1.59, Transloco i18n, FastAPI + paramiko/rclone on the backend.

**Spec:** `docs/features/session_attachment_send_flow.md`. Read §4 (constraints) and §5 (design) before starting any task.

## Global Constraints

- **Never issue an upload outside Angular's `HttpClient`.** A raw `XMLHttpRequest` or `fetch` bypasses `auth.interceptor.ts:43-53`, losing `ngsw-bypass: 1`, `X-CSRF: 1` and `withCredentials`. Without `ngsw-bypass` the Angular service worker re-issues the request through `scope.fetch()`, which **corrupts multipart bodies** (`docs/done/cockpit_service_worker_breaks_file_uploads.md`, fix `1195b54d`) and independently destroys XHR upload-progress events.
- **New queued-bubble CSS goes in `cockpit/src/styles/_chat-queued.scss`, never in `persistent-chat.component.scss`.** The component sheet sits ~0.5 kB under its `anyComponentStyle` budget (`docs/issues/persistent_chat_component_style_budget.md`). Read that file's header comment for the exact specificity chains needed to outrank emulated encapsulation: `.message.message-user.queued.stalled` is (0,4,0) and `.avatar .avatar-icon` is (0,6,0).
- **Never mutate an object held inside a signal in place.** `sendMessage` currently does this at `persistent-chat.service.ts:2486/2490/2494` and nothing re-renders. Always `signal.update(...)` with fresh object identities.
- **Backend caps are 100 MB per file and 20 files per request** (`orchestrator/services/thread_uploads.py:69-70`). The frontend's 5 GB / 100 (`file-handling.service.ts:14,17`) is wrong.
- **The backend has no upload idempotency.** `_claim_name` (`thread_uploads.py:153-164`) resolves collisions by `_1`/`_2` suffix against a live directory listing, so re-uploading a file that already succeeded silently duplicates it. Never re-upload a file whose result is already cached.
- **Test runner is `npx vitest run` from `cockpit/`.** `tsc -p tsconfig.json` is a NO-OP in this repo — use `tsconfig.app.json`. There is no CI typecheck.
- **`PersistentChatComponent` cannot be mounted in TestBed** (NG0951). Component-level tests in `persistent-chat.component.spec.ts` are pure-function only. Do not attempt to render it.
- **Do not push.** Commit locally only; the user pushes.

---

## File Structure

**Slice 1 — the reported bug (frontend only, no backend change)**

| File | Responsibility |
|---|---|
| `cockpit/src/app/core/models/file.model.ts` | `ChatAttachment` moves here? **No** — it stays in `persistent-chat.service.ts`. This file gains nothing in Slice 1. |
| `cockpit/src/app/core/services/persistent-chat.service.ts` | `ChatAttachment.id`, `PendingUpload`, `OutboxItem.pendingFiles`/`threadId`, `sendMessage` reorder, `_flushOutbox` upload stage, `discardQueuedSend` guard |
| `cockpit/src/app/core/services/upload-stage.ts` | **NEW.** Pure helpers extracted so they are testable without the 4300-line service: hint composition, upload-error classification, aggregate progress. |
| `cockpit/src/app/core/services/api.service.ts` | `uploadToThread` → one request per file |
| `cockpit/src/app/core/services/file-handling.service.ts` | Real caps; rejected files reported instead of silently dropped |
| `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` | Bubble track expression, stage line, composer state |
| `cockpit/src/styles/_chat-queued.scss` | Upload stage line styling |
| `cockpit/src/assets/i18n/{en,de-DE}.json` | New keys + keying the hardcoded English upload strings |

**Slice 2 — progress**

| File | Responsibility |
|---|---|
| `cockpit/src/app/app.config.ts` | Drop `withFetch()` |
| `cockpit/src/app/core/services/api.service.ts` | `reportProgress` + `observe: 'events'` |
| `cockpit/src/app/core/services/upload-stage.ts` | Progress aggregation across files |
| `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` | Determinate bar + a11y |
| `cockpit/e2e/` | Playwright progress check against a prod build |

**Slice 3 — eager upload + delete**

| File | Responsibility |
|---|---|
| `cockpit/src/app/core/services/upload-registry.service.ts` | **NEW.** Root service owning in-flight eager uploads keyed by `FilePreview.id`; cancel-intent tracking |
| `cockpit/src/app/core/services/persistent-chat.service.ts` | Adopt registry entries at send; thread-switch abort |
| `orchestrator/services/thread_uploads.py` | `_safe_upload_relpath` validator, `_sftp_delete_file`, `_virtual_delete_file` |
| `orchestrator/main.py` | `DELETE /api/persistent/threads/{id}/uploads/{path:path}` |
| `tests/test_thread_uploads.py` | Traversal + symlink cases |

---

# SLICE 1 — The reported bug

## Task 1: Extract pure upload-stage helpers

Isolates the logic that later tasks depend on into a file small enough to hold in context and test without the 4300-line service.

**Files:**
- Create: `cockpit/src/app/core/services/upload-stage.ts`
- Create: `cockpit/src/app/core/services/upload-stage.spec.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `PendingUpload` interface; `composeAgentContent(text, names)`; `classifyUploadFailure(status)` → `'terminal' | 'retryable'`; `uploadSummary(files)` → `{done, total, allDone, firstFailed}`.

- [ ] **Step 1: Write the failing test**

Create `cockpit/src/app/core/services/upload-stage.spec.ts`:

```ts
import {describe, expect, it} from 'vitest';
import {
    classifyUploadFailure,
    composeAgentContent,
    uploadSummary,
    type PendingUpload,
} from './upload-stage';

function pending(over: Partial<PendingUpload> = {}): PendingUpload {
    return {
        id: 'p1',
        file: new File(['x'], 'a.pdf'),
        name: 'a.pdf',
        size: 1,
        mimeType: 'application/pdf',
        loaded: 0,
        total: 1,
        status: 'queued',
        ...over,
    };
}

describe('composeAgentContent', () => {
    it('appends the uploads hint after the typed text', () => {
        expect(composeAgentContent('look at this', ['a.pdf', 'b.pdf'])).toBe(
            'look at this\n\n[Attached files in uploads/: a.pdf, b.pdf]',
        );
    });

    it('sends the hint alone when there is no typed text', () => {
        expect(composeAgentContent('', ['a.pdf'])).toBe('[Attached files in uploads/: a.pdf]');
    });

    it('returns the text unchanged when there are no files', () => {
        expect(composeAgentContent('hello', [])).toBe('hello');
    });
});

describe('classifyUploadFailure', () => {
    it('treats size and count rejections as terminal', () => {
        expect(classifyUploadFailure(413)).toBe('terminal');
        expect(classifyUploadFailure(400)).toBe('terminal');
    });

    it('treats a not-ready workspace as retryable', () => {
        expect(classifyUploadFailure(409)).toBe('retryable');
    });

    it('treats transport failures as retryable', () => {
        expect(classifyUploadFailure(0)).toBe('retryable');
        expect(classifyUploadFailure(502)).toBe('retryable');
        expect(classifyUploadFailure(503)).toBe('retryable');
    });
});

describe('uploadSummary', () => {
    it('counts resolved files and reports not-all-done', () => {
        const s = uploadSummary([
            pending({id: 'a', status: 'done'}),
            pending({id: 'b', status: 'uploading'}),
        ]);
        expect(s).toMatchObject({done: 1, total: 2, allDone: false});
        expect(s.firstFailed).toBeUndefined();
    });

    it('reports allDone when every file resolved', () => {
        const s = uploadSummary([pending({id: 'a', status: 'done'})]);
        expect(s.allDone).toBe(true);
    });

    it('surfaces the first failed file', () => {
        const s = uploadSummary([
            pending({id: 'a', status: 'done'}),
            pending({id: 'b', status: 'failed', error: 'File too large'}),
        ]);
        expect(s.firstFailed?.id).toBe('b');
        expect(s.allDone).toBe(false);
    });

    it('treats an empty list as done', () => {
        expect(uploadSummary([]).allDone).toBe(true);
    });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/upload-stage.spec.ts`
Expected: FAIL — `Failed to resolve import "./upload-stage"`.

- [ ] **Step 3: Write minimal implementation**

Create `cockpit/src/app/core/services/upload-stage.ts`:

```ts
/**
 * Pure helpers for the outbox's upload stage.
 *
 * Lives outside persistent-chat.service.ts so the send/upload logic that
 * matters most is testable without instantiating a 4300-line service.
 * See docs/features/session_attachment_send_flow.md §5.
 */
import type {ChatAttachment} from './persistent-chat.service';

/** One file queued on an outbox item, before or during its upload. */
export interface PendingUpload {
    /** FilePreview.id — a stable key that exists before the server path does. */
    id: string;
    file: File;
    name: string;
    size: number;
    mimeType: string;
    /** Bytes sent so far; 0 until progress reporting lands (Slice 2). */
    loaded: number;
    /** Total bytes, or null when the browser cannot compute it. */
    total: number | null;
    status: 'queued' | 'uploading' | 'done' | 'failed';
    error?: string;
    /** Set once the server confirms; retries must never re-upload these. */
    resolved?: ChatAttachment;
}

/**
 * What the agent sees: the user's text plus a plain-language hint naming the
 * uploaded files. Kept identical to the pre-refactor string so existing
 * sessions and prompt expectations don't shift.
 */
export function composeAgentContent(text: string, names: readonly string[]): string {
    if (names.length === 0) return text;
    const hint = `[Attached files in uploads/: ${names.join(', ')}]`;
    return text ? `${text}\n\n${hint}` : hint;
}

/**
 * Terminal failures will never succeed on retry, so the bubble must offer a way
 * out rather than a Retry button that can only fail again:
 *   400 — too many files, or no files provided
 *   413 — a file exceeds the backend's 100MB cap
 * Everything else (409 workspace-not-ready, 502/503 transport, 0 offline) is
 * retryable and keeps the item queued.
 */
export function classifyUploadFailure(status: number): 'terminal' | 'retryable' {
    return status === 400 || status === 413 ? 'terminal' : 'retryable';
}

/** Aggregate state of one outbox item's files, for the bubble's stage line. */
export function uploadSummary(files: readonly PendingUpload[]): {
    done: number;
    total: number;
    allDone: boolean;
    firstFailed?: PendingUpload;
} {
    const done = files.filter((f) => f.status === 'done').length;
    return {
        done,
        total: files.length,
        allDone: done === files.length,
        firstFailed: files.find((f) => f.status === 'failed'),
    };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd cockpit && npx vitest run src/app/core/services/upload-stage.spec.ts`
Expected: PASS — 10 tests.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/core/services/upload-stage.ts cockpit/src/app/core/services/upload-stage.spec.ts
git commit -m "feat(cockpit): extract pure upload-stage helpers for the send outbox"
```

---

## Task 2: One upload request per file

Splits the batched multipart POST. This keeps each request under the 100 MB Cloudflare Tunnel body cap (the reported 3-file case was a 90.55 MB single body), stops one bad file from failing the whole message, and is the precondition for per-file progress and cancel.

**Files:**
- Modify: `cockpit/src/app/core/services/api.service.ts:1139-1179`
- Modify: `cockpit/src/app/core/services/api.service.spec.ts`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `uploadOneToThread(threadId: string, file: File): Observable<ThreadUploadedFile[]>` — returns the server's `files[]` for that one request. It returns an **array**, not a single entry, because a `.zip` expands to one entry per extracted member (`thread_uploads.py:417-531`) and the caller must name every one in the agent hint. `uploadToThread` is **removed**; all callers move to the new method.

- [ ] **Step 1: Write the failing test**

Append to `cockpit/src/app/core/services/api.service.spec.ts`, inside the existing top-level `describe`:

```ts
describe('uploadOneToThread', () => {
    it('posts a single file as multipart and returns its server entries', () => {
        const file = new File(['abc'], 'report.pdf', {type: 'application/pdf'});
        let got: ThreadUploadedFile[] | undefined;

        api.uploadOneToThread('t1', file).subscribe((files) => (got = files));

        const req = httpMock.expectOne((r) => r.url.endsWith('/persistent/threads/t1/uploads'));
        expect(req.request.method).toBe('POST');
        const body = req.request.body as FormData;
        expect(body.getAll('files').length).toBe(1);

        req.flush({
            thread_id: 't1',
            files: [{name: 'report.pdf', size: 3, mime_type: 'application/pdf', path: 'uploads/report.pdf'}],
        });

        expect(got).toEqual([
            {name: 'report.pdf', size: 3, mime_type: 'application/pdf', path: 'uploads/report.pdf'},
        ]);
    });

    it('returns every extracted member when the file is an archive', () => {
        const zip = new File(['x'], 'bundle.zip', {type: 'application/zip'});
        let got: ThreadUploadedFile[] | undefined;

        api.uploadOneToThread('t1', zip).subscribe((files) => (got = files));

        httpMock.expectOne((r) => r.url.endsWith('/persistent/threads/t1/uploads')).flush({
            thread_id: 't1',
            files: [
                {name: 'bundle/a.txt', size: 1, mime_type: 'text/plain', path: 'uploads/bundle/a.txt'},
                {name: 'bundle/b.txt', size: 1, mime_type: 'text/plain', path: 'uploads/bundle/b.txt'},
            ],
        });

        expect(got?.length).toBe(2);
    });

    it('rethrows the HttpErrorResponse so the caller can read status and detail', () => {
        let err: unknown;
        api.uploadOneToThread('t1', new File([''], 'a.pdf')).subscribe({error: (e) => (err = e)});

        httpMock
            .expectOne((r) => r.url.endsWith('/persistent/threads/t1/uploads'))
            .flush({detail: "File 'a.pdf' exceeds 100MB"}, {status: 413, statusText: 'Payload Too Large'});

        expect((err as HttpErrorResponse).status).toBe(413);
        expect(api.humanizeUploadError(err)).toBe("File 'a.pdf' exceeds 100MB");
    });
});
```

Add `import {HttpErrorResponse} from '@angular/common/http';` and `import type {ThreadUploadedFile} from '../models/file.model';` to the spec's imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/api.service.spec.ts`
Expected: FAIL — `api.uploadOneToThread is not a function`.

- [ ] **Step 3: Write minimal implementation**

In `cockpit/src/app/core/services/api.service.ts`, replace the `uploadToThread` method (currently `:1151-1165`) with:

```ts
  /**
   * Push ONE file into the persistent thread's live workspace uploads/ directory.
   *
   * Deliberately one request per file rather than one batched multipart POST.
   * Three reasons, in order of severity:
   *   1. The deployment traverses a Cloudflare Tunnel whose request-body cap is
   *      100MB. A batched send sums every file into that ceiling; per-file keeps
   *      each request under the backend's own 100MB per-file cap.
   *   2. A batch fails atomically from the client's point of view, so one
   *      oversized file failed the whole message.
   *   3. Per-file progress and per-file cancel are not expressible otherwise.
   *
   * Returns the server's `files[]` for this request — an ARRAY, because a .zip
   * expands into one entry per extracted member (services/thread_uploads.py).
   *
   * Errors are RE-THROWN (not swallowed to `null`) so the caller can read the
   * status and the server-side `detail` field. Use `humanizeUploadError()` to
   * map an arbitrary HttpErrorResponse to a user-facing string.
   */
  uploadOneToThread(threadId: string, file: File): Observable<ThreadUploadedFile[]> {
    const formData = new FormData();
    formData.append('files', file, file.name);
    return this.http
      .post<ThreadUploadResponse>(
        `${this.baseUrl}/persistent/threads/${threadId}/uploads`,
        formData,
      )
      .pipe(
        map((res) => res.files),
        catchError((error: HttpErrorResponse) => {
          console.error(`Failed to upload ${file.name} to thread ${threadId}:`, error);
          return throwError(() => error);
        }),
      );
  }
```

Ensure `map` is imported from `rxjs/operators` (or `rxjs`) alongside the existing `catchError`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd cockpit && npx vitest run src/app/core/services/api.service.spec.ts`
Expected: PASS.

- [ ] **Step 5: Verify no stale callers remain**

Run: `cd cockpit && rg -n "uploadToThread" src/`
Expected: hits only in the four service spec mocks (`persistent-chat.service.spec.ts`, `.outbox.spec.ts`, `.draft.spec.ts`, `.rewind.spec.ts`) and `persistent-chat.service.ts:2488`. Those are updated in Task 4. If any *other* production caller appears, update it now.

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/core/services/api.service.ts cockpit/src/app/core/services/api.service.spec.ts
git commit -m "feat(cockpit): upload thread attachments one request per file"
```

---

## Task 3: Stable attachment ids on the bubble

`persistent-chat.component.ts:1240` tracks bubble attachment chips by `att.path`, which does not exist before the upload. Two pre-upload chips produce duplicate `undefined` keys — `NG0955` in dev builds, silent DOM mis-reconciliation in prod — and when paths later arrive every key changes, destroying and recreating every chip node.

**Files:**
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts:129-136`
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:1238-1252`
- Modify: `cockpit/src/app/core/services/turn-reducer.spec.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `ChatAttachment` gains `id: string` (required) and `path` becomes `path?: string`.

- [ ] **Step 1: Write the failing test**

Append to `cockpit/src/app/core/services/turn-reducer.spec.ts`:

```ts
describe('user_message attachments before upload', () => {
    it('keeps distinct ids for attachments that have no server path yet', () => {
        const state = reduce(initialState(), {
            type: 'user_message',
            id: 'user-1',
            content: 'here',
            attachments: [
                {id: 'p1', name: 'a.pdf', size: 1, mimeType: 'application/pdf'},
                {id: 'p2', name: 'b.pdf', size: 2, mimeType: 'application/pdf'},
            ],
            timestamp: 1,
        });

        const turn = state.turns[state.turns.length - 1];
        expect(turn.kind).toBe('user');
        const ids = (turn as UserTurn).attachments?.map((a) => a.id);
        expect(ids).toEqual(['p1', 'p2']);
        expect(new Set(ids).size).toBe(2);
    });
});
```

Match the existing spec's helper names for `reduce` / `initialState` — read the top of `turn-reducer.spec.ts` first and reuse whatever it already imports rather than inventing names.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/turn-reducer.spec.ts`
Expected: FAIL — TypeScript rejects the object literals because `ChatAttachment.path` is required and `id` does not exist.

- [ ] **Step 3: Write minimal implementation**

In `persistent-chat.service.ts`, replace the `ChatAttachment` interface at `:129-136`:

```ts
/** Attachment chip shown alongside a user message. */
export interface ChatAttachment {
    /**
     * Stable local id (the FilePreview.id it came from). Exists BEFORE the
     * upload does, which `path` does not — so this is what the bubble's
     * @for tracks by. Tracking by `path` gave every pre-upload chip the key
     * `undefined`: duplicate keys (NG0955) with two files, and a full
     * destroy/recreate of every chip node once the real paths arrived.
     */
    id: string;
    name: string;
    size: number;
    mimeType: string;
    /** Workspace-relative path, e.g. "uploads/photo.jpg". Absent until the
     *  upload resolves. */
    path?: string;
}
```

In `persistent-chat.component.ts`, in the `@case ('user')` block, change the track expression and make the two `path`-dependent bindings safe:

```html
                      @for (att of turn.attachments; track att.id) {
                        <span class="user-attachment-chip" [title]="att.path ?? att.name">
                          <app-icon size="sm">{{
                            att.mimeType.startsWith('image/') ? 'image' :
                            att.mimeType.startsWith('video/') ? 'videocam' :
                            att.mimeType.startsWith('audio/') ? 'audiotrack' :
                            'description'
                          }}</app-icon>
```

- [ ] **Step 4: Fix the compile errors the type change surfaces**

Run: `cd cockpit && npx tsc -p tsconfig.app.json --noEmit`
Every construction site of `ChatAttachment` now needs an `id`. The only production one today is `persistent-chat.service.ts:2506-2511`; give it `id: crypto.randomUUID()` as a placeholder — Task 4 replaces that block wholesale with the `PendingUpload.id`. Fix spec fixtures the same way.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd cockpit && npx vitest run src/app/core/services/turn-reducer.spec.ts src/app/core/models/turn.model.spec.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/core/services/persistent-chat.service.ts cockpit/src/app/views/persistent-chat/persistent-chat.component.ts cockpit/src/app/core/services/turn-reducer.spec.ts
git commit -m "fix(cockpit): track user-message attachment chips by stable local id"
```

---

## Task 4: Move the upload into the outbox flush

The core change. `sendMessage` becomes synchronous through the dispatch; `_flushOutbox` gains an upload stage before the POST.

**Files:**
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts:145-157` (`OutboxItem`), `:2460-2574` (`sendMessage`), `:2585-2649` (`_flushOutbox`), `:2672-2681` (`discardQueuedSend`)
- Modify: `cockpit/src/app/core/services/persistent-chat.service.outbox.spec.ts`

**Interfaces:**
- Consumes: `PendingUpload`, `composeAgentContent`, `classifyUploadFailure`, `uploadSummary` from Task 1; `uploadOneToThread` from Task 2; `ChatAttachment.id` from Task 3.
- Produces: `OutboxItem` gains `pendingFiles?: PendingUpload[]`, `threadId: string`, and `content` becomes optional (computed at flush time). A new private `_uploadStage(head): Promise<{ok: boolean; status: number}>`.

- [ ] **Step 1: Write the failing test**

Append to `cockpit/src/app/core/services/persistent-chat.service.outbox.spec.ts`. Reuse the file's existing `flushTick()`, `readySession()` and `inputPosts()` helpers — read them first (around `:44`, `:139-149`).

```ts
describe('upload as outbox stage 0', () => {
    it('dispatches the bubble and clears the composer before the upload resolves', async () => {
        await readySession();
        const gate = new Subject<ThreadUploadedFile[]>();
        mockApi.uploadOneToThread = vi.fn().mockReturnValue(gate);

        service.addAttachments([filePreview('a.pdf')]);
        void service.sendMessage('look at this');

        // Synchronously after send: bubble exists, composer is empty, nothing POSTed.
        expect(service.turns().some((t) => t.kind === 'user' && t.content === 'look at this')).toBe(true);
        expect(service.pendingAttachments()).toEqual([]);
        expect(inputPosts().length).toBe(0);

        gate.next([{name: 'a.pdf', size: 1, mime_type: 'application/pdf', path: 'uploads/a.pdf'}]);
        gate.complete();
        await flushTick();

        expect(inputPosts().length).toBe(1);
        expect(inputPosts()[0].request.body.content).toBe(
            'look at this\n\n[Attached files in uploads/: a.pdf]',
        );
    });

    it('keeps the bubble and stalls the queue when the upload fails', async () => {
        await readySession();
        mockApi.uploadOneToThread = vi
            .fn()
            .mockReturnValue(throwError(() => new HttpErrorResponse({status: 503})));

        service.addAttachments([filePreview('a.pdf')]);
        await service.sendMessage('hi');
        await flushTick();

        expect(service.turns().some((t) => t.kind === 'user' && t.content === 'hi')).toBe(true);
        expect(service.outbox().length).toBe(1);
        expect(service.outboxStalled()).toBe(true);
        expect(inputPosts().length).toBe(0);
    });

    it('does not re-upload a file that already succeeded when the queue is retried', async () => {
        await readySession();
        const upload = vi
            .fn()
            .mockReturnValueOnce(of([{name: 'a.pdf', size: 1, mime_type: 'application/pdf', path: 'uploads/a.pdf'}]))
            .mockReturnValueOnce(throwError(() => new HttpErrorResponse({status: 503})));
        mockApi.uploadOneToThread = upload;

        service.addAttachments([filePreview('a.pdf'), filePreview('b.pdf')]);
        await service.sendMessage('two files');
        await flushTick();
        expect(service.outboxStalled()).toBe(true);
        expect(upload).toHaveBeenCalledTimes(2);

        upload.mockReturnValue(of([{name: 'b.pdf', size: 1, mime_type: 'application/pdf', path: 'uploads/b.pdf'}]));
        service.retryQueuedSends();
        await flushTick();

        // 3 total, NOT 4 — a.pdf is never re-sent. The backend has no
        // idempotency: a re-upload would land as a_1.pdf.
        expect(upload).toHaveBeenCalledTimes(3);
        expect(inputPosts()[0].request.body.content).toContain('a.pdf, b.pdf');
    });

    it('drops the resolution when the thread changed mid-upload', async () => {
        await readySession();
        const gate = new Subject<ThreadUploadedFile[]>();
        mockApi.uploadOneToThread = vi.fn().mockReturnValue(gate);

        service.addAttachments([filePreview('a.pdf')]);
        void service.sendMessage('hi');
        await service.connect('other-thread');

        gate.next([{name: 'a.pdf', size: 1, mime_type: 'application/pdf', path: 'uploads/a.pdf'}]);
        gate.complete();
        await flushTick();

        expect(inputPosts().length).toBe(0);
    });

    it('refuses to discard an item whose upload is in flight', async () => {
        await readySession();
        mockApi.uploadOneToThread = vi.fn().mockReturnValue(new Subject<ThreadUploadedFile[]>());

        service.addAttachments([filePreview('a.pdf')]);
        void service.sendMessage('hi');
        await flushTick();

        const localId = service.outbox()[0].localId;
        service.discardQueuedSend(localId);
        expect(service.outbox().length).toBe(1);
    });
});
```

Add a `filePreview(name: string): FilePreview` helper near the file's other helpers:

```ts
function filePreview(name: string): FilePreview {
    return {
        id: `preview-${name}`,
        file: new File(['x'], name),
        name,
        size: 1,
        sizeFormatted: '1 B',
        type: FileType.DOCUMENT,
        mimeType: 'application/pdf',
        uploadStatus: UploadStatus.PENDING,
    };
}
```

Update the shared mock at `:62-65` (and the same block in the other three service specs) from `uploadToThread` to `uploadOneToThread: vi.fn().mockReturnValue(of([]))`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/persistent-chat.service.outbox.spec.ts`
Expected: FAIL — the first test fails because no bubble exists until the upload resolves.

- [ ] **Step 3: Write the implementation — `OutboxItem`**

Replace the interface at `:145-157`:

```ts
export interface OutboxItem {
    /** The optimistic user-bubble's makeLocalId('user') — bubble↔queue link. */
    localId: string;
    /** What gets POSTed. Undefined until the upload stage resolves the file
     *  names that go into the attachment hint; computed in _flushOutbox. */
    content?: string;
    /** The user's typed text only — used to re-dispatch the bubble faithfully
     *  (without the attachment hint) after a history reload. */
    displayContent: string;
    /** Attachment chips to re-render on the bubble. Present from creation for
     *  the local descriptors; each gains `path` as its upload resolves. */
    attachments?: ChatAttachment[];
    /** Files still to upload, holding their File handles. Empty/absent once
     *  every file has resolved. */
    pendingFiles?: PendingUpload[];
    /** The thread this item belongs to. The flush already guards the POST
     *  against a mid-flight thread switch via tidAtPost; the upload stage is a
     *  second, longer await that needs the same guard, and an eagerly-started
     *  upload (Slice 3) could otherwise resolve into a foreign queue. */
    threadId: string;
    /** Flush attempts so far (diagnostic; there is deliberately no auto-retry). */
    attempts: number;
}
```

- [ ] **Step 4: Write the implementation — `sendMessage`**

Replace `:2476-2553` (the upload block through the outbox push) with:

```ts
        // Slash commands and attachments don't mix: the bypass above returns
        // before this point, which used to strand the chips in the composer
        // with no message and no error. Refuse explicitly instead.
        if (trimmed.startsWith('/') && queued.length > 0) {
            this.attachmentError.set(
                this.transloco.translate('chat.upload.slashCommandWithAttachments'),
            );
            return false;
        }

        // Local descriptors for the bubble. These exist BEFORE any byte moves —
        // that is the entire point of this design. `path` fills in per file as
        // the upload stage resolves it.
        const pendingFiles: PendingUpload[] = queued
            .filter((p) => p.file)
            .map((p) => ({
                id: p.id,
                file: p.file,
                name: p.name,
                size: p.size,
                mimeType: p.mimeType,
                loaded: 0,
                total: p.size || null,
                status: 'queued' as const,
            }));
        const attachments: ChatAttachment[] = pendingFiles.map((f) => ({
            id: f.id,
            name: f.name,
            size: f.size,
            mimeType: f.mimeType,
        }));

        // The user spoke, so they've resumed the agent themselves — drop any
        // queued auto-continuation rather than stacking a "continue where you
        // left off" behind whatever they just said. Safe against the
        // continuation's own send: workspace_upgrade.complete clears the flag
        // before calling us.
        this.continueAfterUpgrade.set(false);

        // ── One synchronous commit point ────────────────────────────────────
        // Bubble, queue entry and composer clearing happen together, with no
        // await between them. Previously the upload sat above this block, so
        // the text cleared at t=0 and the chips cleared at t=upload-complete,
        // with no bubble in between. Signal Desktop commits the same way
        // (register the message Pending, then upload inside the send job).
        const localId = makeLocalId('user');
        this.dispatch({
            type: 'user_message',
            id: localId,
            content: trimmed,
            attachments: attachments.length > 0 ? attachments : undefined,
            timestamp: Date.now(),
        });
        this.outbox.update((q) => [
            ...q,
            {
                localId,
                displayContent: trimmed,
                attachments: attachments.length > 0 ? attachments : undefined,
                pendingFiles: pendingFiles.length > 0 ? pendingFiles : undefined,
                threadId: this.threadId() ?? '',
                attempts: 0,
            },
        ]);
        this.clearAttachments();
        this.attachmentError.set(null);
        // ────────────────────────────────────────────────────────────────────
```

Delete the now-dead `let uploaded: ThreadUploadedFile[] = []` declaration, the `sendContent` computation, and the `threadId` null-check that produced `'Cannot upload: no active thread'` — a draft session now reaches `_createFromDraftSession` normally and uploads after the thread exists.

Note the draft-session branch below sets `threadId: ''` on the item. `_flushOutbox` only runs when `sessionReady()`, and by then `connect()` has set a real `threadId`; patch the item's `threadId` at the top of the flush loop when it is empty.

- [ ] **Step 5: Write the implementation — the upload stage**

Add a private method and call it from `_flushOutbox` before `_postInput`:

```ts
    /**
     * Stage 0 of a flush: upload any files this item still owes, one request
     * per file, and patch their resolved paths onto the item's attachments.
     *
     * Files that already resolved are skipped — the backend has no upload
     * idempotency (_claim_name resolves collisions with a _1 suffix against a
     * live listing), so re-uploading a success would silently duplicate it.
     *
     * Returns the same {ok, status} shape as _postInput so the flush's existing
     * terminal-vs-retryable branching applies unchanged.
     */
    private async _uploadStage(head: OutboxItem): Promise<{ok: boolean; status: number}> {
        const files = head.pendingFiles ?? [];
        if (files.every((f) => f.status === 'done')) return {ok: true, status: 200};

        this.uploadingItemId = head.localId;
        try {
            for (const f of files) {
                if (f.status === 'done') continue;
                this._patchPendingFile(head.localId, f.id, {status: 'uploading'});
                try {
                    const results = await firstValueFrom(
                        this.api.uploadOneToThread(head.threadId, f.file),
                    );
                    // A .zip expands to one entry per extracted member, so one
                    // PendingUpload can resolve into several ChatAttachments.
                    const resolved: ChatAttachment[] = results.map((r, i) => ({
                        id: i === 0 ? f.id : `${f.id}-${i}`,
                        name: r.name,
                        size: r.size,
                        mimeType: r.mime_type,
                        path: r.path,
                    }));
                    this._patchPendingFile(head.localId, f.id, {
                        status: 'done',
                        loaded: f.size,
                        resolved: resolved[0],
                    });
                    this._mergeResolvedAttachments(head.localId, f.id, resolved);
                } catch (err) {
                    const status = (err as {status?: number})?.status ?? 0;
                    const msg = this.api.humanizeUploadError(err);
                    this._patchPendingFile(head.localId, f.id, {status: 'failed', error: msg});
                    if (classifyUploadFailure(status) === 'terminal') {
                        this.error.set(msg);
                    }
                    return {ok: false, status};
                }
            }
        } finally {
            this.uploadingItemId = null;
        }
        return {ok: true, status: 200};
    }
```

`_patchPendingFile` and `_mergeResolvedAttachments` must go through `this.outbox.update(...)` with fresh object identities — never in-place mutation — and `_mergeResolvedAttachments` must also re-dispatch the bubble so the chips pick up their paths. Add `private uploadingItemId: string | null = null;` beside `flushingHeadId`.

In `_flushOutbox`, between `head.attempts += 1` and the `_postInput` call:

```ts
                if (!head.threadId) head.threadId = tidAtPost;
                if (head.pendingFiles?.length) {
                    const up = await this._uploadStage(head);
                    if (this.threadId() !== tidAtPost) {
                        queueMicrotask(() => void this._flushOutbox());
                        return;
                    }
                    if (!up.ok) {
                        if (up.status === 404 || up.status === 410) {
                            this.outboxStalled.set(false);
                            this._drainOutboxWithRollback();
                            return;
                        }
                        this.outboxStalled.set(true);
                        return;
                    }
                }
                const item = this.outbox().find((i) => i.localId === head.localId);
                const names = (item?.attachments ?? [])
                    .filter((a) => a.path)
                    .map((a) => a.name);
                const content = composeAgentContent(head.displayContent, names);
```

then pass `content` to `_postInput` instead of `head.content`.

- [ ] **Step 6: Write the implementation — `discardQueuedSend`**

```ts
    discardQueuedSend(localId: string): void {
        // Refuse while the POST is in flight (its fate isn't decided) or while
        // the upload stage is running (dropping the item would orphan bytes in
        // the workspace with no way to delete them).
        if (this.flushingHeadId === localId) return;
        if (this.uploadingItemId === localId) return;
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd cockpit && npx vitest run src/app/core/services/`
Expected: PASS. Existing outbox tests that assert `outbox()[0].content` need updating to `displayContent` — that is expected churn, not a regression. Read each failure before changing an assertion.

- [ ] **Step 8: Typecheck**

Run: `cd cockpit && npx tsc -p tsconfig.app.json --noEmit`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add cockpit/src/app/core/services/persistent-chat.service.ts cockpit/src/app/core/services/persistent-chat.service.outbox.spec.ts cockpit/src/app/core/services/persistent-chat.service.spec.ts cockpit/src/app/core/services/persistent-chat.service.draft.spec.ts cockpit/src/app/core/services/persistent-chat.service.rewind.spec.ts
git commit -m "feat(cockpit): move attachment upload into the send outbox flush"
```

---

## Task 5: The bubble's upload stage line

**Files:**
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` (`@case ('user')` block; a `uploadStageLabel(turn)` method)
- Modify: `cockpit/src/styles/_chat-queued.scss`
- Modify: `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`

**Interfaces:**
- Consumes: `uploadSummary` from Task 1; `OutboxItem.pendingFiles` from Task 4.
- Produces: `PersistentChatService.outboxItem(localId): OutboxItem | undefined` (a lookup the template can call).

- [ ] **Step 1: Add the i18n keys**

In `cockpit/src/assets/i18n/en.json`, under `chat`:

```json
    "upload": {
      "stage": "Uploading {{done}} of {{total}}…",
      "sending": "Sending…",
      "waitingOn": "Waiting for {{name}}…",
      "failed": "{{name}} could not be uploaded",
      "removeAndSend": "Remove file and send",
      "slashCommandWithAttachments": "Remove the attached files before running a command.",
      "tooLarge": "{{name}} is larger than the 100 MB limit",
      "tooManyFiles": "You can attach at most 20 files at once",
      "noWorkspace": "This session has no workspace, so files cannot be attached."
    },
```

Add the German equivalents to `de-DE.json` at the same path. Keep the two files structurally identical — they are in sync today and a missing key renders as the raw key.

- [ ] **Step 2: Write the failing test**

In `cockpit/src/app/views/persistent-chat/persistent-chat.component.spec.ts`, this file is pure-function only (the component cannot be mounted — NG0951). Export a pure helper from the component file and test that:

```ts
describe('uploadStageKey', () => {
    it('reports per-file progress while files remain', () => {
        expect(uploadStageKey({done: 1, total: 3, allDone: false})).toBe('chat.upload.stage');
    });

    it('switches to sending once every file has landed', () => {
        expect(uploadStageKey({done: 3, total: 3, allDone: true})).toBe('chat.upload.sending');
    });

    it('reports nothing for an item with no files', () => {
        expect(uploadStageKey(null)).toBeNull();
    });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/views/persistent-chat/persistent-chat.component.spec.ts`
Expected: FAIL — `uploadStageKey` is not exported.

- [ ] **Step 4: Implement**

Export from `persistent-chat.component.ts` near the other pure predicates (around `:357`):

```ts
/**
 * Which label a queued bubble shows for its send stage. One line, one concept:
 * the upload and the POST are phases of the same commitment, so the label
 * changes and the indicator does not. Win32's rule — never reset progress
 * between phases, never reach 100% before the operation completes.
 */
export function uploadStageKey(
    summary: {done: number; total: number; allDone: boolean} | null,
): string | null {
    if (!summary || summary.total === 0) return null;
    return summary.allDone ? 'chat.upload.sending' : 'chat.upload.stage';
}
```

In the `@case ('user')` block, after the `.user-attachments` div and before the `stalled` actions:

```html
                  @if (queued && !stalled) {
                    @let stage = uploadStage(turn.id);
                    @if (stage) {
                      <div class="upload-stage" aria-live="polite">
                        <app-icon size="sm">upload</app-icon>
                        <span>{{ stage.key | transloco: stage.params }}</span>
                      </div>
                    }
                  }
```

with a component method `uploadStage(localId)` that reads `chat.outboxItem(localId)?.pendingFiles`, runs `uploadSummary`, and returns `{key, params}`. Set `[attr.aria-busy]="queued ? 'true' : null"` on the `.message-user` div.

- [ ] **Step 5: Style it**

Append to `cockpit/src/styles/_chat-queued.scss`, following that file's existing specificity discipline (read its header comment first):

```scss
/* Upload stage line inside a queued user bubble. Lives here, not in the
   component sheet, because persistent-chat.component.scss is ~0.5kB under its
   anyComponentStyle budget (docs/issues/persistent_chat_component_style_budget.md). */
.message.message-user.queued .message-body .upload-stage {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 0.85em;
  color: var(--text-secondary);
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd cockpit && npx vitest run && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS, clean typecheck.

- [ ] **Step 7: Commit**

```bash
git add cockpit/src/app/views/persistent-chat/persistent-chat.component.ts cockpit/src/app/views/persistent-chat/persistent-chat.component.spec.ts cockpit/src/styles/_chat-queued.scss cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
git commit -m "feat(cockpit): show the upload stage on the queued user bubble"
```

---

## Task 6: Honest file caps

Today an oversize file vanishes from the composer with only a `console.warn` (`file-handling.service.ts:60-62`), and the frontend's 5 GB / 100-file limits are 50× the backend's real 100 MB / 20.

**Files:**
- Modify: `cockpit/src/app/core/services/file-handling.service.ts:13-17,56-90`
- Create: `cockpit/src/app/core/services/file-handling.service.spec.ts` (if absent; otherwise modify)
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` (surface rejections via `chat.attachmentError`)

**Interfaces:**
- Consumes: the i18n keys from Task 5.
- Produces: `createFilePreviews(files)` returns `{previews: FilePreview[], rejected: {name: string, reason: 'size' | 'count'}[]}`.

- [ ] **Step 1: Write the failing test**

```ts
describe('createFilePreviews caps', () => {
    it('rejects a file over the backend 100MB limit instead of dropping it silently', async () => {
        const big = new File([new Uint8Array(1)], 'huge.pdf');
        Object.defineProperty(big, 'size', {value: 101 * 1024 * 1024});

        const {previews, rejected} = await service.createFilePreviews([big]);

        expect(previews).toEqual([]);
        expect(rejected).toEqual([{name: 'huge.pdf', reason: 'size'}]);
    });

    it('rejects files past the 20-file backend cap', async () => {
        const files = Array.from({length: 21}, (_, i) => new File(['x'], `f${i}.txt`));
        const {previews, rejected} = await service.createFilePreviews(files);

        expect(previews.length).toBe(20);
        expect(rejected).toEqual([{name: 'f20.txt', reason: 'count'}]);
    });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/file-handling.service.spec.ts`
Expected: FAIL — `createFilePreviews` returns an array, not `{previews, rejected}`.

- [ ] **Step 3: Implement**

```ts
  /** Maximum file size in MB. Mirrors MAX_FILE_SIZE in
   *  orchestrator/services/thread_uploads.py:69 — the server rejects anything
   *  larger with 413, and the client should say so before the bytes move. */
  private readonly MAX_FILE_SIZE_MB = 100;

  /** Maximum files per upload. Mirrors MAX_FILES_PER_REQUEST
   *  (thread_uploads.py:70). */
  private readonly MAX_FILES = 20;
```

and change `createFilePreviews` to accumulate `rejected` rather than `continue`-ing with a `console.warn`, enforcing `MAX_FILES` against `previews.length`.

- [ ] **Step 4: Update the four call sites**

`onFilesSelected` (`component.ts:3070-3077`), the desktop camera path (`:3185-3268`), `onPaste` (`:3086-3092`), and `onDrop` (`:3308-3322`) all destructure the new shape and, when `rejected.length`, set `chat.attachmentError` using the `chat.upload.tooLarge` / `chat.upload.tooManyFiles` keys.

- [ ] **Step 5: Run tests and typecheck**

Run: `cd cockpit && npx vitest run && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/core/services/file-handling.service.ts cockpit/src/app/core/services/file-handling.service.spec.ts cockpit/src/app/views/persistent-chat/persistent-chat.component.ts
git commit -m "fix(cockpit): enforce the real upload caps and report rejected files"
```

---

## Task 7: Slice 1 verification gate

**Files:** none modified.

- [ ] **Step 1: Full frontend suite**

Run: `cd cockpit && npx vitest run`
Expected: all green. Record the pass count.

- [ ] **Step 2: Typecheck with the config that actually checks**

Run: `cd cockpit && npx tsc -p tsconfig.app.json --noEmit`
Expected: no output.

- [ ] **Step 3: Production build**

Run: `cd cockpit && npx ng build`
Expected: succeeds within the bundle budgets (they hard-fail CI). If `@monaco-editor/loader` errors, that is a known local-env issue — note it and continue.

- [ ] **Step 4: Report, do not claim**

Report the actual command output. Do not state Slice 1 works in the app — no live gate has run yet. The live gate is the user's call.

---

# SLICE 2 — Progress

## Task 8: Leave the fetch backend and report upload progress

`FetchBackend` emits zero `UploadProgress` events (verified in `@angular/common/fesm2022/_module-chunk.mjs:758-999`; the only emission in the package is `HttpXhrBackend` at `:1206`). `withXhr()` does not exist in Angular 21. Ruled 2026-08-10: remove `withFetch()` globally.

**Files:**
- Modify: `cockpit/src/app/app.config.ts:74`
- Modify: `cockpit/src/app/core/services/api.service.ts` (`uploadOneToThread`)
- Modify: `cockpit/src/app/core/services/api.service.spec.ts`
- Modify: `cockpit/src/app/core/interceptors/auth.interceptor.spec.ts`

**Interfaces:**
- Consumes: `uploadOneToThread` from Task 2.
- Produces: `uploadOneToThread` returns `Observable<{kind: 'progress'; loaded: number; total: number | null} | {kind: 'done'; files: ThreadUploadedFile[]}>`.

- [ ] **Step 1: Write the failing test**

```ts
it('emits fractional progress then the final files', () => {
    const events: unknown[] = [];
    api.uploadOneToThread('t1', new File(['abc'], 'a.pdf')).subscribe((e) => events.push(e));

    const req = httpMock.expectOne((r) => r.url.endsWith('/persistent/threads/t1/uploads'));
    expect(req.request.reportProgress).toBe(true);

    req.event({type: HttpEventType.UploadProgress, loaded: 50, total: 100});
    req.event({type: HttpEventType.UploadProgress, loaded: 100});  // total undefined — the gotcha
    req.flush({thread_id: 't1', files: []});

    expect(events).toEqual([
        {kind: 'progress', loaded: 50, total: 100},
        {kind: 'progress', loaded: 100, total: null},
        {kind: 'done', files: []},
    ]);
});
```

and in `auth.interceptor.spec.ts`, pin the bypass on the uploads URL specifically:

```ts
it('stamps ngsw-bypass on thread uploads — the SW both corrupts multipart bodies and kills upload progress', () => {
    const req = new HttpRequest('POST', '/api/persistent/threads/t1/uploads', new FormData());
    let seen: HttpRequest<unknown> | undefined;
    runInInjectionContext(injector, () =>
        authInterceptor(req, (r) => { seen = r; return of(new HttpResponse({status: 200})); }),
    );
    expect(seen?.headers.get('ngsw-bypass')).toBe('1');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/api.service.spec.ts`
Expected: FAIL — `req.request.reportProgress` is `false`.

- [ ] **Step 3: Implement**

`app.config.ts:74` becomes:

```ts
    // No withFetch(): Angular's FetchBackend emits no UploadProgress events at
    // all, so attachment upload progress is impossible on it, and withXhr()
    // does not exist in Angular 21. angular.json sets ssr:false, so the usual
    // reason to prefer fetch does not apply here.
    // docs/features/session_attachment_send_flow.md §9.2
    provideHttpClient(withInterceptors([authInterceptor, viewAsInterceptor])),
```

Remove the now-unused `withFetch` import. In `uploadOneToThread`, add `{reportProgress: true, observe: 'events'}` and map `HttpEventType.UploadProgress` / `HttpEventType.Response` to the union above, using `event.total ?? null` — never divide by `total` without checking it.

- [ ] **Step 4: Thread progress through to the bubble**

`_uploadStage` subscribes rather than `firstValueFrom`, patching `loaded`/`total` on the `PendingUpload` via `outbox.update(...)`. **Throttle these writes to ~4/s** — the scroll `ResizeObserver` (`component.ts:2611-2679`) pins synchronously by design, and an unthrottled progress signal will fight a user scrolling up mid-upload.

Extend `uploadStageKey`'s summary with a percentage and render a determinate bar whose scale spans upload *and* the POST — one bar, never reset between phases, never 100% before the send is accepted.

- [ ] **Step 5: Run tests and typecheck**

Run: `cd cockpit && npx vitest run && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/app.config.ts cockpit/src/app/core/services/api.service.ts cockpit/src/app/core/services/api.service.spec.ts cockpit/src/app/core/interceptors/auth.interceptor.spec.ts cockpit/src/app/core/services/persistent-chat.service.ts cockpit/src/app/views/persistent-chat/persistent-chat.component.ts
git commit -m "feat(cockpit): report real attachment upload progress"
```

---

## Task 9: Playwright progress check

The unit harness **structurally cannot** prove progress works: `provideHttpClientTesting()` emits whatever event the test hands it, so progress specs go green while production reports nothing. Only a real browser against a production build proves it.

**Files:**
- Create: `cockpit/e2e/attachment-upload-progress.spec.ts`

- [ ] **Step 1: Write the test**

Against `https://localhost` (already authenticated per the local k3d setup; never the remote cluster), attach a ≥50 MB generated file to a live session, hit send, and assert the bubble's progress value takes at least one intermediate reading strictly between 0 and 100 before the send is accepted.

- [ ] **Step 2: Run against a production build with the service worker active**

The service worker only runs in prod builds (`app.config.ts:131`, `enabled: !isDevMode()`).

- [ ] **Step 3: If progress reads flat zero, switch to the query-param bypass**

`?ngsw-bypass=true` rather than the header. The incident doc's own harness could not prove the header was the load-bearing difference, and angular#21191 reports the header being ignored where the param works. This is the first diagnostic, not the last.

- [ ] **Step 4: Commit**

```bash
git add cockpit/e2e/attachment-upload-progress.spec.ts
git commit -m "test(cockpit): pin real upload progress in a production build"
```

---

# SLICE 3 — Eager upload + delete

## Task 10: Delete endpoint for thread uploads

Ruled 2026-08-10: eager upload ships with cleanup. Without this, cancelling after the bytes have landed is a lie and attach/remove cycles accumulate `_1` copies in a directory the agent can list.

**Files:**
- Modify: `orchestrator/services/thread_uploads.py`
- Modify: `orchestrator/main.py` (new route near `:32496`)
- Modify: `tests/test_thread_uploads.py`

**Interfaces:**
- Consumes: `resolve_thread_upload_destination`, `_VirtualTarget`, `_virtual_upload_slot` (all existing).
- Produces: `_safe_upload_relpath(relpath: str) -> str | None`; `delete_file_from_thread_workspace(thread, relpath, *, destination=None) -> bool`; `DELETE /api/persistent/threads/{thread_id}/uploads/{path:path}`.

- [ ] **Step 1: Write the failing test**

```python
class TestSafeUploadRelpath:
    def test_rejects_traversal(self):
        assert _safe_upload_relpath("../../.ssh/authorized_keys") is None
        assert _safe_upload_relpath("uploads/../../etc/passwd") is None

    def test_rejects_absolute_and_windows_and_nul(self):
        assert _safe_upload_relpath("/etc/passwd") is None
        assert _safe_upload_relpath("C:\\evil") is None
        assert _safe_upload_relpath("a\x00b") is None

    def test_rejects_empty(self):
        assert _safe_upload_relpath("") is None

    def test_allows_a_flat_upload(self):
        assert _safe_upload_relpath("report.pdf") == "report.pdf"

    def test_allows_an_extracted_zip_member(self):
        # _sanitize_filename is NOT reusable here: it flattens to .name and
        # would destroy the bundle/sub/a.txt shape extraction legitimately
        # produces.
        assert _safe_upload_relpath("bundle/sub/a.txt") == "bundle/sub/a.txt"
```

Plus a test that the SFTP delete refuses to follow a symlink out of the tree, mirroring the fixtures at `tests/test_thread_uploads.py:780-787`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_thread_uploads.py -k SafeUploadRelpath -v`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement the validator**

Model it on `_safe_zip_member_path` (`:250-268`), which is the correct posture — reject, do not sanitize — and `posixpath.normpath` **before** the prefix check.

- [ ] **Step 4: Implement the two transports**

`_sftp_delete_file` (connect exactly as `_sftp_write_files:669-694`; `lstat` + `S_ISLNK` guard before `remove`; recursive walk for an extracted directory, since SFTP has no `rm -rf`) and `_virtual_delete_file` (`RcloneObjectStore.delete` at `rclone.py:317`; for a zip stem, `store.list` the prefix and delete each key; wrap in `_virtual_upload_slot()` — each delete is another rclone subprocess).

**The validator is load-bearing:** the thread prefix is shared with Canvas state (`services/canvas_files.py` writes at `threads/<id>/<path>`) and tool files. Never delegate the check to the remote — SFTP will remove any path the `agent-host` user can write.

- [ ] **Step 5: Implement the route**

```python
@app.delete("/api/persistent/threads/{thread_id}/uploads/{path:path}")
async def delete_thread_upload(thread_id: str, path: str, request: Request) -> dict[str, Any]:
    user, thread = await require_thread_owner(request, postgres_db, thread_id)
    ...
```

Same one-line auth as the POST. 400 on a rejected path, 404 when the file is absent, and the existing `ThreadUploadError` taxonomy otherwise.

- [ ] **Step 6: Verify the shape against a real API**

Run the route through the local k3d orchestrator, not a mock. A mocked client validates nothing.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_thread_uploads.py -v`

```bash
git add orchestrator/services/thread_uploads.py orchestrator/main.py tests/test_thread_uploads.py
git commit -m "feat(orchestrator): delete a file from a thread's uploads directory"
```

---

## Task 11: Eager upload on attach

**Files:**
- Create: `cockpit/src/app/core/services/upload-registry.service.ts` + spec
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts`

**Interfaces:**
- Consumes: `uploadOneToThread` (Task 2/8), the delete route (Task 10).
- Produces: `UploadRegistryService.start(threadId, preview)`, `.cancel(previewId)`, `.adopt(previewId)`, `.abortAllForThread(threadId)`.

- [ ] **Step 1: Write the failing test**

Cover: an upload starts on attach only when a thread exists, the session is ready and the tier is not `none`; `sendMessage` adopts an in-flight entry instead of starting a second request; cancel-before-completion aborts and issues no DELETE; cancel-after-completion issues a DELETE; a thread switch aborts every in-flight upload and clears `pendingAttachments`; and re-adding an identical `name|size|lastModified` is rejected.

- [ ] **Step 2: Run to verify it fails, then implement**

**Cancellation must be tracked as explicit intent** in a `Set<string>` checked *before* the error handler reads `status`. Angular reports abort and offline identically as `status: 0`, so without this a user-cancelled upload surfaces as *"Network error — check your connection"* — precisely the misleading message from the service-worker incident.

Root-provided (`providedIn: 'root'`), like `PersistentChatService`, because `ChatPageComponent` is destroyed on navigation and the upload must survive it.

- [ ] **Step 3: Close the cross-thread leak**

`pendingAttachments` and `attachmentError` are cleared by none of `connect()`, `disconnect()`, `enterDraftSession()` today, so chips follow the user between threads. With eager upload that would land bytes in the wrong workspace. Clear both, and abort in-flight uploads, on every thread transition.

- [ ] **Step 4: Run tests, typecheck, commit**

```bash
git add cockpit/src/app/core/services/upload-registry.service.ts cockpit/src/app/core/services/upload-registry.service.spec.ts cockpit/src/app/core/services/persistent-chat.service.ts
git commit -m "feat(cockpit): upload attachments eagerly when they are attached"
```

---

## Task 12: Live gate on dev

**Files:** none.

- [ ] **Step 1: Run the §8 live gate from the spec**

With a ≥50 MB file on dev: bubble appears on Enter with chips and an empty composer; generation starts only after the upload; cancel mid-upload leaves no file and no error banner; retry after a forced 503 does not duplicate; attach on the landing page then send creates the session and uploads; send on an ended thread resumes and then uploads.

- [ ] **Step 2: Record results in the spec**

Append a dated results section to `docs/features/session_attachment_send_flow.md`, stating what passed, what failed, and what was not exercised. Do not mark anything verified that was not actually run.

- [ ] **Step 3: Commit the results**

```bash
git add docs/features/session_attachment_send_flow.md
git commit -m "docs(features): record the attachment send-flow live gate results"
```

---

## Self-Review Notes

**Spec coverage.** §5.1 → Tasks 1, 4. §5.2 → Task 5 (label rules), Task 8 (determinate bar, a11y). §5.3 → Task 2. §5.4 → Task 11. §5.5 → Tasks 1, 4. §4.1 `track att.path` → Task 3; caps → Task 6; cross-thread leak → Task 11 Step 3; style budget → Task 5 Step 5. §4.2 no-idempotency → Task 4 Step 5; no-delete → Task 10. §9.1 → Task 10. §9.2 → Task 8. §8 test plan → Tasks 7, 9, 12.

**Deliberately deferred, per spec §6:** reload survival (IndexedDB `version(5)`), chunked/tus, presigned direct-to-store, zip hint flooding, and `historyToTurns` dropping attachments. Each is stated as a non-goal in the spec, not silently dropped.

**Known thinner tasks.** Tasks 10 and 11 carry less literal code than Tasks 1–6 because both depend on live shapes that only a real API validates (§Task 10 Step 6). Their step structure is complete; their implementer should expect to read the neighbouring writer functions before mirroring them.
