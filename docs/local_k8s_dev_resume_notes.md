# Local K8s Dev — Resume Notes (2026-05-21, updated 2026-05-28)

> **2026-05-28 update:** Path B (same-origin chart restructure) landed. The
> 3rd-party-cookie refresh loop is fixed. Both `/api` and `/auth` now route
> through the cockpit host (`https://localhost/`), so the session cookie is
> first-party and works in Brave/Firefox without browser configuration.
> Patches in this section: new `auth.bff.sameOriginApi` value flag (default
> `false`); cockpit ingress conditionally adds `/api` and `/auth` path rules
> to the orchestrator service; `SRW_BFF_REDIRECT_URI` and env.js `apiUrl`
> both use the cockpit origin when the flag is on; Keycloak postStart hook
> now always whitelists both api.* and cockpit-host callback URLs (additive,
> safe for prod). `deployment/values-local{,.example}.yaml` sets
> `sameOriginApi: true` and reverts `cookieSamesite` back to `"lax"` (the
> `"none"` workaround is no longer required).
>
> Remaining open items: the orchestrator code change to `bff.py` (the
> `_cookie_samesite()` helper + `SRW_COOKIE_SAMESITE` env wiring in
> `deployment.yaml`) is still in the local tree only — needs a commit so CI
> rebuilds `:latest`. Until then the local image-import workaround is still
> needed on a fresh cluster. Same goes for all the chart patches landed in
> the original session.

---

# Original notes (2026-05-21)


Snapshot of where we got to setting up a production-parity local Kubernetes
dev environment on k3d. **Almost there** — the chart deploys, certs validate,
login starts succeeding at Keycloak, but the cockpit lands in a 401-refresh
loop because the browser isn't sending the BFF session cookie on cross-site
XHR from `localhost` → `api.localhost`.

This document captures: what works, what doesn't, the most likely root cause,
the two paths forward, and every file touched so far (chart patches,
orchestrator code, scripts, docs).

---

## State of the world

### Working

- k3d cluster (`srw`) running on Docker Desktop's daemon, IPv4/v6 dual-stack
  on host ports 80/443, local registry on `localhost:5000`.
- All 13 chart pods `1/1 Running` (postgres, pgvector, mongodb, neo4j,
  keycloakdb, keycloak, gitea, opencloud, orchestrator, mcp, cockpit,
  reloader, image-prewarm).
- cert-manager v1.16.2 + `mkcert-issuer` ClusterIssuer wrapping the user's
  mkcert root CA. All 6 Certificates `READY=True`.
- All HTTPS endpoints return their expected status from curl with the mkcert
  CA: cockpit 200, api 401 (no auth), keycloak 302, gitea 200, cloud 200,
  mcp 404 (no `/`).
- `*.localhost` resolves via glibc's `myhostname` NSS (no /etc/hosts setup).
- mkcert CA installed in both user trust store and system trust store
  (via `sudo CAROOT=…` workaround for the "two CAs" pitfall).
- `test`/`test` user pre-seeded in `docker/keycloak/realm-export.json`
  with `admin` + `user` roles. Password set on every Keycloak pod start by
  the postStart hook from `KC_REALM_ADMIN_PASSWORD` in
  `deployment/values-local.yaml` (set to `"test"`).
- OIDC flow up to `/auth/callback` works end-to-end — orchestrator logs
  show `BFF session ... opened for sub=...` (the user authenticates and a
  session row is created in the orchestrator DB).

### Not working

- After successful login, the cockpit makes XHR to
  `https://api.localhost/api/auth/me` and gets `401`. The orchestrator's
  log loop is:
  ```
  GET /auth/callback 302       ← session created server-side
  GET /api/auth/me 401         ← cookie didn't ride along
  GET /auth/login 302          ← cockpit redirects to login
  POST .../token 200           ← OIDC dance again
  GET /auth/callback 302       ← new session
  GET /api/auth/me 401         ← still no cookie
  ...                          ← refresh-loop
  ```
- User reported the browser also surfaced a `404` somewhere in the loop,
  but the orchestrator log shows only 401s — the 404 is probably a static
  asset that fails because of the rapid-fire reloads, not the root cause.

### Most likely root cause

**Browser third-party cookie blocking.** The cockpit is served from
`https://localhost/`; the API from `https://api.localhost/`. Chrome and
especially Brave Nightly treat these as **cross-site** (no shared
registrable domain — `localhost` has no eTLD+1). Even after we set
`SameSite=None; Secure` on both the session and pre-auth cookies (verified
on the wire — see "Patches landed" below), Brave can still refuse to send
cross-site cookies entirely as part of its 3rd-party cookie phase-out.

The fix that was applied earlier (`SameSite=None; Secure`) is necessary but
not sufficient on browsers that block 3rd-party cookies regardless. We
need to either (a) tell the browser to allow `localhost` cookies, or
(b) collapse cockpit + API onto the **same origin** so they're no longer
"third party" to each other.

### Diagnostic step before resuming

Open the cockpit in the browser, click Login, get into the refresh loop,
then check DevTools:

1. **Application → Cookies → `https://api.localhost`** — is `srw_session`
   actually present after `/auth/callback`?
2. **Network → `/api/auth/me` request → Request Headers** — is there a
   `Cookie:` header? Does it include `srw_session`?

| Cookie stored? | Cookie sent on XHR? | Diagnosis |
|----------------|---------------------|-----------|
| Yes            | No                  | 3rd-party cookie blocking — pick a path below |
| No             | n/a                 | Cookie set failed for a different reason (less likely; the curl trace shows Set-Cookie present) |
| Yes            | Yes                 | Cookie travels — orchestrator is rejecting it. Check `srw_sessions` table in postgres for the session row and its `expires_at` |

---

## Two paths forward (pick one and resume)

### Path A — Quick (per-dev, no code change)

Allow `localhost` cookies in the browser:

- **Brave**: click Shields on `https://localhost/` → toggle off "Block
  third-party cookies", OR `brave://settings/cookies` → add `[*.]localhost`
  under "Sites that can always use cookies".
- **Chrome**: `chrome://settings/cookies` → "Sites that can always use
  cookies" → add `[*.]localhost`.
- **Firefox**: Enhanced Tracking Protection "Custom" → allow 3rd-party
  cookies for trusted sites, or just use a Standard profile (Strict mode
  blocks them).

Reload. The session cookie should now ride on XHR, the loop should stop,
and the cockpit should load logged in.

**Trade-off**: each dev has to do this once. Not turnkey for the README,
but the cluster + chart are unchanged.

### Path B — Proper (chart restructure, no browser config)

Make cockpit and API live on the **same origin** locally — both reachable
under `https://localhost/`, with path-based routing:

| Path                  | Backend                |
|-----------------------|------------------------|
| `https://localhost/`        | `srw-cockpit:4000` (SPA)  |
| `https://localhost/api/*`   | `srw-orchestrator:8085`   |
| `https://localhost/auth/*`  | `srw-orchestrator:8085`   |
| `https://auth.localhost/`   | `srw-keycloak:8080` (stays separate — OIDC redirect is top-level nav, not XHR, so cookie sharing not needed) |

This makes the cockpit's XHR same-origin → cookie is first-party → 3rd-party
blocking doesn't apply.

**Required changes**:

1. `helm/templates/ingress.yaml` — modify the **cockpit ingress** to add
   `/api` and `/auth` path rules pointing at `srw-orchestrator:8085`. Order
   them before the `/` catch-all (Traefik picks longest-prefix match by
   default but explicit ordering avoids surprises). Keep the `api.localhost`
   ingress too — it's harmless and other consumers (gitea OIDC backchannel,
   mcp clients) may resolve to it.
2. `helm/templates/cockpit/deployment.yaml` — change `env.js` apiUrl from
   `{{ include "srw.apiUrl" . }}/api` to `{{ include "srw.cockpitUrl" . }}/api`
   (or just `/api`, fully relative) when a new value flag
   `auth.bff.sameOriginApi: true` is set. Default `false` so production
   keeps cross-subdomain.
3. `deployment/values-local.yaml` / `.example.yaml` — set
   `auth.bff.sameOriginApi: true`. Can also unwind the `cookieSamesite:
   "none"` override (same-origin → Lax works); leave `cookieDomain: ""` as
   host-only.
4. **Keycloak realm export** — the `cockpit-bff` client's redirect URIs need
   `https://localhost/auth/callback` (currently they have
   `https://api.localhost/auth/callback`). Either add via realm-export.json
   or via the postStart kcadm hook in `helm/templates/services/keycloak.yaml`.
   `SRW_BFF_REDIRECT_URI` in `helm/templates/configmap.yaml` line 63 also
   needs to point at the cockpit origin when sameOriginApi is on.

**Trade-off**: more chart work, but turnkey for every dev. Closer to the
prod cookie story (same-site).

---

## Patches landed during this session

### Chart patches (in working tree, NOT committed — required by the new flow)

| File | What changed | Why |
|------|--------------|-----|
| `helm/templates/workspace-pvc.yaml` | `accessModes` → `{{ .Values.workspace.accessMode \| default "ReadWriteMany" }}` | k3s local-path is RWO-only; chart was hardcoded RWX |
| `helm/values.yaml` (workspace block) | Added `workspace.accessMode: "ReadWriteMany"` default | Pair with the template change above |
| `helm/templates/ingress.yaml` | Wrapped `router.entrypoints: websecure` + `router.tls: "true"` annotations in `{{- if .Values.ingress.tls.enabled }}` … `{{- else }} router.entrypoints: web` (all 10 occurrences) | With TLS off the chart was still requesting `websecure`-only routing → 404 on http |
| `helm/templates/_helpers.tpl` | New `srw.urlScheme` helper (`"https"` or `"http"`); existing URL helpers (`cockpitUrl`, `apiUrl`, `authUrl`, `gitUrl`, `cloudUrl`, `mcpUrl`, `headscaleUrl`) use it instead of hardcoded `printf "https://%s"` | env.js was serving https even when tls.enabled=false |
| `helm/templates/cockpit/deployment.yaml` | Admin URLs in env.js use `{{ include "srw.urlScheme" . }}://…` instead of hardcoded `https://` | Same as above for dozzle/minio/neo4j/pgadmin/mongoExpress |
| `helm/templates/configmap.yaml` | `SRW_COOKIE_DOMAIN` uses `"auto"` sentinel for derived `.<global.domain>`; explicit `""` → host-only literal. New `SRW_COOKIE_SAMESITE` key reading `auth.bff.cookieSamesite` (default `"lax"`) | The old `\| default` filter silently replaced empty string with `.localhost` |
| `helm/values.yaml` (auth.bff block) | `cookieDomain: "auto"` (was `""`); new `cookieSamesite: "lax"` | Same as above + cross-site cookie config |
| `helm/templates/orchestrator/deployment.yaml` | Added `SRW_COOKIE_SAMESITE` env var injection from ConfigMap | Was missing — env var present in ConfigMap but never reached the pod |

### Orchestrator code patch (in working tree, image built+imported locally)

| File | What changed |
|------|--------------|
| `orchestrator/auth/bff.py` | New `_cookie_samesite()` helper reads `SRW_COOKIE_SAMESITE` env (default `"lax"`); both `_set_session_cookie` and `_set_pre_auth_cookie` use it instead of hardcoded `samesite="lax"` |

Image was built locally with:
```bash
docker build -t ghcr.io/knaeckebrothero/superhuman-remote-worker-orchestrator:latest \
  -f docker/Dockerfile.orchestrator .
k3d image import ghcr.io/knaeckebrothero/superhuman-remote-worker-orchestrator:latest -c srw
kubectl --context=k3d-srw -n srw rollout restart deploy/srw-orchestrator
```

For other devs / fresh installs: once we commit the bff.py change, CI will
rebuild GHCR `:latest` and a normal `helm install` will pick it up. Until
then, the local image import above is required.

### New files (committed-or-to-be-committed)

| File | Purpose |
|------|---------|
| `scripts/local-dev-up.sh` | Idempotent bootstrap: creates k3d cluster, installs cert-manager, mkcert ClusterIssuer, srw namespace, dummy `srw-vm-ssh-key` Secret |
| `deployment/values-local.example.yaml` | Committed template — devs `cp` to `values-local.yaml`, paste LLM keys, run helm install |
| `deployment/values-local.yaml` | **Gitignored** — actual credentials |
| README "Local Kubernetes Setup (k3d)" section | Full prereqs → bootstrap → install → login → daily lifecycle → troubleshooting |
| CLAUDE.md "Local Kubernetes (k3d + Helm chart)" subsection | Quick-reference commands for the harness |
| CLAUDE.md Deployment section | Updated — removed stale `deployment-local/` Kustomize reference; now points at `helm/` + `deployment/values-local.example.yaml` |
| `.gitignore` | Added `deployment/values-local.yaml` |

### Untracked workarounds in the cluster (not in git)

- `srw-vm-ssh-key` Secret — dummy ed25519 keypair generated by
  `scripts/local-dev-up.sh`. Orchestrator mounts it unconditionally; it
  doesn't matter what's inside locally because we don't run KubeVirt VMs.
- Local orchestrator image: `ghcr.io/.../orchestrator:latest` rebuilt
  with the `SRW_COOKIE_SAMESITE` fix and imported into k3d's containerd.
  Lost on `k3d cluster delete srw` — re-import after recreation, or commit
  the bff.py change and rebuild from CI.

---

## Pitfalls already discovered (worth keeping in the README troubleshooting)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Browser shows "Not secure" on `https://localhost/` after `sudo mkcert -install` | `sudo` ran as root, created a SECOND CA at `/root/.local/share/mkcert/`, installed THAT into trust store. Cluster certs were signed by the user-level CA. | `sudo mkcert -uninstall` then `sudo CAROOT="$HOME/.local/share/mkcert" mkcert -install`. Restart browser. |
| `localhost` hijacks the prod cockpit domain when k3d is running | LAN's DNS returns `::1` for the prod domain's AAAA record; k3d binds `[::]:443` dual-stack and "wins" happy-eyeballs over the legitimate Cloudflare IPv4 path. | `k3d cluster stop srw` when not using local cluster. Or recreate with IPv4-only port binding: `--port "0.0.0.0:443:443@loadbalancer"`. |
| Docker Desktop GUI "stop container" doesn't free port 443 | k3d has multiple containers (`server-0`, `serverlb`, `registry`). The GUI stops only one at a time; `serverlb` is the one holding the port. | Use `k3d cluster stop srw` — it knows about all containers with label `k3d.cluster=srw`. |
| `helm install` succeeds but pods stuck on `Pending` with `srw-workspace` PVC `ProvisioningFailed` | Chart hardcoded `accessModes: [ReadWriteMany]`; k3s local-path is RWO-only. | **Already patched** (see Chart patches above) — `workspace.accessMode: "ReadWriteOnce"` in `values-local.yaml`. |
| Keycloak in `CreateContainerConfigError` for missing env var (e.g., `PGADMIN_OIDC_CLIENT_SECRET`, `SMTP_USER`) | The realm import does env-var substitution for every referenced var in the JSON, regardless of which services are enabled. | Include stubs for all in `secrets.values` (already done in `values-local.example.yaml`). |
| Pre-existing PVC blocks `helm upgrade` with "field can not be less than previous value" | K8s rejects PVC shrinks. The chart's default for opencloud was 16Gi, but an earlier install had 64Gi. | Pin in `values-local.yaml` to the existing size (`opencloud.dataStorageSize: "64Gi"`), or delete the PVC and reinstall. |
| Login: `{"detail":"Missing pre-auth state"}` | Pre-auth cookie set with `Domain=.localhost` rejected by browser; only Domain attribute that works is none (host-only). | **Already patched** — `cookieDomain: ""` in `values-local.yaml` + chart now honors empty string as host-only (vs. previous bug where empty fell back to `.<global.domain>`). |
| Login: refresh loop with 401s after successful Keycloak callback | Session cookie is `SameSite=Lax` (server) AND third-party cookie blocking (browser). | `cookieSamesite: "none"` in `values-local.yaml` covers the server side. Browser side still needs Path B (same-origin restructure) or per-dev cookie allowlist (Path A). **Current open issue.** |

---

## Where to pick up

1. Have the user check DevTools (Application → Cookies, Network → request
   headers) to confirm 3rd-party cookie blocking is the root cause, not
   something else.
2. If confirmed: decide between Path A (per-dev allowlist) or Path B (chart
   same-origin restructure). For the README to be turnkey, Path B is the
   answer. For "I just need to test the agent today," Path A.
3. If we go Path B, implement the four changes listed under "Required
   changes" above. Plan to:
   - Add `auth.bff.sameOriginApi: bool` value (default false).
   - Conditionally route `/api` + `/auth` paths through the cockpit ingress
     when the flag is true.
   - Conditionally rewrite `apiUrl` in env.js to same-origin.
   - Conditionally rewrite `SRW_BFF_REDIRECT_URI` in the ConfigMap to point
     at the cockpit origin.
   - Add `https://<cockpit-host>/auth/callback` to the `cockpit-bff` client's
     redirect URIs (postStart kcadm hook in `keycloak.yaml`).
4. After fixing: commit all chart patches and the bff.py change so CI
   rebuilds `:latest`. Then the README install path works without the
   local image rebuild step.
5. Update the README troubleshooting section with whatever turns out to be
   the final answer (Path A or B).

The `Tilt` inner-loop work (Stage 3 from the original plan) is still open
and untouched. That's the next thing after login works.
