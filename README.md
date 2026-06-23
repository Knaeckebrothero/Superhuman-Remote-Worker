# Project SRW

> **Goal: a foundation for artificial intelligence to act in the digital world — functionally and safely.**

"SRW" stands for *Superhuman Remote Worker* — the north star, not a description of today. The name is the goal: an AI you can hand real work and a machine to do it on. This repository builds the layer *underneath* that goal: the model-agnostic substrate any such system needs, whichever model does the thinking.

Why a foundation and not just a smarter model? A decision isn't something a model "has"; it emerges when a predictive core is wired to a value signal and a selection loop. Today that core is an LLM predicting tokens — tomorrow it may be a world model predicting latent state, or a vision-language-action model predicting motor commands, with state shifting from text to vectors to frames. What *doesn't* change is everything around the core: a machine to run on, connectors to act through, durable memory and state, observability, scaling, cooperation between agents, and the guardrails that keep it safe. The cognitive core is a swappable organ; the harness is the durable part — and it's where the decision-making loop actually lives. In effect, SRW is an operating system for AI to interact with the digital world.

Concrete capabilities are **milestones** toward that foundation, not the point in themselves. The flagship milestone today is the self-improving loop.

## Milestone: The Self-Improving Loop

A self-improving AI agent system. Specialized agents form a continuous innovation cycle: one explores ideas, one tears them apart, one builds the survivors, one curates what was learned. The system gets better on its own.

Built on LangGraph with a config-driven architecture. Same codebase, different YAML configs, different roles. Runs as job-based workers or interactive persistent sessions.

### The Innovation Cycle

The typical human-AI workflow looks like this: you have an idea, you dump it on the AI, the AI builds it, then you spend forever refactoring because your inner perfectionist won't let you merge something that works but isn't elegant. Repeat.

This system replaces that loop with four agents that run it continuously:

```
         ┌──────────────────────────────────────────────────┐
         │                                                  │
         ▼                                                  │
   ┌──────────┐     writes ideas     ┌──────────┐          │
   │  SCHOLAR │ ──────────────────► │  CRITIC  │          │
   │          │                      │          │          │
   │ Explores │     reviews &        │ Reviews  │          │
   │ the web, │     rates them       │ rejects  │          │
   │ codebase,│ ◄────────────────── │ or       │          │
   │ papers,  │  feedback/issues     │ approves │          │
   │ logs     │                      └────┬─────┘          │
   └──────────┘                           │                │
                              approved ideas               │
                              & issue fixes                │
                                          │                │
                                          ▼                │
                                   ┌──────────┐            │
                                   │DEVELOPER │            │
                                   │          │  code changes feed
                                   │ Builds,  │ ───────────┤
                                   │ tests,   │            │
                                   │ ships    │            │
                                   │ PRs      │            │
                                   └──────────┘            │
                                                           │
                                   ┌──────────┐            │
                                   │ CURATOR  │            │
                                   │          │ ───────────┘
                                   │ Extracts │  knowledge feeds
                                   │ insights │  back into the cycle
                                   │ into KB  │
                                   └──────────┘
```

**Scholar** — The idea factory. Continuously scans the web, digs through the codebase, analyzes past agent runs, and runs experiments. Produces a high volume of idea artifacts. Doesn't self-filter — that's the Critic's job. Also takes inspiration from issues and feedback the Critic raises about the existing codebase.

**Critic** — The quality gate. Reads Scholar's proposals, reviews code diffs, audits the codebase for tech debt, runs tests. Everything gets a verdict: APPROVED, REJECTED, or NEEDS INVESTIGATION. Harsh, direct, evidence-based. Every claim cites file:line. No approval with failing tests.

**Developer** — The PR factory. Picks up approved ideas and Critic-identified issues, delegates implementation to Claude Code sessions, verifies results via git, and ships focused PRs. One feature per PR, one bug fix per PR. Throughput over perfection.

**Curator** — The knowledge extractor. Runs as a subjob after other jobs complete, extracting structured insights from job artifacts into the project knowledge base. Ensures learnings from one job are available to future jobs.

The cycle repeats. Scholar sees the new code and finds more to explore. Critic reviews what Developer shipped. Developer picks up the next batch. Curator distills what was learned. The system improves because the loop never stops.

### Why This Works

The problem with human-in-the-loop AI development isn't the AI — it's the human bottleneck. You can only review so fast, you get attached to your ideas, and your perfectionism stalls shipping.

This system applies evolutionary pressure:
- **Random mutation** — Scholar generates volume, most of it mediocre, some of it brilliant
- **Selection pressure** — Critic filters ruthlessly, only the good stuff survives
- **Implementation** — Developer builds the survivors, fast and focused

No human bottleneck in the loop. You set the direction, the system iterates.

### Beyond the Cycle

Not everything fits the innovation loop. The system also has agents for direct interaction and design work:

**General Secretary** (`config/defaults.yaml`) — Jack-of-all-trades with all tools enabled and no specialization. The agent you talk to directly for ad-hoc tasks. The escape hatch for "just do this thing."

**Interactive** — Conversational assistant for persistent sessions. No phase/todo structure — continuous tool-calling loop with WebSocket transport. For when you need an agent that stays online and responds in real time.

**Designer** — UI/UX design specialist that creates self-contained HTML/CSS mockups using the project's design system. Analyzes interface patterns and produces structured design specifications.

**Designer-Interactive** — Same design capability as Designer, but runs as a persistent session for real-time collaborative design iteration.

## What It Can Do

Each agent is a general-purpose LangGraph worker that can:
- Research topics on the web and synthesize findings
- Browse websites and interact with web pages (Playwright)
- Process and analyze documents (PDF, DOCX, PPTX, images)
- Query and manipulate databases (PostgreSQL, Neo4j, MongoDB)
- Write, review, and manage structured output
- Execute multi-step workflows with checkpointing and crash recovery
- Manage citations and literature

**What makes it different:**
- **Two operating modes** — job-based workers for batch tasks and persistent sessions for real-time interaction, both from the same codebase
- **Persistent memory** — workspace files survive context window limits, plus a hybrid dense+sparse memory system (RecallStore) for cross-job knowledge sharing
- **Phase-based execution** — alternates between strategic planning and tactical work, adapting its plan as it learns
- **Crash recovery** — checkpoints at every step, resume any job from where it left off
- **Config-driven roles** — same codebase, different YAML configs for different specializations
- **Multi-database support** — attach PostgreSQL, Neo4j, or MongoDB datasources to any job
- **Multi-backend workspaces** — local filesystem, SSH/SFTP to remote containers, or dedicated VMs via QEMU/KubeVirt
- **Notifications** — email (SMTP), Slack, Discord, and ntfy webhooks for job status updates

## Table of Contents

- [Quick Start](#quick-start)
- [Docker Compose Deployment](#docker-compose-deployment)
- [Local Kubernetes Setup (k3d)](#local-kubernetes-setup-k3d)
- [Development Setup](#development-setup)
- [Architecture](#architecture)
- [Debugging](#debugging)
- [License](#license)

### Which path should I use?

| Goal | Path |
|------|------|
| Iterate on Python code (orchestrator, agent) as fast as possible | [Development Setup](#development-setup) — services run natively on the host with `uvicorn --reload` / `npm start` |
| Run the full stack without Kubernetes (smaller deployments, no k8s API) | [Docker Compose Deployment](#docker-compose-deployment) |
| Reproduce the production Helm chart locally to test K8s-specific code paths (provisioners, ingress, cert-manager, OIDC, etc.) | [Local Kubernetes Setup (k3d)](#local-kubernetes-setup-k3d) |

## Quick Start

```bash
# Clone and set up
git clone <repo-url>
cd Superhuman-Remote-Worker
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Add your API keys

# Start databases + workspace containers
podman-compose -f docker-compose.dev.yaml up -d
python init.py

# Start the orchestrator (terminal 1)
uvicorn orchestrator.main:app --reload --port 8085

# Start the agent server (terminal 2) — --loop keeps it alive between jobs
python agent.py --port 8001 --loop

# Submit a job via the Cockpit UI (http://localhost:4200) or the orchestrator REST API
```

## Docker Compose Deployment

Deploy the complete system using containers. This is the simplest deployment option — no Kubernetes required. The orchestrator auto-detects the environment and uses static workspace pools instead of dynamic pod provisioning. See [`docs/docker_compose_mode.md`](docs/docker_compose_mode.md) for architecture details.

For Kubernetes deployment (recommended for production), see [`docs/deployment.md`](docs/deployment.md) and the `deployment/` or `deployment-local/` directories.

### 1. Clone and Configure

```bash
git clone <repo-url>
cd Superhuman-Remote-Worker
cp .env.example .env
```

### 2. Edit Environment Variables

Edit `.env` with your configuration:

**Required:**
- `OPENAI_API_KEY` — LLM API key (or compatible provider)
- `LLM_BASE_URL` — Custom endpoint URL (if using self-hosted models)

**Optional:**
- `ANTHROPIC_API_KEY` — For Claude models
- `GOOGLE_API_KEY` — For Gemini models
- `TAVILY_API_KEY` — For web search
- `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD` — SSO admin (default: admin/admin)
- `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` — Email notifications via SMTP
- `SLACK_WEBHOOK_URL` — Slack notifications
- `DISCORD_WEBHOOK_URL` — Discord notifications
- `NTFY_URL` / `NTFY_TOPIC` / `NTFY_TOKEN` — ntfy push notifications
- `LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR (default: INFO)

Edit `docker/keycloak/realm-export.json` for OIDC client secrets if needed (see the example file for details).

### 3. Start All Services

```bash
podman-compose up -d
```

This starts:
- **PostgreSQL** — Job tracking and data storage (+ SSO databases for Keycloak/Nextcloud)
- **PostgreSQL (Vector)** — Citations, embeddings, knowledge index (pgvector)
- **MongoDB** — LLM request logging and audit trail
- **Neo4j** — Graph database for project knowledge base
- **Keycloak** — SSO identity provider (OIDC for all services)
- **Gitea** — Git server for agent workspace repositories (Keycloak OIDC login)
- **Nextcloud** — Cloud storage / WebDAV datasource
- **VPN Sidecars** — Three VPN services routing LLM, research, and workstation traffic through the university network
- **Orchestrator** — Backend API for job management and agent coordination
- **Agent** — Worker instances (defaults to 2 replicas via `AGENT_REPLICAS`)
- **Workspace containers** — Static pool of 5 isolated workspace containers (SSH access), auto-provisioned by the orchestrator
- **MCP Server** — Claude Code integration (port 8055)
- **Cockpit** — Web UI for job management and monitoring
- **NATS** — Messaging for VM lifecycle and agent communication
- **MinIO** — S3-compatible object storage for VM snapshots and IDE sessions
- **pgAdmin / mongo-express** — Database admin UIs
- **Dozzle** — Container log viewer

### 4. Access Services

| Service | URL |
|---------|-----|
| Cockpit (Web UI) | http://localhost:4000 |
| Keycloak SSO | http://localhost:8180 |
| Orchestrator API | http://localhost:8085 |
| Gitea | http://localhost:3000 |
| Nextcloud | http://localhost:8800 |
| MCP Server | http://localhost:8055 |
| pgAdmin | http://localhost:5050 |
| MinIO Console | http://localhost:9001 |
| Dozzle (logs) | http://localhost:9999 |

### 5. Common Operations

```bash
# View logs
podman-compose logs -f
podman-compose logs -f agent

# Scale workers
podman-compose up -d --scale agent=4

# Stop all services
podman-compose down

# Stop and remove all data
podman-compose down -v
```

For local builds (no GHCR access), use `docker-compose.local.yaml` instead — it builds all custom images from source.

## Local Kubernetes Setup (k3d)

Runs the **production Helm chart** on a local [k3d](https://k3d.io) cluster — k3s in Docker. Use this when you need to test K8s-specific code paths (workspace pod provisioning, ingress routing, cert-manager + OIDC, the Keycloak realm/client flow, etc.). The cluster can be started and stopped like any Docker container, so it doesn't consume resources when you're not working.

### Prerequisites

Install on the host (Fedora 43 commands shown; adapt for your distro):

```bash
# Docker Engine (k3d runs k3s as Docker containers)
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker

# kubectl + helm from Fedora repos
sudo dnf -y install kubernetes-client helm

# k3d (no RPM, official installer)
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | sudo bash

# mkcert — issues a local CA that browsers + the cluster will trust
sudo dnf -y install mkcert nss-tools
mkcert -install                                                       # user trust + Firefox NSS
sudo CAROOT="$HOME/.local/share/mkcert" mkcert -install                # system trust + Chrome NSS (must use the same CAROOT)
```

Sanity check: `docker run --rm hello-world`, `k3d version`, `kubectl version --client`, `helm version`, `mkcert -CAROOT`.

### 1. Bootstrap the cluster

```bash
./scripts/local-dev-up.sh
```

Idempotent — re-runs are safe. Creates the k3d cluster (host ports 80/443 → Traefik, local image registry on `localhost:5005`), installs cert-manager, registers a `mkcert-issuer` ClusterIssuer wrapping your mkcert root CA, creates the `srw` namespace, and seeds a dummy `srw-vm-ssh-key` Secret (the orchestrator mounts it unconditionally for VM workspaces; locally a stub is fine).

The script's behaviour is documented inline; if anything fails it bails out with a clear message rather than silently continuing.

### 2. Set the OpenCloud OIDC workaround address

OpenCloud's proxy needs to reach Keycloak from inside the pod, and on k3d `auth.localhost` resolves to the pod's own loopback. `values-local.example.yaml` ships a `hostAliases` block that maps `auth.localhost` to Traefik's ClusterIP — but that IP is determined at cluster-create time, so grab it now:

```bash
kubectl --context=k3d-srw -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}'
```

Note the value — you'll paste it into `values-local.yaml` in the next step (under `opencloud.hostAliases[0].ip`). It's stable for the life of the cluster and only needs to be re-grabbed after `k3d cluster delete && create`.

### 3. Mint the session-router JWT secret

The orchestrator mints short-lived JWTs to authorize browser → agent WebSocket handshakes. Without this Secret, `/api/sessions/{tid}/connection` 500s and sessions never come up.

```bash
kubectl --context=k3d-srw -n srw create secret generic srw-session-jwt \
  --from-literal=jwt-secret="$(openssl rand -base64 48 | tr -d '\n' | head -c 64)"
```

### 4. Copy the values template and install the chart

```bash
cp deployment/values-local.example.yaml deployment/values-local.yaml
$EDITOR deployment/values-local.yaml
# - paste at least one LLM key (OPENAI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY)
# - paste the Traefik ClusterIP from step 2 into opencloud.hostAliases[0].ip

helm install srw ./helm -n srw --kube-context=k3d-srw -f deployment/values-local.yaml
```

`deployment/values-local.yaml` is gitignored (it holds your LLM keys). Everything else in it is dev-only stub credentials.

### 5. Wait for pods, then log in

```bash
kubectl --context=k3d-srw -n srw get pods -w
# Ctrl-C once all pods are 1/1 Running (the orchestrator takes longest — ~5 init containers chained on database/keycloak/gitea readiness)
```

Open `https://localhost/` in your browser and log in:

| Username | Password |
|----------|----------|
| `test`   | `test`   |

The `test` user is pre-seeded in the Keycloak realm with `admin` + `user` roles, email already verified, mapped to `srw-admin` on Gitea — no approval flow, no email verification step.

| URL | What it is |
|-----|-----------|
| `https://localhost/`        | Cockpit (the UI) |
| `https://api.localhost/`    | Orchestrator REST API |
| `https://auth.localhost/`   | Keycloak (admin console at `/admin`) |
| `https://git.localhost/`    | Gitea |
| `https://cloud.localhost/`  | OpenCloud |
| `https://mcp.localhost/`    | MCP server |

`*.localhost` resolves to `::1` automatically (RFC 6761 + glibc `myhostname` NSS), so there's no DNS config to do.

### Smoke-testing the install

Quick checklist after a fresh `helm install` (or after recreating the cluster). Each step exercises an independent slice of the stack.

**1. Cockpit + Keycloak login** — open `https://localhost/`, log in as `test`/`test`. Lands on the Sessions list (`/sessions`). If you land in a refresh loop, jump to the matching troubleshooting entry below.

**2. Sessions (persistent agent + WS)** — Sessions → **New Session** → pick any expert (e.g. Scholar) → **Create Session**. Expected sequence in the UI:

- "Creating thread" ✓ within 1 s
- "Provisioning agent" ✓ within ~10 s (k8s pulls the agent + workspace images on the first run)
- "Booting agent runtime" ✓
- "Establishing connection" ✓ → "What shall we conquer today?" greeting

Type a one-liner and send it. If you have a working LLM key, the assistant streams a reply within seconds. If the only error in the right-rail is *"Incorrect API key provided…"*, the platform is fine — you just need to set a real LLM key in `values-local.yaml` and `helm upgrade`.

You can also confirm the agent and workspace pods exist:

```bash
kubectl --context=k3d-srw -n srw get pods -l app.kubernetes.io/component=agent
kubectl --context=k3d-srw -n srw get pods -l app.kubernetes.io/component=workspace
```

**3. Jobs (worker dispatch)** — Create → enter any short description → pick an expert → **Create Job**. Expected:

- A row appears under Jobs with status `created` then `processing`.
- A worker agent pod (`srw-agent-j-*`) spins up.
- Status flips to `completed` or `failed` depending on whether the LLM key works. Either is a pass for *infrastructure*: it proves the orchestrator dispatched, the agent provisioned, the completion callback fired, and the pod recycled.

**4. Gitea SSO** — open `https://git.localhost/`, click **Sign In** → **Sign in with Keycloak**. Should land on `test - Dashboard` without a manual credentials step.

**5. OpenCloud SSO** — open `https://cloud.localhost/`. Should redirect through Keycloak and land on `Personal - OpenCloud` (`/files/spaces/personal/test`). If you land on `/access-denied`, the OpenCloud OIDC workaround isn't applied — see Troubleshooting (almost always: stale `hostAliases` IP).

### Daily usage

```bash
k3d cluster stop  srw        # frees host ports + resources, preserves PVCs and Helm release
k3d cluster start srw        # back online in seconds
k3d cluster list             # see all clusters and their state
```

Stopping the cluster also frees host port 443 — important if you also access the live homelab cluster from this machine on the same domain.

### Fast inner loop with Tilt (recommended)

Tilt watches the repo and live-syncs source files into the running pods (or rebuilds + rolls them for the agent), so edits take effect locally in seconds — no commit → CI → image-build → Fleet-sync → rollout round trip. **All four components are covered**:

| Component | Loop | Edit-to-effect |
|-----------|------|-----------------|
| Orchestrator | `live_update` sync + `uvicorn --reload` | ~3 s |
| Cockpit | `live_update` sync + `ng serve` HMR | ~5 s (~36 ms ng compile + Vite push) |
| MCP | `live_update` sync + `watchfiles` wrapper | ~10–15 s (watchfiles + Tilt debounce) |
| Agent | full image rebuild + helm fan-out + Reloader bounce | ~50 s (~8 s warm docker build + orchestrator restart) |

Tilt is opt-in but is now the **default development workflow**. The `helm install` path above still works standalone for people without Tilt installed. Design and rationale: [`docs/features/tilt_inner_loop_dev.md`](docs/features/tilt_inner_loop_dev.md).

**One-time install** (binary to `~/.local/bin/`, no sudo):

```bash
TILT_VER=0.37.3
curl -fsSL https://github.com/tilt-dev/tilt/releases/download/v${TILT_VER}/tilt.${TILT_VER}.linux.x86_64.tar.gz \
  | tar -xz -C ~/.local/bin/ tilt
chmod +x ~/.local/bin/tilt
tilt version
```

**Run Tilt — first time (or after `k3d cluster delete && create`)**:

```bash
./scripts/local-dev-tilt-up.sh
```

This bootstrap is idempotent — it runs `scripts/local-dev-up.sh` underneath (cluster + cert-manager + namespace + vm-ssh-key Secret), then adds the `srw-session-jwt` Secret, syncs the current Traefik ClusterIP into `values-local.yaml`'s `opencloud.hostAliases` entry, mirrors the MinIO images into the cluster registry (the `virtual` workspace tier and workspace snapshots / IDE-session blobs run on a single-node MinIO fixture, `deployment/tilt-minio.yaml`, deployed by the Tiltfile — k3d's node has no external DNS, so the images must be pre-loaded into the registry), and finally runs `tilt up` in the foreground.

**Run Tilt — subsequent sessions**: the cluster, secrets, and Helm release persist across `k3d cluster stop/start`, and the Traefik ClusterIP is stable for the life of the cluster, so you don't need the bootstrap again. Just bring the cluster back and start Tilt directly (always cluster first, then Tilt — Tilt deploys *into* a running cluster):

```bash
k3d cluster start srw   # if it was stopped
tilt up                 # from the repo root; Tilt UI at https://localhost:10350
```

Press Ctrl-C to stop Tilt (the cluster keeps running; use `k3d cluster stop srw` to stop that too). The bootstrap script is always safe to re-run if you're unsure — it skips anything already in place.

#### The Plan → Develop → Verify workflow

The point of having Tilt + a local prod-parity cluster is that **every change can be verified locally before it ships**. The loop:

1. **Plan**: design doc under `docs/features/` or `docs/issues/` (whatever fits). Get alignment on scope + acceptance before you start editing.
2. **Develop**: edit the relevant source. Tilt handles the rebuild/sync automatically — watch the Tilt UI at `https://localhost:10350` for the affected resource going green again.
3. **Verify locally** — do not push until this passes:
   - **Unit/lint tests** at file granularity:
     - Python: `pytest tests/test_<area>.py -x -q --tb=short` + `ruff check src/ orchestrator/ tests/`
     - Cockpit: `cd cockpit && npm test` or a single spec via `npx vitest run src/path/to/foo.spec.ts`
   - **Cockpit / UX changes**: open `https://localhost/` in a browser (or Playwright), exercise the actual feature, watch the cockpit pod's `ng serve` logs in the Tilt UI for HMR updates.
   - **Orchestrator / API changes**: hit the live endpoint from inside the cluster — `kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- curl -sf http://localhost:8085/api/...` — or from a logged-in cockpit, or via cookies set after `test`/`test` login.
   - **Agent / graph changes**: create a session or job through cockpit (UI → New Session / New Job), then `kubectl --context=k3d-srw -n srw logs -l srw/managed-by=agent-provisioner -f` to watch the spawned agent pod react.
   - **MCP / tool changes**: `kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- curl -sf http://srw-mcp:8055/health`. For actual tool calls, port-forward and point a Claude Code MCP client at the local URL: `kubectl --context=k3d-srw -n srw port-forward svc/srw-mcp 8055:8055`.
   - **Cross-component flows** (e.g. orchestrator → agent → workspace): walk the README smoke test under "Local Kubernetes Setup (k3d)" — login, new session, new job — and confirm pod states with `kubectl get pods -n srw`.
4. **Commit + push** only after local verification passes. The CI/CD path (build → GHCR → Fleet sync → homelab rollout) takes ~30 min, so catching a regression locally first is several rounds of feedback faster than catching it on the dev cluster.

A failed verification ≠ ready to push. Iterate on local until the smoke test you'd want a reviewer to run is green.

#### Per-component edit signals

What to watch for in the Tilt UI / pod logs to confirm an edit actually took effect:

- **Orchestrator** (`orchestrator/*.py`, `src/*.py`, `config/*`): orchestrator pod log shows `WatchFiles detected changes in '/app/...'` followed by a uvicorn worker re-import. Hit `kubectl ... curl http://localhost:8085/api/health` to confirm.
- **Cockpit** (`cockpit/src/**/*.ts|html|scss`): cockpit pod log shows `Component update sent to client(s)` (CSS) or `Page reload required` (TS/HTML). Browser auto-refreshes within ~5 s. If `[vite] connected.` is missing from the browser console, the HMR WebSocket dropped — refresh.
- **MCP** (`orchestrator/mcp/*.py`, `orchestrator/services/formatters.py`): mcp pod log shows `watchfiles: N changes detected` followed by the FastMCP banner with the (potentially updated) server name. `/health` returns 200 within ~10 s.
- **Agent** (`src/*.py`, `config/*`, `agent.py`): srw-agent image rebuilds (visible in Tilt UI), helm upgrade fans the new `tilt-<hash>` tag into `srw-config`'s `PERSISTENT_AGENT_IMAGE`, Stakater Reloader rolls the orchestrator, **next** session/job picks up the new code. Existing agent pods are unaffected (they hold the old image — agent pods are per-job, not long-running).
- **Requirements / Dockerfile edits**: trigger `fall_back_on` → full image rebuild + roll. Cold rebuild is ~3-5 min the first time after a `tilt down`; warm is ~10-20 s thanks to the cache mounts in `Dockerfile.*.dev`.

**Common operations under Tilt**:

```bash
tilt down                                    # stop watching + remove the Helm release
tilt trigger srw-agent                       # force-rebuild any component
tilt args -- --port 10351                    # run the Tilt UI on a different port
```

**Known limitations**:

- `uvicorn --reload` doesn't reload changes to the lifespan/startup phase — if you edit `lifespan()` itself, force a restart from the Tilt UI.
- Agent pods are baked at provision time (`restartPolicy: Never`) — a `src/` edit affects the *next* session, not in-flight agent pods. End an active session and start a new one to pick up the change.
- Reloader bouncing the orchestrator on an agent image change incurs a ~30 s orchestrator restart. Acceptable for dev, ugly if you're hammering agent edits.

### Updating the install

After editing `values-local.yaml`:

```bash
helm upgrade srw ./helm -n srw -f deployment/values-local.yaml
```

After editing the chart itself (templates under `helm/`):

```bash
helm upgrade srw ./helm -n srw -f deployment/values-local.yaml
# For some changes (env.js ConfigMap edits), force a pod restart:
kubectl -n srw rollout restart deploy/srw-cockpit
```

### Full teardown / reset

```bash
helm uninstall srw -n srw                  # remove the release (PVCs are kept by chart annotation)
kubectl delete namespace srw               # nuke everything including PVCs and the vm-ssh-key Secret
k3d cluster delete srw                     # destroy the whole cluster (including the local registry)
```

### Troubleshooting

- **Browser shows "Not secure"** — `sudo mkcert -install` was run *without* `CAROOT`, so root created a *second* CA. Re-run it as shown in Prerequisites. Then restart the browser (Firefox/Chrome cache the NSS db at process start).
- **`localhost` opens the wrong cluster (e.g., your homelab cockpit)** — the local LAN DNS may map your prod domain's AAAA record to `::1`, which k3d also binds. Stop k3d (`k3d cluster stop srw`) when you're not using it, or recreate the cluster with IPv4-only port binds (`--port "0.0.0.0:443:443@loadbalancer"`).
- **`ImagePullBackOff` with 401/403** — your GHCR packages are private. Create a pull secret and uncomment `global.imagePullSecrets` in `values-local.yaml`:
  ```bash
  kubectl -n srw create secret docker-registry ghcr-pull-secret \
    --docker-server=ghcr.io \
    --docker-username=<github-user> --docker-password=<github-PAT-with-read:packages>
  ```
- **`PersistentVolumeClaim ... is invalid: ... storage: Forbidden: field can not be less than previous value`** — you tried to shrink a PVC. Either bump the size back up in `values-local.yaml` or delete the PVC and re-upgrade (`kubectl -n srw delete pvc <name>`).
- **Keycloak in `CreateContainerConfigError` for missing secret keys** — the realm import references env vars for every OIDC client (even disabled ones). Check that all keys from `values-local.example.yaml` are present in `values-local.yaml`.
- **`https://localhost/` returns 404 right after `k3d cluster start srw`** — Traefik's endpoint discovery can go stale through a long idle (the pod restart cascade after `k3d cluster stop`/`start` sometimes leaves Traefik holding empty endpoint state). Kick it: `kubectl --context=k3d-srw -n kube-system rollout restart deploy/traefik`.
- **Login lands in a 401-refresh loop in Brave/Firefox** — the chart defaults to `auth.bff.sameOriginApi: true` in `values-local.example.yaml` so the cockpit and BFF share an origin and the session cookie is first-party. If you flipped that off, either flip it back on or allowlist `[*.]localhost` for cookies in the browser. Symptom in orchestrator logs: `GET /auth/callback 302` immediately followed by `GET /api/auth/me 401` on repeat.
- **New session stuck on "Provisioning agent" / WebSocket errors `Unexpected response code: 200`** — usually the cockpit's Service Worker (`ngsw-worker.js`) is serving stale assets that point at the legacy `/ws/persistent/...` WS path (which the orchestrator no longer hosts). In DevTools → Application → Service Workers, **Unregister** all SWs and clear caches under Storage, then hard-reload. Programmatic version:
  ```js
  (await navigator.serviceWorker.getRegistrations()).forEach(r => r.unregister());
  (await caches.keys()).forEach(k => caches.delete(k));
  ```
- **OpenCloud lands on `/access-denied`, logs show `connect: connection refused` to `auth.localhost`** — the `hostAliases` IP in `values-local.yaml` no longer matches Traefik's ClusterIP (typically after `k3d cluster delete && create`). Re-grab it and `helm upgrade`:
  ```bash
  kubectl --context=k3d-srw -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}'
  $EDITOR deployment/values-local.yaml   # update opencloud.hostAliases[0].ip
  helm upgrade srw ./helm -n srw --kube-context=k3d-srw -f deployment/values-local.yaml
  kubectl --context=k3d-srw -n srw rollout restart deploy/srw-opencloud
  ```
- **`/api/sessions/.../connection` returns 500 with `'NoneType' object has no attribute 'mint'`** — `srw-session-jwt` Secret was never created. See setup step 3.
- **Cluster looks healthy but the cockpit / orchestrator behave oddly after pulling latest develop** — the published GHCR `:latest` images can lag the chart on `develop` HEAD. If you suspect skew (e.g., a recently-merged backend change has no effect, or the cockpit JS bundle is missing a route the chart now expects), rebuild affected images locally and point the chart at them:
  ```bash
  docker build -f docker/Dockerfile.orchestrator -t srw-orchestrator:local-fix .
  k3d image import srw-orchestrator:local-fix -c srw
  ```
  Then set `image.orchestrator.{repository,tag,pullPolicy: IfNotPresent}` in `values-local.yaml` (the example file has a commented template). Same shape for cockpit and agent.

## Development Setup

Run databases in containers while developing locally with Python.

### 1. Set Up Python Environment

```bash
git clone <repo-url>
cd Superhuman-Remote-Worker

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# System dependencies (Fedora)
sudo dnf install poppler-utils         # PDF rendering
# Debian/Ubuntu: sudo apt-get install poppler-utils
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API credentials
```

### 3. Start Databases

```bash
podman-compose -f docker-compose.dev.yaml up -d
```

### 4. Initialize

```bash
python init.py                          # Initialize everything
python init.py --force-reset            # Reset everything (WARNING: deletes all data)
python init.py --only-orchestrator      # Databases only
python init.py --only-agent             # Workspace only
```

### 5. Run the Agent

The agent is always driven by the orchestrator over HTTP — there is no CLI
mode that processes jobs directly. Start the agent as a server and let the
orchestrator dispatch work to it:

```bash
# Dual-mode server (accepts jobs and persistent sessions — default)
python agent.py --port 8001 --loop

# Worker-only (jobs, no persistent sessions)
python agent.py --mode worker --port 8001 --loop

# Persistent-only interactive agent
python agent.py --mode persistent --port 8002 --loop

# With a non-default agent config
python agent.py --config scholar --port 8001 --loop

# Debug mode
LOG_LEVEL=DEBUG python agent.py --port 8001 --loop
```

Why `--loop`: without it the agent process exits after the first job, which
is fine in K8s (pod restart) but kills the dev loop on bare metal or Docker
Compose. Jobs, documents, descriptions, git URLs, feedback, and freeze
approvals are all submitted to the orchestrator (REST API or Cockpit UI)
and dispatched to the running agent — not passed as CLI flags.

The workspace always lives in an SSH-accessible container or VM; the agent
process never operates on its own filesystem. The orchestrator injects the
SSH credentials at dispatch time.

### 6. Backup and Restore

```bash
python init.py --create-backup                          # Auto-named backup
python init.py --create-backup before_experiment        # Named backup
python init.py --restore-backup backups/20260201_001    # Restore
```

### 7. Testing

```bash
pytest tests/                              # All tests
pytest tests/test_graph.py -v              # Single file
pytest tests/ -k "todo"                    # Pattern match
pytest tests/ --cov=src                    # With coverage
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                           COCKPIT                                │
│              (Web UI — Jobs, Threads, Monitoring)                 │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
                 ┌───────────────────────────┐
                 │       ORCHESTRATOR        │
                 │                           │
                 │  Job queue & coordination │
                 │  Agent health monitoring  │
                 │  Persistent sessions      │
                 │  Statistics & API         │
                 └─────────────┬─────────────┘
                               │
       ┌───────────┬───────────┼───────────┬───────────┐
       │           │           │           │           │
       ▼           ▼           ▼           ▼           ▼
 ┌──────────┐┌──────────┐┌─────────┐┌─────────┐┌───────────┐
 │ SCHOLAR  ││  CRITIC  ││DEVELOPER││ CURATOR ││ DESIGNER  │
 │          ││          ││         ││         ││           │
 │ R&D,     ││ Review,  ││ Claude  ││ Extract ││ UI/UX     │
 │ ideas,   ││ quality  ││ Code    ││ insights││ mockups,  │
 │ research ││ gating   ││ PRs     ││ into KB ││ prototypes│
 └──────────┘└──────────┘└─────────┘└─────────┘└───────────┘
       │           │           │           │
       ▼           ▼           ▼           ▼
 ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
 │ PostgreSQL│ │  MongoDB  │ │   Neo4j   │ │Datasources│
 │ (system)  │ │ (logging) │ │(knowledge)│ │ (per-job) │
 └───────────┘ └───────────┘ └───────────┘ └───────────┘
```

### Expert Lineup

| Expert | Config | Mode | Role |
|--------|--------|------|------|
| **General Secretary** | `config/defaults.yaml` | Worker | Default — all tools, no specialization, ad-hoc tasks |
| **Scholar** | `config/experts/scholar/` | Worker | R&D exploration, idea generation, web research, paper analysis |
| **Critic** | `config/experts/critic/` | Worker | Code review, proposal review, codebase audits, test execution |
| **Developer** | `config/experts/developer/` | Worker | Claude Code delegation, PR factory, implementation |
| **Curator** | `config/experts/curator/` | Worker | Knowledge extraction from job artifacts into project KB |
| **Designer** | `config/experts/designer/` | Worker | UI/UX design, HTML/CSS mockups, design specifications |
| **Interactive** | `config/experts/interactive/` | Persistent | Conversational assistant, real-time tool use via WebSocket |
| **Designer-Interactive** | `config/experts/designer-interactive/` | Persistent | Collaborative design iteration in real-time sessions |

All experts share the same universal agent codebase. Worker-mode experts extend `config/defaults.yaml`, persistent-mode experts extend `config/persistent_defaults.yaml`. Both use `$extends` for deep-merge inheritance. See [config/README.md](config/README.md) for details.

### Two Operating Modes

**Worker mode** (job-based) — Phase-alternating execution for batch tasks:

- **Strategic phase** — Reviews progress, reflects on what worked, updates the plan, creates the next batch of tasks
- **Tactical phase** — Executes tasks using domain-specific tools until all todos are complete
- This loop continues until the job is done. Each phase boundary creates a snapshot for recovery.

**Persistent mode** (session-based) — Continuous tool-calling loop for interactive use:

- No phase/todo structure. The agent stays online and responds in real time via WebSocket.
- Supports idle timeout handling and memory injection across turns.
- Used by Interactive and Designer-Interactive experts.

Agents run in `dual` mode by default, accepting both jobs and persistent sessions.

### Knowledge & Memory

Long-term memory lives outside the LLM context window in two always-on systems, both injected into every call as transient messages:

- **Knowledge base** — Project-scoped notes written via `kb_write` (decisions, learnings, facts), retrieved by hybrid search. Shared across jobs in a project.
- **Memory system (RecallStore)** — Hybrid dense+sparse search (pgvector) with TTL-managed memories scoped to projects. An auxiliary LLM extracts memories asynchronously while the agent continues working.

File-based artifacts complement these:

- `plan.md` — Strategic plan, updated at phase boundaries
- `notes/` — Working notes the agent writes during a job
- `datasources.md` — Connection reference (names, repo clone paths, kube contexts) for attached datasources
- `archive/` — Phase retrospectives and completed task lists

This means the agent can work on tasks that exceed any single context window, and knowledge accumulates across jobs.

## Debugging

- **Workspace files** (inside the workspace container, SSH in to look): `plan.md`, `todos.yaml`, `notes/`, `datasources.md`, `output/`
- **Checkpoints**: `workspace/checkpoints/job_<id>.db` (SQLite, on the agent host)
- **Logs**: `workspace/logs/job_<id>.log`
- **Phase snapshots**: `workspace/phase_snapshots/job_<id>/phase_<n>/`
- **Persistent sessions**: `workspace/messages/<thread_id>/`

Job lifecycle actions (resume, recover-to-phase, approve-frozen, inject feedback) are all driven through the orchestrator REST API or the Cockpit UI — there are no CLI flags on `agent.py` for them. See the `/api/jobs/` endpoints in `orchestrator/main.py` or the job detail page in Cockpit.

```bash
# Clean up local checkpoint/log state
rm workspace/checkpoints/job_*.db workspace/logs/job_*.log
```

## License

Licensed under the [Functional Source License, Version 1.1 (FSL-1.1-ALv2)](LICENSE) — a source-available license permitting use, modification, and redistribution for any purpose **except competing with the Software**, with each release converting to the Apache License 2.0 two years after its publication.

Third-party components bundled into our images (and their upstream NOTICE
obligations) are inventoried in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
Server images the Helm chart pulls from public registries (Neo4j, PostgreSQL,
MongoDB, …) arrive under their own upstream licenses and are not redistributed by
this project.
