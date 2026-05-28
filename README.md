# Superhuman Remote Worker

A self-improving AI agent system. Specialized agents form a continuous innovation cycle: one explores ideas, one tears them apart, one builds the survivors, one curates what was learned. The system gets better on its own.

Built on LangGraph with a config-driven architecture. Same codebase, different YAML configs, different roles. Runs as job-based workers or interactive persistent sessions.

## The Innovation Cycle

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

### 1. Create the cluster

```bash
k3d cluster create srw \
  --servers 1 \
  --port "80:80@loadbalancer" \
  --port "443:443@loadbalancer" \
  --registry-create srw-registry:0.0.0.0:5000
```

Single node, host ports 80/443 mapped to the cluster's traefik, plus a local image registry on `localhost:5000` for later (Tilt/Skaffold workflows).

### 2. Install cert-manager + a mkcert ClusterIssuer

The chart's ingresses request TLS certs via cert-manager. Locally we issue them from your mkcert root CA so browsers trust them without a security warning.

```bash
helm repo add jetstack https://charts.jetstack.io --force-update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.16.2 --set crds.enabled=true

# Wait for cert-manager to be ready
kubectl -n cert-manager rollout status deploy/cert-manager
kubectl -n cert-manager rollout status deploy/cert-manager-webhook
kubectl -n cert-manager rollout status deploy/cert-manager-cainjector

# Wrap mkcert's CA as a cert-manager ClusterIssuer
kubectl -n cert-manager create secret tls mkcert-ca-key-pair \
  --cert="$HOME/.local/share/mkcert/rootCA.pem" \
  --key="$HOME/.local/share/mkcert/rootCA-key.pem"

kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: mkcert-issuer }
spec: { ca: { secretName: mkcert-ca-key-pair } }
EOF
```

### 3. Bootstrap the SRW namespace + VM SSH key secret

The orchestrator mounts a `srw-vm-ssh-key` Secret unconditionally (for VM workspaces in prod). Locally, a dummy keypair is fine.

```bash
kubectl create namespace srw

ssh-keygen -t ed25519 -f /tmp/vm-key -N "" -q
kubectl -n srw create secret generic srw-vm-ssh-key \
  --from-file=id_ed25519=/tmp/vm-key \
  --from-file=id_ed25519.pub=/tmp/vm-key.pub
rm /tmp/vm-key /tmp/vm-key.pub
```

### 4. Copy the local values file and install the chart

```bash
cp deployment/values-local.example.yaml deployment/values-local.yaml
$EDITOR deployment/values-local.yaml   # paste at least one LLM key (OPENAI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY)

helm install srw ./helm -n srw -f deployment/values-local.yaml
```

`deployment/values-local.yaml` is gitignored (it holds your LLM keys). Everything else in it is dev-only stub credentials.

### 5. Wait for pods, then log in

```bash
kubectl -n srw get pods -w
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

### Daily usage

```bash
k3d cluster stop  srw        # frees host ports + resources, preserves PVCs and Helm release
k3d cluster start srw        # back online in seconds
k3d cluster list             # see all clusters and their state
```

Stopping the cluster also frees host port 443 — important if you also access the live homelab cluster from this machine on the same domain.

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
playwright install chromium            # Browser-based research
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

### Workspace-Centric Memory

Long-term memory lives in files, not in the LLM context window:

- `workspace.md` — Injected into every LLM call, survives context compaction
- `plan.md` — Strategic plan, updated at phase boundaries
- `archive/` — Phase retrospectives and completed task lists

Cross-job knowledge sharing uses the **RecallStore** — a hybrid dense+sparse search system (pgvector) with TTL-managed memories scoped to projects. An auxiliary LLM extracts memories asynchronously while the agent continues working.

This means the agent can work on tasks that exceed any single context window, and knowledge accumulates across jobs.

## Debugging

- **Workspace files** (inside the workspace container, SSH in to look): `workspace.md`, `todos.yaml`, `plan.md`, `output/`
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

Creative Commons Attribution 4.0 International License (CC BY 4.0). See [LICENSE.txt](LICENSE.txt).
