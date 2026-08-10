# Session attachment send flow

Status: **IMPLEMENTED on `develop` (Slices 1–3); live gate RUN and PASSED
2026-08-10 — see §13.** §9 is ruled: 9.1 → (b) delete endpoint, 9.2 → remove
`withFetch()` globally (both shipped). Two defects found by the gate remain
open, both pre-existing and both recorded in §13.4.
Date: 2026-08-10
Scope: Cockpit persistent-chat composer, the send outbox, the thread upload
endpoint, and (optionally) a new delete endpoint for thread uploads.

Related: [[session_reliability_and_transport_simplification]],
[[cockpit_pwa_mobile_hardening]], [[canvas_office_documents]].
Prior art in `docs/done/`: `cockpit_service_worker_breaks_file_uploads.md`,
`session_uploads_never_extract_archives.md`,
`session_uploads_never_implemented_for_lite_workspace_tiers.md`.

---

## 1. Summary

Sending a message with attachments leaves the composer in a split state for the
entire duration of the upload. The typed text disappears immediately; the
attachment chips stay behind; no message bubble appears. On a 90 MB batch that
window is over a minute long, and during it the user has no record of what they
just sent and no way to tell whether anything is happening.

The fix is one structural change: **the upload stops being a precondition for
the message and becomes the first stage of the outbox item.** The bubble is
created and the composer is cleared in the same synchronous block, exactly as a
text-only send already works. Everything else in this document follows from
that change or is a defect it exposes.

Three findings from the implementation survey change what is buildable:

1. **Upload progress is currently impossible.** `provideHttpClient(withFetch(), …)`
   (`cockpit/src/app/app.config.ts:74`) selects Angular's `FetchBackend`, which
   emits **zero** `UploadProgress` events — verified in the shipped source
   (`@angular/common/fesm2022/_module-chunk.mjs:758-999` has no such emission;
   the only one in the package is `HttpXhrBackend` at `:1206`). `withXhr()` does
   not exist in Angular 21. Any progress bar requires leaving the fetch backend.
   **Done (Slice 2, 2026-08-10):** `withFetch()` removed app-wide per §9.2. The
   build still prerenders nothing (`dist/cockpit/prerendered-routes.json` is
   `{"routes": {}}`, `index.html` ships a bare `<app-root>` with no `ngh`
   markers), so no `XhrFactory` shim is needed off-browser.
2. **The current batch upload is one request away from a hard edge failure.**
   All files go in a single multipart POST (`api.service.ts:1151-1156`). The
   deployment traverses a Cloudflare Tunnel, whose plan-level request-body cap
   is **100 MB**. The reported case — 32.84 + 18.45 + 39.26 MB — is a 90.5 MB
   body, at 90% of that ceiling. One more file and it fails at the edge with an
   HTML error page the client cannot parse. Splitting into one request per file
   is a reliability fix, not only a progress enabler.
3. **Attaching in a draft session is an unrecoverable dead end today**, and
   **sending an attachment on an ended pod/VM thread silently refuses to resume
   the session.** Both are caused by the same misordering this design removes.

---

## 2. The current behaviour, precisely

`PersistentChatComponent.send()` — `persistent-chat.component.ts:2993-3039`:

```ts
:3015  this.inputText = '';                    // text cleared synchronously
:3019  clearDraft(threadId);
:3026  void this.chat.sendMessage(text).then(…)
```

`PersistentChatService.sendMessage()` — `persistent-chat.service.ts:2460-2574`,
in execution order:

| Step | Line | Note |
|---|---|---|
| Slash-command bypass | `:2464-2466` | returns **before** the attachment block — `/compact` with a queued file strands the chips silently |
| Office-frame flush gate | `:2474` | an existing `await` that already precedes the bubble |
| **Upload — `await firstValueFrom(uploadToThread(...))`** | `:2476-2504` | the whole window |
| Build `ChatAttachment[]` from the server response | `:2506-2511` | `path` only exists after upload |
| Build the agent hint | `:2514-2519` | `[Attached files in uploads/: …]` |
| **Dispatch the optimistic bubble** | `:2531-2537` | first moment a bubble exists |
| Enqueue the outbox item | `:2544-2553` | |
| Draft-create / resume / flush | `:2555-2573` | all **below** the upload |

Chips are cleared at `:2503`, inside the success branch of the upload. So the
text clears at t=0 and the chips clear at t=upload-complete. That asymmetry
*is* the bug.

Secondary consequences of the same ordering:

- **Draft session (landing page).** `onPaste` (`component.ts:3086-3092`) has no
  connection guard, so a pasted screenshot attaches with `threadId() === null`.
  `canSend()` returns true, `send()` clears the input, and `sendMessage` hits
  `:2478-2481` → `attachmentError.set('Cannot upload: no active thread')` and
  `return false`. `_createFromDraftSession` at `:2555` is never reached. No
  session is created, the chips are never cleared, and every subsequent Enter
  reproduces it. The error string is a hardcoded English literal, not a
  transloco key.
- **Ended thread, pod/VM tier.** The upload at `:2488` runs before
  `resumeSession()` at `:2569`. The pod is gone, so
  `resolve_thread_upload_destination` raises 409 *"Workspace is not ready — try
  again in a moment"* (`thread_uploads.py:617-623`), `sendMessage` returns
  `false`, and **the resume never fires**. The 409 message is a lie for an ended
  thread: it will never become true without an explicit resume. On the
  `virtual` tier (the deployment default,
  `SESSION_DEFAULT_WORKSPACE_BACKEND = "virtual"`, `main.py:3526`) the same
  upload *succeeds*, because the backing is durable object storage — so the
  behaviour diverges by workspace tier and no tier-agnostic assumption is safe.
- **Upload state is already unrenderable.** `sendMessage` mutates
  `p.uploadStatus` in place at `:2486/:2490/:2494` on objects held inside a
  signal, with no `.set()`/`.update()`. Nothing re-renders. This is why the
  composer shows only a static placeholder today. `FilePreview.uploadProgress`
  (`file.model.ts:48`) is rendered exactly once in the whole app, in
  `job-create.component.ts:200`, where it is set to `0` then `100` and never
  incremented — a fake progress bar that ships today.

---

## 3. What comparable products do

Surveyed with sources; full evidence in the research appendix (§12).

**Upload timing.** Eager (bytes leave on attach) is the majority: ChatGPT
(3-phase register → PUT → finalize), Discord (`POST /channels/{id}/attachments`
→ PUT → send referencing `uploaded_filename`), Notion (staged, 1-hour attach
deadline), Gmail, Mattermost, Zulip, Discourse, Linear. Deferred: **Signal
Desktop**, which stages attachments in the composer and uploads *inside the send
job*. Telegram and WhatsApp are protocol-forced to upload first but still render
the bubble during the upload.

**Where the pending upload lives** splits into three architectures, and only one
of them is free of the bug we have:

- **In the transcript** (Signal, Telegram, WhatsApp). Signal Desktop is the
  closest published match to the target design: it registers the message with
  `SendStatus.Pending`, renders it, clears the composer **in the same redux
  batch** as the job enqueue, and only then uploads inside
  `sendNormalMessage`. Failures are written onto the *bubble*
  (`markMessageFailed`), never back into the composer.
- **In the composer, send blocked** (Mattermost, and apparently ChatGPT/Claude).
  Mattermost's `doSubmit` early-returns while `uploadsInProgress.length > 0` —
  silently. Pressing Enter does nothing at all. This shape never produces our
  split state because it never lets a message be committed with an incomplete
  attachment.
- **Composer chip, transcript-level failure banner** (Discord: *"Upload Failed -
  Click Here to retry the upload"*).

Gmail's help page is the clearest published statement of the target behaviour:
*"You can send an email even if the attachment is still uploading. The upload
finishes before sending in the background."*

**Ordering.** Signal enforces strict per-conversation FIFO via
`PQueue({concurrency: 1})` — and its issue #3720 is a user complaint that
messages typed during a large upload are *invisible* until it completes. Matrix
MSC2246 names the same head-of-line blocking as its motivating problem. Our
outbox is already FIFO and already renders queued items as bubbles, so we have
the correct half of this by construction; what is missing is naming *what* a
queued message is waiting on.

**Every product that ships eager upload also ships cleanup.** Discord has
`DELETE /attachments/{name}`. Notion expires unattached uploads after 1 hour.
Synapse ships `max_pending_media_uploads: 5` and `unused_expiration_time: 24h`.
Rails, Zulip, Discourse and Livewire all run orphan sweepers. We have neither a
delete endpoint nor a sweeper (§4.3).

---

## 4. Constraints the design must respect

### 4.1 Frontend

- **`withFetch()` blocks upload progress** (finding 1 above). `angular.json:81-82`
  sets `"ssr": false` and `"outputMode": "static"`, so `withFetch()` is not
  load-bearing here; removing it is low-risk but is an app-wide backend change.
  The scoped alternative is a child `EnvironmentInjector` providing a second
  `HttpClient` without `withFetch()` for the upload path only.
- **`ngsw-bypass` is mandatory and now doubly load-bearing.** The Angular
  service worker re-issues every non-bypassed request through `scope.fetch()`,
  which **corrupts multipart bodies** (`docs/done/cockpit_service_worker_breaks_file_uploads.md`,
  fix `1195b54d`). `auth.interceptor.ts:43-53` stamps `ngsw-bypass: 1` on every
  non-safe method. Independently, **a service worker that calls `respondWith()`
  destroys XHR upload progress** (angular#24683, #24716) — so the same header is
  also the precondition for progress events firing at all. **A raw
  `XMLHttpRequest` or `fetch` issued outside `HttpClient` loses the interceptor
  and therefore loses `ngsw-bypass`, `X-CSRF` and `withCredentials`** —
  re-breaking uploads entirely. Any implementation must stay inside `HttpClient`.
- **Both interceptors are event-stream safe** — `auth.interceptor.ts:30-64` and
  `view-as.interceptor.ts:43-75` use only `req.clone()` + `catchError`. No
  `take(1)`, no `map` to `HttpResponse`. Progress events pass through cleanly.
- **`track att.path` breaks with two or more pre-upload attachments.**
  `component.ts:1240` tracks the bubble's attachment chips by the server path,
  which does not exist before upload. Two chips → duplicate `undefined` keys →
  `NG0955` in dev builds, silent DOM mis-reconciliation in prod; and when paths
  later arrive, every key changes and every chip node is destroyed and
  recreated. `ChatAttachment` must gain a stable local id and the track must use
  it.
- **New queued-state CSS must go in `cockpit/src/styles/_chat-queued.scss`,
  not the component sheet.** `persistent-chat.component.scss` sits ~0.5 kB under
  its `anyComponentStyle` budget (`docs/issues/persistent_chat_component_style_budget.md`),
  which is why `.stalled` and `.queued-actions` already live there. That file
  documents the exact (0,4,0)/(0,6,0) specificity chains needed to outrank
  emulated encapsulation.
- **Client and server caps disagree by 50×.** Frontend allows 5 GB/file and 100
  files (`file-handling.service.ts:14,17`) and drops oversize files with only a
  `console.warn` at `:60-62` — the file just vanishes. Backend allows 100 MB and
  20 files (`thread_uploads.py:69-70`).
- **`pendingAttachments` and `attachmentError` leak across thread switches.**
  Neither is cleared by `connect()`, `disconnect()`, or `enterDraftSession()`.
  With eager upload this becomes a correctness bug: bytes could land in the
  wrong workspace, or an in-flight upload could resolve after a switch and patch
  an outbox item that now belongs to a different thread — the same class of bug
  the flush's `tidAtPost` guard (`service.ts:2596, :2611-2614`) exists to kill.

### 4.2 Backend

- **The backend structurally cannot reject early.** With
  `files: list[UploadFile] = File(...)`, FastAPI parses the multipart during
  dependency solving, before any handler line runs. By then the client has
  already uploaded everything. Quoting the prior design doc: *"A genuinely early
  reject has to happen client-side, which is the real argument for the composer
  knowing the tier."*
- **No idempotency.** Collision resolution is `_claim_name` against a live
  directory listing (`thread_uploads.py:153-164`), so the **same file uploaded
  twice in two requests becomes `report.pdf` and `report_1.pdf`**. A retry after
  a client timeout where the server actually succeeded silently duplicates. This
  makes per-file upload-result caching (§5.3) mandatory, not optional.
- **No cancellation.** `asyncio.to_thread` work is uncancellable once running.
  Aborting the HTTP request stops the bytes only if they have not all arrived;
  once the body is complete, the SFTP/object write runs to completion regardless.
  Cancel is therefore only honest **while progress is still advancing**.
- **No delete, no list, no sweeper.** `POST …/uploads` is the only route on that
  surface. `_release_thread_resources(reclaim_volume=True)` on a permanent
  thread delete is the only cleanup, and for the `virtual` tier there is no
  object-store purge at all — `threads/<id>/` keys survive thread deletion
  indefinitely.
- **Writes are not transactional.** A mid-batch failure returns 500 with already-
  written files left on disk and no indication of which landed.
- **Memory.** Each file is fully buffered (`contents = await f.read()`,
  `main.py:32538`), and the virtual tier copies it again inside
  `RcloneObjectStore.put` (`rclone.py:262`). A 3×90 MB virtual-tier batch peaks
  ~360 MB on a pod limited to 2 Gi with a ~200 Mi steady state. Per-file
  requests reduce peak per request but `MAX_CONCURRENT_VIRTUAL_UPLOADS = 4`
  permits four concurrently.
- **Zip attachments explode the response.** An extracted archive returns one
  entry per member, and the composer turns every one into a name in the agent
  hint. A 60-file zip yields a 60-name hint
  (`docs/issues/tool_configuration_deferred_findings.md:302-303`).

### 4.3 The eager-upload cleanup gap

Eager upload means bytes land in the workspace before the user commits to
sending. With no delete endpoint and no sweeper, every removed-or-abandoned
attachment is permanent litter in `uploads/` — and because collisions resolve by
suffix, an attach → remove → re-attach cycle leaves `Zeugniss.pdf` *and*
`Zeugniss_1.pdf`, with the message hint naming the second. The agent can list
that directory. This is the one decision in this document that cannot be
deferred without shipping a known defect; see §9.1.

---

## 5. Design

### 5.1 The core change: upload becomes outbox stage 0

`OutboxItem` (`service.ts:145-159`) gains a pre-upload carrier and a thread pin:

```ts
export interface OutboxItem {
    localId: string;
    displayContent: string;             // the user's typed text
    content?: string;                   // agent-facing; computed at POST time
    attachments?: ChatAttachment[];     // resolved after upload
    pendingFiles?: PendingUpload[];     // pre-upload; holds the File handles
    threadId: string;                   // NEW — pins the item to its thread
    attempts: number;
}

export interface PendingUpload {
    id: string;                         // FilePreview.id — stable track key
    file: File;
    name: string; size: number; mimeType: string;
    loaded: number; total: number | null;
    status: 'queued' | 'uploading' | 'done' | 'failed';
    error?: string;
    resolved?: ChatAttachment;          // set once the server confirms
}
```

`ChatAttachment` gains `id: string` so the bubble can track chips by a key that
exists before the upload does.

`sendMessage` becomes synchronous from the user's point of view:

1. Slash-command handling — but **refuse a slash command while attachments are
   queued** rather than silently stranding them (the `:2464-2466` bypass in §2).
2. The office-frame flush gate stays where it is (`:2474`); it is a local
   in-memory commit, not a network upload.
3. Build `PendingUpload[]` from `pendingAttachments()`.
4. **In one synchronous block:** dispatch the `user_message` bubble carrying
   `displayContent` + local attachment descriptors, push the `OutboxItem`, and
   call `clearAttachments()`. Text and chips leave the composer together.
5. Draft-create / resume / flush exactly as today — but now *below* nothing.

`_flushOutbox` gains a stage before `_postInput`:

```
while (sessionReady && outbox.length):
    head = outbox[0]
    if head.threadId !== threadId(): drop resolution, hand off   # existing guard, now also for uploads
    if head.pendingFiles has any not-done:
        upload them (§5.3); on failure → classify (§5.5) and return
        patch head.attachments from the resolved results
        head.content = displayContent + hint(names)
    POST head.content                                            # unchanged from here down
```

Everything downstream — single-flight, the `tidAtPost` thread-switch guard, the
404/410 rollback, the deliberate absence of timed auto-retry, `pendingTurnCount`
— is untouched and inherited by the upload stage.

**Why this shape.** It collapses two state machines into one. The outbox already
models *queued, not yet accepted, retryable, discardable, rolled back on a dead
thread*. The upload needs exactly those states. It also fixes the draft-session
and ended-thread dead ends for free: the upload now runs after
`_createFromDraftSession` / `resumeSession` have produced a live workspace,
because the flush is gated on `sessionReady()`.

### 5.2 What the user sees

The bubble uses the existing queued treatment — `.message-user.queued`,
`opacity: 0.65`, dashed border, `schedule` avatar icon
(`persistent-chat.component.scss:911-925`) — the same style an offline send
already gets. This is deliberate: "committed but not yet delivered" is one
concept and should have one appearance.

Inside the bubble, below the chips, a stage line:

```
┌────────────────────────────────────────┐
│ Okay, ich habe zusammengekratzt was …  │   opacity .65, dashed
│ [Zeugniss.pdf] [scan_…] [scan_…]       │
│ ⬆ Uploading 2 of 3 — 34%               │
└────────────────────────────────────────┘
🕐
```

Rules taken from the progress research (Win32 UX Guide, Material 3, Apple HIG):

- **One bar per message, not one per file, and never both.** Per-file *state*
  (queued/uploading/done/failed) drives the chip's own affordance because errors
  and retries are per-file; the aggregate bar is the only determinate indicator.
- **Never reset the bar between phases, never reach 100% before the send is
  accepted.** The bar's scale spans upload *and* the POST, so "uploaded, now
  sending" is a label change, not a second bar.
- **Change the label, not the indicator**, when the phase changes:
  `Uploading 2 of 3 — 34%` → `Sending…`.
- **Indeterminate until `event.total` is known.** `HttpUploadProgressEvent.total`
  is optional and must never be divided by blindly.
- `aria-busy="true"` on the pending bubble plus a `polite` live region;
  `role="progressbar"` is not a live region and announces nothing on its own.
  Never `assertive` — it would interrupt the user's own typing feedback.

The composer, meanwhile, is empty and immediately usable. A second message typed
during the upload gets its own queued bubble behind the first (the outbox is
FIFO), which is the ordering behaviour we want; its stage line reads
`Waiting for Zeugniss.pdf…` so the cause is visible rather than mysterious —
the specific failure Signal #3720 reported.

### 5.3 One request per file

`uploadToThread` splits from one batched POST into one POST per file, with
bounded concurrency (2; the server's virtual-tier semaphore is 4 and is shared
across all users). This buys four things:

1. **Stays under the 100 MB Cloudflare body cap per request** instead of summing
   the batch into it.
2. Per-file progress and per-file cancel become expressible at all.
3. One bad file no longer fails the whole message — today `:2493-2497` marks
   *every* queued file `FAILED` on any error.
4. **Retry never re-uploads a file that already succeeded**, because each file's
   `resolved` `ChatAttachment` is cached on the item. Without this, the backend's
   suffix-based collision resolution turns every retry into a duplicate.

Cost: cross-file collision resolution within one logical batch weakens (each
request re-lists, so names still never clobber — they just get `_1` suffixes
across the batch), and each request receives its own zip-extraction budget,
weakening the DoS bound that the shared 100-entry/300 MB budget was designed
around. The second is the real trade-off and should be revisited if zip
attachments become common.

### 5.4 Eager upload on attach

When a file is attached and the thread can accept it, the upload starts
immediately, keyed by `FilePreview.id` in a registry on the service. At send
time the outbox item adopts the in-flight or completed entry rather than
starting a new one; `_flushOutbox`'s upload stage awaits whatever is already
running.

Preconditions to start eagerly: a thread exists, the session is ready, and the
workspace tier is not `none`. Otherwise the file simply waits and uploads at
flush time — the deferred path is always the fallback, never removed.

Guards this requires:

- **Chip removal aborts an in-flight upload** by unsubscribing (verified to
  abort on both Angular backends). Because Angular reports abort and offline
  identically as `status: 0`, cancellation must be tracked as **explicit intent**
  in a set checked *before* the error handler reads `status` — otherwise a
  cancel surfaces as *"Network error — check your connection"*, which is exactly
  the misleading message from the service-worker incident.
- **A completed eager upload cannot be un-uploaded** without §9.1.
- **A thread switch aborts every in-flight eager upload and clears
  `pendingAttachments`**, closing the leak in §4.1.
- **Client-side dedupe on `name|size|lastModified`** (Uppy's key) rejects
  re-adding a file already attached, which removes the most common path to a
  `_1` duplicate at zero cost.
- **Client-side enforcement of the real backend caps** (100 MB, 20 files) with a
  visible message, replacing the silent `console.warn` drop.

### 5.5 Failure taxonomy

The upload stage classifies exactly as the POST stage does, into terminal and
retryable:

| Condition | Status | Treatment |
|---|---|---|
| File too large / too many files | 400, 413 | **Terminal for that file.** Bubble shows the file failed; offer *Remove file and send* or *Discard message*. Never silently drop. |
| Workspace not ready | 409 | Retryable. Item stays queued, bubble stalls with Retry. On an ended thread the resume now runs first, so this should be rare. |
| Workspace has no storage tier (`none`) | 409 | Terminal. Permanent refusal; the message can still be sent without the files. |
| Transport unreachable / capacity exhausted | 502, 503, 0 | Retryable. Existing stall + Retry/Discard. |
| Thread gone | 404, 410 | Existing `_drainOutboxWithRollback`. Already-uploaded files are abandoned; the thread is gone, so this is acceptable and stated. |
| User cancelled | — | Not an error. Filtered before `humanizeUploadError`. |

`discardQueuedSend` currently refuses only while `flushingHeadId === localId`
(`:2673`), which is null during an upload — so an item would be discardable
mid-upload with no cancellation, orphaning bytes. It needs an upload-stage
equivalent guard.

---

## 6. What this does not do

- **No resume across reload.** An in-flight upload cannot survive a reload; the
  browser cancels it, and `keepalive`/`sendBeacon` cap at 64 KB. The outbox is
  already memory-only, so this is not a regression. Storing `File` handles in
  IndexedDB (Dexie is at `version(4)`; this would be `version(5)`) would let us
  offer *"Resume upload of Zeugniss.pdf?"* on next load, but it restarts from
  byte 0 — a UX win, not a bandwidth win. Uppy's own IndexedDB path caps at
  ~10 MB/file, so at 90 MB the honest fallback is its "ghost file" pattern:
  show the name, ask the user to re-select. Deferred.
- **No chunked or resumable protocol (tus).** Byte-level resume needs a server
  that can report "I already have bytes 0..N" — a backend feature, not a browser
  one. Deferred until users report reload loss.
- **No presigned direct-to-object-store.** The destination is a live workspace
  reached over SFTP for pod/VM tiers; only `virtual` has a bucket, so this would
  mean two upload architectures. It also skips server-side zip extraction,
  collision resolution and MIME reporting, and would be the first time a scoped
  credential crosses to the browser on this surface — `virtual_workspace.py:1-5`
  is explicit that credentials are *never* copied into client-visible state.
- **No fix for the hint flooding on large zips**, tracked separately.
- **No fix for `historyToTurns` dropping attachments.** After a reload, a sent
  message renders as raw `[Attached files in uploads/: …]` text with no chips,
  because `service.ts:4185-4196` builds `UserTurn` without an `attachments`
  field. Pre-existing; worth fixing next to this work but not required by it.

---

## 7. Slices

**Slice 1 — the reported bug.** Optimistic bubble; composer clears text and
chips atomically; upload moves into the outbox flush; per-file requests;
`ChatAttachment.id` + fixed track expression; upload-stage failure taxonomy with
Retry/Discard on the bubble; per-file result caching so retries never duplicate;
client-side caps matching the backend; slash-command-with-attachments refusal.
Indeterminate stage label (`Uploading 2 of 3…`), no percentage yet. **No backend
change.** Fixes the draft-session dead end and the ended-thread no-resume as a
side effect.

**Slice 2 — progress. IMPLEMENTED 2026-08-10, browser gate not yet run.** Left
the fetch backend globally (§9.2), `reportProgress: true` + `observe: 'events'`,
determinate aggregate bar with the label rules from §5.2, `aria-busy` + polite
live region, and a spec pinning `ngsw-bypass` on the uploads URL with a comment
naming progress as the second reason it exists. The bar is byte-weighted across
the item's files and scaled to 90% of its own track, so the remaining 10% is the
POST and the bar cannot fill before the send is accepted. Progress writes are
throttled to ~4/s (`PROGRESS_WRITE_INTERVAL_MS`) for the scroll-pin reason in
§10. The Playwright check in §8 — a production build with the SW active,
asserting an intermediate value strictly between 0 and 100 — is still OWED and
is the only real proof that progress works outside the mock backend.

**Slice 3 — eager upload.** Upload on attach, cancel-on-remove with explicit
intent tracking, thread-switch abort + `pendingAttachments` clearing, dedupe key,
and whichever cleanup §9.1 selects.

---

## 8. Test plan

Unit (vitest — the reliable runner here):

- `persistent-chat.service.outbox.spec.ts` is the highest-impact file: every
  test asserts `outbox()[0].content` and POST counts, both of which change. Add:
  bubble exists before the upload resolves; composer cleared at dispatch time;
  upload failure stalls without rolling back the bubble; retry does not
  re-upload an already-resolved file; a thread switch mid-upload drops the
  resolution instead of patching a foreign queue.
- The existing mocks return a **synchronous** `of({thread_id:'t', files: []})` in
  all four service specs, so no current test can observe an in-flight window —
  progress and staging tests need a `Subject`. And because `files: []` is always
  empty, **no test today exercises a non-empty `ChatAttachment[]`**, which is
  why the `track att.path` hazard went unnoticed.
- `persistent-chat.component.spec.ts` is pure-function only; `canSendMessage`
  and `isMicMode` gain an upload-state dimension, plus draft-attach cases.
- `api.service.spec.ts`: assert `req.request.reportProgress === true` and that
  an unsubscribe yields `req.cancelled` without surfacing an error.

**The unit harness structurally cannot prove progress works.** Tests run on
`provideHttpClientTesting()`, whose mock backend emits whatever event you hand
it — so progress tests go green while production, on `FetchBackend`, reports
nothing. This is the local instance of the "mocked client validates nothing"
rule. One Playwright check against a **production build with the service worker
active** asserting an intermediate progress value strictly between 0 and 100 is
the only real verification. The service worker only runs in prod builds
(`app.config.ts:131`, `enabled: !isDevMode()`).

Live gate on dev, with a ≥50 MB file: bubble appears on Enter with chips and
empty composer; generation starts only after upload; cancel mid-upload leaves no
file and no error banner; retry after a forced 503 does not duplicate; attach on
the landing page then send creates the session and uploads; send on an ended
thread resumes and then uploads.

---

## 9. Decisions

### 9.1 Cleanup for eagerly-uploaded files — **RULED 2026-08-10: (b)**

Uploading on attach means files reach the workspace before the user commits.
Options:

- **(a) Abort in-flight, accept the rest.** Removing a chip cancels a transfer in
  progress (usually nothing lands). A completed upload stays in `uploads/`
  unreferenced. Zero backend work. Risk: the agent can list that directory, and
  attach/remove cycles accumulate `_1`, `_2` copies.
- **(b) Add `DELETE /api/persistent/threads/{id}/uploads/{path:path}`** — the
  recommendation. Reuses `require_thread_owner` (one line) and
  `resolve_thread_upload_destination` verbatim. Needs two transport functions
  mirroring the writers, a **new** path validator (`_sanitize_filename` is not
  reusable — it flattens `bundle/sub/a.txt`, which zip-extracted members
  legitimately need) with `posixpath.normpath` before a prefix check, and a
  symlink guard on the SFTP side. Load-bearing because the thread prefix is
  shared with Canvas state and tool files. Discord — the closest analog — ships
  exactly this endpoint.
- **(c) Stage then commit** — `uploads/.staging/<draft>/` moved into place on
  send. Cleanest semantics, most backend work, plus a sweeper for stale staging
  dirs.

**Ruling: (b)**, scoped into Slice 3. Every surveyed product that uploads
eagerly also ships cleanup, and (a) ships a known defect into a directory the
agent reads.

### 9.2 How to leave the fetch backend — **RULED 2026-08-10: remove globally** (DONE)

`withFetch()` is removed from `app.config.ts:74`. One line, app-wide;
`ssr: false` means its usual justification does not apply here. The scoped child
injector was rejected because it leaves two `HttpClient` instances whose
interceptor lists must be kept in sync by hand, and drift there silently drops
`ngsw-bypass`. The Playwright check in §8 is the guard.

### 9.3 Concurrency for per-file uploads

Proposed 2, against a server-side virtual-tier semaphore of 4 shared across all
users and a 2 Gi orchestrator memory limit. Worth confirming against observed
dev behaviour rather than assumed.

---

## 10. Risks

- **Progress may still read zero in production** if the `ngsw-bypass` *header*
  is not honoured by the deployed service worker. The incident doc's own harness
  could not prove the header was the load-bearing difference (its control POST
  also succeeded locally), and angular#21191 reports the header being ignored
  where the query param works. If progress is flat in prod despite everything in
  Slice 2, switching uploads to `?ngsw-bypass=true` is the first diagnostic.
- **Frequent progress signals re-run `canSend()` and `micMode()`** on every tick
  and fire the scroll `ResizeObserver`, which pins **synchronously** by design
  (`component.ts:2611-2679`). A user scrolling up during a long upload can be
  yanked back by each progress-driven tick unless the wheel/touch escape fires
  first. Throttle progress writes to ~4/s and verify the escape still wins.
- **Cancel is honest only while bytes are still moving.** Once the body has
  fully arrived the server completes the write regardless; the user sees
  "cancelled" and the file exists. Offer cancel only while progress advances,
  and pair it with §9.1(b) if the guarantee needs to be real.
- **A retry storm across four concurrent 90 MB virtual-tier uploads is ~720 MB
  of transient heap** on a pod whose steady state is ~200 Mi and whose limit is
  2 Gi.
- **`auth.interceptor.ts:28` uses a module-level `isRedirecting` flag** that is
  never reset. Concurrent uploads will exercise a 401 storm harder than today's
  serial path.

---

## 11. Files touched

Frontend:
`cockpit/src/app/core/services/persistent-chat.service.ts` (outbox item shape,
`sendMessage`, `_flushOutbox`, attachment registry, thread-switch guards),
`cockpit/src/app/core/services/api.service.ts` (per-file upload, progress,
cancel, keyed errors),
`cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` (`send()`,
bubble template, track expression, composer state),
`cockpit/src/styles/_chat-queued.scss` (upload stage line — **not** the
component sheet),
`cockpit/src/app/core/models/file.model.ts` (`ChatAttachment.id`,
`PendingUpload`),
`cockpit/src/app/core/services/file-handling.service.ts` (real caps, visible
rejection),
`cockpit/src/app/app.config.ts` (Slice 2),
`cockpit/src/assets/i18n/{en,de-DE}.json` (new keys, plus keying the four
hardcoded English upload strings in `api.service.ts:1174-1178` and the one at
`persistent-chat.service.ts:2480`).

Backend (Slice 3 only, if §9.1(b)):
`orchestrator/main.py` (delete route),
`orchestrator/services/thread_uploads.py` (path validator, two delete
transports),
`tests/test_thread_uploads.py` (traversal and symlink cases mirroring
`:780-787` and `:264-292`).

---

## 12. Research appendix

Sources for §3, with the sharpest items only.

**Reference implementations.** Signal Desktop `ts/models/conversations.preload.ts`
(message registered with `SendStatus.Pending` before any byte moves) and
`ts/jobs/helpers/sendNormalMessage.preload.ts` (`MAX_CONCURRENT_ATTACHMENT_UPLOADS = 5`);
composer cleared in the same redux batch via `extraReduxActions`. Mattermost
`webapp/channels/src/components/advanced_text_editor/use_submit.tsx` (send
silently no-ops while `uploadsInProgress.length > 0`). Discord's client-parity
reimplementation `endcord/discord.py` (per-attachment state machine
`uploading/done/too large/restricted/failed`, plus `DELETE /attachments/{name}`).

**Protocol-level.** Matrix MSC2246 names head-of-line blocking explicitly
(*"reuploading a large file would block all messages"*) and answers it by
minting the content URI before the bytes exist; Synapse ships
`max_pending_media_uploads: 5`, `unused_expiration_time: 24h`. Slack's
`files.completeUploadExternal` returns before async scanning finishes, so a
message referencing the file immediately after can fail — if generation fires on
"upload HTTP 200" it can fire before the file is usable.

**Progress UX.** Win32 UX Guide supplies the operative rules: never let a bar
reach 100% before completion; never reset per phase; change the label, not the
indicator; yellow for impeded-but-alive, red for user-recoverable. Material 3:
single indicator for grouped work, indeterminate → determinate as information
arrives. Apple HIG: prefer determinate, keep it moving, and non-uniform pacing
"can even feel deceptive". NN/g: determinate at ≥10 s, and users tolerate ~3×
longer waits with visible movement. Harrison et al. (UIST '07): stalls are far
better tolerated at the *beginning* of an operation — which is where the network
transfer naturally sits, so no reordering is needed.

**Orphan cleanup precedents.** Rails `ActiveStorage::Blob.unattached` swept after
2 days; Livewire `livewire-tmp/` with a 24 h S3 rule; Zulip
`delete_old_unclaimed_attachments` (5 weeks, dry-run unless `-f`) — whose source
carries the exact race we would inherit: *"We upload files to the backend storage
and _then_ make the database entry, so must give some leeway to recently-added
files which do not have DB rows."* Zulip has failed in both directions: a
sweeper never wired into cron (#19007) and over-eager reference counting that
deleted live attachments (7.0–7.3). Zulip #33243 is the one to remember here —
**a draft holds a reference the sweeper cannot see.**

**Accessibility.** `role="progressbar"` requires an accessible name, is *not* a
live region, and announces nothing on its own; omitting `aria-valuenow` means
indeterminate. `aria-valuetext` exists for cases where a bare percentage
misleads — MDN's own example is `aria-valuenow="23" aria-valuetext="23 of 500
files"`. WCAG 2.1 SC 4.1.3 is the normative hook and warns against being "too
chatty". There is no Progressbar pattern in the WAI-ARIA APG; the widely-repeated
"announce every 25%" rule has no normative source.

---

## 13. Live gate results — 2026-08-10

Run against local k3d (`k3d-srw`, namespace `srw`) at `develop` `b9abcbf0`, in
a real Chromium, on a **production** `npm run build` bundle with the Angular
service worker proven *controlling the page* (`navigator.serviceWorker
.controller` → `…/ngsw-worker.js`) before any assertion ran. The k3d cockpit pod
runs `ng serve` and therefore registers no service worker, so the Task 9 harness
(`cockpit/e2e/upload-progress/prod-serve.mjs`) served `dist/` and proxied to the
live backend; the gate extended a scratch copy of it with fault injection at the
proxy, because a Playwright route intercept answers before the bytes leave the
page and so cannot produce either "fail while a real body is mid-flight" or
"the body landed but the response has not".

**Workspace state was verified at the destination, never through the UI.** A
helper run inside the orchestrator pod loads the thread row, calls the
production `resolve_thread_upload_destination()`, and then lists what is
actually there — `RcloneObjectStore.list` for `virtual`, a paramiko SFTP walk
for `sandbox`. It refuses to report an empty directory when the destination
cannot be resolved, because "unresolvable" reading as "no files" would have made
every absence assertion in the gate pass vacuously.

### 13.1 Verdicts

| # | Gate item | Verdict |
|---|---|---|
| 1 | ≥50 MB attached: Enter clears text **and** chips together, bubble appears at once with text + chips, queued style | **PASS** |
| 2 | Generation starts only after the upload lands | **PASS** |
| 3 | A message typed during a large upload names what it waits on | **PASS** |
| 4 | Cancel mid-upload: no file, no error banner | **PASS** |
| 5 | Cancel just after the body is in: no file survives | **PASS** |
| 6 | Retry after a forced 503 does not duplicate | **PASS** |
| 7 | Landing-page (draft) attach → send creates the session and uploads | **PASS**, with a defect found alongside it (§13.4a) |
| 8 | Attachment on an ended thread resumes, then uploads | **PASS** (on `sandbox`, the tier where the bug existed) |
| 9 | Failed-then-retried connect keeps the chips and deletes nothing | **PASS** |
| 10 | Pod-tier attach → remove round trip over SFTP | **PASS** — closes the item Task 10 could not run |

### 13.2 Evidence

**1.** 56 MB `reported-bug.pdf`, upload throttled to 4 MiB/s so it was provably
still in flight. A `requestAnimationFrame` sampler recorded (composer length,
chip count, queued-bubble count) every frame across the Enter. Text cleared,
chips cleared and the bubble appeared **in the same frame** — 29.5 ms after the
keypress in one run, 39.3 ms in another — with the bubble already carrying its
one attachment chip and zero upload responses received. Computed
`opacity: 0.65` on `.message-user.queued` confirms the queued treatment. The
asymmetry in §2 (text at t=0, chips at t=upload-complete) is gone.

**2.** Same run, from the network: upload POST issued at `…959406`, its 200 at
`…975406` (a 16 s transfer), first `POST …/input` at `…975409` — **3 ms after**
the upload response, never before it. Both queued messages posted after.

**3.** A second message sent while the first upload was still moving rendered
`Waiting for reported-bug.pdf…` on its own bubble. Explicitly asserted *not* to
read `Uploading 0 of …`, and the run asserts the first upload was still in
flight so the case was genuinely exercised.

**4.** 56 MB attached, ~3 s of real bytes on the wire, then the chip's remove
button. No `.attachment-error` rendered, and `uploads/` was empty at the
destination.

**5.** Fault: the proxy forwards the upload, the backend writes the file and
answers 200, and the proxy **holds** that response. During the hold the gate
confirmed `uploads/` contained `late-cancel.pdf` — the bytes really were in the
workspace and uncancellable. The chip was then removed and the response
released; a real `DELETE …/uploads/late-cancel.pdf` was observed in the browser
*and* at the proxy, and the directory ended empty, with no error banner.

**6.** A 503 injected once ~35 % of the body was in, twice (the eager upload
eats the first, the send-stage upload the second). `uploads/` after both
failures: **empty** — the early failure never reached the backend. The bubble
stalled with a Retry button; one click and the directory held exactly
`retry-probe.pdf`, with no `retry-probe_1.pdf`.

**7.** Attached on `/`, typed, Enter: composer text and chips cleared together
37 ms later, the session was created, the URL became `/sessions/<id>`, the queue
drained and `uploads/` held exactly `draft-attach.pdf`. The permanent dead end
in §2 is gone. See §13.4a for what the same run also showed.

**8.** Run on **`sandbox`**, deliberately: on `virtual` the pre-fix upload
succeeded anyway (durable object storage), so the 409-and-no-resume only ever
manifested on a pod/VM tier. A sandbox thread was ended (its workspace pod torn
down), reopened, and a file attached and sent. The session resumed, a new
workspace pod was provisioned, and `uploads/` on that pod contained exactly
`ended-thread.pdf` (one upload request, one `/input`).

**9.** The history GET was failed with 503 so the connect left
`historyLoaded === false`; a file was then staged, the fault cleared, and the
connect retried **through the router** — sessions list, then back into the
thread — with a `window` sentinel asserting the SPA never reloaded (an earlier
attempt used `page.goto`, whose document load dropped the chips for reasons that
had nothing to do with the guard). The chip survived, no `DELETE` was issued,
and the file uploaded earlier in that thread was still present.

**10.** `sandbox` thread, real workspace pod `ws-thread-…`, SFTP destination
confirmed by the lister. Attach → the eager upload landed
(`uploads/pod-tier.pdf` present over SFTP) → remove the chip → a real DELETE →
directory empty. Separately, straight against the API: DELETE returns 200 and
the file disappears, a second DELETE returns 404, and
`..%2F..%2F.ssh%2Fauthorized_keys` returns 400.

### 13.3 What was *not* exercised

- **Nothing at all touched a VM-tier (`backend: "vm"`) thread.** Item 10 and
  item 8 both ran on `sandbox`. The SFTP transport is shared, but a VM's
  `metadata.vm` path, its reachability and its resume timing are unverified
  here, and `docs/issues/vm_reliability_assessment.md` says VM infra fails 2.2×
  as often as containers.
- **No HTTPS / Cloudflare-Tunnel path.** The harness serves the app over
  plain-HTTP loopback, so the 100 MB edge body cap in §1 finding 2 and the
  original HTTPS multipart corruption symptom still have a deployed
  environment as their only arbiter.
- **No multi-file batch and no `.zip`.** Every item used a single non-archive
  file. Byte-weighted aggregate progress across files, cross-file `_1`
  resolution within one send, and the per-request zip-extraction budget in §5.3
  are untested live.
- **No reload during an upload**, which §6 already declares a non-goal.
- **Nothing about the agent's use of the file.** The gate stops at "the bytes
  are in `uploads/`"; it never asked an agent to read one.
- **The 409 "no workspace" (`none` tier) terminal branch** of §5.5 was not
  exercised.

### 13.4 Defects the gate found

**(a) The landing draft still loses the bubble — pre-existing, not from this
work.** In item 7 the composer cleared correctly but **no queued bubble was
visible at any frame in the 8 s after Enter** (`bubbleVisibleFrames = 0`), and
the message only reappears once the session is up. The cause is
`createAndConnect`, which dispatches `{type: 'reset', threadId: null}`
synchronously to keep the "Creating thread" card off the previous session's
turns, and only re-shows queued sends via `_redispatchOutboxBubbles()` after
`connect()`'s `loadHistory`. `git blame` puts that reset at `b7685d4a7`,
2026-05-18 — months before this design. So on the landing page the user still
gets the §1 symptom (no record of what they just sent) for the whole
session-creation window, which on the dev cluster was **3–5 minutes**. Fixing it
means re-dispatching the outbox bubbles immediately after the reset instead of
after the history load.

**(b) A client-observed failure that hides a server-side success duplicates the
file, and eager upload makes it certain.** Probing beyond the gate's wording: a
503 *rewritten by the proxy after the backend had already written the file*
produced `ghost-success.pdf` **and** `ghost-success_1.pdf` before the user
touched anything — the eager upload wrote the first, the send-stage re-upload
wrote the second — and one Retry click added `ghost-success_2.pdf`. This is
exactly the no-idempotency hazard §4.2 predicts, and it is now reachable without
any user action, because eager upload puts a whole extra attempt in front of
every send. Any edge that answers after the origin has committed (a Cloudflare
502/524, a proxy timeout, a dropped response) triggers it. The client-side
result cache in §5.3 cannot help: it only skips files the client *saw* succeed.
The durable fix is server-side idempotency — a client-supplied upload key, or
content-hash dedupe inside `_claim_name`.

### 13.5 Cluster-condition notes (not product findings)

Session provisioning on the local k3d cluster degraded from ~30 s to 3–5 minutes
as the run accumulated agent pods; three gate runs failed purely on a startup
budget and were re-run rather than recorded as product failures. The control
WebSocket (`wss://localhost/p/<id>/ws`) cannot be proxied by this harness and
errors throughout — it does not block sends, but readiness sometimes only lands
after a reload, and one early run showed the outbox flush retrying an upload on
its own where a hand-driven Retry was expected. Both are harness/cluster
artefacts; neither was treated as evidence.

---

## 14. Open items after the final review — 2026-08-10

The whole-branch review returned two merge-blockers, both fixed in `24a2a0cd`
and `d74ee577` and re-reviewed clean. What follows is what remains open.

### 14.1 Needs a product ruling

**The original split-state symptom still occurs on the landing page.** Attaching
on a draft session and pressing Enter leaves no bubble visible for the whole
session-creation window (measured: 0 visible frames over 8 s; creation takes
3–5 min). The cause is a different seam from the one this work fixed —
`createAndConnect` calls a synchronous `reset` that wipes turns and only
re-dispatches after `loadHistory`. It is **pre-existing**, blame `b7685d4a7`
(2026-05-18), months before this plan. The reported case was a resumed session,
which is fixed. Fixing the draft path is separate work.

**Uploads can duplicate with no user action.** A 503 rewritten *after* the
backend already wrote the file produces a duplicate: the eager upload writes,
the client sees failure, and the send stage writes again. Measured live —
`ghost-success.pdf` plus `_1` before any retry, then `_2` on retry. §5.3's
per-file result cache cannot help, because the client never learns it
succeeded. **This needs server-side idempotency** (a client-supplied upload key,
or content-hash dedupe in `_claim_name`). Eager upload made it more likely by
adding an attempt in front of every send; the new DELETE endpoint makes it
recoverable rather than permanent.

### 14.2 Tickets

- **Discard after "upload succeeded, POST failed" orphans the bytes** —
  `persistent-chat.service.ts:3112`. That is precisely when Discard is offered,
  and `adopt` has already released the registry entry. Delete the resolved
  attachments on discard.
- **A never-settling upload wedges the gate.** With `MAX_CONCURRENT_UPLOADS = 2`
  and no `HttpClient` timeout, two hung requests block uploads page-wide until a
  thread switch or reload. `HttpRequest.timeout` works on the XHR backend now.
- **Terminal per-file failures still offer only Retry.** A 413 sets a banner but
  the bubble's only action can never succeed. `chat.upload.removeAndSend` and
  `chat.upload.failed` exist, unwired (§5.5).
- **A→B→A stale flush** — narrowed, not closed. A `connectGeneration` compare is
  the cure, but it must sit *before* `_postInput`; after it, an accepted
  resolution would be dropped and the send would double.
- **`carryOutbox` chips stay pinned to the old thread id**, so `adopt` correctly
  re-uploads to the new thread but the old registry entry is never released and
  its bytes orphan.
- **`_sftp_delete_tree` can 409 mid-walk**, leaving a partial subtree. Matches
  the writers' posture; the alternative is guessing a file type, which is the
  failure class this work closed.
- **Same-thread failed-connect retry still clears the outbox wholesale**
  (`persistent-chat.service.ts:1016`), so an in-flight flush can POST the text
  without its attachment hint. The chips half was fixed; this half was not.
- **The 20-file cap is per call** (`file-handling.service.ts:127`), so two
  attach actions yield 40 chips.
- **Dead CSS**: `_chat-queued.scss`'s surviving `font-size`/`color` under
  `.upload-stage` are overridden by `.upload-stage-line`.

### 14.3 Not exercised

No VM tier. No HTTPS/Cloudflare edge — the e2e gate runs over plain-HTTP
loopback, so the original multipart-corruption symptom is settled only for
progress, not for body integrity. No multi-file or zip batch was ever gated
live; the new concurrency limits are unit-covered only. No `none`-tier 409, and
no reload-mid-upload.
