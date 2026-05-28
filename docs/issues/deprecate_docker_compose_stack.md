# Deprecate the Docker Compose stack — k3d is the local-dev target

**Status**: Proposed. Migration to local k3d verified end-to-end 2026-05-28.

## Outcome that triggers this

Local dev now runs the production Helm chart on k3d. Same chart, same templates,
same secrets layout, same ingress topology as the homelab cluster — only the
values overlay differs (`deployment/values-local.example.yaml`). The smoke-test
path documented in `README.md` ("Smoke-testing the install") was walked
end-to-end and passes on a fresh install:

1. Cockpit login as `test/test` via Keycloak
2. New persistent session → agent + workspace pods spawn, WS handshake completes
3. New job → row flips `created → processing → completed`, freeze data + diff land
4. Gitea SSO from cockpit footer → no second login
5. OpenCloud SSO via `cloud.localhost` → personal space loads (the
   `hostAliases` workaround for the RFC 6761 `localhost`-loopback hijack is
   documented inline in the values file and in CLAUDE.md)

This means the chart — not Compose — is now the only thing local-dev exercises
that the production deploy doesn't already exercise. Compose became dead weight
the moment that gap closed.

## Why deprecate it

The Compose stack costs us, on every PR that touches dispatch / workspace
assignment / agent lifecycle / database init, the maintenance of a second
deployment topology that no one in the team runs anymore. Concretely:

- **Two provisioner code paths.** `orchestrator/services/docker_provisioner.py`
  (584 lines) exists exclusively to assign jobs to a static container pool fed
  by `WORKSPACE_HOSTS`. Every dispatcher decision in `orchestrator/main.py`
  branches on `docker_provisioner.is_available` (17 call sites including
  `_trigger_dispatch`, `_try_dispatch_pending_jobs`, the session-attach path,
  and the resume path). The k8s `ContainerProvisioner` is the path the
  product actually ships on; the Compose path is the path we keep accidentally
  breaking and re-fixing.
- **A whole agent CLI surface that only exists for Compose.** `agent.py
  --loop` exists because under Compose the process isn't respawned after a
  job completes (`AGENT_LOOP=1` → `dual_app.py:323` flips the post-task
  exit). Under k8s, the agent is a single-shot pod the orchestrator
  re-provisions; `--loop` is meaningless. The flag exists, is documented
  prominently in `agent.py --help` and CLAUDE.md, and adds a state machine
  branch in dual-mode for a topology we no longer dev against.
- **Two test surfaces, one of which is undocumented.** `docs/docker_compose_mode.md`
  (833 lines) describes the Compose deployment architecture in detail. Nobody
  runs it. The k3d setup that we *do* run only got real documentation last
  week (commit on `develop` HEAD).
- **Compose files drift.** `docker-compose.yaml`, `docker-compose.dev.yaml`,
  and `docker-compose.local.yaml` total ~89 kB. They get rubber-stamped when
  the chart changes — sometimes correctly, sometimes not. Issue
  `deployment_separation_of_concerns.md` already had to call out three
  helm-vs-compose drifts (Keycloak DB split, pgvector password key, MongoDB
  auth) that were fixed on the chart side first and back-ported to Compose
  manually.
- **Marketing tax.** The "no Kubernetes required" framing in README turns out
  to be a lie in practice — the chart works on a single-node k3d cluster with
  one bootstrap script, which is the same UX promise without the second
  codebase.

## What deprecation covers

### Files to delete (or move to `legacy/`)

| Path | Lines | Role |
|------|------:|------|
| `docker-compose.yaml`       | ~960 | "Production-ish" Compose stack |
| `docker-compose.dev.yaml`   | ~660 | Dev databases-only Compose |
| `docker-compose.local.yaml` | ~840 | Same as `.yaml` but builds images from source |
| `orchestrator/services/docker_provisioner.py` | 584 | Static-pool workspace assigner |
| `docs/docker_compose_mode.md` | 833 | Compose-mode architecture doc |

### Code paths to retire in-place

- `orchestrator/main.py`: drop the 17 `docker_provisioner.is_available`
  branches. The post-deprecation flow always uses `container_provisioner`
  (k8s) — the orchestrator simply requires a Kubernetes API target to start.
- `orchestrator/main.py:2187` and `:3306` — the "neither k8s API nor
  WORKSPACE_HOSTS available" diagnostics become "Kubernetes API
  unreachable", which is also a more honest error message.
- `orchestrator/main.py:3280-3292` — the provisioner-bootstrap branch in
  `lifespan` collapses to the k8s path.
- `agent.py`: remove `--loop` flag, the `AGENT_LOOP=1` env, the
  `if args.loop:` branch in `main()`, and the dual-app loop check at
  `src/api/dual_app.py:323`. Each agent pod becomes single-shot, which is
  what the orchestrator's dispatcher already assumes.
- `init.py`: keep the schema-migration entry point (still useful for
  `kubectl exec` smoke tests against the in-cluster orchestrator), but
  remove the docstring's Compose-oriented examples and the `--force-reset`
  path's expectation of locally-mounted Compose volumes.

### Documentation to rewrite

- `README.md`:
  - Drop the "Docker Compose Deployment" top-level section
    (lines 115–235 around the current `## Docker Compose Deployment` heading)
  - Drop the comparison row in the install-path picker that points to it
  - Replace the "Start the agent server" Compose-and-bare-metal block
    (lines ~460–500) with a pointer to the existing "Local Kubernetes Setup
    (k3d)" section
  - `python agent.py --port 8001 --loop` examples (six of them) all go
- `CLAUDE.md`: drop the `--loop` example in the "Run agent as a server"
  block and the "bare-metal/Compose dev" mention in its description
- `docs/docker_compose_mode.md`: delete (or move to
  `docs/legacy/docker_compose_mode.md` with a top banner pointing at the k3d
  setup)

### What stays

- `docker/Dockerfile.*` — these build the images the chart deploys. Compose
  consumed the same images, but the Dockerfiles aren't Compose-specific.
- `deployment/legacy/` — already reference-only per its README; no action.
- `VM provisioner` (`orchestrator/services/vm_provisioner.py`) — VM mode
  runs over NATS in k8s; it has nothing to do with Compose and remains
  active on the prod cluster (see `project_vm_backend_disabled_on_dev` in
  memory for the dev-side NATS_URL gap).
- `init.py` — keep as the schema-migration + workspace-scaffold entry
  point; just sand off the Compose-flavored examples.
- The Dockerfiles' build-from-source path lives on through
  `docker-compose.local.yaml`; we'll need a one-liner script (`scripts/build-local-images.sh`)
  to replace the Compose-driven local rebuild loop documented in the README
  troubleshooting section ("image skew workaround").

## Proposed sequencing

Five independently mergeable PRs, none of which should break the chart or
existing k3d dev loops mid-flight.

1. **PR 1 — Documentation pre-cutover (no code).** Add a banner to
   `docs/docker_compose_mode.md` ("Deprecated 2026-05-28, see k3d setup in
   README"), demote the Compose entries in the README install-path picker,
   add a paragraph in `CLAUDE.md`'s deployment overview noting that Compose
   is end-of-life. ~30 min. Lets downstream readers redirect now while the
   code still runs.
2. **PR 2 — `scripts/build-local-images.sh`.** Replace the Compose-driven
   local rebuild loop with a small shell script that wraps `docker build` +
   `k3d image import` for orchestrator / cockpit / agent. Update the README
   troubleshooting "image skew" entry to call the script. ~1 hour. Removes
   one of the two remaining dev workflows that needed Compose-the-tool.
3. **PR 3 — Remove `--loop` from `agent.py` + `AGENT_LOOP` handling.**
   Touches `agent.py`, `src/api/dual_app.py`, and the agent
   `Dockerfile.agent` if it sets `AGENT_LOOP=1` (verify; the chart's
   dispatch loop expects single-shot pods anyway). Update README + CLAUDE.md
   examples. No chart change required. ~2 hours including a CI run.
4. **PR 4 — Delete `DockerProvisioner` and its call sites.** Remove
   `orchestrator/services/docker_provisioner.py`, the import in `main.py`,
   and the 17 fallback branches. The orchestrator's `lifespan` collapses
   to "require k8s API; fail to start if unavailable", which is the
   prod behavior already. Add a clear startup error message
   ("Kubernetes API unreachable; the orchestrator no longer supports
   the Compose-static workspace pool — see deployment/values-local.example.yaml").
   Touches `main.py` (~50 lines net deletion after the branches collapse)
   plus tests under `tests/test_orchestrator_*` that mocked
   `docker_provisioner.is_available`. ~1 day.
5. **PR 5 — Delete Compose files + `docs/docker_compose_mode.md`.**
   Pure deletion PR. Drop `docker-compose.yaml`, `docker-compose.dev.yaml`,
   `docker-compose.local.yaml`. Move `docs/docker_compose_mode.md` to
   `docs/legacy/` if we want to keep it as historical reference, otherwise
   delete. Rewrite the README "Docker Compose Deployment" section as a
   one-paragraph "Compose mode was removed in <commit-sha>; see Local
   Kubernetes Setup (k3d)" callout. ~2 hours including README rewrite.

## Acceptance criteria

- [ ] `orchestrator/services/docker_provisioner.py` removed
- [ ] Zero references to `docker_provisioner` or `WORKSPACE_HOSTS` in
      `orchestrator/`
- [ ] `agent.py` no longer accepts `--loop`; `AGENT_LOOP` env unused in
      `src/`
- [ ] No `docker-compose*.yaml` at the repo root
- [ ] `README.md`'s install-path picker no longer offers Compose
- [ ] `CLAUDE.md`'s "Run agent as a server" block no longer documents
      `--loop` or "bare-metal/Compose dev"
- [ ] `docs/docker_compose_mode.md` gone or moved under `docs/legacy/`
      with a banner
- [ ] Fresh `./scripts/local-dev-up.sh` + `helm install srw ./helm -n srw
      -f deployment/values-local.example.yaml` + the 5-step smoke-test
      from the README all still pass
- [ ] Full pytest suite + `ruff check` + `ruff format --check` green

## Effort estimate

- Per-PR estimates above sum to roughly **2–2.5 engineering days**.
- Calendar-time the whole thing in a week so each PR can sit on `develop`
  for a day before the next deletes more.
- Pre-condition: GHCR `:latest` is fresh enough that no one needs the
  Compose-driven local rebuild loop while PR 2's replacement script lands.

## Related

- `docs/issues/deployment_separation_of_concerns.md` — three of its
  findings (Issues A, B, D) shipped chart-first and back-ported to
  Compose manually. Removing Compose removes the back-port step on all
  future chart changes.
- `docs/issues/local_e2e_testing.md` — proposed an e2e harness against
  the Compose stack; if still relevant, retarget at k3d (the harness is
  cheaper to write against the chart we already test).
- `docs/issues/helm_deployment.md` and `docs/issues/helm_fresh_deploy_issues.md`
  — chart-side polish items uncovered during the migration; track-adjacent
  to this work but not blockers.
- `feedback_no_local_workspace.md` (memory) — already established that
  the agent never runs on its own filesystem; this issue is the natural
  continuation that retires the Compose-backed remote workspace pool too.
- `project_local_k8s_dev.md` (memory) — the k3d setup this deprecation
  is predicated on.
