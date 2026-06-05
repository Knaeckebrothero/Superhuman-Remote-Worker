---
tags:
  - cloud
  - main-cloud
  - nextcloud
  - opencloud
  - webdav
  - architecture
  - tech-debt
aliases:
  - Main Cloud Issues
  - Main Cloud Abstraction Issues
related:
  - "[[main_cloud_abstraction]]"
  - "[[cloud_collaboration_model]]"
  - "[[job_cloud_export]]"
  - "[[opencloud_share_bugs]]"
  - "[[nextcloud_oidc_username]]"
  - "[[session_folder_placement]]"
  - "[[orchestrator_main_py_monolith]]"
---

# Main Cloud — Known Issues

**Status:** Audit / issues log — *not* a design doc. Captured 2026-06-05.

**Scope:** the "main cloud" storage subsystem end-to-end — the backend abstraction (`orchestrator/services/cloud/`), the agent-side workspace sync (`src/services/cloud_sync/`), the agent file tools (`src/tools/cloud/`), config + hot-swap (`orchestrator/main.py`, `system_settings.main_cloud`), and the Helm wiring (`helm/`).

**How produced:** multi-agent code sweep + direct reads of the four code areas above and `helm/` (configmap + orchestrator deployment). Every claim is cited to `file:line`; line numbers are point-in-time (develop @ 2026-06-05) and may drift.

**The one-line summary:** the abstraction is **real and well-built for the control plane** (provisioning users / groups / folders / shares, using each cloud's *native* API — OpenCloud LibreGraph, Nextcloud OCS + Group Folders). It is **incomplete and leaky at the data plane** (every file byte rides WebDAV regardless of backend), the **hot-swap path has a secrets footgun**, and **several names collide**. This log enumerates what we found so we can decide what — if anything — to fix. Most items are deviations from, or explicitly-deferred parts of, [[main_cloud_abstraction]].

---

## Summary

| # | Issue | Severity | Area |
|---|-------|----------|------|
| 1 | Abstraction is bypassed at the data plane — WebDAV for all file bytes | **High** | Architecture |
| 2 | Two parallel cloud abstractions that don't share an interface | Medium | Architecture |
| 3 | Keycloak token-exchange logic duplicated across both OpenCloud impls | Medium | Architecture |
| 4 | Folder/session provisioning duplicated across call sites | Low–Med | Architecture |
| 5 | Hot-swap can't introduce a backend whose secrets aren't pre-wired (+ silent fallback to dev defaults) | **High** | Config / hot-swap |
| 6 | No data migration on backend switch/removal | Medium | Config / hot-swap |
| 7 | Duplicate module filenames `opencloud.py` / `nextcloud.py` in two packages | Medium | Naming |
| 8 | "datasource" overloaded across three different concepts | Medium | Naming |
| 9 | Helm ↔ app config naming mismatch (`cloud.externalBackend` vs `MAIN_CLOUD_*`, `CLOUD_SERVICE_*` vs `NEXTCLOUD_AGENT_*`) | Medium | Naming / config |
| 10 | Nextcloud byte-level project-folder methods are `NOT_SUPPORTED` stubs → Mode-A job export is OpenCloud-only | **High** | Implementation gap |
| 11 | MS365 / Google are scaffold-only; the agent-side non-WebDAV refactor is unwritten | Medium | Implementation gap |
| 12 | `NextcloudBackend` ignores its own `NextcloudSettings` class | Low | Implementation gap |
| 13 | Silent cloud-sync failures → unsynced session with no surfaced error | **High** | Reliability |
| 14 | Stale, self-contradicting doc claim (service account "can read any home Space") | Low | Docs |
| 15 | Dev/prod backend divergence — dev no longer validates the prod path | Medium | Process |

Severity is a working judgement, not a commitment.

---

## Issue 1: The abstraction is bypassed at the data plane (WebDAV-everywhere)

**Severity:** High — it's the gap behind the whole "give each cloud its optimal API instead of WebDAV" goal.

**What:** The control plane is cleanly abstracted and *does* use native APIs — OpenCloud → LibreGraph (`/graph/v1.0`), Nextcloud → OCS + Group Folders. But **every actual file byte moves over WebDAV**, on every backend, and the `MainCloudBackend` Protocol doesn't *hide* WebDAV — it **exports** it as part of its public contract.

**Evidence:**
- Protocol members that publish transport: `get_project_folder_webdav_url()`, `get_session_folder_webdav_url()`, `webdav_credentials` (`orchestrator/services/cloud/base.py:55` ff). `get_*_webdav_url()` returns `Optional[str]` precisely so a non-WebDAV backend returns `None`.
- OpenCloud LibreGraph is used **only** for metadata/structure/sharing; file bytes go to `/dav/spaces/...` (`orchestrator/services/cloud/opencloud.py` — `get_project_folder_file_bytes` ~`:784`, `put_session_file` ~`:1193`). No `/drives/{id}/items/{id}/content` calls anywhere. No TUS anywhere.
- The byte-movers downstream are two *other* layers: the agent sync (`src/services/cloud_sync/`, WebDAV-only) and the raw agent tools (`src/tools/cloud/webdav.py`, a bare `webdav3` client with no cloud abstraction at all).

**Impact:** A genuinely non-WebDAV cloud (MS Graph / Google Drive, which have no usable WebDAV) cannot work even though the control-plane abstraction is clean — the agent file tier falls over the moment `get_*_webdav_url()` returns `None`. The design names this the "load-bearing blocker" and defers it (Phase 5). See Issue 11.

**Direction:** Treat file I/O as a first-class part of the abstraction (a transport/driver seam the agent tier dispatches on `handle.backend`), instead of having the Protocol emit WebDAV URLs that three separate consumers each re-speak. Blocked on the unwritten `agent_cloud_tools.md`.

---

## Issue 2: Two parallel cloud abstractions that don't share an interface

**Severity:** Medium.

**What:** There are two cleanly-built but **separate** abstractions, plus one un-abstracted path:
1. `orchestrator/services/cloud/` — `MainCloudBackend` Protocol (control plane). `base.py:55`.
2. `src/services/cloud_sync/` — `WorkspaceSyncBase` ABC (agent push/pull mirror). `base.py:54`.
3. `src/tools/cloud/webdav.py` — agent `cloud_read/write/list` tools, raw `webdav3`, no abstraction.

They don't import each other (only `TYPE_CHECKING` references + "mirrors …" docstrings). The user's mental model was *one* class used everywhere; reality is two-and-a-half.

**Impact:** Adding/changing a backend means touching two interfaces with different shapes (the orchestrator's ~30 domain methods vs the agent's 5 transport primitives) and a raw tool path. Knowledge has to be kept in sync by hand.

**Direction:** Accept the split deliberately (control plane vs data plane are legitimately different responsibilities) but document the boundary, or unify the data-plane half (cloud_sync + the agent tools) behind one driver. Don't pretend it's one layer.

---

## Issue 3: Keycloak token-exchange logic duplicated across both OpenCloud impls

**Severity:** Medium.

**What:** The OpenCloud Keycloak `client_credentials` fetch and the RFC 8693 `requested_subject` impersonation exchange are implemented **twice**, independently:
- agent side: `src/services/cloud_sync/opencloud.py:131-181` (`_fetch_service_token`, `_exchange_for_user_token`).
- orchestrator side: `orchestrator/services/cloud/opencloud.py` (`_get_service_token` ~`:1266-1296`).

**Impact:** The prod-private token-exchange incident (shared Keycloak rejecting `requested_subject`) lives in the *agent* copy; any fix has to be applied in both places. Drift risk. See [[srw-prod-private-cloud-sync-token-exchange]].

**Direction:** Extract a shared token-minting helper, or have the orchestrator mint and hand down tokens (it already hands down the WebDAV URL + auth block).

---

## Issue 4: Folder/session provisioning duplicated across call sites

**Severity:** Low–Medium.

**What:** The same provisioning sequences appear in multiple places:
- Project Space + group + datasource: runtime heal path (`orchestrator/main.py` ~`:17565-17609`) **and** startup backfill (`orchestrator/init.py` ~`:1082-1120`).
- Session-folder provisioning: 3 call sites (`main.py` ~`:9028` Mode-B export, ~`:11924` thread create, ~`:12413` re-provision), differing only in `ensure_user` vs `resolve_user_identity`.
- "Write a file over WebDAV" is reimplemented in all three paths (MainCloud adapters' `put_*`, cloud_sync `_upload_file`, and the `cloud_write` tool).

**Impact:** Bug fixes (e.g. the MKCOL-before-PUT idiom, share rate-limiting) have to be made N times. `orchestrator/main.py` monolith makes this worse — see [[orchestrator_main_py_monolith]].

**Direction:** Collapse the provisioning sequences into one helper on the router; collapse the WebDAV write idiom into one place if/when the data-plane seam is built.

---

## Issue 5: Hot-swap can't introduce a backend whose secrets aren't pre-wired (silent fallback to dev defaults)

**Severity:** High — it's a quiet footgun that produces "connected but wrong" rather than a clean error.

**What:** Main-cloud config is two layers: **env vars from Helm = permanent baseline**, **`system_settings.main_cloud` DB row = optional live override** (written only by the cockpit admin UI). The loader merges per field `DB overlay > env > default` (`orchestrator/services/cloud/config.py:124-205`), and **secrets are never stored in the DB** — the overlay holds only a `credentials_ref="env:NAME"` pointer; the real secret is read from that env var (`config.py:178-193`, sanitizer at `main.py:16964-16984`).

Two problems compound:
1. The orchestrator deployment only injects a backend's secret env (`NEXTCLOUD_ADMIN_PASSWORD`, `NEXTCLOUD_AGENT_PASSWORD`, `OPENCLOUD_KEYCLOAK_CLIENT_SECRET`) when that backend is `*.enabled` **or** `cloud.externalBackend == <that backend>` (`helm/templates/orchestrator/deployment.yaml:359`, `:439`). So a pod Helm'd for OpenCloud has **no** `NEXTCLOUD_*` secrets present.
2. If you then live-swap to Nextcloud from the UI, the loader doesn't hard-fail on the missing secret — it falls back to **dev defaults**: `admin_password` → `"admin"`, `agent_password` → `"agent-service-dev"` (`config.py:228`, `:240`; OpenCloud's client secret → `"opencloud-orchestrator-local-secret"` `:276`).

**Impact:** The UI implies you can switch backends freely, but you can only meaningfully switch to a backend whose secrets are *already* in the pod env. Switching to an un-wired backend connects with wrong/default credentials and fails at *runtime* (first WebDAV/Graph call), not at swap time — exactly the kind of silent breakage this whole investigation started from.

**Direction:** Make the loader **fail loudly** when a `credentials_ref` points at an unset env var (no silent dev-default fallback in non-dev). Surface "secret env not present for backend X" in the PUT/`/test` response. Long-term: decide whether the UI should be allowed to select a backend whose secrets aren't wired at all. (The admin GET already reports each secret env var's set/unset + length — `main.py:17026-17033` — but nothing blocks the swap.)

**Boot/swap flow for reference:** module load builds the active backend from env only (`main.py:238`); startup applies the DB overlay if present (`main.py:3363-3380`); `PUT` validates → upsert → local `reload_from_db` → `fire_reload` pg_notify to other replicas (`main.py:17102-17187`); `DELETE` = "reset to Helm defaults" → `reload_from_db(None)` (`main.py:17272-17293`). Helm is **not** a one-time seed — env is always the floor, the DB row is a patch on top.

---

## Issue 6: No data migration on backend switch/removal

**Severity:** Medium.

**What:** Switching backends is non-destructive (the router caches the old backend so existing project/thread rows — each carries its own `main_cloud_backend` column — keep routing). But there is **no** cross-backend data migration, and if the old cloud service is removed, calls into old-backend rows fail with `CloudBackendError(UNAVAILABLE)` (per [[main_cloud_abstraction]]).

**Impact:** "Swappable" means "new resources use the new backend," not "your data moves." A real OpenCloud→Nextcloud cutover strands existing project/session folders on the now-removed OpenCloud.

**Direction:** Either a documented manual migration runbook or the sketched per-project migration CLI before any cutover that removes the old service.

---

## Issue 7: Duplicate module filenames in two packages

**Severity:** Medium (naming / discoverability).

**What:** `opencloud.py` and `nextcloud.py` exist in **both** `orchestrator/services/cloud/` and `src/services/cloud_sync/`, implementing entirely different contracts (admin backend vs workspace-sync transport).

**Impact:** Grep/jump-to-file lands on the wrong one constantly; reviewers conflate them; the agent sweeps repeatedly had to disambiguate. Easy to "fix a bug" in the wrong file.

**Direction:** Rename for unambiguous import paths, e.g. `cloud_sync/opencloud_sync.py` / `cloud_sync/nextcloud_sync.py`, or fold per Issue 2.

---

## Issue 8: "datasource" is overloaded across three concepts

**Severity:** Medium (naming).

**What:** "datasource" / "cloud" refers to three different things:
1. **Main-cloud backend config** — the Helm `cloud:` block / `MAIN_CLOUD_*` env / `system_settings.main_cloud`. Selects *which* cloud and how to admin it (the hot-swappable thing).
2. **Runtime `datasources` rows** — per-project / per-user WebDAV connection rows the orchestrator *creates at runtime* from the active backend. Not Helm-managed; the chart even says so: *"Per-user OAuth/linking is configured in-app as datasources, not by the chart"* (`helm/values.yaml:737-738`).
3. **`cloud.externalBackend: "webdav"` + `DEFAULT_DS_WEBDAV_*`** (`helm/templates/configmap.yaml:171-172`) — a raw single-mount WebDAV datasource with **no** `MainCloudBackend` at all.

**Impact:** "We have Helm values for the datasource" is ambiguous between (1) and (3); "swap it live" only applies to (1). Conversations and config reviews stall on which one is meant.

**Direction:** Reserve "main cloud" for (1), "datasource" for (2), and call (3) the "raw WebDAV mount." Align docs + Helm comments.

---

## Issue 9: Helm ↔ app config naming mismatch

**Severity:** Medium.

**What:** Helm's surface (`cloud.externalBackend`, `cloud.externalUrl`, `cloud.externalServiceUrl`, `CLOUD_SERVICE_USER/PASSWORD`) does **not** match what the app actually reads (`MAIN_CLOUD_BACKEND`, `MAIN_CLOUD_URL`, `NEXTCLOUD_*` / `OPENCLOUD_*`). A translation layer in `helm/templates/configmap.yaml:176-196` maps one to the other.

**Evidence:**
- `cloud.externalBackend` → `MAIN_CLOUD_BACKEND`, and for nextcloud sets `NEXTCLOUD_URL`/`NEXTCLOUD_PUBLIC_URL` (`configmap.yaml:185-196`).
- But the **loader** reads agent creds from `NEXTCLOUD_AGENT_USER`/`NEXTCLOUD_AGENT_PASSWORD` (`config.py:229-240`), while the Helm/ESO secret plumbing talks about `CLOUD_SERVICE_USER`/`CLOUD_SERVICE_PASSWORD` (`helm/templates/keycloak/bootstrap-externalsecret.yaml:70-77`, deployment `:476-486`). **Verify these line up** for an external Nextcloud — if `CLOUD_SERVICE_*` is not also surfaced as `NEXTCLOUD_AGENT_*`, the agent-service creds won't be read. (Open question flagged for task #7.)

**Impact:** An operator setting `cloud.externalBackend: nextcloud` has to know the hidden mapping to predict which env vars (and which secret keys) actually take effect. Mis-wiring is silent (see Issue 5).

**Direction:** Either rename the Helm surface to mirror the app env (`mainCloud.backend`, `mainCloud.url`, …) or document the mapping table explicitly in `values.yaml`. Resolve the `CLOUD_SERVICE_*` vs `NEXTCLOUD_AGENT_*` question.

---

## Issue 10: Nextcloud byte-level project-folder methods are `NOT_SUPPORTED` stubs

**Severity:** High — Nextcloud is the backend prod-private is moving to.

**What:** Four `MainCloudBackend` methods raise `CloudBackendError(NOT_SUPPORTED, "… not implemented on the Nextcloud backend yet")`: `list_project_folder`, `get_project_folder_file_bytes`, `put_project_folder_file_bytes`, `delete_project_folder_file` (`orchestrator/services/cloud/nextcloud.py:498-560`). OpenCloud implements all four.

**Impact:** The **Mode-A job cloud baseline** (seed a job from the project cloud folder, write accepted edits back — `orchestrator/services/job_cloud_baseline.py`) is **OpenCloud-only**. On Nextcloud it raises. So moving prod-private to Nextcloud silently drops a job feature that works on dev (OpenCloud).

**Direction:** Implement the four methods on `NextcloudBackend` (WebDAV PROPFIND/GET/PUT/DELETE against the Group Folder) before relying on Mode-A in prod, or gate Mode-A off when the active backend is Nextcloud with a clear message.

---

## Issue 11: MS365 / Google are scaffold-only; the agent-side non-WebDAV refactor is unwritten

**Severity:** Medium (strategic / deferred).

**What:** `MS365Settings` (config), a `"ms365"` enum value + loader branch, a commented-out `REGISTRY` line, and a contract-test concession all exist — but there is **no** `MS365Backend` / `OneDriveBackend` / `GoogleDriveBackend` class, and no Graph/Drive request code. The blocking feature doc `docs/features/agent_cloud_tools.md` (the agent-side non-WebDAV driver) is "not yet written"; Phase 5 is deferred (`docs/features/main_cloud_abstraction.md:29`, `:1186`, `:1233`).

**Impact:** The "later add OneDrive / Google Drive" story is a half-built rail. Anyone reading the config/enum may assume more exists than does. Ties directly to Issue 1 — without the data-plane seam, these backends can't transfer files.

**Direction:** Leave deferred, but make the scaffold's status legible (a comment in `config.py` pointing here), so it isn't mistaken for a working path.

---

## Issue 12: `NextcloudBackend` ignores its own `NextcloudSettings` class

**Severity:** Low.

**What:** `OpenCloudBackend` consumes a validated `OpenCloudSettings` dataclass; `NextcloudBackend` instead reads `NEXTCLOUD_*` env vars directly in `__init__` and never uses `NextcloudSettings` (documented inconsistency at `orchestrator/services/cloud/__init__.py:69-73`; constructor env reads at `nextcloud.py:78-92`).

**Impact:** Two different config paths for two backends; the Pydantic validation (`extra="forbid"`, typed `SecretStr`) that protects OpenCloud doesn't protect Nextcloud. The admin GET has to special-case reading Nextcloud's private attrs vs OpenCloud's settings object (`main.py:17000-17022`).

**Direction:** Make `NextcloudBackend` accept and consume `NextcloudSettings` like OpenCloud does.

---

## Issue 13: Silent cloud-sync failures → unsynced session with no surfaced error

**Severity:** High — this is the original reported incident's mechanism.

**What:** Several cloud failures are caught, logged at WARNING, and execution continues as if nothing happened:
- Session-attach initial clone: `src/api/persistent_app.py` ~`:1135-1156` catches any exception, logs *"Failed to start cloud workspace sync"*, sets `workspace_sync=None`, and the session runs **unsynced for its whole life** with no further retry.
- Orchestrator session-folder provisioning: `orchestrator/main.py` ~`:11949-11954` swallows to a warning.
- Default-project home-URL resolution, JIT `ensure_user`, project Space creation: all swallow to warnings.

**Impact:** This is exactly how "files didn't clone, but I saw no error" happened on prod-private (the token-exchange 400 was swallowed here). The user-visible symptom is an empty workspace and no signal.

**Direction:** Surface a non-fatal-but-visible state to the UI (a "cloud sync degraded" banner / `workspace_sync.error` broadcast already exists for the turn-loop path at `persistent_app.py:2592` — extend it to the initial-clone path). Don't let a failed initial pull silently disable sync for the session.

---

## Issue 14: Stale, self-contradicting doc claim

**Severity:** Low (docs hygiene).

**What:** `docs/done/cloud_collaboration_model.md:388` still asserts service accounts "can read/write any user's home Space without per-user share grants" — directly contradicted by the same doc at `:320` ("OpenCloud Personal Spaces are single-owner … my earlier claim … was wrong"), which is why Phase 2.1 token-exchange exists at all.

**Impact:** A reader trusting the locked-decisions section builds on a false premise (the exact premise that broke prod-private).

**Direction:** Correct or strike `:388`.

---

## Issue 15: Dev/prod backend divergence

**Severity:** Medium (process).

**What:** Dev runs OpenCloud (Keycloak token-exchange auth); prod-private is moving to Nextcloud (basic-auth). The `MainCloudBackend` abstraction makes this cheap at the provisioning layer, but **dev no longer exercises the backend prod runs**.

**Impact:** A Nextcloud-path regression (e.g. Issue 10's stubs, or the basic-auth path) won't surface on dev — you'd hit it in prod, silently (cf. Issue 13). It re-introduces, at the backend layer, the same dev≠prod gap that produced the original incident at the Keycloak-config layer.

**Direction:** Decide the canonical backend and align dev to what prod runs, **or** keep the split deliberately with a thin Nextcloud smoke/regression test so the prod path isn't wholly untested. See [[srw-prod-private-cloud-sync-token-exchange]].

---

## Triage notes (relative to current work)

- **Blocks connecting Nextcloud on prod-private (task #7):** Issues **5** and **9** (wire secrets via Helm/ESO first, and don't trust a silent success), Issue **9**'s `CLOUD_SERVICE_*` vs `NEXTCLOUD_AGENT_*` open question, and Issue **10** (Mode-A job export won't work on Nextcloud yet).
- **Independent / safe to do anytime:** Issues **7**, **8**, **12**, **14** (naming + docs hygiene).
- **Strategic, not urgent:** Issues **1**, **2**, **11** (the data-plane seam / non-WebDAV story) — gated on `agent_cloud_tools.md` and the lean/feature-freeze posture.
- **Reverting the legacy Keycloak token-exchange flag (task #8)** is unrelated to these issues and stays safe once Nextcloud is verified (prod no longer uses OpenCloud impersonation).
