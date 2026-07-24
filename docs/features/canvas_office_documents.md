---
tags:
  - feature
  - cockpit
  - agent-tool
  - collaboration
  - canvas
aliases:
  - office canvas
  - collabora integration
  - docx canvas
  - office co-editing
  - wopi host
related:
  - "[[dynamic_canvas]]"
  - "[[shared_browser]]"
  - "[[ms365_main_cloud_and_datasource]]"
---

# Feature: Office Documents on Canvas (Collabora + WOPI)

> The agent can already author and edit real Office files — `python-docx`,
> `openpyxl`, `python-pptx` all work in the workspace today. What it cannot do
> is *show* the result: `set_canvas` on a `.docx` fails with
> `unsupported_canvas_file`, and the user has no way to open, review, or edit
> the document without downloading it. This feature puts Office documents on
> the shared Canvas — first view-only, then live user editing through an
> embedded Collabora editor — while the AI keeps editing the same file through
> Python, turn-based.

**Status:** Design approved 2026-07-21. Not implemented. Slicing below.

## Motivation

Observed failure (2026-07-20, dev session): the agent produced
`output/premium_marketplace_profile_setup_pack.docx` and called `set_canvas`
on it. The orchestrator's closed Slice-1 renderer set
(`markdown | text | html | image`) rejected it — a `.docx` is a binary ZIP
container, so it fails both the UTF-8 text decode and the extension allowlist
in `validate_canvas_bytes` (`orchestrator/services/canvas_files.py`). The
model recovered by presenting a parallel `.md`, which proves the content
pipeline works and only the presentation surface is missing.

The goal is the full loop: *"work with the AI in Word, Excel, or PowerPoint"*
— the AI drafts a document, the user sees the real thing beside the chat,
edits it directly, and the AI iterates on the user's edits.

## Decisions

1. **SRW-native WOPI host + bundled Collabora CODE** (approach A). The
   orchestrator implements the WOPI storage contract over workspace files; a
   chart-bundled, opt-in Collabora CODE renders and edits them. The workspace
   file remains the **single source of truth**, exactly as
   [[dynamic_canvas]] requires ("both parties edit the same workspace file").

   Rejected alternatives:
   - *Ride OpenCloud's Collabora integration:* would edit the **cloud copy**
     while the AI edits the **workspace copy**. Session↔cloud sync is an open
     defect (per-session shared folder holds a frozen pre-mount partial
     snapshot), so this creates two diverging sources of truth by design.
   - *LibreOffice→PNG preview pipeline:* `src/services/document_renderer.py`
     already sketches this, but **no deployed image ships LibreOffice** (only
     `poppler-utils`); the DOCX/PPTX paths are latent code with a dead
     dependency. Shipping previews would add ~500 MB to an image and still
     deliver less than Collabora's view mode, which we deploy anyway.

2. **Turn-based AI participation.** No realtime AI cursor in v1. This matches
   the Canvas doctrine ("turn-based collaboration with honest, best-effort
   optimistic file writes. No CRDT"). Neither Collabora nor OnlyOffice
   Community lets a headless program join as a realtime collaborator; the
   bridge is the WOPI storage-version mechanism plus an explicit editor
   reopen after agent writes. The realtime-cursor idea is recorded under
   *Deferred*, not committed.

3. **Collabora CODE, not OnlyOffice.** LibreOffice technology, MPL-licensed
   (no friction with the FSL product license), one engine for Word + Excel +
   PowerPoint, native ODF support, self-hostable single container, and prior
   operational experience in the HomeLab (2026-06-03 Nextcloud HA design ran
   Collabora CODE + WOPI). OnlyOffice's community edition caps connections
   and its automation API is commercial.

4. **Fail closed without the service.** If `collabora.enabled=false`, Office
   files remain unsupported with a clear tool error. No degraded half-mode,
   no silent fallback renderer.

## Format Scope

| In v1 | Out of v1 |
|---|---|
| `.docx`, `.xlsx`, `.pptx` (OOXML) | Legacy binary `.doc`, `.xls`, `.ppt` |
| `.odt`, `.ods`, `.odp` (ODF — Collabora-native, near-free) | Anything Collabora merely imports (`.csv` stays on the text path, `.rtf`, …) |

Detection: extension allowlist **and** byte sniffing. libmagic reports OOXML
as `application/vnd.openxmlformats-officedocument.*` or plain
`application/zip`, and ODF as `application/vnd.oasis.opendocument.*`; accept
those combinations only when the extension agrees, mirroring the existing
`mime_renderer_mismatch` posture. New size bound
`CANVAS_MAX_OFFICE_BYTES` (default 25 MiB) wired into
`_workspace_read_limit` via the office extension set.

## Architecture

```
Cockpit canvas pane
  └─ iframe → https://office.<domain>   (Collabora CODE, opt-in chart service)
                 │  WOPI (cluster-internal)
                 ▼
       Orchestrator WOPI host
         CheckFileInfo / GetFile / PutFile
                 │  existing canvas file transport
                 ▼
       Workspace file  (single source of truth)
                 ▲
       Agent edits via python-docx / openpyxl / python-pptx (unchanged)
```

### Collabora deployment

- Chart opt-in `collabora.enabled` (default **off**), following the bundled
  Garage pattern: single replica, `Recreate` strategy, ~1–2 GB RAM request
  ceiling, readiness probe on `/hosting/discovery`.
- Public hostname `office.<domain>` rides the deployment's existing
  Cloudflare Tunnel to the cluster service, per the hosted-edge decision.
  Collabora uses **standard** WebSockets, which cloudflared supports (the
  known headscale breakage is specific to its non-standard upgrade); this is
  a **verification item in the first live gate, not an assumption**.
- Collabora config: TLS termination at the edge
  (`--o:ssl.enable=false --o:ssl.termination=true`), WOPI host allow-list
  pinned to the orchestrator's cluster-internal service URL,
  `frame-ancestors` pinned to the Cockpit origin only.

### WOPI host (orchestrator)

Three endpoints on a dedicated router, **cluster-internal only** (Collabora →
orchestrator; never user-facing, never through the public ingress):

- `GET  /wopi/files/{file_id}` → CheckFileInfo
- `GET  /wopi/files/{file_id}/contents` → GetFile
- `POST /wopi/files/{file_id}/contents` → PutFile (Slice 2 only)

Mechanics:

- **file_id / docKey** is a stable digest of `(thread_id, canonical_path)` so
  all viewers of one Canvas source join the same Collabora document session.
- **Access tokens** are short-lived signed tokens minted by the orchestrator
  when the Cockpit mounts the editor, scoped to
  `(thread_id, path, user_id, write_flag, expiry)`. Every WOPI call
  validates the token against the *current* Canvas state (source must still
  point at that path; thread membership must still hold). Token travels only
  via the standard WOPI form-post into the iframe, mirroring the Slice-3B
  "no credential in the iframe URL" boundary.
- **Reads** reuse the bounded canvas transport (`canvas_files.py` SSH reads,
  office byte cap, magic re-validation on every serve).
- **Writes** (Slice 2) land in the **same cluster-wide coordinator lock +
  precondition pipeline** as Slice-2 text saves: advisory lock keyed by
  `(thread_id, canonical_path)`, re-hash, atomic rename where supported,
  `source_version` + presentation revision bumped in the same transaction.
  WOPI's `X-WOPI-Timestamp`/version header maps onto the existing
  ETag-precondition semantics: a version mismatch returns the WOPI conflict
  status (409 + `X-WOPI-ItemVersion`), which makes Collabora surface its
  "document changed in storage" dialog. The Cockpit text-edit `PUT` route
  stays text-only; office bytes never flow through it.

### Canvas renderer surface

New renderer value `office` beside `markdown | text | html | image`.
Touched surfaces (kept in sync, mirroring the Slice-1 pattern):

- `src/tools/canvas/__init__.py` — renderer `Literal`s in both arg schemas;
  `editable` description; no new tool and no new source type
  (`workspace_file` covers it).
- `orchestrator/services/canvas_files.py` — office extension/magic detection
  in `validate_canvas_bytes`, office byte bound, renderer compatibility set
  (`office` is compatible only with itself; no `text` downgrade for a ZIP
  container).
- `orchestrator/services/canvas.py` — `editable` legality: Slice 1 rejects
  `editable=true` for `office`; Slice 2 allows it when the deployment has
  Collabora enabled.
- Cockpit — `CanvasTrustedRenderer` gains `office`; a new office iframe
  component follows the Slice-3 viewer iframe lifecycle (mount/remount,
  anti-framing posture, no service-worker interception on the foreign
  origin). `selectCanvasRenderer` keeps failing closed on unknown values, so
  an old Cockpit against a new orchestrator degrades to "unsupported", not
  breakage.
- Capabilities: `can_edit` reflects Slice-2 availability; no new
  capability keys.

## Slices

### Slice 1 — View (closes the observed failure)

- Chart: `collabora.enabled` service, hostname, tunnel route.
- Orchestrator: office detection/validation; WOPI **read** path only —
  `CheckFileInfo` with `UserCanWrite=false`, `GetFile`; token minting.
- Cockpit: office renderer component mounting Collabora in view mode.
- Tools: `set_canvas` accepts office files (`renderer: auto → office`);
  `editable=true` rejected with a clear message.
- No concurrency machinery whatsoever. Agent writes file → `set_canvas` →
  user sees the real document.

### Slice 2 — Edit (turn-based collaboration)

- WOPI `PutFile` through the coordinator-locked precondition pipeline.
- `editable=true` legal for office sources on writable backends.
- Turn-taking, per the doctrine:
  1. User edits in Collabora; autosave flows through `PutFile`, bumping
     `source_version` and firing the existing `canvas.source_updated`
     invalidation, so the agent's next `read_file` is fresh (doctrine step 5
     applies unchanged — companion guidance: `get_canvas` + fresh
     `read_file` before agent edits).
  2. Agent writes the file via Python, then calls `set_canvas` to republish.
  3. Cockpit sees the version bump and drives the editor through Collabora's
     **PostMessage API** (the documented hosting-frame contract, negotiated
     via `Host_PostmessageReady`): `Host_VersionRestore
     {Status: "Pre_Restore"}` makes Collabora **save any unsaved user edits
     first, then reload the document** — confirmed by Collabora upstream as
     the supported "storage changed underneath the editor" flow (discussion
     #5474). `Action_Save {DontSaveIfUnmodified: true}` is available for
     explicit pre-agent-write flushes. Known cosmetic cost: a repaint and a
     brief "user left" tooltip during reload. The guaranteed fallback
     remains the WOPI version-mismatch conflict dialog on the editor's next
     save; no admin-websocket dependency exists in this design.
- Honest-concurrency stance carried over verbatim: this narrows stale-save
  windows, it does not make them impossible. UI and docs must not call it
  race-free.

### Deferred (recorded, not committed)

- **Realtime AI cursor** — puppet an editor client (headless browser joining
  the Collabora session) or a commercial automation API. Reuses all WOPI
  plumbing if ever built.
- **Browser as Canvas source** — the parked "agent's browser in the canvas"
  idea; belongs to [[shared_browser]] and the reserved `browser` source type
  sketched in [[dynamic_canvas]].
- Legacy `.doc/.xls/.ppt`; Collabora `convert-to` REST for thumbnails or a
  PDF viewer; multi-document canvases.
- **Agent visual self-review via `convert-to`** — the AI edits `.pptx`
  (and `.docx`) blind to rendering; Collabora's stateless `convert-to`
  endpoint can produce page/slide PNGs so the agent inspects its own
  rendered output with vision before ending its turn. High-leverage for
  deck quality; needs the same token-gated, bounded posture as WOPI reads.

## Security Notes

- WOPI endpoints are cluster-internal with per-call token validation; a
  leaked token is scoped to one file, one thread, one direction, minutes of
  validity. NetworkPolicy pins Collabora→orchestrator and denies
  Collabora→anything-else (it needs no other egress).
- Collabora renders untrusted agent-produced bytes. It runs as its own pod
  with no workspace credentials, no DB access, and no S3 access; its only
  path to bytes is the token-gated WOPI host. Macro execution stays at
  Collabora's default-off posture.
- The editor iframe is a foreign origin; the existing trusted-parent
  anti-framing boundary and `frame-ancestors` pinning apply. No wildcard
  CORS anywhere in the path.
- Byte caps and magic re-validation on every serve prevent the office path
  from becoming an oversized/undetected-content smuggling route.

## Testing / Verification

- **Unit:** token mint/validate/expiry/scope, office magic+extension
  detection (incl. `application/zip` ambiguity and mismatch rejections),
  renderer compatibility gating, `editable` legality per slice.
- **Integration (podman):** real Collabora CODE container against a stubbed
  workspace — CheckFileInfo/GetFile round-trip; Slice 2 adds PutFile save,
  version-conflict dialog path, and post-agent-write reopen.
- **Live k3d gate before any dev rollout:** tunnel WebSocket pass-through,
  iframe mount from the real Cockpit origin, two-browser co-edit of one
  `.xlsx`, agent turn (Python edit → `set_canvas` → editor reopen), and
  `collabora.enabled=false` failing closed with the documented error.

## Resolved Questions (2026-07-23)

- **Editor refresh capability:** resolved — no admin-API dependency. The
  hosting frame drives save/reload via the PostMessage API
  (`Host_PostmessageReady` handshake, `Action_Save`,
  `Host_VersionRestore`), a long-standing documented part of the Collabora
  SDK; upstream confirms `Host_VersionRestore` flushes unsaved edits before
  reloading (CollaboraOnline/online discussion #5474). The chart pins a
  current CODE release; the podman integration test is the compatibility
  gate for the pinned image.
- **Presence identity:** yes — `CheckFileInfo` surfaces the SRW user's
  display name via the standard WOPI `UserFriendlyName` property so
  Collabora's presence UI shows who is editing.
- **Local dev posture:** yes — plain Ingress on a `.localhost` hostname on
  local k3d, mirroring the viewer gateway's dev posture; the Cloudflare
  Tunnel hostname is production/dev-cluster wiring only.

## Open Verification Items (first live gate)

- Collabora's standard WebSockets through cloudflared (the known headscale
  breakage is specific to its non-standard upgrade protocol).
- PostMessage reload UX cost (repaint + transient "user left" tooltip) is
  acceptable in practice; if not, explore suppressing the tooltip upstream.
