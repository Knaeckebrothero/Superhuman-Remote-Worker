---
tags:
  - test
  - verification
  - canvas
related:
  - "[[canvas_office_documents]]"
  - "[[dynamic_canvas]]"
---

# Canvas Office Documents — Live Verification Runbook

**Status: NOT RUN.** Slices 1+2 are implemented on develop and unit/spec
green (152 Python office/canvas tests, 1381 Cockpit tests as of 2026-07-25),
but no live environment gate has executed — the implementation machine could
not run k3d. **This runbook must pass on a live cluster before any dev
rollout with `collabora.enabled=true`.** Design:
`docs/features/canvas_office_documents.md`.

## Environments

| Phase | Environment | Covers |
|---|---|---|
| A | Local k3d (`k3d srw` + tilt) with `collabora.enabled=true`, `.localhost` Ingress | Everything except tunnel |
| B | Dev cluster (`office.srw.works` via Cloudflare Tunnel) | Tunnel-specific items |

### Enablement inputs (per environment)

No new Vault leaves. Preconditions and values:

- **Secret precondition:** the `sessionRouter` JWT secret
  (`SESSION_JWT_SECRET`) must already exist in the env — WOPI tokens sign
  with it. Verify the Vault/ESO leaf or values entry BEFORE enabling
  (absent secret = the canvas-gateway `CreateContainerConfigError` class).
- **Values:** `collabora.enabled=true`, `collabora.publicUrl`,
  `collabora.collabora.server_name` (= publicUrl host),
  `collabora.collabora.aliasgroups[0].host` (= in-cluster orchestrator
  URL — the chart's validation error/NOTES prints the exact string),
  `extra_params` with the Cockpit origin appended after `frame-ancestors`
  (trailing `;`), `collabora.networkPolicy.enabled` + edge selectors.
- **Outside the chart:** cloudflared ingress rule `office.<domain>` →
  collabora service `:9980`; DNS via tunnel; hostname NOT behind
  Cloudflare Access. Local k3d uses `collabora.ingress.enabled` with a
  `.localhost` host instead.
- No admin-console secret (console disabled) and no proof keys in v1.

Prep notes (from research, unverified live):
- The `collabora/code` 26.04 image is **distroless** — no `kubectl exec`
  shell. Debug via `kubectl logs` and coolwsd's websocket log level only.
- Pin the image tag; record which tag ran this gate.
- Local k3d gotchas that have bitten before: CoreDNS upstream dead (image
  pulls), SSH key `0444` check is root-image-only.

## Phase A — local k3d

### A1. Deployment health
- [ ] Subchart renders with `collabora.enabled=true`; verify rendered
      manifests pin `autoscaling: false`, `replicaCount: 1`.
- [ ] Pod Ready on the `/` probe; RAM at rest recorded (expect ≈0.5–1 GiB).
- [ ] `collabora.enabled=false` renders NO collabora objects, and
      `set_canvas` on a `.docx` returns the documented fail-closed error to
      the model (not a stack trace).

### A2. WOPI plumbing
- [ ] Orchestrator fetched `/hosting/discovery` at startup; log line shows
      cache populated; `urlsrc` host matches the office origin.
- [ ] From inside the Collabora pod's network scope: WOPI callback URL
      (orchestrator in-cluster service) is reachable — check coolwsd logs
      for CheckFileInfo 200 on first open. An aliasgroup mismatch presents
      as an infinite spinner: verify `aliasgroup1` equals the orchestrator
      URL exactly (scheme://host:port).
- [ ] `frame-ancestors` present on the editor response and contains exactly
      the Cockpit origin (a past CODE release shipped a CSP-emission
      regression — check with browser devtools, do not assume).

### A3. Slice 1 — view
- [ ] Agent writes a `.docx`, calls `set_canvas` → renderer resolves
      `office`, canvas pane shows the rendered document read-only.
- [ ] Repeat for `.xlsx` and `.pptx`; one ODF file (`.odt`).
- [ ] `editable=true` on `set_canvas` is rejected in *view-only mode*
      (e.g. read-only backend) with a clear message.
- [ ] Old-Cockpit skew spot-check: confirm deployed Cockpit build already
      contains `office` in `CANVAS_RENDERERS` (view page source / bundle)
      before orchestrator rollout in shared envs.

### A4. Slice 2 — editing
- [ ] `editable=true` → editor opens writable (`UserCanWrite`), user types,
      autosave lands: orchestrator log shows PutFile → 200 with
      `LastModifiedTime` echo; `source_version` bumped; agent
      `read_file` sees the change without manual cache clearing.
- [ ] Two browsers, same `.xlsx`: both cursors visible (human↔human
      realtime works), both sets of edits persist.
- [ ] **Conflict contract:** modify the file out-of-band (shell write),
      then save in the editor → PutFile 409 + `COOLStatusCode 1010` →
      editor shows overwrite/reload dialog. Verify BOTH branches:
      "overwrite" (forced save, timestamp header omitted, write succeeds)
      and "reload" (editor shows the out-of-band content).
- [ ] **Agent turn:** with the editor open and unsaved user edits, agent
      edits via python-docx → `set_canvas` → `Pre_Restore` handshake:
      user's pending edits saved first, editor reloads showing agent
      changes. Record the UX cost (repaint/"user left" tooltip) — accept
      or escalate.
- [ ] **Chat-send flush:** type in a cell, immediately send a chat message
      asking about that value → agent's answer reflects the just-typed
      value (Action_Save flush ran before the read).
- [ ] Read-only token cannot PutFile (403/401 in logs, no write).

### A5. Lifecycle
- [ ] `kubectl delete pod` on Collabora with unsaved edits → graceful
      shutdown saves (PutFile with `IsExitSave: true` in logs) before
      termination; reopening shows the edits.
- [ ] Token renewal: check pinned CODE version emits `App_TokenExpiring`
      (shorten token TTL to force it) and `Reset_Access_Token` swaps
      credentials without an editor reload. If unsupported in the pinned
      version, record it and confirm the fallback (conflict dialog after
      expiry) is survivable.
- [ ] Connection-cap smoke: open >10 documents / >20 connections
      (script tabs) — confirm no `home_mode` cap is active in the shipped
      package.

## Phase B — dev cluster (tunnel)

- [ ] `office.srw.works` reaches the ClusterIP service; hostname is NOT
      behind Cloudflare Access.
- [ ] Two-browser co-edit of one `.xlsx` **through the tunnel** for
      ≥15 min: WebSocket stays up (idle-timeout behavior recorded),
      edits flow both ways. This is the one item with no authoritative
      upstream guarantee — it is the launch gate.
- [ ] Editor asset load + image-insert POST through the tunnel (body-size
      cap sanity, expect fine ≤25 MiB).
- [ ] Full A3 + A4 agent-turn pass once, end-to-end, on dev.

## Results

| Date | Env | Image tag | Result | Notes |
|---|---|---|---|---|
| — | — | — | NOT RUN | — |
