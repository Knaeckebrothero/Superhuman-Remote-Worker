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

**Status:** Design approved 2026-07-21; refined 2026-07-24 after a six-lane
research pass (three codebase inventories + three web lanes source-verified
against `CollaboraOnline/online.mirror` current `main`, the SDK 25.04 manual,
`nextcloud/richdocuments`, `cs3org/wopiserver`, the official
`collabora-online` Helm chart, and Microsoft's WOPI docs).
**Slices 1+2 IMPLEMENTED on develop 2026-07-25** (Slice 1:
`dc683398..30be0d18`; Slice 2: `33837e2b..f210dfc9`; unit/spec green —
152 Python office/canvas tests, 1381 Cockpit tests). **Live gate NOT RUN**
— the mandatory pre-rollout checklist is
`docs/tests/canvas_office_verification.md`; do not enable
`collabora.enabled` on any shared environment before it passes.

## Motivation

Observed failure (2026-07-20, dev session): the agent produced
`output/premium_marketplace_profile_setup_pack.docx` and called `set_canvas`
on it. The orchestrator's closed renderer set rejected it — a `.docx` is a
binary ZIP container, so it fails both the UTF-8 text decode and the
extension allowlist in `validate_canvas_bytes`
(`orchestrator/services/canvas_files.py:400-501`). The model recovered by
presenting a parallel `.md`, which proves the content pipeline works and only
the presentation surface is missing.

The goal is the full loop: *"work with the AI in Word, Excel, or PowerPoint"*
— the AI drafts a document, the user sees the real thing beside the chat,
edits it directly, and the AI iterates on the user's edits.

## Decisions

1. **SRW-native WOPI host + bundled Collabora CODE** (approach A). The
   orchestrator implements the WOPI storage contract over workspace files; a
   chart-bundled, opt-in Collabora CODE renders and edits them. The workspace
   file remains the **single source of truth**, exactly as
   [[dynamic_canvas]] requires ("both parties edit the same workspace file").
   The dedicated-WOPI-bridge shape is independently validated: cs3org's
   wopiserver and its full Go rewrite as the oCIS/OpenCloud `collaboration`
   service use the same stateless-gateway pattern.

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
   bridge is the WOPI conflict/timestamp mechanism plus the sanctioned
   `Host_VersionRestore` reload handshake after agent writes. The
   realtime-cursor idea is recorded under *Deferred*, not committed.

3. **Collabora CODE, not OnlyOffice.** LibreOffice technology, MPL-2.0 core,
   one engine for Word + Excel + PowerPoint, native ODF support,
   self-hostable single container, prior operational experience in the
   HomeLab. OnlyOffice's community edition caps connections and its
   automation API is commercial. Licensing nuance recorded honestly: the
   CODE *brand package* is not open source (a `nobrand` build arg produces a
   100% OSS build); CODE is positioned as the free development edition ("not
   intended for production", welcome banner, no SLA). The
   forum-lore "20 connections / 10 documents" cap applies **only** with
   `home_mode.enable=true`; server deployments default to compile-time
   9999/9999 (verified in `configure.ac`/`COOLWSD.cpp`; what Collabora's
   binary packages pass is unconfirmed — live-gate check below). For the
   current two-pilot stage, opt-in CODE is defensible; if document editing
   becomes a paid selling point, budget a Collabora partner conversation.

4. **Fail closed without the service.** If `collabora.enabled=false`, Office
   files remain unsupported with a clear tool error (new `CanvasFileError`
   code, mapped in `src/api/orchestrator_client.py:100-130` so the model gets
   a clean message). No degraded half-mode, no silent fallback renderer.

5. **Deploy via the official Helm chart as a subchart.** `collabora-online`
   (collaboraonline.github.io/online, also OCI at
   `oci://ghcr.io/collaboraonline/charts`; chart 1.3.x, appVersion 26.04.x)
   is production-grade: proof-key secret support, probe scheme derivation,
   seccomp installer, WOPISrc-affinity guidance. We gate it on
   `collabora.enabled` and add SRW-side glue (NetworkPolicy, admin secret,
   values passthrough) instead of hand-rolling Garage-style templates.
   **Trap:** the chart defaults `autoscaling.enabled: true` with min 2
   replicas — HPA reshuffles WOPISrc affinity and breaks co-editing. We pin
   `autoscaling.enabled: false`, `replicaCount: 1`.

6. **WOPI access tokens are session-length JWTs with per-call state
   re-checks — not short-TTL tokens.** Collabora integrations historically
   have *no* token refresh (Nextcloud's TTL default is 10 h; their refresh
   feature request has been open since 2022). Current CODE `main` does ship
   `App_TokenExpiring` → `Reset_Access_Token` renewal, but it is
   version-dependent. Design: mint HS256 JWTs following the existing
   `SessionTokenService` pattern (`orchestrator/services/session_tokens.py`,
   PyJWT, `SESSION_JWT_SECRET`), claims
   `(sub=user, tid=thread, path, write_flag, exp, jti)`, TTL ≈ the
   session-length bound (hours). **Revocation is not the expiry**: every
   WOPI call re-validates the token against live state — thread membership,
   canvas still pointing at that path, editability — mirroring the viewer
   sessions' `authenticate()` posture (`canvas_viewer_sessions.py:957-1085`).
   A leaked token is scoped to one file, one thread, one direction, and dies
   the moment the canvas moves. Implement `Reset_Access_Token` renewal where
   the deployed CODE supports it.

7. **Office view gets a positive capability key.** The first draft said "no
   new capability keys"; the codebase inventory reversed that. The app and
   browser renderers each gate their Cockpit mount on a positive capability
   (`can_create_viewer_session`, `can_stream_browser`); office view follows
   the precedent with `can_view_office` (set only when the deployment has
   Collabora enabled and healthy), plus the existing `can_edit` for Slice 2.

## Format Scope

| In v1 | Out of v1 |
|---|---|
| `.docx`, `.xlsx`, `.pptx` (OOXML) | Legacy binary `.doc`, `.xls`, `.ppt` |
| `.odt`, `.ods`, `.odp` (ODF — Collabora-native, near-free) | Anything Collabora merely imports (`.csv` stays on the text path, `.rtf`, …) |

Detection: extension allowlist **and** byte sniffing, as a **new binary
branch placed before the text branch** in `validate_canvas_bytes` (a `.docx`
currently dies at the UTF-8 decode, `canvas_files.py:438-444`, before any
renderer logic runs). libmagic reports OOXML as
`application/vnd.openxmlformats-officedocument.*` or plain
`application/zip`, and ODF as `application/vnd.oasis.opendocument.*`; accept
those combinations only when the extension agrees, mirroring the existing
`mime_renderer_mismatch` posture. New size bound `CANVAS_MAX_OFFICE_BYTES`
(default 25 MiB) with its own branch in `_workspace_read_limit`
(`canvas_files.py:305-316` — office extensions currently fall through to the
image ceiling by accident) and checked before the 2 MiB text cap can fire.

## Architecture

```
Cockpit canvas pane
  └─ office component: hidden form POST (access_token, access_token_ttl)
     → iframe https://office.<domain>/browser/<hash>/cool.html?WOPISrc=…
                 │  WOPI callbacks (cluster-internal)
                 ▼
       Orchestrator WOPI host
         CheckFileInfo / GetFile / PutFile
                 │  existing canvas transport + edit coordinator
                 ▼
       Workspace file  (single source of truth)
                 ▲
       Agent edits via python-docx / openpyxl / python-pptx (unchanged)
```

### Collabora deployment (chart)

- Official `collabora-online` subchart, gated on `collabora.enabled`
  (default **off**), `replicaCount: 1`, `autoscaling.enabled: false`.
  Image `collabora/code` 26.04.x line: distroless (no shell — no
  `kubectl exec` debugging), non-root `USER 1001`, port 9980,
  cosign-signed. Needs `SYS_CHROOT` (present in default K8s caps) via file
  capabilities; runs under the default securityContext. A fully-restricted
  (drop-ALL) posture exists via
  `--o:security.capabilities=false` + emptyDir working dirs, at the cost of
  slower jail setup — record as an operator option, not the default.
- Key config (all via `extra_params`):
  `--o:ssl.enable=false --o:ssl.termination=true` (TLS at the edge),
  `server_name=office.<domain>`,
  `aliasgroup1=<orchestrator in-cluster service URL>` (the WOPI-host
  allowlist — an exact `scheme://host:port` mismatch is the classic
  infinite-spinner cause),
  `--o:per_document.always_save_on_exit=true`,
  `--o:admin_console.enable=false` (PostMessage covers all our control
  needs; disabling kills an attack surface and the edge deny-list chore),
  `--o:net.content_security_policy=frame-ancestors <cockpit origin>;`
  (**`net.frame_ancestors` is obsolete in 26.04**; note a CSP-emission
  regression existed in 24.12.4.1, so the live gate must verify the header
  is actually emitted).
- Autosave defaults are fine: idle-save 30 s, periodic 300 s, doc unload
  after 3600 s idle. Graceful shutdown **saves and uploads all modified
  documents** on SIGTERM (bounded wait; chart sets
  `terminationGracePeriodSeconds: 60`), so the SIGKILL-only data-loss
  window is ~30 s of idle typing / ≤300 s of continuous typing.
- Sizing: start at the chart's test tier (≈1800m/2Gi requests=limits);
  budget ~50–100 MB per open document + ~1 GB base. coolwsd evicts idle
  docs at 80% of the cgroup memory limit, so always set a real limit.
- Proof keys: **not mounted, not validated in v1.** The docker image ships
  none by default; the in-cluster NetworkPolicy + token auth is the
  baseline (also the SDK-sanctioned alternative). Recorded as optional
  hardening with the known gotchas (TLS-termination URL mismatch breaks
  proof validation; under GitOps use a pre-created `proofKeysSecretRef`,
  never chart-side generation, which Argo/Flux would rotate).
- Public hostname `office.<domain>` rides the existing Cloudflare Tunnel to
  the ClusterIP service (`http://…:9980`). Cloudflared proxies standard
  WebSockets natively; known field failures trace to integration config,
  not WS transport — but a two-browser co-edit smoke test through the real
  tunnel stays a launch gate, not an assumption. **Keep the hostname out of
  Cloudflare Access** (Access challenges break WOPI callbacks and WS).
  Cloudflare's proxied-body cap (100 MB Free/Pro) is above our 25 MiB
  office bound and only affects browser→Collabora POSTs.
- SRW-side glue: NetworkPolicy pinning ingress to the tunnel/edge selectors
  and egress to the orchestrator WOPI port + DNS only (copy the
  canvas-gateway netpol pattern, `helm/templates/canvas-gateway/network-policy.yaml`);
  admin-console secret via the lookup-preserving pattern
  (`helm/templates/secret.yaml:52-81`) if the console is ever enabled;
  Cockpit learns the office origin via the `docker/cockpit-canvas-env.sh`
  env-injection pattern (`window['env']`, read through
  `environment.ts getEnvOrNull`).

### WOPI host (orchestrator)

Routes (token-authenticated; reachable in-cluster only via NetworkPolicy —
Collabora cannot send `X-Internal-Key`, so the access token IS the
authentication, exactly like the gateway's session cookie):

- `GET  /wopi/files/{file_id}` → CheckFileInfo
- `GET  /wopi/files/{file_id}/contents` → GetFile
- `POST /wopi/files/{file_id}/contents` + `X-WOPI-Override: PUT` → PutFile
  (Slice 2 only)

Contract specifics (source-verified):

- **file_id** = stable digest of `(thread_id, canonical_path)` (URL-safe
  hex). Deliberately path-based, unlike the id-based schemes the survey
  recommends: canvas doctrine treats the path as the presentation identity —
  a rename is a new source, and sessions ending on rename is correct here.
- **CheckFileInfo** (required: `BaseFileName` — basename only, COOL rejects
  slashes — `OwnerId`, `Size`, `UserId`): plus `UserFriendlyName` (presence
  UI; COOL error-logs without it), `LastModifiedTime` (ISO 8601 with
  sub-second precision — it is the conflict-detection anchor),
  **`PostMessageOrigin` = the exact Cockpit origin** (without it Collabora
  emits *no* postMessages and the whole Slice-2 mechanism silently dies),
  and `UserCanWrite` (omit/false in Slice 1 → read-only UI; true in
  Slice 2). `SupportsLocks` stays false — Collabora's protocol deliberately
  omits locks and serializes writers itself; `UserCanNotWriteRelative`
  stays true (no Save-As into the workspace in v1).
- **GetFile** reuses the bounded canvas transport via a new
  `materialize_binary` gateway method (the existing `materialize_current`
  hard-wires text validation): `_materialize`'s generation gates + office
  magic re-sniff instead of `validate_canvas_bytes`'s UTF-8 path, response
  under `acquire_canvas_response_lease`, ETag = `"sha256:…"`.
- **PutFile** (Slice 2) calls the **existing Slice-2 edit coordinator
  unchanged**: `CanvasService.edit_file` (`canvas.py:742`) with a writer
  callback into a new binary `replace_current_binary` (hash precondition →
  `412` path → the already-binary `_write` primitive with temp +
  `posix_rename` → read-back hash). Conflict contract: compare
  `X-COOL-WOPI-Timestamp` against the stored `LastModifiedTime`; on
  mismatch **do not save**, return `409` with body
  `{"COOLStatusCode": 1010}` → the editor shows its overwrite/reload
  dialog; a forced overwrite arrives with the header omitted. Success
  returns `200` + `{"LastModifiedTime": "<new mtime>"}` (COOL stores it for
  the next check). Honor `X-COOL-WOPI-IsAutosave` / `IsExitSave` /
  `IsModifiedByUser` for logging/metrics. The two-replica save-race
  guarantee is inherited from the shared advisory-lock coordinator
  (verified live in `docs/tests/dynamic_canvas_slice2_verification.md`).
  The Cockpit text-edit `PUT` route stays text-only; office bytes never
  flow through it.
- **Discovery**: the orchestrator fetches `/hosting/discovery` at startup
  and caches it (hours, stale-on-error, explicit admin refresh) — the
  `urlsrc` embeds a version-hashed browser path that changes on every
  Collabora upgrade, so hardcoding is a known bug class
  (richdocuments #1007/#2201). `/hosting/capabilities` gates feature
  detection.
- **Office session mint**: a new BFF-cookie-authed route (mirroring the
  view-attachment routes' auth posture, `canvases.py:278-300`) returns the
  form-post parameters to the Cockpit: resolved `urlsrc`, `WOPISrc`
  (the orchestrator's in-cluster files URL — not a secret), `access_token`,
  `access_token_ttl` (**absolute epoch milliseconds**, not a duration).

### Canvas renderer surface

New renderer value `office`. The current enum is
`auto | markdown | text | html | html-interactive | image` (the first draft
of this doc omitted `html-interactive`) and it is **duplicated across ≥11
sites with no shared constant** — the implementation plan must touch all of
them in lockstep:

- `src/tools/canvas/__init__.py` — the renderer `Literal` appears **5×**
  (four args schemas + the runtime signature); plus `editable`/`alt_text`
  descriptions (office needs no alt text).
- `orchestrator/services/canvas.py:51` — the canonical `CanvasRenderer`
  alias; `CanvasSetInput._source_specific_fields` editable legality
  (`:153-160`); `_validate_edit_record` (`:944-954`).
- `orchestrator/services/canvas_files.py` — `ValidatedCanvasFile.renderer`
  (`:250`) + a local annotation (`:415`); the office detection branch; the
  renderer `compatible` map (`:477-482` — **KeyErrors on unknown keys**;
  `office: {office}`, no text downgrade, same commit as detection);
  `supports_editing` (`:1122-1127`) and `validate_edit_candidate`
  (`:1158-1163`).
- `orchestrator/routers/canvases.py:1471-1474` — the fourth editable gate.
- Cockpit — `CanvasTrustedRenderer` + `selectCanvasRenderer`
  (`canvas-rendering.ts:8-16, 175-201`), `CanvasRenderer`
  (`canvas.model.ts:47-53`), **`CANVAS_RENDERERS` wire-validation Set
  (`canvas.service.ts:819-826`)**, the pane `@switch` + `hasVisual`
  (`canvas-pane.component.ts:290-320, 426-433`), content-controller fetch
  skip (`canvas-content.controller.ts:76` — office sources bytes from
  Collabora, never from `/content`), the edit-session `Exclude` type
  (`canvas-edit.controller.ts:28` — office excluded in Slice 1), i18n
  `canvas.renderer.office` in **both** `en.json` and `de-DE.json`.
- **No DB migration**: the `renderer` column is an unconstrained
  `VARCHAR(32)`; the cleared-state CHECK only pins `renderer='auto'` when
  the source is NULL.

**Version-skew correction (supersedes the first draft):** an old Cockpit
against a new orchestrator does **not** "degrade to unsupported" — the
`CANVAS_RENDERERS` Set in `isCanvasState` hard-rejects the whole canvas
state. Rollout order is therefore mandatory: **ship the Cockpit that accepts
`office` (and renders it or falls back) before any orchestrator that can
emit it.** `selectCanvasRenderer`'s `unsupported` fallback only applies
after the wire validator accepts the value.

Editable legality is enforced in **four independent places** (listed above);
Slice 1 leaves all four rejecting office (correct fail-closed), Slice 2
widens all four consistently, gated on Collabora availability.

### Cockpit office component

Mirrors the live-app iframe discipline
(`canvas-live-app-renderer.component.ts`): bind the `WindowProxy` before
assigning `src` (mount-generation + microtask), filter messages by
`event.source` AND `event.origin`, `referrerpolicy="no-referrer"`, sandbox
with `allow-scripts allow-same-origin allow-forms`. Collabora postMessage
parsing gets its own protocol module with the exact-keys fail-closed
discipline of `canvas-viewer-protocol.ts`.

Canonical handshake (verified against Collabora's own example):
`App_LoadingStatus {Status: Document_Loaded}` → host posts
`Host_PostmessageReady` → full API usable. `App_LoadingStatus.Features`
must contain `VersionStates` for the `Host_VersionRestore` flow (check it,
don't assume).

## Slices

### Slice 1 — View (closes the observed failure)

- Chart: `collabora-online` subchart wiring, NetworkPolicy, tunnel entry,
  Cockpit env injection.
- Orchestrator: office detection/validation branch; discovery
  fetch-and-cache; WOPI CheckFileInfo (no `UserCanWrite`) + GetFile;
  JWT minting + per-call state re-check; office-session mint route;
  `can_view_office` capability.
- Cockpit: office renderer component (form-post + iframe + handshake).
- Tools: `set_canvas` accepts office files (`renderer: auto → office`);
  `editable=true` rejected with a clear message at all four gates.
- Rollout order: Cockpit first (see version-skew correction).

### Slice 2 — Edit (turn-based collaboration)

- WOPI PutFile through the existing coordinator (seam above);
  `editable=true` legal for office on writable backends — the four gates
  widen together. Virtual-tier caveat: rclone-backed writes are
  read-modify-write (non-atomic), and `memory`-type virtual workspaces are
  not orchestrator-writable — office editing gates exactly like
  `supports_editing` does today.
- Turn-taking, per the doctrine:
  1. User edits in Collabora; autosave flows through `PutFile`, bumping
     `source_version` and firing the existing `canvas.source_updated`
     invalidation, so the agent's next `read_file` is fresh. When the user
     sends a chat message, the Cockpit first posts
     `Action_Save {DontSaveIfUnmodified: true, Notify: true}` and waits for
     `Action_Save_Resp` — killing the "AI ignored the number I just typed"
     class before the agent reads.
  2. Agent writes the file via Python, then calls `set_canvas` to
     republish.
  3. Cockpit sees the version bump and runs the **sanctioned reload
     handshake** (reference implementation:
     richdocuments `src/mixins/version.js`):
     `Host_VersionRestore {Status: "Pre_Restore"}` → wait for
     `App_VersionRestore {Status: "Pre_Restore_Ack"}` (Collabora flushes
     unsaved user edits) → the agent's bytes are already in place → editor
     reloads. Known cosmetic cost: a repaint and a brief "user left"
     tooltip. Guaranteed fallback: the `409`/`COOLStatusCode 1010` conflict
     dialog on the editor's next save. No admin-websocket dependency.
- Honest-concurrency stance carried over verbatim: this narrows stale-save
  windows, it does not make them impossible. Out-of-band writes (shell,
  other processes) remain invisible to the coordinator. UI and docs must
  not call it race-free.

### Deferred (recorded, not committed)

- **Realtime AI cursor** — puppet an editor client or a commercial
  automation API. Reuses all WOPI plumbing if ever built.
- **Browser as Canvas source** — the parked "agent's browser in the canvas"
  idea; belongs to [[shared_browser]] and the reserved `browser` source
  type in [[dynamic_canvas]].
- **Agent visual self-review via `convert-to`** — the AI edits `.pptx`
  (and `.docx`) blind to rendering; Collabora's stateless `convert-to`
  endpoint can produce page/slide PNGs so the agent inspects its own
  rendered output with vision before ending its turn. High-leverage for
  deck quality; needs the same token-gated, bounded posture as WOPI reads.
  (`net.post_allow` already restricts `convert-to` to in-cluster callers.)
- Legacy `.doc/.xls/.ppt`; PutRelativeFile ("Save As" into the workspace);
  WOPI proof-key validation (defense-in-depth; see deployment notes);
  watermarks / view-only hardening fields (`DisableExport`, `DisableCopy`,
  `WatermarkText` — all confirmed available); WOPI locks for interop with
  non-canvas write paths; multi-document canvases.

## Security Notes

- WOPI endpoints are cluster-internal (NetworkPolicy pins
  Collabora→orchestrator; Collabora needs no other egress than DNS) and
  token-authenticated per call with live state re-validation. A leaked
  token is scoped to one file, one thread, one direction, and dies when the
  canvas state moves. Tokens travel via the standard WOPI form-POST +
  `Authorization: Bearer` — never in a URL the Cockpit constructs. Nothing
  in the pipeline may rewrite the query string (breaks WOPI, and proof
  validation if ever enabled).
- Collabora renders untrusted agent-produced bytes in its jail system, in
  its own pod, with no workspace credentials, no DB access, no S3 access;
  its only path to bytes is the token-gated WOPI host. Admin console
  disabled. Macro execution stays default-off.
- The editor iframe is a foreign origin; Collabora emits
  `frame-ancestors <cockpit origin>` via `net.content_security_policy`
  (verify emission in the gate — a past regression shipped without it), and
  the Cockpit-side origin/source filtering matches the live-app viewer's.
  `PostMessageOrigin` pins outbound messages to the Cockpit origin. No
  wildcard CORS anywhere in the path.
- Byte caps and magic re-validation on every serve prevent the office path
  from becoming an oversized/undetected-content smuggling route.

## Testing / Verification

- **Unit:** token mint/validate/expiry/scope + state re-check; office
  magic+extension detection (incl. `application/zip` ambiguity and
  mismatch rejections); renderer compatibility gating; `editable` legality
  at all four gates per slice; CheckFileInfo field shape
  (`BaseFileName` basename rule, `PostMessageOrigin`, epoch-ms TTL);
  PutFile conflict contract (`409`+`1010`, forced-save header omission,
  `LastModifiedTime` echo).
- **Existing tests to extend** (they pin the enum today):
  `test_canvas_tool.py:135-159`, `test_canvas_slice1_backend.py:173-322`,
  `test_canvas_slice0.py`, `test_canvas_slice2_backend.py`;
  `canvas-rendering.spec.ts:42-84`, `canvas.service.spec.ts:55-75`,
  `canvas-pane.component.spec.ts`, `tool-descriptors.spec.ts`.
- **Integration (podman):** real `collabora/code` container against a
  stubbed workspace — discovery parse, CheckFileInfo/GetFile round-trip,
  view-only mount; Slice 2 adds PutFile save, timestamp-conflict dialog
  path, `Action_Save` flush, and the full `Pre_Restore` handshake. The
  pinned image version is gated by this suite.
- **Live k3d gate before any dev rollout:** tunnel WebSocket pass-through
  (two-browser co-edit of one `.xlsx` through the real tunnel);
  `frame-ancestors` header actually emitted; aliasgroup exact-match sanity;
  in-cluster reachability of the WOPI callback URL from inside the
  Collabora container; agent turn end-to-end (Python edit → `set_canvas` →
  `Pre_Restore_Ack` → reload); graceful-shutdown save on pod delete;
  >10 open documents (connection-cap smoke, per the packaging
  uncertainty); `collabora.enabled=false` failing closed with the
  documented error; `Reset_Access_Token` support check against the pinned
  CODE version.

## Resolved Questions

- **Editor refresh capability (2026-07-23, hardened 2026-07-24):** no
  admin-API dependency. The hosting frame drives save/reload via the
  PostMessage API; the full sanctioned flow is
  `Host_VersionRestore {Pre_Restore}` → `App_VersionRestore
  {Pre_Restore_Ack}` → reload (upstream discussion #5474 + the
  richdocuments reference implementation). Requires `PostMessageOrigin`
  in CheckFileInfo and the `Host_PostmessageReady` handshake.
- **Presence identity:** yes — `UserFriendlyName` (COOL error-logs without
  it) plus optional `UserExtraInfo.avatar` later.
- **Local dev posture:** plain Ingress on a `.localhost` hostname on local
  k3d, mirroring the viewer gateway's dev posture; the Cloudflare Tunnel
  hostname is production/dev-cluster wiring only.
- **Token lifetime (2026-07-24):** session-length JWT + per-call state
  re-check as the real revocation; `Reset_Access_Token` renewal where the
  deployed CODE supports it (decision 6).

## Open Verification Items (first live gate)

- Two-browser co-edit WebSockets through cloudflared (no positive
  authoritative guarantee found; all known field failures were config, not
  transport).
- PostMessage reload UX cost (repaint + transient "user left" tooltip)
  acceptable in practice.
- Whether Collabora's binary CODE packages override the 9999-connection
  compile-time default (the >10-docs smoke covers it).
- `App_TokenExpiring`/`Reset_Access_Token` availability in the pinned CODE
  release.
