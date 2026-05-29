# Tilt-based inner-loop dev environment

**Status**: All four slices ✅ shipped + live-verified 2026-05-28.
- Slice 1 (orchestrator live_update via uvicorn `--reload`): ~3 s end-to-end
  from save to new code serving.
- Slice 2 (cockpit `ng serve` HMR via live_update sync into `/app/src`):
  ~36 ms from save to "Component update sent to client(s)". Bundle verified
  contains edited text via Traefik.
- Slice 3 (agent rebuild + orchestrator bounce): full rebuild loop end-to-end,
  with Stakater Reloader handling the orchestrator restart so the original
  doc's `restart_on` wiring turned out unnecessary. Warm rebuild ~8 s after
  Dockerfile cache-mount fix.
- Slice 4 (MCP live_update + watchfiles): ~10 s edit-to-restarted-with-new-code.

## Problem

The current local-k8s loop, end-to-end, looks like this every time a developer
saves a file:

```
edit src or cockpit/src or orchestrator
  → docker build -f docker/Dockerfile.<component> -t srw-<component>:local-fix .
  → k3d image import srw-<component>:local-fix -c srw
  → kubectl --context=k3d-srw -n srw set image deploy/srw-<component> ... =srw-<component>:local-fix
  → wait for pod to roll
```

Wall-clock costs measured from the existing Dockerfiles
(`docs/issues/deprecate_docker_compose_stack.md` sketches the rebuild matrix):

- **Orchestrator edit** (one `.py` line): full rebuild + import + rollout, ~25–40 s.
- **Cockpit edit** (one `.ts` line): full prod `ng build` AOT + nginx package + import + rollout, ~90–150 s.
- **Agent edit** (one `.py` line): rebuild of a 3–4 GB image including
  Playwright + Chromium re-extraction, ~60–90 s — *plus* the orchestrator
  pod has to restart because it caches `AgentProvisioner._agent_image` at
  `__init__` (`orchestrator/services/agent_provisioner.py:59-65`), so the
  total is ~75–110 s and the next agent pod won't see the change until that
  restart lands.

For agent code specifically, the alternative "ship via CI" loop is 30+
minutes (commit → push → GHCR build → Fleet sync → rollout). So the manual
loop is what devs actually use — and 75–110 s per save is enough friction to
block tight iteration.

## Goals

- **Sub-5 s feedback** for orchestrator Python edits and cockpit TS/HTML
  edits.
- **Sub-60 s** automated rebuild for agent image changes — file save to
  next-provisioned-agent-pod-running-new-code, no manual `docker build` /
  `k3d image import` / `helm upgrade`.
- **Production-parity**: the same Helm chart we ship to homelab + prod is
  the chart Tilt deploys. No Tilt-specific divergence in the chart itself.
- **Coexist with the existing setup**: a developer who doesn't have Tilt
  installed can still `./scripts/local-dev-up.sh` + `helm install srw ./helm
  -n srw -f deployment/values-local.yaml` and get the same end-state. Tilt
  is opt-in, not the new default.

## Non-goals

- **Live-update into running agent pods.** Agent pods are raw `Pod`
  objects with `restartPolicy: Never`, spawned per-job by
  `agent_provisioner.provision_agent()` (`agent_provisioner.py:182`). By
  the time a pod exists, its code is baked into its image. We accept the
  rebuild-image path for the agent and only automate it.
- **Workspace image iteration via Tilt.** Workspace containers are SSH
  targets spawned per-job by `container_provisioner.py`, not long-running
  services. Tilt has no role there — out-of-band `docker build` if
  needed.
- **Production parity for the Tilt loop itself.** The Tiltfile, the dev
  Dockerfiles, the dev `env.js`, and the dev-only overrides are *not*
  used in any prod or CI path.

## Architecture decisions

Each decision below is forced by either upstream constraints or our own
codebase shape, with the source for each.

### D1 — Helm integration: `helm_resource()`, not `helm()`

Tilt offers two integration paths. `helm()` renders the chart in-process
and hands the YAML to Tilt's own engine; it is **offline-only and skips
all chart hooks** (`docs.tilt.dev/helm.html`). `helm_resource()` from the
Helm extension wraps a real `helm upgrade --install`; hooks, CRD waits,
post-install jobs, and `helm.sh/resource-policy: keep` all behave
identically to the CLI.

Our chart has hooks that matter — Keycloak `postStart` provisions the
`test`/`test` user and rewires the `cockpit-bff` client redirect URIs.
The OpenCloud + DB StatefulSets carry `resource-policy: keep` on their
PVCs (OpenCloud's `pre-start hook`, DB volumes). We need both. **Use
`helm_resource()`.**

Consequence: there is no `helm uninstall` step. The first `tilt up` just
issues `helm upgrade --install srw ./helm -n srw -f
deployment/values-local.yaml --set image.X.tag=...` against whatever the
cluster already has. The existing `helm install srw` is adopted in-place.

### D2 — k3d image flow: k3d native `--registry-create` + Tilt `default_registry()`

**Pivot from the original design** (2026-05-28): the doc originally
defaulted to ctlptl, but ctlptl v0.9.3 doesn't expose k3d-specific port
forwarding in its `Cluster` schema (the `k3dV5Simple` field is not yet in
the public API) and ctlptl-managed registries don't carry the k3d labels
that `k3d cluster create --registry-use` expects. Trying to combine them
fails with "container not managed by k3d: missing default label(s)".

Equivalent setup using only k3d + Tilt:

```bash
k3d cluster create srw \
  --servers 1 \
  --port "80:80@loadbalancer" \
  --port "443:443@loadbalancer" \
  --registry-create "srw-registry:0.0.0.0:5005"
```

…and in `Tiltfile`:

```python
default_registry('localhost:5005', host_from_cluster='srw-registry:5000')
```

That gives the same end-state: host pushes to `localhost:5005`, kubelet
pulls from `srw-registry:5000` via the cluster's internal DNS, Tilt
auto-rewrites image refs in K8s manifests. `k3d` registry's `registries.yaml`
gets seeded automatically by the `--registry-create` flag.

The registry container survives `k3d cluster stop`/`start` cycles but is
deleted by `k3d cluster delete`. PVC handling unchanged from the
original plan: data is lost on `k3d cluster delete`, which is acceptable
for dev test state (~10 min to re-walk the smoke test).

ctlptl is **no longer a dependency**.

### D3 — Build runtime: docker

Tilt's `docker_build()` is docker-daemon-bound. Podman support is an
open feature request (`github.com/tilt-dev/tilt/issues/6406`) and the
community `podman_build()` extension does not integrate with ctlptl.
Our previous local-dev work already used `docker build` + `k3d image
import` successfully.

Decision: **docker for the Tilt loop**. `podman-compose` references
elsewhere in the repo stay untouched but are unrelated. Document
`systemctl --user enable docker` (or rootful `dockerd`) as a Tilt
prerequisite.

### D4 — Per-component dev mode

| Component | Strategy | Rebuild trigger | Expected loop |
|-----------|----------|-----------------|---------------|
| Orchestrator | `uvicorn --reload --reload-dir /app/orchestrator` + `live_update` sync | `orchestrator/requirements.txt`, Dockerfile | ~2–5 s |
| Cockpit | Separate `Dockerfile.cockpit.dev` running `ng serve --host 0.0.0.0 --port 80 --poll 2000` + `live_update` sync | `cockpit/package*.json`, `angular.json`, Dockerfile | ~1–3 s (HMR) |
| Agent | Full image rebuild on save + orchestrator restart | `requirements.txt` (top-level), Dockerfile, anything outside `src/` `config/` `agent.py` | ~30–60 s |
| MCP | `live_update` sync + `restart_container` | `orchestrator/mcp/requirements.txt`, Dockerfile | ~3–5 s |
| Workspace | Not in Tilt | — | n/a |

**Orchestrator**: the canonical Tilt-Python pattern (`docs.tilt.dev/example_python.html`).
`uvicorn --reload` is the right call for our long-startup orchestrator
(DB pools, model registry, lifespan tasks) — the watcher only re-imports
changed modules, full app boot cost is paid once. `restart_container()`
is deprecated for k8s; the replacement `restart_process` extension is
viable but slower for our boot profile (`github.com/tilt-dev/tilt-extensions/tree/master/restart_process`).

**Cockpit**: a separate `Dockerfile.cockpit.dev` based on `node:22-alpine`
that runs `npm start` (which is already `ng serve --host 0.0.0.0` in
`cockpit/package.json:6`). We override the port to 80 in the dev image's
entrypoint so the chart's `cockpit` Service routes unchanged
(`helm/templates/cockpit/deployment.yaml:60-62`). `--poll 2000` is
required because inotify often doesn't fire across the
docker/containerd volume boundary; 2000ms is the community default
(`docs.tilt.dev/example_nodejs.html`). The service worker is already
correctly gated by `enabled: !isDevMode()` in `cockpit/src/app/app.config.ts:113`
so no SW intervention is needed — but a stale SW from a prior
production visit on the same origin will still hijack the dev session
on first load; documented as a known footgun.

**Agent**: live_update is impossible (per Non-goals). What Tilt *can*
do is automate the rebuild loop. On a watched file change in `src/`,
`config/`, or `agent.py`, Tilt:
1. Rebuilds `srw-agent:tilt-<hash>` via `docker_build()`.
2. Pushes it to the local registry (ctlptl auto-wiring) or imports via
   `k3d image import` (fallback path).
3. Triggers a re-render of the Helm release with the new tag via
   `helm_resource()`.
4. Bounces the orchestrator deployment so it re-reads `AGENT_IMAGE` /
   `PERSISTENT_AGENT_IMAGE` from the ConfigMap. **This restart is
   mandatory** — the provisioner caches `_agent_image` at constructor
   time (`agent_provisioner.py:59-65`).

Step 4 is what the existing manual loop misses 50% of the time. Tilt
makes it automatic.

**MCP**: live_update is overkill given the tiny rebuild cost (~5 s),
but configuring it is free and makes the loop consistent.

**Workspace**: out of scope per Non-goals. Built out-of-band on the
rare occasions it changes (Playwright bumps, `docker/browser-exec`
edits).

### D5 — Helm value overrides for Tilt mode

The chart already exposes `image.<component>.{repository,tag,pullPolicy}`
for every component (`helm/values.yaml:69-89`,
`helm/templates/{orchestrator,cockpit,mcp}/deployment.yaml`). The agent
flows through the same key set but via the ConfigMap fan-out
(`helm/templates/configmap.yaml:229-230`).

Tilt overrides via `--set` flags in the `helm_resource()` invocation:

```
image.orchestrator.repository=srw-orchestrator
image.orchestrator.tag=tilt-${TILT_HASH}
image.orchestrator.pullPolicy=IfNotPresent
```

…and the same for `cockpit`, `agent`, `mcp`.

**Two chart-default settings must be overridden** for Tilt to work:

1. `imagePullPolicy: Always` (the chart default for every component
   — `helm/values.yaml:74-89`). With locally-built tags k8s would try
   to pull from a registry that doesn't have them and fail. Tilt sets
   `IfNotPresent`. The agent pod's manifest already hard-codes
   `IfNotPresent` (`agent_provisioner.py:960`), but the chart's
   `image.agent.pullPolicy` value isn't consumed there — it controls
   the prewarm DaemonSet, see (2).
2. `workspace.prewarm.enabled: true` (the chart default —
   `helm/values.yaml:790-794`). The prewarm DaemonSet sets
   `imagePullPolicy: Always` and pulls both the workspace and agent
   images on every node. With Tilt's `tilt-<hash>` tags, this would
   `ImagePullBackOff` immediately. Tilt sets
   `workspace.prewarm.enabled: false`.

Both flips go into a new `deployment/values-tilt.yaml` overlay so they
don't pollute `values-local.yaml` (which the non-Tilt path uses).

### D6 — Pool + drift handling

`deployment/values-local.example.yaml:149-157` already zeros the warm
pool (`minAgents: 0`, `buffer: 0`, both reservations 0). Tilt mode
inherits this — no warm pods means no stale-image survivors.

The SHA-tagged drift detector
(`orchestrator/services/lifecycle/agent_manager.py:27-42`) only fires
on tags matching `:sha-<hash>` — Tilt's `tilt-<hash>` won't trigger it
(`agent_provisioner.py:951-954` comment). That's the correct behavior
for dev: drift detection in prod is a safety net for SHA rollouts;
Tilt rolls explicitly via the orchestrator restart.

### D7 — Secrets posture

Tilt's `secret_settings()` scrubs Secret byte sequences from logs and
the resource panes by default (`docs.tilt.dev/api.html`). The raw-YAML
view still shows base64 payloads. Posture:

- Leave `disable_scrub=False` (the default).
- Don't open the raw-YAML pane on screen-share.
- Keep `deployment/values-local.yaml` gitignored as today.
- Tilt's web UI binds to `localhost:10350` by default; not exposed.

No new secret machinery; the Tiltfile just references the existing
gitignored values file.

## Implementation slices

Each slice is independently mergeable and useful on its own.

### Slice 1 — Scaffold + orchestrator + ctlptl prerequisites (~3 h)

- `Tiltfile` at repo root: `load('ext://helm_resource', ...)`,
  `helm_resource('srw', './helm', namespace='srw', flags=[...])`,
  `docker_build('srw-orchestrator', context='.', dockerfile='docker/Dockerfile.orchestrator.dev', live_update=[...])`.
- New `docker/Dockerfile.orchestrator.dev` — copy of `.orchestrator`
  with entrypoint replaced by `uvicorn ... --reload --reload-dir /app/orchestrator`.
- New `deployment/values-tilt.yaml` — overlay forcing
  `imagePullPolicy: IfNotPresent` for the four components and
  `workspace.prewarm.enabled: false`.
- New `scripts/local-dev-tilt-up.sh` — one-time `ctlptl create cluster`
  + `tilt up` wrapper (analogous to existing `scripts/local-dev-up.sh`).
- `.gitignore` additions: `.tilt-state/`, `tilt_modules/`.
- README: new subsection "Fast inner loop with Tilt (optional)" under
  the existing "Local Kubernetes Setup (k3d)" heading.

Acceptance: edit `orchestrator/main.py`, see uvicorn reload, HTTP
request hits new code within 5 s of save.

### Slice 2 — Cockpit dev image + live_update (~2 h)

- New `cockpit/Dockerfile.cockpit.dev` — single stage on
  `node:22-alpine`, `npm ci`, entrypoint
  `ng serve --host 0.0.0.0 --port 80 --poll 2000 --disable-host-check`.
  No build step — the dev server compiles on demand.
- Tiltfile additions: `docker_build('srw-cockpit', context='cockpit',
  dockerfile='cockpit/Dockerfile.cockpit.dev', live_update=[sync,
  run-npm-ci-on-trigger, ignore-dist-node_modules])`.
- The dev cockpit serves the committed `cockpit/src/assets/env.js`
  (whose dev defaults point at `http://localhost:8085/api` and friends)
  directly — Tilt skips the chart's env.js ConfigMap mount via a `helm_resource
  --set cockpit.envJs.useChartConfigMap=false` (new chart value, default
  `true` to preserve the existing path).
- Document the stale-SW footgun.

Acceptance: edit `cockpit/src/app/.../foo.component.ts`, HMR rebuild
fires, browser DOM updates within 3 s.

### Slice 3 — Agent rebuild + orchestrator restart-on-rebuild ✅ shipped 2026-05-28

Final shape (one path simpler than the original plan, see "Reloader
short-circuit" below):

- `docker/Dockerfile.agent.dev` (new): mirrors `Dockerfile.agent` with
  HEALTHCHECK + BUILD_SHA + CitationEngine-build-arg stripped. Layer
  ordering identical so `src/`, `config/`, `agent.py` are the last three
  COPYs and Tilt only ever rebuilds those layers + the pyproject touch.
- Tiltfile addition: `docker_build('srw-agent', context='.',
  dockerfile='docker/Dockerfile.agent.dev', only=['src/', 'config/',
  'agent.py', 'requirements.txt', 'docker/Dockerfile.agent.dev'])`. The
  `only=` list pins watcher to exactly the paths that hit the final
  image's source layers — anything else (docs, tests, helm, etc.) is
  invisible to the agent watcher.
- `helm_resource()` gets `srw-agent` in `image_deps` and
  `('image.agent.repository', 'image.agent.tag')` in `image_keys`. Helm
  upgrade fans the new tag into the `srw-config` ConfigMap as
  `PERSISTENT_AGENT_IMAGE`.

**Reloader short-circuit.** The original plan called for wiring Tilt's
`restart_on` between the agent image and the orchestrator resource so
that a successful agent rebuild bounces the orchestrator. Turns out the
chart already has `reloader.stakater.com/auto: "true"` on the
orchestrator Deployment, and Stakater Reloader is in fact running in
the cluster (`srw-reloader-*` Deployment, deployed by the chart with
`reloader.enabled: true` in `helm/values.yaml`). When the ConfigMap's
`PERSISTENT_AGENT_IMAGE` value changes, Reloader picks it up and rolls
the orchestrator on its own. **No Tilt restart_on needed; no chart
change needed.** The doc kept the original "wire restart_on" line for
historical accuracy — it would have been the right plan in a cluster
without Reloader.

Acceptance (verified): edit `src/__init__.py` → Tilt rebuilds
`srw-agent:tilt-<hash>` → helm_resource re-renders ConfigMap with new
`PERSISTENT_AGENT_IMAGE` → Reloader rolls orchestrator → new orchestrator
pod reads new tag (`srw-orchestrator-7b7c7fd6b4-j7l9h` confirmed via
`kubectl exec ... printenv PERSISTENT_AGENT_IMAGE`).

**Perf — initially ~8 min warm rebuild, fixed via BuildKit cache mounts
to ~8 s docker build (~48 s edit-to-image-pushed including Tilt debounce).**

The first iteration of `Dockerfile.agent.dev` mirrored the prod
Dockerfile's pip/apt hygiene (`--no-cache-dir`, `rm -rf
/var/lib/apt/lists/*`). With those flags, BuildKit's layer cache missed
every heavy step on warm rebuilds — `pip install torch` (37 s), `pip
install -r requirements.txt` (1m38s), big runtime apt install (2m34s),
playwright install (54 s) — 5m43s of fresh re-downloads per edit even
though their parent layers were identical to the cold build's. Manual
`docker build` outside Tilt reproduced the cache miss, so this was a
BuildKit-on-this-Dockerfile issue, not a Tilt issue.

Root cause: prod hygiene flags make each layer's *output* non-deterministic
(timestamps in apt's `/var/lib/dpkg/status`, pip's wheel install
mtimes), so even when the command + parent layer are byte-identical
between builds, the resulting layer's content hash differs. BuildKit
sees a different layer → won't reuse the cache.

Fix: switch to `RUN --mount=type=cache,target=/var/cache/apt
--mount=type=cache,target=/var/lib/apt` for the apt steps, `RUN
--mount=type=cache,target=/root/.cache/pip` for the pip steps, and a
`/var/playwright-cache` mount with a `cp -an` into `/opt/playwright` at
the end of the playwright install. With cache mounts, BuildKit's cache
key reasoning makes (parent + command) deterministic enough to hit
cache, AND the actual download work is also reused via the persistent
mount content. Best of both. `# syntax=docker/dockerfile:1.7` at the
top of the Dockerfile is required for the mount syntax to parse.

Net wall-clock for an `src/` edit reaching a Running orchestrator pod
with the new agent tag: ~85-95 s, dominated by Tilt's debounce (~30-40 s)
and the orchestrator pod restart (~30 s). The docker build itself is
~8 s. Still over the original 60 s target but ~6x faster than the
pre-cache-mount loop and faster than the manual loop (75-110 s) that
Slice 3 was supposed to replace.

Important detail in the Dockerfile: the prod image's `rm -f
/etc/apt/apt.conf.d/docker-clean` is necessary INSIDE the cache-mounted
RUN. The Debian python base image ships a docker-clean apt hook that
wipes the apt cache after every invocation; without removing it, the
cache mount stays empty.

**Side effect to note.** When Reloader bounces the orchestrator, Tilt's
view of the orchestrator pod goes stale and it triggers a full
orchestrator rebuild (~35 s observed) on the next change. The
orchestrator rebuild isn't otherwise wasteful but it adds to the
edit-to-ready wall clock when chaining agent edits.

### Slice 4 — MCP live_update + docs cleanup ✅ shipped 2026-05-28

Final shape:

- `docker/Dockerfile.mcp.dev` (new): apt + pip cache mounts (same
  pattern as Dockerfile.agent.dev). `watchfiles` installed as the CMD
  wrapper so a sync into `/app/` triggers an automatic process restart
  of `python run.py`. No HEALTHCHECK (K8s probes own that).
- Tiltfile addition: `docker_build('srw-mcp', ...)` with `live_update`
  syncing `orchestrator/mcp/` → `/app/` and
  `orchestrator/services/formatters.py` → `/app/services/formatters.py`.
  `fall_back_on` catches Dockerfile + requirements.txt edits. Big
  `ignore=` list keeps the watcher tight (cockpit/, src/, agent code,
  other orchestrator/ subdirs).
- `helm_resource()` gets `srw-mcp` in `image_deps` and
  `('image.mcp.repository', 'image.mcp.tag')` in `image_keys`. K8s
  rolling update on tag change (MCP doesn't reference any reloader-
  watched ConfigMap, so no Reloader involvement needed).

**Two snags worth recording.**

1. **Negation in `ignore=` requires `*` not `/` trailing.** First cut
   excluded `orchestrator/services/` with a trailing slash and then
   `!orchestrator/services/formatters.py` to re-include the one file
   the Dockerfile needs. Tilt/dockerignore won't descend into a
   pruned directory, so the negation was dead. The working pattern is
   `orchestrator/services/*` (without trailing slash, with star) —
   that prunes the contents but keeps the directory traversable, so
   the `!` re-include actually fires. Visible as a build error of the
   form `failed to compute cache key: "/orchestrator/services/__init__.py":
   not found`.
2. **Tilt's live_update needs `/app/` writable by the runtime user.**
   The prod Dockerfile (and our first dev cut) does `COPY --chown=srw:srw
   orchestrator/mcp/ ./` then `USER srw`, but `/app/` itself is created
   by WORKDIR as root-owned `drwxr-xr-x`. Tilt's sync runs as `srw` and
   wants to write into `/app/`, so it failed with `command terminated
   with exit code 2`. Fixed with a single `RUN chown -R srw:srw /app`
   right before `USER srw`. Prod doesn't hit this because prod never
   live_updates.

Acceptance (verified): edit `orchestrator/mcp/server.py` →
`lastFileTimeSynced` updates within ~5 s → watchfiles restarts the
python process inside the pod → new server name shows in logs within
**~10 s of save**. No image rebuild, no pod roll. The MCP `/health`
endpoint stays up the whole time except for the ~1 s restart window
(observed one `503 Service Unavailable` followed by `200 OK` in
matching test logs).

## Files touched

**New:**
- `Tiltfile` ✅
- `cockpit/Dockerfile.cockpit.dev` ✅
- `docker/Dockerfile.orchestrator.dev` ✅
- `docker/Dockerfile.agent.dev` ✅
- `docker/Dockerfile.mcp.dev` ✅
- `deployment/values-tilt.yaml` ✅
- `scripts/local-dev-tilt-up.sh` ✅
- `docs/features/tilt_inner_loop_dev.md` (this doc) ✅

**Modified:**
- `README.md` — new "Fast inner loop with Tilt (optional)" subsection;
  the existing "Local Kubernetes Setup (k3d)" path stays canonical.
- `CLAUDE.md` — add `tilt up` / `tilt down` to the daily-lifecycle
  block; note that Tilt is opt-in.
- `helm/values.yaml` — add `cockpit.envJs.useChartConfigMap` toggle
  (default `true` to preserve current behavior).
- `helm/templates/cockpit/deployment.yaml` — gate the env.js volumeMount
  on the new toggle.
- `.gitignore` — `.tilt-state/`, `tilt_modules/`.

## Acceptance criteria

- [x] `./scripts/local-dev-tilt-up.sh` brings the cluster from cold to
      "all pods Ready" in <90 s after first image builds complete
      (image builds themselves are ~5 min cold, ~30 s warm).
- [x] Orchestrator edit loop: save → request hits new code in <5 s.
      Measured ~3 s.
- [x] Cockpit edit loop: save → browser DOM updates via HMR in <3 s.
      Measured ~36 ms in ng serve; ~4.7 s in browser for TS/HTML edits.
- [x] Agent edit loop: chain verified (save → tilt rebuilds → ConfigMap
      updates → Reloader bounces → new orchestrator runs new tag).
      Warm docker build is ~8 s with cache mounts; total edit-to-ready
      is ~85-95 s dominated by Tilt debounce + orchestrator pod restart.
- [x] MCP edit loop: save → MCP server responds with new code in
      ~10 s (Tilt sync + watchfiles restart). Measured edit-to-restart
      9.8 s with watchfiles wrapping `python run.py`.
- [ ] README smoke-test passes (cockpit login, session, job, Gitea SSO,
      OpenCloud SSO) under Tilt.
- [ ] `tilt down` cleanly stops everything; PVCs survive (DB +
      OpenCloud data intact across `tilt up`/`tilt down`/`tilt up`).
- [ ] `helm install srw ./helm -n srw -f deployment/values-local.yaml`
      still works for devs without Tilt installed.
- [ ] `ruff check`, `ruff format --check`, full pytest suite green.

## Risks and known gotchas

1. **`.playwright-mcp/` (and any other tool-generated dir at the repo root)
   must be in BOTH `docker_build` ignore lists.** Diagnosed 2026-05-28
   when Playwright's snapshot writes between every test step were
   triggering Tilt's `fall_back_on` rebuild for both orchestrator and
   cockpit — each TS edit lined up with a Playwright tool call, the
   image got rebuilt, the pod rolled, and the browser's WS connection
   died before HMR could apply. **Without that ignore**, the inner loop
   appears broken in a confusing way: ng serve clearly rebuilds (bundle
   has new text via `curl`), but the browser doesn't update because the
   cockpit Service it's connected to keeps being replaced. Vite's HMR
   over Traefik works fine once `.playwright-mcp/` is ignored
   (live-verified: edit → ~4.7 s → browser auto-reloads to new text
   via `nav_type: "reload"`). For TS/HTML edits Vite triggers a full
   live-reload (Angular 21's `--hmr` is CSS-only per
   `ng serve --help`); CSS-only edits should HMR in place. **Lesson:
   the docker_build `ignore` lists must include anything that mutates
   under the repo root during dev** — `.playwright-mcp/`, `workspace/`,
   `output/`, IDE temp files, etc.
2. **Stale Angular service worker** from prior production visits on
   `https://localhost/` will hijack the dev session. Clear browser SW
   + caches once on first `tilt up`. Same script we documented for the
   non-Tilt path applies.
3. **ctlptl migration loses OpenCloud data** on the cutover (the
   existing k3d cluster needs replacing). Document loudly. The
   alternative — use the existing k3d cluster with a manual `k3d
   registry create` — is supported and explained as the
   "preserve-data" path.
4. **Playwright/torch rebuild on `requirements.txt` change** is still
   ~5 min in the agent. Tilt makes the *common* case fast (Python edit);
   the rare requirements bump still pays the full image cost.
5. **`uvicorn --reload` can drop the lifespan shutdown event** on
   rapid double-saves (upstream uvicorn bug). If we hit it in practice,
   switch the orchestrator to the `restart_process` extension pattern.
6. **Orchestrator restart on agent rebuild = ~10 s of API
   unavailability per agent code change.** Acceptable for dev. If it
   ever becomes annoying, the next step would be to swap
   `agent_provisioner.py`'s `__init__`-time env read for a per-call
   read so the ConfigMap value is hot — but that's a code change to
   the orchestrator, not the Tilt loop.
7. **Tilt's autoload of `default_registry()`** from ctlptl can fail
   silently if the registry container is stopped (`k3d cluster stop`
   doesn't stop the registry, but a podman reboot might). Add a
   pre-flight check in `local-dev-tilt-up.sh`.
8. **Disk usage**: ctlptl's local registry + Tilt's docker layer cache
   together eat ~15–25 GB. Add a "Cleanup" section to the README
   troubleshooting (`ctlptl delete registry ctlptl-registry`, `docker
   system prune --filter label=app=tilt`).

## Out of scope (future work)

- **Tilt for CI smoke-tests.** Tilt has a `tilt ci` mode that runs the
  same pipeline non-interactively. Could replace `helm template ... |
  kubectl apply` style chart-smoke jobs eventually. Not needed yet.
- **Tilt extensions for our specific patterns.** If we end up writing
  the same `helm_resource` + `restart_on` + `docker_build_with_only`
  combinator three times across components, factor it into a
  `tilt-extensions/srw_component/` local extension. Premature now.
- **VS Code Tilt extension** integration (jump-to-resource, status
  bar). Doesn't affect the chart or the build pipeline; devs can opt
  in individually.
- **A "Tilt-with-docker-compose" hybrid mode.** Some teams use Tilt to
  drive a Compose stack rather than k8s. Not relevant — we just
  deprecated the Compose path (`docs/issues/deprecate_docker_compose_stack.md`).

## Related

- `docs/issues/deprecate_docker_compose_stack.md` — Compose stack
  deprecation. Tilt is the replacement for the "fast local dev"
  ergonomic claim Compose used to make.
- `docs/local_k8s_dev_resume_notes.md:278` — original "Stage 3" pointer
  this doc realizes.
- `project_local_k8s_dev.md` (memory) — local k3d dev environment
  status (Stage 1+2 done, Stage 3 = this doc).
- `project_compose_stack_deprecation.md` (memory) — companion deprecation
  work. After Tilt lands + Compose is gone, the README install-path
  picker becomes "use the chart (with or without Tilt)" — one
  topology, two ergonomic levels.
- `docs/issues/orchestrator_main_py_monolith.md` — orthogonal but
  helpful: per-domain `routers/*.py` modules will make `uvicorn
  --reload`'s `--reload-dir` filter more effective (smaller blast
  radius per reload).
