---
tags:
  - feature
  - canvas
  - orchestrator
  - cockpit
  - durability
aliases:
  - canvas persistence
  - canvas snapshot
  - offline canvas
  - canvas re-pin
related:
  - "[[dynamic_canvas]]"
  - "[[canvas_interactive_html]]"
  - "[[canvas_office_documents]]"
  - "[[workspace_pvc_branch_a]]"
  - "[[vm_snapshots_and_ide]]"
---

# Canvas Durable Presentation — surviving workspace death

**Status:** SHIPPED — Parts 1 and 2 plus the §6 UI fix built and unit-covered
2026-07-28 (`0f2bcddc`); the §11.1 chart auto-default landed and was confirmed
live 2026-07-29. Re-checked 2026-08-05 after a week of unrelated churn and
still green (§12.1). §12 records what was and was **not** confirmed on the
cluster.

**Anchors in this document are symbols, not line numbers.** 332 commits landed
in the week after filing and every line reference in the original draft had
rotted. Cite `file::symbol` here; it survives refactors and stays greppable.
**Parent:** `docs/features/dynamic_canvas.md` (authority for Canvas
architecture). This document **amends** that authority: §1 changes one of its
founding constraints and must be accepted or rejected explicitly.
**Scope:** two changes — a durable byte store for published file canvases
(Part 1) and content-hash re-pinning of a stale workspace generation (Part 2) —
plus one truthfulness fix in the failure UI.
**Non-scope:** live applications (`workspace_app`), the shared browser, the
canvas gateway, the wildcard edge, multi-canvas slots, restore history.

---

## 1. The architectural departure (sign-off required)

`dynamic_canvas.md` is built on one bet, stated in its own words:

> PostgreSQL stores the durable presentation selection: what logical source is
> presented — not a copy of its bytes.

and enforced in its file-response contract:

> Do not serve one file by unpacking the whole suspended-workspace tarball. For
> an inactive remote workspace, v1 reports `unavailable`. This is the honest
> boundary between a live shared stage and durable publishing.

**Part 1 breaks that bet.** After this change the orchestrator stores a copy of
the published bytes and serves them when the workspace cannot be reached. The
canvas stops being purely a pointer and becomes a pointer plus a last-published
copy. That is a real, permanent widening of the feature's data model, and it is
the reason this document exists rather than a commit.

The authority anticipated this exact move and pre-authorized its shape without
committing to it (its "Optional immutable published copies" section, and the
Slice 6 list). What follows is a decision to exercise that option now, at a
deliberately smaller scope than the sketch: one current copy, not a
content-addressed archive with restore history.

### What stays true

- The **workspace remains the only writable authority.** Snapshots are never a
  write target, never merged back, never editable, and never satisfy
  `PUT /main/content`, `POST /main/refresh`, or WOPI `PutFile`.
- **Byte identity is unchanged.** `source_version` is already a sha256 over the
  exact published bytes. A snapshot is served only under the `source_version`
  it was captured for, so a client can never receive different bytes under an
  identity it already holds.
- **The presentation pointer stays the record of truth.** `canvases` is
  unchanged in meaning; the snapshot is subordinate to it and is ignored the
  moment the two disagree.
- **No new endpoint, no new origin, no new auth path.** The existing
  content route serves snapshot bytes under the existing owner check and the
  existing response headers, including the locked `sandbox` CSP for HTML
  renderers (`routers/canvases.py::_content_headers`).

### What changes, and what it obliges us to

1. **Content outlives the workspace.** Deleting a workspace, or a session going
   to sleep, no longer implies the presented bytes are gone from the platform.
   Clearing the canvas deletes the stored copy (§4.5); nothing else does.
2. **The object store now holds user document content.** Previously the
   platform's only copy of a canvas file lived inside the workspace. The
   snapshot bucket now carries document bytes, so backup, retention, and any
   future export/erasure obligation extend to it. Postgres holds metadata and
   an object key only — deliberately, given the database-overflow incidents
   this deployment has already hit.
3. **Deletion becomes a two-store operation.** Every row delete must be paired
   with an object delete, and a failed object delete leaves an orphan. Bounded
   remediation in §4.5.
4. **Canvas durability now depends on the object store being configured.**
   Every shipped overlay has one — the bundled Garage under Tilt, external
   MinIO on the dev cluster (`s3.endpoint` in `values-experimental.yaml`). The
   chart's own default was `garage.enabled: false`, which meant a consumer who
   configured nothing got a Canvas that silently never remembered anything;
   §11.1 records how that was closed (`garage.enabled` is now tri-state,
   defaulting to auto). It stays inert only where an operator explicitly opts
   out of object storage, and the orchestrator warns at startup when that
   happens.
5. **A new class of staleness exists.** A user can be looking at bytes whose
   source file was deleted or rewritten in the workspace hours ago. This is the
   intended behavior, but it must be labelled in the UI every time, never
   silently.
6. **Storage grows with usage.** One object per canvas, replaced on publish;
   non-zero and unbounded in thread count.

### The alternative that preserves the bet, and why it is rejected

Persist the workspace SSH host key on the PVC so the canvas generation stops
rotating. One line in `docker/workspace-entrypoint.sh`, zero canvas changes,
and the pointer would survive a pod restart intact. Rejected for two reasons:
it contradicts a deliberate trust posture stated in that file's section 2a comment
("Host identity must not live below the agent-owned home directory"), and — the
decisive one — it does nothing for the case that dominates the user's day.
A sleeping workspace has no host key to pin. See §2.4.

**Signed off 2026-07-28** by the repository owner: the departure described in
this section, with the six obligations above, is the accepted basis for Parts 1
and 2.

---

## 2. Evidence

### 2.1 The observed failure

Dev-cluster thread `b1758f38`. The agent published
`output/hotel-rheinland-first-three-job-prompts.md` to the canvas. The user
returned later; the canvas was dead and the only recovery was asking the agent
to present the file again.

### 2.2 The bytes survived a full S3 round-trip

The thread had **no PVC at all** — no `pvc-workspace-b1758f38-*` existed. The
orchestrator logged `Workspace restored from S3` against a ~99 MB snapshot. So
this is not "the same volume was reattached". The workspace was torn down,
tarred to object storage, and rebuilt from that tarball — and the file came
back **byte-identical**:

| | value |
|---|---|
| `canvases.source_version` (pinned at publish) | `sha256:eb1cd2e58ec298b892aae309808f8009b3a5f7248b81f97d2d9815a156ebc4fc` |
| `sha256sum` of the file in the restored workspace | identical |
| `canvases` source generation | `eb180d35-24d5-4b65-96c7-f3d82e14dfeb` |
| `threads.metadata.workspace_container._canvas_workspace_generation` | `3ea91431-7531-4b21-8ed9-84a1fdbcbf79` |

**Only the generation UUID moved.** This is the empirical basis for Part 2: the
content-hash predicate hits the happy path across the harshest lifecycle the
platform has, not merely across a PVC reattach.

### 2.3 Why the generation moved

The workspace SSH host key lives on a pod-private `emptyDir`
(`docker/workspace-entrypoint.sh` (`HOST_KEY_DIR`)), and the entrypoint says so outright:
"a new pod/container receives a new key and the provisioner rotates the paired
Canvas generation". `bind_thread_workspace_backing` mints a fresh generation
whenever the backing id **or** the pinned fingerprint changes
(`postgres.py::bind_thread_workspace_backing`). A rebuilt workspace always
changes the fingerprint.

### 2.4 The workspace is down more than it is up

That same workspace **re-suspended roughly 30 minutes after being resumed**,
leaving the namespace with zero `ws-thread` pods. Idle sessions route through
`_suspend_thread_resources` (`main.py::_suspend_thread_resources`), which snapshots to S3
and deletes the pod.

This is the sequencing argument. Part 2 only helps in the minority of wall-clock
time when a workspace happens to be running. **Part 1 is what covers the actual
daily experience**, and it is the half that works when nothing is running at
all. Part 2 is the upgrade from "frozen copy" to "live, editable canvas".

---

## 3. Current behavior

### 3.1 The pointer is durable; the binding is not

`canvases` (migration `0058_canvases.sql`) is thread-scoped and survives
everything. The cockpit re-fetches it on thread select and auto-opens the pane
(`chat-page.component.ts`, the canvas auto-open effect). Nothing about canvas state is memory-only.

What breaks is the pin. `WorkspaceFileSource` carries
`{path, workspace_generation}` (`services/canvas.py::WorkspaceFileSource`), and every read
demands the current generation match:

```
materialize_current            canvas_files.py:1190   → 409 workspace_generation_changed
materialize_for_refresh        canvas_files.py:1243   → same check, so refresh cannot rescue it
_represent                     routers/canvases.py:584-589 → status "unavailable"
```

The failure is **permanent**. A fully healthy workspace holding a byte-identical
file still yields `unavailable` forever, because nothing ever re-pins. The only
recovery today is a fresh `set_canvas` — which is precisely the re-prompting
this work removes.

### 3.2 The failure UI misreports the failure

With `status: "unavailable"`, `syncPresentation` bails at
`canvas-content.controller.ts::syncPresentation` and calls `clearVisual()`, which sets
`displayRenderer = 'unsupported'` (`clearVisual`). `effectiveRenderer()` falls
through to that display value for every file renderer
(`canvas-pane.component.ts::effectiveRenderer`), and `rendererLabel()` prints it
(`rendererLabel`) next to a `sourceKindLabel()` that correctly reads "File"
(`sourceKindLabel`).

Net user-visible result, over the correct file path:

> **File** · Unsupported source
> *Canvas source is currently unavailable.*

The status line is honest. The renderer chip is a lie, and it is the more
alarming of the two: it reads as a corrupt or unrenderable document rather than
a sleeping workspace. Part 1 makes the common case stop happening, but
`unavailable` remains reachable (live apps, browser sources, oversize files, no
snapshot), so the chip is fixed independently in §6.

---

## 4. Part 1 — durable published bytes

### 4.1 Store

**Bytes go to the object store; Postgres holds metadata and a key.** The
snapshot bucket is already the platform's answer for large durable blobs, it
handles image-sized payloads far better than `bytea`, and it keeps document
content out of a database this deployment has overflowed before.

Transport is the existing `SnapshotService` blob API — `put_blob` / `get_blob` /
`delete_blob` (`snapshot_service.py::put_blob` / `get_blob` / `delete_blob`) — the same
seam the job-log archive uses. No new client, no new credentials, no new bucket.

**Key scheme:** `canvas/<thread_id>/<canvas_id>/<sha256-hex>`, written with
`put_blob`.

Explicitly **not** `save_blob`'s content-addressed
`<prefix>/<sha[:2]>/<sha>` scheme (`save_blob`), despite it being the closer-looking
fit. Content addressing makes two threads presenting byte-identical files share
one object, which breaks this feature in two ways: clearing one canvas would
delete bytes another thread still points at, and — across users — object
existence becomes a weak cross-tenant oracle. Thread-scoped keys make deletion
unambiguous and per-tenant. The dedup we give up is worth approximately nothing
here; identical documents across threads are rare, and re-publishing identical
bytes within a canvas overwrites the same key anyway.

Migration **`0073_canvas_snapshots.sql`** (0072 is `jobs_failed_at`):

```sql
CREATE TABLE canvas_snapshots (
    thread_id       UUID        NOT NULL,
    canvas_id       VARCHAR(64) NOT NULL DEFAULT 'main',
    path            TEXT        NOT NULL,
    renderer        VARCHAR(32) NOT NULL,
    media_type      TEXT        NOT NULL,
    source_version  TEXT        NOT NULL,
    object_key      TEXT        NOT NULL,
    byte_size       BIGINT      NOT NULL,
    last_modified   TIMESTAMPTZ,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_canvas_snapshots PRIMARY KEY (thread_id, canvas_id),
    CONSTRAINT fk_canvas_snapshots_canvas
        FOREIGN KEY (thread_id, canvas_id)
        REFERENCES canvases (thread_id, canvas_id) ON DELETE CASCADE,
    CONSTRAINT ck_canvas_snapshots_source_version
        CHECK (source_version ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_canvas_snapshots_object_key
        CHECK (char_length(object_key) BETWEEN 1 AND 1024),
    CONSTRAINT ck_canvas_snapshots_size
        CHECK (byte_size > 0)
);
```

Notes on the shape:

- The composite FK rides `uq_canvases_thread_canvas` and gives one cascade
  chain for the *rows*: `threads` → `canvases` → `canvas_snapshots`. Objects are
  not cascaded by the database — see §4.5.
- Keyed `(thread_id, canvas_id)`, not `thread_id`, so multi-canvas (Slice 6)
  does not need a second migration.
- `renderer` carries **no** CHECK, matching `canvases.renderer`
  (`0058_canvases.sql`) — renderer vocabulary stays app-enforced so adding
  one never needs a migration.
- `object_key` is stored rather than derived from `source_version`. It is
  derivable today, but storing it survives a future prefix or scheme change
  without a backfill.

### 4.2 Eligibility

Snapshot-eligible: `workspace_file` sources whose renderer is one the cockpit
draws from bytes — `markdown`, `text`, `html`, `html-interactive`, `image`.

**Images are in.** With bytes going to the object store, a 25 MiB PNG is an
ordinary object rather than a fat database row, so the reason to exclude them
disappeared. Size is bounded upstream anyway (§4.5).

Excluded, deliberately:

- **`office`** — bytes reach Collabora through the WOPI read path
  (`routers/wopi.py::get_file`), with write semantics attached. Read-only offline
  Office is a coherent follow-up, not this slice.
- **`workspace_app`** and **`browser`** — there are no published bytes; a port
  or a live browser generation cannot be snapshotted, and the user has
  explicitly accepted that these need the container back.

### 4.3 Capture

Capture at every mutation that establishes the published bytes. All three
already hold the validated bytes in memory as `ValidatedCanvasFile.data`, so
capture costs no extra workspace read:

| trigger | service entry point |
|---|---|
| agent `set_canvas` | `CanvasService.set` / `set_if_changed` (`services/canvas.py::set` / `set_if_changed`) |
| `POST /main/refresh` | `CanvasService.refresh_file` (`refresh_file`) |
| user save, `PUT /main/content` | `CanvasService.edit_file` (`edit_file`) |

Capture is **best-effort and non-fatal**: a snapshot write failure logs and is
swallowed, never failing the publish. This matches the codebase's
graceful-degradation convention (audit store, Neo4j) and keeps a new storage
dependency off the critical path of the agent's tool call.

### 4.4 Serve

One invariant governs the whole read path:

> A snapshot is served **only** when `canvas_snapshots.source_version` equals
> the `source_version` on the live `canvases` row.

Any disagreement means the snapshot is stale; it is ignored, not repaired.
Because of this, capture ordering relative to the row update is irrelevant — a
half-written or superseded snapshot can never be served.

**State path** (`routers/canvases.py::_represent`): where the current code
maps `workspace_unavailable` / `workspace_generation_changed` /
`canvas_file_not_found` to `status = "unavailable"` (`:584-589`), it first looks
for a matching snapshot. On a hit: `status = "ready"`,
`content_origin = "snapshot"`, `capabilities.can_edit = false`,
`can_pop_out = true`, and `content_url` is emitted as usual. On a miss: today's
`unavailable`, unchanged.

**Content path** (`get_main_canvas_content`): `_verify_content_identity`
against the live row runs **first and unchanged**, so the caller must already
present the correct revision, fingerprint, and version — a snapshot can never be
addressed independently of the current presentation. Only when
`materialize_current` raises one of the three workspace codes does the handler
fall back to snapshot bytes, with the identical ETag (`"sha256:<source_version>"`),
the identical `_content_headers` output including the HTML CSP, plus
`X-Canvas-Content-Origin: snapshot` for operators. `If-None-Match`, `Range`, and
`HEAD` behave exactly as on the live path — same bytes, same validators.

The post-read owner re-admission (`the post-read owner re-admission`) is retained on the snapshot path.

**When the object store is unavailable or the object is missing**, `get_blob`
returns `None` (`snapshot_service.py::get_blob` swallows and logs) and the request
falls through to today's `unavailable` path. A snapshot never turns a working
canvas into a broken one; the worst case is the behavior that exists now.

### 4.5 Limits, retention, deletion

**Size needs no meaningful new limit.** The canvas already refuses to
materialize anything larger than its renderer allows — 2 MiB text/HTML, 25 MiB
image, 50 MiB absolute (`canvas_files.py` MAX_* constants, applied per path in
`_workspace_read_limit`, `_workspace_read_limit`). The bytes are therefore *already* bounded
before capture ever sees them, and the 1 GB case is structurally impossible: it
could never have been presented in the first place. `CANVAS_SNAPSHOT_MAX_BYTES`
exists as a redundant guard and an operator brake, defaulting to
`MAX_FILE_BYTES` (50 MiB) so the rule is simply **"if the canvas can present
it, the canvas can remember it"**. `0` disables capture and fallback entirely,
and with them the §1 departure.

**Retention** is exactly **one object plus one row per canvas**, replaced on
each publish. No history, no versions, no GC job in the steady state.

**Deletion** happens in three places, and only the first is subtle:

1. `clear_canvas` — an **explicit** row delete inside `CanvasService.clear`'s
   existing locked transaction (`services/canvas.py::clear`), because clearing
   nulls the source on the `canvases` row rather than deleting it, so the FK
   cascade does not fire.
2. Canvas-row or thread deletion — the FK cascade removes the row.
3. Replacement on publish — when the new `source_version` differs, the previous
   object is deleted after the new one is written.

In all three, the **object** delete is a separate `delete_blob` call that the
database cannot cascade. Order is row-first, object-second: a row without an
object degrades to `unavailable` (safe), whereas an object without a row is an
orphan (leaked bytes). A failed `delete_blob` is logged at error and leaves an
orphan.

**Orphan remediation** is deliberately not a background reconciler in v1. The
key scheme is deterministic and thread-scoped, so a bounded sweep —
list `canvas/<thread_id>/`, delete anything not named by that thread's row — is
straightforward to add to the existing `SnapshotService.run_gc`
(`snapshot_service.py::run_gc`) if orphan volume ever justifies it. Until then the
failure is logged and finite: at most one leaked object per failed delete, each
capped at 50 MiB.

### 4.6 API shape, and why it is an additive field

`CanvasPublicState` gains one optional field:

```python
content_origin: Literal["workspace", "snapshot"] | None = None
```

Deliberately **not** a new `CanvasStatus` value. The cockpit type guard gates on
`CANVAS_STATUSES.has(value['status'])` (`canvas.service.ts`, `CANVAS_STATUSES` + `isCanvasState`);
an unknown status fails `isCanvasState`, the whole state object is dropped, and
the pane goes blank. An unknown *field* is ignored by the same guard. So under
rollout skew, an old cockpit against a new orchestrator renders the document
with no offline banner — degraded, correct, and not a blank pane.

The field is inside the state payload and therefore inside the ETag
(`services/canvas.py::build_public_canvas_representation`). That is
intended: when the workspace wakes and the origin flips back to `workspace`, the
ETag changes and clients re-fetch.

---

## 5. Part 2 — re-pin by content hash

### 5.1 Predicate

When a `workspace_file` canvas fails only with `workspace_generation_changed`
and the thread currently has a ready workspace: read `source.path` under the
**current** generation and hash it.

- **Hash equals the pinned `source_version`** → re-pin. Update
  `source.workspace_generation` to the current generation, recompute
  `source_fingerprint`, bump `presentation_revision`, emit `canvas.updated`.
  The canvas returns to `content_origin: "workspace"`, editable if it was
  editable, refreshable, poppable.
- **Hash differs** → no re-pin. Status `source_changed`, which already has the
  "ask the agent to refresh" / "load current workspace version" affordance
  (`canvas.status.sourceChanged`). The snapshot stays viewable underneath.
- **File missing or unreadable** → no re-pin; snapshot serve per Part 1.

### 5.2 Why this is provenance-safe

The generation pin exists so that a stale presentation "can never silently
serve new bytes or a replacement source under its old rendering context"
(`dynamic_canvas.md`, the workspace-file gateway section). Re-pinning on byte-identity preserves that property
exactly, and by a stronger mechanism: the served bytes are proven identical by
sha256 rather than inferred from workspace identity.

What is given up is the claim "these bytes came from the same physical backing".
That is not a security boundary here — `canvases` is thread-scoped, threads are
user-scoped, and the workspace is thread-scoped, so no cross-tenant path exists.
§2.2 is the demonstration that the property being given up is already not what
the platform delivers: the bytes had been through S3 and back.

`workspace_app` and `browser` sources are **never** re-pinned. A port on a
rebuilt workspace is a genuinely different thing, and no hash can say otherwise.

### 5.3 Why the revision must be bumped

`canonical_source_fingerprint` includes `workspace_generation`
(`services/canvas.py::canonical_source_fingerprint`). Re-pinning necessarily changes
`source_fingerprint`, which is embedded in `content_url` and enforced by
`_verify_content_identity`. Old content URLs must therefore be invalidated, and
the revision bump plus `canvas.updated` is the existing mechanism for exactly
that. A silent re-pin would strand every mounted client on a URL that now 409s.

### 5.4 Trigger

**v1: lazy, on the read paths** (`_represent` and the content GET). This fires
when the user opens or refocuses the session — which is when it matters — and
keeps the provisioner uncoupled from canvas internals.

It is a mutation on a read path, so it takes the existing presentation advisory
lock (`canvas_presentation_lock_key`) under `_canvas_mutation_admission()`,
re-reads the row inside the lock, and no-ops if another request already
re-pinned. Attempted at most once per (generation, revision) pair so a
permanently mismatched file cannot turn every GET into a workspace read.

A provisioner-side push on workspace-ready — so an already-open pane re-pins
without user action — is a clean follow-up, listed in §10 rather than built now.

### 5.5 Tool-card wrinkle

The `set_canvas` chat card decides "this presentation has since been replaced"
by comparing revisions. A re-pin bumps the revision without replacing anything,
so an unchanged presentation would start reading as replaced. In scope for Part
2: compare the card's source identity (`path` + `source_version`) instead of the
revision.

---

## 6. Failure-UI truthfulness

Independent of Parts 1 and 2, and required by both.

- `rendererLabel()` must report the **declared** renderer from
  `state.renderer` whenever the state carries a known file renderer, reserving
  `'unsupported'` for genuinely unknown renderer vocabulary. The chip must never
  contradict the source line beneath it.
- `content_origin === 'snapshot'` renders one banner — *"Workspace is asleep —
  showing the version presented on {{date}}"* — and that banner **replaces** the
  `canvas.status.unavailable` overlay rather than sitting beside it. Two
  messages about the same condition is the current bug in miniature.
- Snapshot-backed state hides edit affordances entirely (server already sends
  `can_edit: false`; the cockpit must not show a disabled-looking editor).
- New i18n keys under `canvas.status.*` / `canvas.offline.*` in **both**
  `en.json` and `de-DE.json`.

---

## 7. Acceptance criteria

Backend criteria are pytest; cockpit criteria are vitest; A20 is manual on k3d.
S3-touching backend tests use a stubbed `SnapshotService`, not a live bucket.

**Part 1 — durable bytes**

1. Publishing a markdown file writes exactly one `canvas_snapshots` row whose
   `source_version` equals the `canvases` row's, and one object at
   `canvas/<thread_id>/main/<sha>` whose length equals the row's `byte_size`.
2. With no `ws-thread` pod in the namespace, `GET /main` returns
   `status: "ready"`, `content_origin: "snapshot"`,
   `capabilities.can_edit: false`, and a non-null `content_url`.
3. With no workspace, `GET /main/content` returns the exact published bytes,
   ETag `"sha256:<pinned source_version>"`, the same `Content-Type` /
   `Content-Disposition` / CSP headers as the live path, and honors
   `If-None-Match` (304) and `Range` (206).
4. An image canvas snapshots and serves offline on the same path as text,
   including `Range`.
5. With `CANVAS_SNAPSHOT_MAX_BYTES` lowered below the file size, no row and no
   object are written, and the offline behavior is byte-for-byte today's
   `unavailable` path.
6. **`clear_canvas` deletes both the row and the object.** Deleting the thread
   cascades the row away and deletes the object. Re-publishing with different
   bytes deletes the superseded object.
7. A failed `delete_blob` logs at error, leaves the row deleted, and never
   fails the user-facing operation.
8. Snapshots are never writable: with the workspace down, `PUT /main/content`,
   `POST /main/refresh`, and WOPI `PutFile` all still fail with the existing
   workspace error and leave `canvas_snapshots` untouched.
9. A snapshot whose `source_version` differs from the live `canvases` row is
   never served, on either the state or the content path.
10. **Object store down or object missing** → `get_blob` returns `None` → the
    request degrades to today's `unavailable`, never a 5xx.
11. A capture failure (`put_blob` false) logs at error and does **not** fail
    `set_canvas`; the canvas publishes normally with no snapshot.
12. `CANVAS_SNAPSHOT_MAX_BYTES=0` fully disables capture and fallback; behavior
    is identical to pre-change on every path.

**Part 2 — re-pin**

13. **Suspend → S3 restore → re-pin.** Publish a file, force the session to
    suspend (workspace tarred to S3, pod deleted, *no PVC reattach*), resume,
    then `GET /main` returns `status: "ready"` with
    `content_origin: "workspace"`, restored `can_edit`, a bumped
    `presentation_revision`, and one `canvas.updated` event. This is the
    `b1758f38` regression test: identical hash, moved generation.
14. A file modified in the workspace after restore yields `source_changed`, not
    a silent re-pin, and the snapshot remains viewable.
15. A file deleted in the workspace after restore yields snapshot-backed
    `ready` with `content_origin: "snapshot"`, never a re-pin.
16. `workspace_app` and `browser` sources are never re-pinned and never
    snapshotted.
17. Concurrent GETs during a re-pin produce exactly one revision bump.

**UI and rollout**

18. Snapshot-backed state renders the document, shows the offline banner with
    the capture date, hides edit affordances, and the chip reads the real
    renderer. **"Unsupported source" never appears for a state carrying a known
    file renderer** — the `b1758f38` visible regression.
19. An old cockpit build against the new orchestrator still renders a
    snapshot-backed canvas (unknown `content_origin` ignored; `status` stays
    within `CANVAS_STATUSES`), with no banner and no blank pane.
20. A `set_canvas` tool card does not read "replaced" after a re-pin of the
    same presentation.

**Live verification (manual, k3d — bundled Garage, auto-enabled per §11.1)**

21. Create a session → `set_canvas` on a markdown file → confirm live render →
    force the workspace to suspend → confirm `kubectl get pods -l ...` shows no
    `ws-thread` pod → the canvas still renders from the snapshot with the
    offline banner and no edit affordance → resume the session → the canvas
    returns to live and editable, with the pane updating without a page reload.

---

## 8. Migration and repo hygiene

- Migration number is **`0073_canvas_snapshots.sql`**. `0072_jobs_failed_at.sql`
  landed 2026-07-28.
- `scripts/schema-snapshot.sh` **must** be run after the migration and the
  regenerated `orchestrator/database/schema_current.sql` committed with it.
- **Never `git add -A` in this repo.** Commit `95ee3011`
  ("feat(chat): add affordances for stalled message retries and discards")
  swept `Tiltfile`, `scripts/local-dev-up.sh`, and
  `docs/tests/app_guide_m2_live_acceptance_results_2026-07-28.md` into an
  unrelated feature commit. `Tiltfile` and `scripts/local-dev-up.sh` are dirty
  in the working tree right now. Stage every path explicitly.
- `schema.sql` and `vector_schema.sql` are frozen; they are not touched.

---

## 9. Rollout

Orchestrator and cockpit ship in the same Helm release, but a rolling upgrade
still produces brief skew in both directions:

- **Old cockpit → new orchestrator:** safe by construction (§4.6). Unknown
  `content_origin` is ignored; `status` never leaves the known set. Worst case
  is a snapshot rendered without its banner.
- **New cockpit → old orchestrator:** `content_origin` is absent. The cockpit
  must treat *absent* as `"workspace"`, never as unknown-and-blocked.
- **Old orchestrator reading new rows:** `canvas_snapshots` is a new table that
  older code never queries; `canvases` is unmodified. No down-migration hazard.

Migration `0073` is additive, transactional, and takes only a brief lock on
`canvases` for the foreign key.

---

## 10. Deferred, with the seam left open

- **Office offline read** — snapshot `.docx`/`.xlsx` bytes and serve them to
  Collabora with `UserCanWrite: false`. Needs a WOPI-side read path decision.
- **Provisioner-side re-pin push** on workspace-ready, so an open pane
  self-heals without a user action (§5.4).
- **Orphan-object sweep** folded into `SnapshotService.run_gc` — bounded and
  straightforward given deterministic thread-scoped keys (§4.5), deferred until
  logged orphan volume justifies it.
- **Restore history / content-addressed archive** — the full `:1341` sketch.
  Explicitly out of scope: one current copy is what the daily experience needs.

---

## 11. Decisions taken

Recorded so the next reader does not reopen them. Owner: user, 2026-07-28.

| # | Decision |
|---|---|
| 1 | **§1 departure accepted.** Canvas may store a copy of published bytes. |
| 2 | **Object store, not Postgres** — S3 is best practice for blobs and this deployment has already hit database-overflow incidents. |
| 3 | **Re-pin trigger is lazy-on-read** for v1; provisioner push deferred. |
| 4 | **One current copy**, no history. |
| 5 | **Office excluded** from v1. |
| 6 | **Capture failures non-fatal**, with an alertable error log. |
| 7 | **Tool-card "replaced" fix in scope** for Part 2. |
| 8 | **Images included** — free once bytes live in the object store. |
| 9 | **No small cap.** Upstream renderer validators already bound size; the knob is a redundant guard at `MAX_FILE_BYTES`. |
| 10 | **Ship default-on.** |
| 11 | **Banner reads "Workspace is asleep — showing the version presented on {{date}}"**; "asleep" over "offline" because suspension is normal and reversible. |
| 12 | **No extra deletion UI.** The stored copy is deleted when the canvas is cleared or the session/thread is deleted (§4.5); the banner is the only user-facing mention. |
| 13 | **§6 renderer-label fix ships bundled** with Parts 1 and 2, not ahead. |

### 11.1 Chart default — RESOLVED 2026-07-28

`garage.enabled` was `false`, so a chart consumer who configured no object
store got a Canvas that silently never remembered anything. Flipping it to
`true` was rejected: `values-experimental.yaml` sets external MinIO endpoints
but never disables Garage, so the dev cluster would have gained an unused
StatefulSet **and a 20Gi PVC** — and in this repo an unexpected PVC is what
hard-fails later `helm upgrade`s.

Shipped instead: `garage.enabled` is **tri-state**, defaulting to `null` = auto
— bring your own store by setting `s3.endpoint`, or say nothing and get the
bundled one. `true`/`false` remain explicit and always win. Resolved through
one helper, `srw.garageEnabled`, which every template now reads instead of the
raw value.

Rendered outcomes (all verified with `helm template`):

| Configuration | Garage renders | `S3_ENDPOINT` |
|---|---|---|
| chart defaults, nothing set | **yes** (the fix) | bundled service |
| `s3.endpoint` set (dev cluster) | no (unchanged) | external |
| `garage.enabled=true` + external | yes | external wins |
| `garage.enabled=false` | no | empty |

The one remaining silent case — explicitly `false` with no external endpoint —
is now a startup **warning** naming what breaks, rather than the previous INFO
line (`snapshot_service.py`). A hard chart `fail` was deliberately not added:
minimal/lite installs may legitimately run without object storage.


---

## 12. Verification record (2026-07-28, k3d + live Garage)

Session `c324a85a-f8d4-4dc4-b4c1-58d79985a9c7`, sandbox tier, remote binding,
no PVC — the same shape as the `b1758f38` post-mortem.

**Confirmed on the cluster:**

| Criterion | Evidence |
|---|---|
| 1 | `set_canvas` wrote one row plus `canvas/<thread>/main/4dae066e…` (44 bytes) in the `srw-snapshots` bucket |
| 2 | With **zero** `ws-thread` pods: `status: ready`, `content_origin: snapshot`, `can_edit: false`, `can_pop_out: true` |
| 3 | Content GET returned the exact bytes, `ETag: "sha256:4dae066e…"`, `Content-Type: text/markdown`, `X-Canvas-Content-Origin: snapshot`, `nosniff` |
| 6 | `clear_canvas` left 0 rows **and** 0 objects under `canvas/` |
| 13 | Generation rotated `92af90ea…` → `38ddd009…`; with byte-identical content restored, the Canvas re-pinned: revision 1 → 2, new fingerprint, `content_origin` back to `workspace`, snapshot row still valid |
| 15 | Same rotation with the file **absent**: no re-pin, revision stayed 1, stored copy still served |

**Not confirmed live, and why:**

- **The genuine suspend → S3 → restore leg of criterion 13.** Workspace
  suspension fails on the k3d dev stack: `/run/secrets/vm-ssh-key` is mounted
  `0444`, OpenSSH refuses it, `capture_vm_snapshot` fails, and
  `suspend_thread_workspace` keeps the workspace alive instead. This is a
  **known, already-triaged, dev-only** issue —
  `docs/issues/dev_snapshot_ssh_key_perms_0444.md`, filed 2026-06-22 and
  re-confirmed live here on 2026-07-28/29. It is dev-only because OpenSSH runs
  its permission check **only when the key is owned by the uid running ssh**:
  the dev image runs as root (uid 0 == the key's owner, check fires), while
  prod runs as non-root `srw` (uid ≠ owner, check skipped). Do not "fix" it by
  lowering `defaultMode` to `0400` — that breaks prod, where non-root cannot
  read a root-owned `0400` file.

  Substituted workspace pod deletion plus re-provision, which produces the same
  generation rotation the re-pin actually keys on. **Canvas durability does not
  depend on that issue**, and prod's suspend → restore path is unaffected —
  which is also what `b1758f38` in §2.2 demonstrates, since that thread really
  did restore from S3.

  **Unblocked 2026-08-05** (`52c1ba80`): `resolve_ssh_key_path` now stages a
  runtime-owned `0600` copy, so suspend works on k3d and this criterion is
  verifiable there for the first time. Left open rather than claimed — nobody
  has run it. It is the cheapest remaining item in this document.
- **Criterion 14 (changed bytes → `source_changed`).** The live attempt was
  invalid: `workspace_container.status` had already flipped to `deleted`, so the
  read failed as unavailable before any hash comparison. Covered by
  `test_source_changed_never_falls_back_to_the_snapshot`, which pins the exact
  error-code mapping.
- **Criteria 18–20 (cockpit).** Covered by vitest, not driven in a browser.

**Change made during verification:** the re-pin attempt guard originally
suppressed retries for *any* declined verification. A workspace that is still
restoring reads as unreadable, so that would have stranded the Canvas on its
stored copy until the next publish or a process restart. The guard now records
an attempt only for a definitive content mismatch; unreadable stays retryable
(`_maybe_repin`, `routers/canvases.py`).

**Chart auto-default, verified on the same cluster (2026-07-29):** after the
§11.1 change, Tilt re-ran helm (release revision 30) with `garage.enabled`
absent from the values entirely — `helm get values` shows no `garage` key — and
the auto path resolved it. `srw-config` carries
`S3_ENDPOINT=http://srw-garage:3900`, the Garage StatefulSet and Service are
still owned by the release and were **not** recreated (18h pod uptime, as the
byte-identical render predicted), and the orchestrator logs "Snapshot service
ready" rather than the new disabled-warning. A put/get/verify/delete round-trip
through `SnapshotService` under the exact `canvas/<thread>/main/<sha>` key
scheme succeeded against live Garage.

**Known behavior, per §1 obligation 1:** presenting a live app or shared browser
after a file leaves that file's stored copy in place until the Canvas is cleared
or another file is published. Consistent with "clearing is the delete
affordance", but worth revisiting if it ever surprises someone.

### 12.1 Re-check after a week of churn (2026-08-05)

332 commits landed on `develop` between the canvas commit and this check,
including two unrelated canvas fixes that touch the same files:

- `51db3570` — Cloudflare rewrites the strong state ETag to `W/"canvas:…"`, so
  every mutation refused the only value a browser could echo back. Added
  `strong_state_precondition`, which drops the weak marker at all seven
  precondition sites. Adjacent to this feature's `If-Match` story (§4.6) but
  orthogonal: the full-digest comparison is unchanged, so the requirement that
  the `visible_builder` closures rebuild an identical representation still
  holds exactly as written.
- `27235e71` — the canvas gateway's restricted role cannot lock rows it only
  holds `SELECT` on. Viewer-session path only; no durability surface.

Confirmed still true today:

- **461 canvas tests pass** (up from 440 — the two fixes brought their own),
  including all 24 in `tests/test_canvas_snapshots.py`.
- `canvas_snapshots`, `CanvasSnapshotStore`, the `content_origin` field, and
  the re-pin path are all intact and unmodified by the churn.
- **Chart auto-default still resolves correctly** against today's overlays,
  which matters because `values-experimental.yaml` moved substantially:
  dev-cluster render yields **0** Garage resources with
  `S3_ENDPOINT` pointing at external MinIO, bare chart defaults yield **9**
  Garage resources pointing at the bundled service.

Not re-run: the live k3d session walk from §12. The cluster has moved on by
~330 commits, and the unit + render coverage above is what actually pins this
feature's behavior.
