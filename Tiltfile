# -*- mode: python -*-
# vim: set ft=python:
# =============================================================================
# Tiltfile — inner-loop dev for the SRW stack on local k3d.
#
# Design doc: knowledge-base/knowledge/features/tilt_inner_loop_dev.md
#
# Prerequisite: ./scripts/local-dev-tilt-up.sh has been run once. That script:
#   - creates the k3d cluster `srw` with an embedded registry on localhost:5005
#   - installs cert-manager + the mkcert ClusterIssuer
#   - creates the srw namespace + srw-vm-ssh-key + srw-session-jwt Secrets
#   - drops a values-local.yaml from the example template
#
# After that, `tilt up` brings the chart online and watches your code.
#
# Scope (Slice 1): orchestrator only. Cockpit/agent/mcp follow in later slices.
# =============================================================================

# The full chart's first Helm install can exceed Tilt's 30-second custom-deploy
# default even on a healthy local k3d cluster. Let Helm finish recording the
# release instead of killing it after resources have already been applied and
# leaving a `pending-install` revision behind.
#
# This only narrows the window — Tilt still kills the apply on a superseding
# build or a Ctrl-C, which strands the release the same way. Closing it is
# scripts/tilt-helm-apply.sh's preflight; see the `srw` resource below.
update_settings(k8s_upsert_timeout_secs=180)

# -----------------------------------------------------------------------------
# Global watch exclusions — paths Tilt must never treat as a code change.
#
# `eval/` holds memory-eval harness output (eval/memory/runs/*.log, etc.). An
# active eval streams log lines several times a second; because the
# orchestrator/cockpit/mcp docker_builds use context='.', each write looked
# like an in-context source change with no matching sync() and forced a full
# image rebuild per log line. Those Dockerfiles don't COPY eval/, so every
# rebuild was a cache-identical no-op (no pod roll) — but it pinned the inner
# loop in a perpetual rebuild and masked real edits behind the churn.
# Excluding it at the watcher level stops the loop for every resource at once
# without touching any build context. (agent/workspace use only=[…] allowlists
# and were already immune.)
# -----------------------------------------------------------------------------
watch_settings(ignore=['eval/'])

# -----------------------------------------------------------------------------
# Registry — k3d created via `--registry-create srw-registry:0.0.0.0:5005`.
#   docker push targets        : localhost:5005    (host-side mapped port)
#   kubelet image references   : srw-registry:5000 (k3d internal DNS + port)
# Tilt auto-rewrites image refs in the K8s manifest from one to the other.
# -----------------------------------------------------------------------------
default_registry('localhost:5005', host_from_cluster='srw-registry:5000')

# -----------------------------------------------------------------------------
# Orchestrator — uvicorn --reload picks up sync'd Python files without a
# container restart. fall_back_on triggers a full image rebuild for dependency,
# migration, and metering-package changes that must be present in every new
# replica rather than only in Tilt's current live-sync target.
# -----------------------------------------------------------------------------
docker_build(
    'srw-orchestrator',
    context='.',
    dockerfile='docker/Dockerfile.orchestrator.dev',
    live_update=[
        fall_back_on([
            'docker/Dockerfile.orchestrator.dev',
            'orchestrator/requirements.txt',
            # Migrations must be BAKED, never just live-synced: a synced
            # migration gets applied to the DB by the reloaded app, but the
            # image still lacks the file — the next pod from that image (e.g.
            # a Reloader bounce) fails startup with "applied but missing on
            # disk" and crash-loops (2026-06-11, 0025_security_events).
            'orchestrator/database/migrations/',
            # A package change can add a module imported by main.py. Live sync
            # targets the currently selected Pod, so during a rolling update an
            # older baked image can receive main.py without receiving the new
            # module. Bake this package atomically into a new image instead.
            'orchestrator/services/infrastructure_metering/',
        ]),
        # orchestrator/ contents are flattened into /app/ to match the prod
        # Dockerfile's layout (so `from services.foo import bar` resolves).
        sync('orchestrator/', '/app/'),
        sync('src/', '/app/src/'),
        sync('config/', '/app/config/'),
    ],
    ignore=[
        '**/__pycache__',
        '**/*.pyc',
        '**/*.pyo',
        '.pytest_cache',
        '.mypy_cache',
        '.ruff_cache',
        '.venv',
        '*.egg-info',
        '.coverage',
        'htmlcov',
        'tests/',
        'workspace/',
        '.tilt-state/',
        # Cross-component dirs the orchestrator build doesn't care about — any
        # file change here would otherwise force a fall_back full rebuild.
        'cockpit/',
        'helm/',
        'knowledge-base/',
        'knowledge-history/',
        'deployment/',
        'scripts/',
        'docker/Dockerfile.cockpit*',
        'docker/Dockerfile.agent*',
        'docker/Dockerfile.mcp*',
        '*.md',
        '.git/',
        '.playwright-mcp/',
    ],
)

# -----------------------------------------------------------------------------
# Cockpit — Angular dev image runs `ng serve` so live_update sync into
# /app/src triggers Angular's incremental rebuild + HMR push. ~1–3 s loop.
#
# Build context is the cockpit/ subdirectory; the Dockerfile inside that
# context expects `cockpit/` paths because docker_build's `context` lops
# off the path prefix.
# -----------------------------------------------------------------------------
docker_build(
    'srw-cockpit',
    context='.',
    dockerfile='cockpit/Dockerfile.cockpit.dev',
    live_update=[
        fall_back_on([
            'cockpit/Dockerfile.cockpit.dev',
            'cockpit/package.json',
            'cockpit/package-lock.json',
            'cockpit/angular.json',
            'cockpit/tsconfig.json',
            'cockpit/tsconfig.app.json',
            'cockpit/ngsw-config.json',
        ]),
        sync('cockpit/src/', '/app/src/'),
        sync('cockpit/public/', '/app/public/'),
    ],
    ignore=[
        'cockpit/node_modules/',
        'cockpit/dist/',
        'cockpit/.angular/',
        'cockpit/coverage/',
        # Chart ConfigMap mounts the rendered env.js at /app/src/assets/env.js
        # (gated by cockpit.envJs.mountPath in values-tilt.yaml). Excluding
        # the source-side file from sync keeps the ConfigMap-mounted version.
        'cockpit/src/assets/env.js',
        # Monaco assets are built once into the image; don't sync over them.
        'cockpit/public/monaco/',
        # Cross-component dirs the cockpit build doesn't care about.
        'orchestrator/',
        'src/',
        'config/',
        'agent.py',
        'init.py',
        'helm/',
        'knowledge-base/',
        'knowledge-history/',
        'deployment/',
        'scripts/',
        'tests/',
        'docker/',
        'workspace/',
        '*.md',
        '.git/',
        '.playwright-mcp/',
        '.tilt-state/',
    ],
)

# -----------------------------------------------------------------------------
# Agent — live_update is impossible (per-job pods with restartPolicy: Never,
# spawned by AgentProvisioner; see Non-goals in tilt_inner_loop_dev.md). What
# Tilt CAN do is automate the rebuild loop end-to-end:
#
#   1. file save under src/, config/, agent.py, or requirements.txt
#   2. docker_build rebuilds srw-agent:tilt-<hash>
#   3. push to localhost:5005 (k3d pulls via srw-registry:5000)
#   4. the `srw` custom deploy re-renders the srw-config ConfigMap with the
#      new PERSISTENT_AGENT_IMAGE tag
#   5. Stakater Reloader (already running in cluster; chart's orchestrator
#      Deployment carries `reloader.stakater.com/auto: "true"`) detects the
#      ConfigMap change and rolls the orchestrator
#   6. AgentProvisioner re-reads PERSISTENT_AGENT_IMAGE at __init__ time and
#      provisions the next agent pod with the new tag
#
# The `only=` list pins the file watcher to exactly the paths that go into
# the final image's source layers — pyproject changes don't rebuild, doc
# edits don't rebuild, test files don't rebuild. Cold rebuild is ~30-60 s;
# warm cache (only src/ changed) is ~10-15 s.
# -----------------------------------------------------------------------------
docker_build(
    'srw-agent',
    context='.',
    dockerfile='docker/Dockerfile.agent.dev',
    only=[
        'src/',
        'config/',
        'agent.py',
        'requirements.txt',
        'docker/Dockerfile.agent.dev',
    ],
    ignore=[
        '**/__pycache__',
        '**/*.pyc',
        '**/*.pyo',
        'src/**/.pytest_cache',
        'src/**/.mypy_cache',
        'src/**/.ruff_cache',
    ],
)

# -----------------------------------------------------------------------------
# MCP — FastMCP HTTP server bridging Claude Code → orchestrator REST API.
# Small, stateless, restart-tolerant. We use live_update sync into /app
# plus a `watchfiles` wrapper in the dev Dockerfile so source edits
# trigger a process restart without rebuilding the image. ~3-5 s loop.
#
# fall_back_on covers the cases where live_update can't help (requirements
# bump, Dockerfile change, formatters touch from outside mcp/).
# -----------------------------------------------------------------------------
docker_build(
    'srw-mcp',
    context='.',
    dockerfile='docker/Dockerfile.mcp.dev',
    live_update=[
        fall_back_on([
            'docker/Dockerfile.mcp.dev',
            'orchestrator/mcp/requirements.txt',
        ]),
        # Layout matches the Dockerfile's COPYs:
        #   orchestrator/mcp/   → /app/
        #   src/shared/         → /app/src/shared/   (client + formatters)
        #   orchestrator/security/anti_framing.py → /app/security/anti_framing.py
        # The watchfiles CMD watches /app recursively, so synced shared-package
        # edits restart the server like any mcp/ edit.
        sync('orchestrator/mcp/', '/app/'),
        sync('src/shared/', '/app/src/shared/'),
        sync('orchestrator/security/anti_framing.py', '/app/security/anti_framing.py'),
    ],
    ignore=[
        '**/__pycache__',
        '**/*.pyc',
        '**/*.pyo',
        # Cross-component dirs the mcp build doesn't care about. `src` uses
        # `*` (not trailing `/`) so the `!` negation below can re-include the
        # shared package the image ships.
        'src/*',
        '!src/shared',
        'config/',
        'cockpit/',
        'helm/',
        'knowledge-base/',
        'knowledge-history/',
        'deployment/',
        'scripts/',
        'tests/',
        'workspace/',
        'agent.py',
        'init.py',
        # Other orchestrator code outside of mcp/ + the two synced files
        # would otherwise trigger fall_back via context-content hash.
        # `!` negation re-includes the two files the Dockerfile COPYs
        # from orchestrator/services/.
        'orchestrator/api/',
        'orchestrator/auth/',
        'orchestrator/database/',
        'orchestrator/dispatch/',
        'orchestrator/routers/',
        'orchestrator/services/',
        'orchestrator/main.py',
        'orchestrator/requirements.txt',
        'docker/Dockerfile.orchestrator*',
        'docker/Dockerfile.agent*',
        'docker/Dockerfile.cockpit*',
        'docker/Dockerfile.workspace*',
        '*.md',
        '.git/',
        '.playwright-mcp/',
        '.tilt-state/',
    ],
)

# -----------------------------------------------------------------------------
# Workspace — dynamically-created SSH/code-server workspaces. These pods are
# spawned by the orchestrator, so Helm must receive the Tilt-built image tag via
# image_keys just like the agent image.
# -----------------------------------------------------------------------------
docker_build(
    'srw-workspace',
    context='.',
    dockerfile='docker/Dockerfile.workspace',
    only=[
        'docker/Dockerfile.workspace',
        'docker/workspace-entrypoint.sh',
        'docker/browser-exec',
        'docker/check-browser-stream.py',
        'docker/assert-browser-stack.sh',
    ],
    ignore=[
        '.git/',
        '.playwright-mcp/',
        '.tilt-state/',
    ],
)

# -----------------------------------------------------------------------------
# Vendored chart dependencies. `helm/charts/` is gitignored (*.tgz), so a fresh
# clone has no collabora-online tarball and `helm upgrade` refuses to run at all
# — the dependency-presence check fires before `collabora.enabled` is evaluated,
# so this bites even though Collabora is off in every local values file.
#
# CI does the same repo-add + build before each helm invocation. Doing it here
# rather than in local-dev-tilt-up.sh covers a bare `tilt up` too, which is the
# documented path for every session after the first.
#
# `helm dependency list` is offline and instant, so the common case (deps
# already vendored) adds nothing to Tiltfile evaluation.
# -----------------------------------------------------------------------------
local(
    """
    if helm dependency list ./helm | grep -q 'missing'; then
        helm repo add collabora https://collaboraonline.github.io/online --force-update
        helm repo add cloudnative-pg https://cloudnative-pg.github.io/charts --force-update
        helm dependency build ./helm
    fi
    """,
    quiet=True,
)

# -----------------------------------------------------------------------------
# Helm chart — same chart as production. Values stack (last wins):
#   1. helm/values.yaml                      chart defaults
#   2. deployment/values-local.yaml          gitignored — dev secrets +
#                                            sessionRouter + opencloud
#                                            hostAliases (Traefik ClusterIP)
#   3. deployment/values-tilt.yaml           committed — imagePullPolicy
#                                            IfNotPresent + prewarm disabled +
#                                            cockpit envJs mountPath redirect
#
# The image list below auto-substitutes the Tilt-built images for all runtime
# components, including the dynamically-spawned workspace image.
#
# This is a hand-rolled k8s_custom_deploy rather than the `helm_resource`
# extension. The extension's apply helper is a plain `helm upgrade --install`,
# and Tilt kills that subprocess whenever it cancels an in-flight deploy — a
# superseding build, a Ctrl-C, or the k8s_upsert_timeout_secs deadline above.
# Helm writes its release secret as `pending-upgrade` before applying and flips
# it to `deployed` only at the end, so a killed helm wedges every later upgrade
# with "another operation (install/upgrade/rollback) is in progress" until
# someone clears the lock by hand — restarting Tilt does not help, because the
# lock lives in the cluster. scripts/tilt-helm-apply.sh runs the identical helm
# command with a preflight that clears a stale pending revision first. See
# knowledge-base/knowledge/features/tilt_inner_loop_dev.md "Risks and known gotchas".
# -----------------------------------------------------------------------------
# (image name, chart repository key, chart tag key)
_srw_images = [
    ('srw-orchestrator', 'image.orchestrator.repository', 'image.orchestrator.tag'),
    ('srw-cockpit', 'image.cockpit.repository', 'image.cockpit.tag'),
    ('srw-agent', 'image.agent.repository', 'image.agent.tag'),
    ('srw-mcp', 'image.mcp.repository', 'image.mcp.tag'),
    ('srw-workspace', 'image.workspace.repository', 'image.workspace.tag'),
]

# Tilt fills in TILT_IMAGE_<i> (the freshly built+pushed ref) per image_deps
# entry; these tell the apply script which chart keys each half maps to.
_srw_helm_env = {
    'CHART': './helm',
    'RELEASE_NAME': 'srw',
    'NAMESPACE': 'srw',
    'TILT_IMAGE_COUNT': '%s' % len(_srw_images),
}
for i in range(len(_srw_images)):
    _srw_helm_env['TILT_IMAGE_KEY_REPO_%s' % i] = _srw_images[i][1]
    _srw_helm_env['TILT_IMAGE_KEY_TAG_%s' % i] = _srw_images[i][2]

k8s_custom_deploy(
    'srw',
    apply_cmd=[
        os.path.abspath('scripts/tilt-helm-apply.sh'),
        # A custom-deploy Force Update (including `tilt trigger srw`) runs
        # delete_cmd first. The chart-managed Secret is resource-policy=keep;
        # reclaim that same object on reinstall so generated encryption/Garage
        # keys do not rotate.
        '--take-ownership',
        '--values=deployment/values-local.yaml',
        '--values=deployment/values-tilt.yaml',
    ],
    apply_env=_srw_helm_env,
    delete_cmd=['helm', 'uninstall', '--namespace', 'srw', 'srw'],
    # Chart/values edits do not trigger a redeploy on their own — same as the
    # `helm_resource` default this replaced. Image rebuilds drive the loop.
    deps=[],
    image_deps=[img[0] for img in _srw_images],
)

# -----------------------------------------------------------------------------
# Object store: the bundled single-node Garage provides S3 for local dev — the
# same path self-hosters get. The chart deploys Garage + an idempotent bootstrap
# Job and auto-wires both seams (snapshots + virtual tier) to it, so there is no
# separate fixture to apply here. The old MinIO manifest
# (deployment/tilt-minio.yaml) was removed.
#
# Nothing here enables it: `garage.enabled` defaults to auto (run the bundled
# store unless `s3.endpoint` is set), and deployment/values-tilt.yaml keeps that
# input blank rather than forcing the flag on — so local dev resolves the store
# exactly the way a fresh `helm install` does. Canvas durability depends on this
# being wired (knowledge-base/knowledge/features/canvas_durable_presentation.md §11.1); if the
# orchestrator logs "Snapshot service disabled" at startup, a stale
# deployment/values-local.yaml is setting s3.endpoint.
#
# For a pure no-store smoke test set virtualWorkspace.rclone.type=memory.
# -----------------------------------------------------------------------------
