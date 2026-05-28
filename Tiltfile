# -*- mode: python -*-
# vim: set ft=python:
# =============================================================================
# Tiltfile — inner-loop dev for the SRW stack on local k3d.
#
# Design doc: docs/features/tilt_inner_loop_dev.md
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

load('ext://helm_resource', 'helm_resource')

# -----------------------------------------------------------------------------
# Registry — k3d created via `--registry-create srw-registry:0.0.0.0:5005`.
#   docker push targets        : localhost:5005    (host-side mapped port)
#   kubelet image references   : srw-registry:5000 (k3d internal DNS + port)
# Tilt auto-rewrites image refs in the K8s manifest from one to the other.
# -----------------------------------------------------------------------------
default_registry('localhost:5005', host_from_cluster='srw-registry:5000')

# -----------------------------------------------------------------------------
# Orchestrator — uvicorn --reload picks up sync'd Python files without a
# container restart. fall_back_on triggers a full image rebuild only when the
# Dockerfile or requirements.txt change.
# -----------------------------------------------------------------------------
docker_build(
    'srw-orchestrator',
    context='.',
    dockerfile='docker/Dockerfile.orchestrator.dev',
    live_update=[
        fall_back_on([
            'docker/Dockerfile.orchestrator.dev',
            'orchestrator/requirements.txt',
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
        'docs/',
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
        'docs/',
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
#   4. helm_resource re-renders the srw-config ConfigMap with the new
#      PERSISTENT_AGENT_IMAGE tag
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
# Helm chart — same chart as production. Values stack (last wins):
#   1. helm/values.yaml                      chart defaults
#   2. deployment/values-local.yaml          gitignored — dev secrets +
#                                            sessionRouter + opencloud
#                                            hostAliases (Traefik ClusterIP)
#   3. deployment/values-tilt.yaml           committed — imagePullPolicy
#                                            IfNotPresent + prewarm disabled +
#                                            cockpit envJs mountPath redirect
#
# image_keys auto-substitutes the Tilt-built images. MCP is still on GHCR
# until Slice 4.
# -----------------------------------------------------------------------------
helm_resource(
    'srw',
    chart='./helm',
    namespace='srw',
    flags=[
        '--values=deployment/values-local.yaml',
        '--values=deployment/values-tilt.yaml',
    ],
    image_deps=['srw-orchestrator', 'srw-cockpit', 'srw-agent'],
    image_keys=[
        ('image.orchestrator.repository', 'image.orchestrator.tag'),
        ('image.cockpit.repository', 'image.cockpit.tag'),
        ('image.agent.repository', 'image.agent.tag'),
    ],
)
