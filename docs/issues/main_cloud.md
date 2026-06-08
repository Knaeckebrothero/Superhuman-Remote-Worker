---
tags:
  - cloud
  - main-cloud
  - nextcloud
  - opencloud
  - webdav
  - architecture
  - multi-tenancy
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

**Status:** Audit / issues log — *not* a design doc. Captured 2026-06-05; **resolutions tracked inline (per-issue Status lines + the Status column below); last updated 2026-06-08.**

**Scope:** the "main cloud" storage subsystem end-to-end — the backend abstraction (`orchestrator/services/cloud/`), the agent-side workspace sync (`src/services/cloud_sync/`), the agent file tools (`src/tools/webdav/`), config + hot-swap (`orchestrator/main.py`, `system_settings.main_cloud`), and the Helm wiring (`helm/`).

**How produced:** multi-agent code sweep + direct reads of the four code areas above and `helm/` (configmap + orchestrator deployment). Every claim is cited to `file:line`; line numbers are point-in-time (develop @ 2026-06-05) and may drift.

**The one-line summary:** the abstraction is **real and well-built for the control plane** (provisioning users / groups / folders / shares, using each cloud's *native* API — OpenCloud LibreGraph, Nextcloud OCS + Group Folders). It is **incomplete and leaky at the data plane** (every file byte rides WebDAV regardless of backend), the **hot-swap path has a secrets footgun**, and **several names collide**. This log enumerates what we found so we can decide what — if anything — to fix. Most items are deviations from, or explicitly-deferred parts of, [[main_cloud_abstraction]].

**Update (2026-06-08):** 8 of 16 are now resolved — the secrets footgun (5), silent-sync (13, incl. the orchestrator-side path), the byte-method gap (10), the router seam (16), and the name/hygiene set (7, 8, 12, 14). The data-plane leak (1), MS365/Google adapters (11), and cross-backend migration (6) remain **deferred by design** (they wait on a non-WebDAV customer / a real cutover); 9 waits on a prod-private Nextcloud connect; 2/3/4/15 are optional refactors. Nothing actionable or High-severity remains. See the Status column and per-issue ✅ lines.

---

## Summary

| # | Issue | Severity | Area | Status |
|---|-------|----------|------|--------|
| 1 | Abstraction is bypassed at the data plane — WebDAV for all file bytes | **High** | Architecture | ⏸ deferred (Phase 5) |
| 2 | Two parallel cloud abstractions that don't share an interface | Medium | Architecture | ◻ open (optional) |
| 3 | Keycloak token-exchange logic duplicated across both OpenCloud impls | Medium | Architecture | ◻ open (optional) |
| 4 | Folder/session provisioning duplicated across call sites | Low–Med | Architecture | ◻ open (optional) |
| 5 | Hot-swap can't introduce a backend whose secrets aren't pre-wired (+ silent fallback to dev defaults) | **High** | Config / hot-swap | ✅ 2026-06-07 |
| 6 | No data migration on backend switch/removal | Medium | Config / hot-swap | ⏸ deferred |
| 7 | Duplicate module filenames `opencloud.py` / `nextcloud.py` in two packages | Medium | Naming | ✅ 2026-06-07 |
| 8 | "datasource" overloaded across three different concepts | Medium | Naming | ✅ 2026-06-07 |
| 9 | Helm ↔ app config naming mismatch — `CLOUD_SERVICE_*` is NOT the Nextcloud agent cred (confirmed) | Med → **High** (#7) | Naming / config | ◻ open (prod-private) |
| 10 | Nextcloud byte-level project-folder methods are `NOT_SUPPORTED` stubs → Mode-A job export is OpenCloud-only | **High** | Implementation gap | ✅ 2026-06-07 |
| 11 | MS365 / Google are scaffold-only; the agent-side non-WebDAV refactor is unwritten | Medium | Implementation gap | ⏸ deferred (Phase 5) |
| 12 | `NextcloudBackend` ignores its own `NextcloudSettings` class | Low | Implementation gap | ✅ 2026-06-07 |
| 13 | Silent cloud-sync failures → unsynced session with no surfaced error | **High** | Reliability | ✅ 2026-06-07 |
| 14 | Stale, self-contradicting doc claim (service account "can read any home Space") | Low | Docs | ✅ 2026-06-07 |
| 15 | Dev/prod backend divergence — dev no longer validates the prod path | Medium | Process | ◻ open (optional) |
| 16 | Backend resolution bypasses the router seam (eager global `.active`) → blocks per-tenant resolution | **High** | Architecture | ✅ 2026-06-07 |

Status key: ✅ resolved (uncommitted/committed on `develop`) · ⏸ deferred by design (external trigger) · ◻ open but optional/conditional. Severity is a working judgement, not a commitment.

---

## Issue 1: The abstraction is bypassed at the data plane (WebDAV-everywhere)

**Severity:** High — it's the gap behind the whole "give each cloud its optimal API instead of WebDAV" goal.

**What:** The control plane is cleanly abstracted and *does* use native APIs — OpenCloud → LibreGraph (`/graph/v1.0`), Nextcloud → OCS + Group Folders. But **every actual file byte moves over WebDAV**, on every backend, and the `MainCloudBackend` Protocol doesn't *hide* WebDAV — it **exports** it as part of its public contract.

**Evidence:**
- Protocol members that publish transport: `get_project_folder_webdav_url()`, `get_session_folder_webdav_url()`, `webdav_credentials` (`orchestrator/services/cloud/base.py:55` ff). `get_*_webdav_url()` returns `Optional[str]` precisely so a non-WebDAV backend returns `None`.
- OpenCloud LibreGraph is used **only** for metadata/structure/sharing; file bytes go to `/dav/spaces/...` (`orchestrator/services/cloud/opencloud.py` — `get_project_folder_file_bytes` ~`:784`, `put_session_file` ~`:1193`). No `/drives/{id}/items/{id}/content` calls anywhere. No TUS anywhere.
- The byte-movers downstream are two *other* layers: the agent sync (`src/services/cloud_sync/`, WebDAV-only) and the raw agent tools (`src/tools/webdav/tools.py`, a bare `webdav3` client with no cloud abstraction at all).

**Impact:** A genuinely non-WebDAV cloud (MS Graph / Google Drive, which have no usable WebDAV) cannot work even though the control-plane abstraction is clean — the agent file tier falls over the moment `get_*_webdav_url()` returns `None`. The design names this the "load-bearing blocker" and defers it (Phase 5). See Issue 11.

**Direction:** Treat file I/O as a first-class part of the abstraction (a transport/driver seam the agent tier dispatches on `handle.backend`), instead of having the Protocol emit WebDAV URLs that three separate consumers each re-speak. Blocked on the unwritten `agent_cloud_tools.md`.

---

## Issue 2: Two parallel cloud abstractions that don't share an interface

**Severity:** Medium.

**What:** There are two cleanly-built but **separate** abstractions, plus one un-abstracted path:
1. `orchestrator/services/cloud/` — `MainCloudBackend` Protocol (control plane). `base.py:55`.
2. `src/services/cloud_sync/` — `WorkspaceSyncBase` ABC (agent push/pull mirror). `base.py:54`.
3. `src/tools/webdav/tools.py` — agent `webdav_read/write/list` tools, raw `webdav3`, no abstraction. (Renamed from `cloud_*` 2026-06-07 — see [[webdav_datasource_tools]].)

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
- "Write a file over WebDAV" is reimplemented in all three paths (MainCloud adapters' `put_*`, cloud_sync `_upload_file`, and the `webdav_write` tool).

**Impact:** Bug fixes (e.g. the MKCOL-before-PUT idiom, share rate-limiting) have to be made N times. `orchestrator/main.py` monolith makes this worse — see [[orchestrator_main_py_monolith]].

**Direction:** Collapse the provisioning sequences into one helper on the router; collapse the WebDAV write idiom into one place if/when the data-plane seam is built.

---

## Issue 5: Hot-swap can't introduce a backend whose secrets aren't pre-wired (silent fallback to dev defaults)

**Severity:** High — it's a quiet footgun that produces "connected but wrong" rather than a clean error.

**Status:** ✅ Resolved 2026-06-07 (uncommitted on `develop`). The loader keeps its dev-default fallbacks (so a bare `.env` / test run still works), but a new `missing_secret_envs()` presence-check (`orchestrator/services/cloud/config.py`) now backstops them at the surfaces that matter: the admin **PUT refuses** (400, naming the exact missing env var) to *activate* a backend whose required secrets would resolve to dev defaults; **`/test` reports** the precise missing var instead of probing with dev creds; and **startup logs a loud warning** when the active backend is running on built-in dev credentials. Live-verified on k3d (active backend = nextcloud, wired): swapping to opencloud — whose `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` is *not* injected because it isn't the enabled backend — is now refused by name, where it previously fell back to the dev default against a non-running opencloud; the wired nextcloud backend still passes (`ok:true`). 10 unit tests (`tests/cloud/test_secret_presence.py`). The loader is deliberately *not* made strict-by-default (tests + dev boot rely on the fallbacks); the GET endpoint already reported each secret's set/unset — this makes the system *act* on that, not just display it.

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

**Status:** ✅ Resolved 2026-06-07. `src/services/cloud_sync/{opencloud,nextcloud}.py` → `{opencloud,nextcloud}_sync.py` (+ the two `tests/cloud_sync/test_*` files), so grep / jump-to-file no longer collides with `orchestrator/services/cloud/{opencloud,nextcloud}.py`. 4 import sites updated; 172 cloud/cloud_sync tests green.

**What:** `opencloud.py` and `nextcloud.py` exist in **both** `orchestrator/services/cloud/` and `src/services/cloud_sync/`, implementing entirely different contracts (admin backend vs workspace-sync transport).

**Impact:** Grep/jump-to-file lands on the wrong one constantly; reviewers conflate them; the agent sweeps repeatedly had to disambiguate. Easy to "fix a bug" in the wrong file.

**Direction:** Rename for unambiguous import paths, e.g. `cloud_sync/opencloud_sync.py` / `cloud_sync/nextcloud_sync.py`, or fold per Issue 2.

---

## Issue 8: "datasource" is overloaded across three concepts

**Severity:** Medium (naming).

**Status:** ✅ Resolved 2026-06-07. The tool-naming facet was done earlier (`cloud_*`→`webdav_*`, see [[webdav_datasource_tools]]); now the Helm `cloud:` block comment in `values.yaml` explicitly distinguishes the three concepts — the main-cloud backend, runtime `datasources` rows, and the raw `externalBackend: "webdav"` mount — and corrects the `CLOUD_SERVICE_*` misattribution (a Keycloak bootstrap-Job cred, **not** the main-cloud backend cred).

**What:** "datasource" / "cloud" refers to three different things:
1. **Main-cloud backend config** — the Helm `cloud:` block / `MAIN_CLOUD_*` env / `system_settings.main_cloud`. Selects *which* cloud and how to admin it (the hot-swappable thing).
2. **Runtime `datasources` rows** — per-project / per-user WebDAV connection rows the orchestrator *creates at runtime* from the active backend. Not Helm-managed; the chart even says so: *"Per-user OAuth/linking is configured in-app as datasources, not by the chart"* (`helm/values.yaml:737-738`).
3. **`cloud.externalBackend: "webdav"` + `DEFAULT_DS_WEBDAV_*`** (`helm/templates/configmap.yaml:171-172`) — a raw single-mount WebDAV datasource with **no** `MainCloudBackend` at all.

**Impact:** "We have Helm values for the datasource" is ambiguous between (1) and (3); "swap it live" only applies to (1). Conversations and config reviews stall on which one is meant.

**Direction:** Reserve "main cloud" for (1), "datasource" for (2), and call (3) the "raw WebDAV mount." Align docs + Helm comments.

---

## Issue 9: Helm ↔ app config naming mismatch — `CLOUD_SERVICE_*` is NOT the Nextcloud agent cred (CONFIRMED)

**Severity:** Medium generally → **High for task #7** (it silently breaks an external Nextcloud connect).

**What:** Helm's external-cloud surface advertises `CLOUD_SERVICE_USER` / `CLOUD_SERVICE_PASSWORD` as the cloud service-account creds (`values.yaml:736-738`, `ci/customer-external-values.yaml:61`), but the Nextcloud backend loader **never reads them**. They are two unrelated things and nothing maps one to the other.

**Confirmed wiring (2026-06-05 end-to-end trace):**
- **What the Nextcloud loader actually reads** (`orchestrator/services/cloud/config.py`, nextcloud branch ~`:207-245`): `NEXTCLOUD_ADMIN_USER` / `NEXTCLOUD_ADMIN_PASSWORD` / `NEXTCLOUD_AGENT_USER` / `NEXTCLOUD_AGENT_PASSWORD` (or `MAIN_CLOUD_*` aliases). Passwords are injected from the **main app Secret** `srw.secretName` (`deployment.yaml:382-398`).
- **What `CLOUD_SERVICE_*` actually is:** a **Keycloak service-account** credential consumed by the Keycloak *bootstrap Job* to create/set-password a cloud user *in Keycloak* (`keycloak/bootstrap-job.yaml:145-155`, `bootstrap-configmap.yaml:225-262`), gated on `serviceAccounts.cloud.enabled`. It is also injected into the orchestrator pod (`deployment.yaml:476-486`) but `config.py` contains **zero** reads of `CLOUD_SERVICE_*`. It is an OIDC/OpenCloud-world artifact, irrelevant to Nextcloud basic-auth.
- **Secondary gap — usernames:** the external-cloud configmap branch (`configmap.yaml:185-200`) sets only `NEXTCLOUD_URL` + `NEXTCLOUD_PUBLIC_URL`; it does **not** set `NEXTCLOUD_ADMIN_USER` / `NEXTCLOUD_AGENT_USER` (those exist only under `nextcloud.enabled`, `:167-168`). So for an external Nextcloud the usernames fall to the loader defaults `admin` / `agent-service`, and there is **no values knob** to override them (they're configMap keys, so a Vault entry won't reach them) — a chart gap if the real users are named differently.
- **Plumbing note (good news):** the main app `ExternalSecret` uses `dataFrom: extract` over the whole Vault bundle (`external-secret.yaml:19-21`), so new secret keys flow through **without a chart change** — they just need to exist at `externalSecrets.vaultPath`.

**Impact:** An operator who follows the `values.yaml` comment and sets only `CLOUD_SERVICE_*` gets a Nextcloud backend on **dev-default creds** (`admin`/`admin`, `agent-service`/`agent-service-dev`, `config.py:228`/`:240`) → 401 against the real Nextcloud → swallowed silently (Issue 13). Empty workspace, no error — the exact original failure mode.

**Direction:** Fix the misleading `values.yaml:736-738` comment; either teach the loader to accept `CLOUD_SERVICE_*` as the Nextcloud agent cred, or stop injecting `CLOUD_SERVICE_*` for the Nextcloud backend. Expose `cloud.adminUser` / `cloud.agentUser` values for external backends. Wiring checklist in **Direction** below.

---

## Issue 10: Nextcloud byte-level project-folder methods are `NOT_SUPPORTED` stubs

**Severity:** High — Nextcloud is the backend prod-private is moving to.

**Status:** ✅ Resolved 2026-06-07 (uncommitted on `develop`). All four methods implemented on `NextcloudBackend` — WebDAV PROPFIND (Depth-1 BFS) / GET / PUT (MKCOL parents) / DELETE against the Group Folder via the groupfolders DAV endpoint, basic-auth as `agent-service`. The PROPFIND parser was extracted to a shared `orchestrator/services/cloud/_propfind.py` (both backends import it). Verified: 20 new unit tests (`tests/cloud/test_nextcloud.py` + `test_propfind.py`) + a live put → get → list → delete round-trip on k3d against group folder id=2 (binary-clean, nested path). Byte methods are byte-clean; the v1 *text-only* baseline limitation still lives in `job_cloud_baseline.py`, not these methods.

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

**Status:** ✅ Resolved 2026-06-07. `NextcloudBackend.__init__` now takes a validated `NextcloudSettings` (built by `load_main_cloud_config` in `build_backend`) and reads its fields — exactly like `OpenCloudBackend`. So the `MAIN_CLOUD_*` aliases, the DB overlay, and `credentials_ref` now apply to Nextcloud too (the old `os.getenv` constructor silently ignored them). 15 test call-sites pass a `_nc_test_settings()`; live-verified on k3d (`build_backend()` → backend holds `_settings`, pod healthy).

**What:** `OpenCloudBackend` consumes a validated `OpenCloudSettings` dataclass; `NextcloudBackend` instead reads `NEXTCLOUD_*` env vars directly in `__init__` and never uses `NextcloudSettings` (documented inconsistency at `orchestrator/services/cloud/__init__.py:69-73`; constructor env reads at `nextcloud.py:78-92`).

**Impact:** Two different config paths for two backends; the Pydantic validation (`extra="forbid"`, typed `SecretStr`) that protects OpenCloud doesn't protect Nextcloud. The admin GET has to special-case reading Nextcloud's private attrs vs OpenCloud's settings object (`main.py:17000-17022`).

**Direction:** Make `NextcloudBackend` accept and consume `NextcloudSettings` like OpenCloud does.

---

## Issue 13: Silent cloud-sync failures → unsynced session with no surfaced error

**Severity:** High — this is the original reported incident's mechanism.

**Status:** ✅ Mostly resolved 2026-06-07. The agent-side **initial-clone** path (the primary mechanism) already broadcasts `workspace_sync.error` (committed `a20e7e5c`) and the cockpit consumes it — but the handler showed the *turn-loop* copy ("your changes … will retry on next turn"), which is **false** for an initial-pull failure: sync is *disabled* for the session, the workspace may be *missing* cloud files, and the backend may be Nextcloud not "OpenCloud". Fixed the cockpit handler (`cockpit/.../persistent-chat.service.ts`) to branch on `degraded===true` / `op==='initial_pull'`: a **sticky** (no auto-dismiss) danger toast + a session-long `cloudSyncDegraded` signal with accurate copy, and dropped the hardcoded "OpenCloud". 3 cockpit specs added; the agent broadcast was already live-proven. **Follow-up ✅ done 2026-06-07:** the orchestrator-side path is surfaced too — `agent_get_thread_workspace` now derives a `cloud_sync_degraded` flag (cloud up + no sync target: no `cloud_sync` cfg and no `nc_session_folder`), and the agent's attach path broadcasts the same `workspace_sync.error {degraded:true}` (op `provision`) when it sees it, reusing the cockpit sticky-toast handler — no new transport needed. Live-verified on k3d: the workspace endpoint returns `cloud_sync_degraded: true` for a no-target session.

**What:** Several cloud failures are caught, logged at WARNING, and execution continues as if nothing happened:
- Session-attach initial clone: `src/api/persistent_app.py` ~`:1135-1156` catches any exception, logs *"Failed to start cloud workspace sync"*, sets `workspace_sync=None`, and the session runs **unsynced for its whole life** with no further retry.
- Orchestrator session-folder provisioning: `orchestrator/main.py` ~`:11949-11954` swallows to a warning.
- Default-project home-URL resolution, JIT `ensure_user`, project Space creation: all swallow to warnings.

**Impact:** This is exactly how "files didn't clone, but I saw no error" happened on prod-private (the token-exchange 400 was swallowed here). The user-visible symptom is an empty workspace and no signal.

**Direction:** Surface a non-fatal-but-visible state to the UI (a "cloud sync degraded" banner / `workspace_sync.error` broadcast already exists for the turn-loop path at `persistent_app.py:2592` — extend it to the initial-clone path). Don't let a failed initial pull silently disable sync for the session.

---

## Issue 14: Stale, self-contradicting doc claim

**Severity:** Low (docs hygiene).

**Status:** ✅ Resolved 2026-06-07. Corrected `cloud_collaboration_model.md:388`: the "service accounts can read/write any home Space" claim holds for Nextcloud (admin basic-auth) but is **false** for OpenCloud (single-owner Personal Spaces — the Phase 2.1 token-exchange exists precisely because of this).

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

## Issue 16: Backend resolution bypasses the router seam (eager global `.active`)

**Severity:** High — it's the actual foundation the rest of the cloud code sits on, *and* a present-day correctness hazard.

**Status:** ✅ Resolved 2026-06-07 (uncommitted on `develop`). All resource resolution now routes through the router seam (`for_project` / `for_thread` / new `for_owner`); direct `.active` reads were removed from call sites — the load-bearing one being the resume re-provision (`main.py` `_late_cloud_setup` → `for_thread`), which previously served a pinned thread on `.active` after a swap. `for_owner(owner=None)` returns the global active today and is reserved for per-org keying. `.active` is now documented as the admin-config surface only. Verified by `tests/cloud/test_router_reload.py` (incl. a dispatch-to-demoted case) + a live in-pod per-row dispatch probe on k3d.

**What:** Backend resolution is inconsistent. The orchestrator builds one global router at boot — `main_cloud_router = MainCloudRouter(build_backend())` (`orchestrator/main.py:238`) — and most call sites correctly resolve *per resource* through it: `for_project(row)` (`main.py:10957`), `for_thread(row)` (`:12339`), `for_backend(id)` (`:11042`, `:12095`, `:12112`, `:12217`, `:12240`). But several reach straight for the global `main_cloud_router.active`: Mode-B job export (`:9029`), thread/session setup (`:11904`), session re-provision (`:12415`). The router already keeps a *keyed* instance cache (`_legacy: dict[backend_id → backend]`), so the seam exists — it's just used inconsistently.

**Impact:**
- **Present-day correctness hazard.** Under the non-destructive switching the hot-swap enables, a project/thread pinned to backend A (via its `main_cloud_backend` column) can be served by `.active` = B after an admin swaps the active backend → operations land on the wrong cloud. The `.active` paths silently assume "the global backend is the right one for this resource," which is exactly what switching breaks.
- **Foundation risk for multi-tenancy.** Per-org cloud (the planned SaaS direction) requires resolving the backend from request/job context (owner → org → backend). Every `.active` shortcut bakes in "one global cloud" at a call site that would then need refactoring when org-keying arrives. This — *not* config storage (env vs DB) and *not* the hot-swap admin UI — is the load-bearing seam: the ~20 consumers depend on *how they get a backend* (the router), never on *where config is stored*.

**Direction:** Route **all** resolution through one context-keyed method (`for_project` / `for_thread`, plus a `for_owner(ctx)` / `for_org(org_id)` that returns the single global today); delete every direct `.active` read from call sites. Keep backends **stateful** (Keycloak service-token cache + httpx pools — re-minting per call would hammer Keycloak) behind the keyed instance cache, and generalize the cache key from `backend_id` toward owner/org. Add the owner/org parameter to the seam **now** (defaulted/ignored), so multi-tenancy later is "fill in the key," not a call-site refactor. The agent side is already per-session (handed cloud coordinates per job), so this change is contained to the orchestrator. See **Direction** below.

---

## Direction

*Agreed approach as of 2026-06-05 — a decision record, not an implementation spec. Reconciles this log with the planned (but not-yet-sold) multi-tenant SaaS direction and the current feature-freeze / runway posture. A fuller plan gets written when any of this is scheduled.*

**The principle.** The foundation the cloud code sits on is **backend resolution**, not config storage and not the hot-swap UI. Multi-tenancy = resolve the backend *per-org at every operation*. The current hot-swap (one global backend, mutated at runtime) is single-tenant and is **not** a step toward that — per-org needs per-org *rows* and per-org *resolution*, which the single global model does not pre-build. So: protect and generalize the resolution seam (Issue 16); treat config storage (env vs DB) as a leaf that can change later *behind* that seam; demote the hot-swap UI to a non-foundational convenience.

**Decided approach, by area:**

1. **Resolution seam (Issue 16) — the one foundational change.** Funnel all resolution through a context-keyed router method, kill `.active`, keep backends stateful behind a keyed instance cache, and add the owner/org parameter to the seam now. Cheap, regret-free, makes multi-tenancy "fill in the key" instead of a 20-site refactor — and fixes the present-day swap-correctness hazard along the way.

2. **Config storage (Issues 5, 6).** Env/Helm is the canonical config path and stays so permanently for self-hosted / open-core installs (one deployment = one org). Fix the fail-loud footgun (Issue 5) regardless. Demote the live-swap UI — keep it only as a fail-loud, non-foundational convenience, or drop it. Per-org DB config — DB-authoritative, *seeded from env* like the LLM provider rows — is **deferred** until multi-tenant SaaS is actually sold, and is built keyed by org *then* (a single global DB row today buys nothing toward it).

3. **Data plane / native APIs (Issues 1, 10, 11).** Deferred — Phase 5 of [[main_cloud_abstraction]], gated on the unwritten `agent_cloud_tools.md`. Optional cheaper interim *if* there's appetite: consolidate the agent file tier behind one WebDAV seam (still WebDAV) so nothing bypasses it — addresses Issues 2/7/8 without the native-API build. Implement Nextcloud's stubbed byte methods (Issue 10) only before relying on Mode-A job export on Nextcloud.

   **Update (2026-06-07):** evaluated this cheaper interim and chose **not** to build the seam. The live consumer (the `cloud_sync` mirror) is already cleanly abstracted, and the agent's WebDAV tools are a *datasource* concern, not main-cloud — a unifying seam would couple two deliberately-separate responsibilities. Instead: renamed the tools `cloud_*`→`webdav_*` (Issue 8 naming) and removed the **project-folder double-exposure** — the project working folder is cloned into the workspace (Mode-A / `projects/` mount), so it's no longer *also* auto-attached as a `webdav` datasource. The `webdav_*` tools are now scoped to clouds that are NOT cloned (personal home + BYO WebDAV). Decision record: [[webdav_datasource_tools]].

4. **Naming + docs hygiene (Issues 7, 8, 9, 12, 14).** Independent and safe anytime; pick up opportunistically.

**Sequencing — now vs later:**

- **Worth touching under the freeze (cheap, regret-free, nothing to rip out later):** fail-loud config (Issue 5) ✅ done 2026-06-07; the resolution-seam cleanup / kill `.active` (Issue 16) ✅ done; surface silent cloud-sync failures (Issue 13) ✅ done 2026-06-07 (incl. the orchestrator-side path); naming + docs hygiene (Issues 7, 8, 12, 14 + the dead-code delete) ✅ done 2026-06-07.
- **When multi-tenant SaaS is actually being sold:** per-org config table (seed-from-env), generalize the resolution key to org, per-tenant admin surface.
- **When a customer forces non-WebDAV (MS365 / Google), or by deliberate choice:** write `agent_cloud_tools.md`, build the data-plane driver seam (Phase 5).

**Why this order:** the cheapest items de-risk an *active* footgun (Issue 5) and protect the *foundation* (Issue 16); everything expensive or strategic is deferred behind real demand, consistent with ship-over-build. Crucially, **nothing here forces a foundation refactor later** — env config survives as the self-hosted default, and the resolution seam is made tenant-ready *in place* rather than rebuilt.

**Relative to current work (tasks):**
- **Connecting Nextcloud on prod-private (task #7)** is blocked by Issues **5** + **9** (the `CLOUD_SERVICE_*` vs `NEXTCLOUD_AGENT_*` question is now resolved — see checklist below). Issue **10** (Mode-A job export on Nextcloud) is now ✅ resolved (2026-06-07), so that's no longer a limiter.
- **Reverting the legacy Keycloak token-exchange flag (task #8)** is unrelated to these issues and stays safe once Nextcloud is verified (prod no longer uses OpenCloud impersonation).

### External Nextcloud wiring — task #7 checklist (verified 2026-06-05)

Traced Helm → ESO → env → loader. Because the main app `ExternalSecret` uses `dataFrom: extract` over the whole Vault bundle (`external-secret.yaml:19-21`), new keys flow through **without a chart change** — they just need to exist at `externalSecrets.vaultPath` under the names the loader reads.

1. **Helm values:** `cloud.externalBackend: "nextcloud"`; `cloud.externalUrl: "https://<public>"` (→ `NEXTCLOUD_PUBLIC_URL`, required); `cloud.externalServiceUrl: "https://<server-to-server>"` (→ `NEXTCLOUD_URL`; defaults to externalUrl); `nextcloud.enabled: false`; `opencloud.enabled: false`.
2. **Vault bundle** (at `externalSecrets.vaultPath`) — add keys **`NEXTCLOUD_ADMIN_PASSWORD`** and **`NEXTCLOUD_AGENT_PASSWORD`** (real values). **Not** `CLOUD_SERVICE_*` — the loader ignores those (Issue 9).
3. **Usernames:** ensure the real Nextcloud has users named exactly **`admin`** and **`agent-service`** (loader defaults; the agent-service user in the `srw-agents` group with share perms per [[main_cloud_abstraction]]). Different names need a chart change today (Issue 9 username gap).
4. **Verify present, not defaulted:** `GET /api/admin/system-settings/main_cloud` reports each secret env var's set/unset + length (`main.py:17026-17033`) — confirm both passwords show non-zero length (else you're silently on dev defaults).
5. **Smoke-test the real mechanism:** the `agent-service` account can read a real user's home over WebDAV / the OCS share path resolves. Watch orchestrator logs for the silent `"Failed to start cloud workspace sync"` warning (Issue 13).
6. **Remember Issue 10:** Mode-A job cloud baseline (seed-from-cloud / accept-back) is `NOT_SUPPORTED` on Nextcloud — gate or implement before relying on it.
