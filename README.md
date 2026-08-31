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
         ┌─────────────────────────────────────────────────┐
         │                                                 │
         ▼                                                 │
   ┌──────────┐     writes ideas     ┌──────────┐          │
   │  SCHOLAR │ ───────────────────► │  CRITIC  │          │
   │          │                      │          │          │
   │ Explores │     reviews &        │ Reviews  │          │
   │ the web, │     rates them       │ rejects  │          │
   │ codebase,│ ◄─────────────────── │ or       │          │
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

**General Worker** — The database-backed application default for new jobs. A safe generalist for research, writing, analysis, planning, and file deliverables; administrators can customize it without rebuilding the image.

**Assistant** — The database-backed application default for persistent sessions. It uses the continuous tool-calling loop and can be customized by administrators or forked as a user's personal default.

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
- **Isolated workspaces** — SSH/SFTP to per-job containers, or dedicated VMs via QEMU/KubeVirt for a stronger boundary
- **Notifications** — email (SMTP), Slack, Discord, and ntfy webhooks for job status updates

## Table of Contents

- [Security Model: Separate Harness and Workspace](#security-model-separate-harness-and-workspace)
- [Quick Start](#quick-start)
- [Local Kubernetes Setup (k3d)](#local-kubernetes-setup-k3d)
- [Development Setup](#development-setup)
- [Architecture](#architecture)
- [Debugging](#debugging)
- [License](#license)

### Which path should I use?

SRW deploys to Kubernetes, and only to Kubernetes — the Helm chart in
[`helm/`](helm/) is the single deployment artifact for every environment
(production, homelab, and your laptop). Dynamic agent scaling is the product:
the orchestrator provisions agent pods, workspace pods and PVCs on demand
through the Kubernetes API, so a single-node cluster is the smallest sensible
target. [k3d](https://k3d.io) turns any one machine into that cluster.

| Goal | Path |
|------|------|
| Run the system, on one machine or a hundred | [Local Kubernetes Setup (k3d)](#local-kubernetes-setup-k3d), then the same chart on a real cluster |
| Iterate on Python code with the fastest possible edit-to-effect | [Fast inner loop with Tilt](#fast-inner-loop-with-tilt-recommended) — live-syncs source into the running pods |
| Run tests, lint and one-off scripts on your host | [Development Setup](#development-setup) |

## Security Model: Separate Harness and Workspace

SRW treats repositories, model-generated files, and every command an agent runs
as potentially hostile. A sufficiently capable model can use ordinary debugging
facilities, malicious dependencies, or chained exploits to attack the process
that is supposed to constrain it. A working directory or container path is not a
security boundary when the harness and generated code share the same filesystem
and security identity.

For that reason, SRW deliberately separates its trusted control plane from its
untrusted execution plane:

```text
TRUSTED CONTROL PLANE                     UNTRUSTED EXECUTION PLANE

┌──────────────────────────────┐          ┌──────────────────────────────┐
│ Orchestrator                 │          │ Per-job workspace            │
│ Agent harness and policy     │ SSH/SFTP │ Repository and generated code│
│ LLM and control credentials  ├─────────►│ File operations and commands │
│ Final job-state authority    │          │ Container or dedicated VM    │
└──────────────────────────────┘          └──────────────────────────────┘
```

The agent process never uses its own filesystem as the job workspace. There is
no local-backend fallback: if the orchestrator cannot provision a remote
workspace and provide SSH credentials, the job fails closed. This keeps normal
workspace activity from directly modifying the harness, its policy checks, or
its control-plane credentials. Only narrowly scoped, job-specific capabilities
should cross into the workspace.

This separation limits blast radius; it does not make arbitrary code safe.
Workspace containers share a node kernel and therefore provide a weaker boundary
than dedicated workspace VMs, which add a separate guest kernel and hypervisor
boundary. Network policy, least-privilege credentials, audit logging, disposable
workspaces, and human gates for privileged or irreversible actions remain part of
the security model. Agent runtimes that require their harness and credentials to
run inside the writable workspace cross this boundary and require an explicit,
separate threat analysis rather than being treated as drop-in model providers.

## Quick Start

```bash
# Clone
git clone <repo-url>
cd Superhuman-Remote-Worker

# Bring up a local k3d cluster (cert-manager, namespace, local registry)
./scripts/local-dev-up.sh

# Session-router JWT secret (without it sessions never connect)
kubectl --context=k3d-srw -n srw create secret generic srw-session-jwt \
  --from-literal=jwt-secret="$(openssl rand -base64 48 | tr -d '\n' | head -c 64)"

# Fill in at least one LLM key, then install the chart
cp deployment/values-local.yaml.example deployment/values-local.yaml
$EDITOR deployment/values-local.yaml
helm repo add collabora https://collaboraonline.github.io/online --force-update
helm repo add cloudnative-pg https://cloudnative-pg.github.io/charts --force-update
helm dependency build ./helm
helm install srw ./helm -n srw --kube-context=k3d-srw -f deployment/values-local.yaml

# Log in at https://localhost/ as test / test and create a job
```

Full prerequisites, the two k3d-specific traps, and the smoke-test checklist
are in [Local Kubernetes Setup (k3d)](#local-kubernetes-setup-k3d) below.

## Local Kubernetes Setup (k3d)

Runs the **production Helm chart** on a local [k3d](https://k3d.io) cluster — k3s in Docker. This is the same chart and the same code paths as a real deployment (workspace pod provisioning, ingress routing, cert-manager + OIDC, the Keycloak realm/client flow), scaled down to one machine. The cluster can be started and stopped like any Docker container, so it doesn't consume resources when you're not working.

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

OpenCloud's proxy needs to reach Keycloak from inside the pod, and on k3d `auth.localhost` resolves to the pod's own loopback. `values-local.yaml.example` ships a `hostAliases` block that maps `auth.localhost` to Traefik's ClusterIP — but that IP is determined at cluster-create time, so grab it now:

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
cp deployment/values-local.yaml.example deployment/values-local.yaml
$EDITOR deployment/values-local.yaml
# - paste at least one LLM key (OPENAI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY)
# - paste the Traefik ClusterIP from step 2 into opencloud.hostAliases[0].ip

# the chart depends on the Collabora subchart (Canvas Office rendering), so its
# repo must be registered before `helm dependency build` will resolve Chart.lock
helm repo add collabora https://collaboraonline.github.io/online --force-update
helm repo add cloudnative-pg https://cloudnative-pg.github.io/charts --force-update
helm dependency build ./helm
helm install srw ./helm -n srw --kube-context=k3d-srw -f deployment/values-local.yaml
```

`deployment/values-local.yaml` is gitignored (it holds your LLM keys). Everything else in it is dev-only stub credentials.

### 5. Wait for pods, then log in

```bash
kubectl --context=k3d-srw -n srw get pods -w
# Ctrl-C once all pods are 1/1 Running (the orchestrator takes longest — ~5 init containers chained on database/keycloak/gitea readiness)
```

Open `https://localhost/` in your browser and log in:

| Username | Password            | Roles         |
|----------|---------------------|---------------|
| `test`   | `srw-k3d-dev-test`  | admin + user  |

`test` is the bootstrap admin. It is seeded on every install and its password is whatever you set
`KC_REALM_ADMIN_PASSWORD` to — the value above is the one in `values-local.yaml.example`. Email is
pre-verified and it maps to `srw-admin` on Gitea, so there is no approval flow and no email step.

**Shared development accounts.** For anything needing more than one user — sharing a project,
testing permissions, reviewing another user's job — the chart can seed a fixed set. They are
**off by default** and enabled by `values-local.yaml.example`, which is local-dev only:

| Username      | Password           | Roles        |
|---------------|--------------------|--------------|
| `dev-admin-1` | `srw-k3d-dev-adm1` | admin + user |
| `dev-admin-2` | `srw-k3d-dev-adm2` | admin + user |
| `dev-user-1`  | `srw-k3d-dev-usr1` | user         |
| `dev-user-2`  | `srw-k3d-dev-usr2` | user         |
| `dev-user-3`  | `srw-k3d-dev-usr3` | user         |
| `dev-user-4`  | `srw-k3d-dev-usr4` | user         |

> **Never set `keycloak.devUsers.enabled: true` on a deployment anyone else can reach.** These
> passwords are published here on purpose, so that anyone who has read this file has admin on any
> install where the flag is on. The chart default is `false` and a test enforces it.

All passwords are 16 characters because the realm enforces `length(16) and notUsername`. Adding a
user means adding it to `keycloak.devUsers.users` in `helm/values.yaml` and to the table above.

| URL | What it is |
|-----|-----------|
| `https://localhost/`        | Cockpit (the UI) |
| `https://api.localhost/`    | Orchestrator REST API |
| `https://auth.localhost/`   | Keycloak (admin console at `/admin`) |
| `https://git.localhost/`    | Gitea |
| `https://cloud.localhost/`  | Nextcloud (the main cloud backend) |
| `https://mcp.localhost/`    | MCP server |

`*.localhost` resolves to `::1` automatically (RFC 6761 + glibc `myhostname` NSS), so there's no DNS config to do.

### Smoke-testing the install

Quick checklist after a fresh `helm install` (or after recreating the cluster). Each step exercises an independent slice of the stack.

**1. Cockpit + Keycloak login** — open `https://localhost/`, log in as `test`/`test`. Lands on the Sessions list (`/sessions`). If you land in a refresh loop, jump to the matching troubleshooting entry below.

**2. Sessions (persistent agent + WS)** — Sessions → **New Session** → keep the preselected Assistant (or choose another session expert) → **Create Session**. Expected sequence in the UI:

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

**5. Nextcloud SSO** — open `https://cloud.localhost/`, click **Log in with Keycloak**. Should land on the Files view. A plain `curl -sk https://cloud.localhost/status.php` is the cheaper check that the pod and Ingress are healthy (`"installed":true`).

> The local stack runs **Nextcloud**, not OpenCloud (`nextcloud.enabled=true` + `opencloud.enabled=false` in `values-local.yaml.example`). Sections below that describe an OpenCloud `hostAliases` workaround are leftovers from before that switch and no longer apply — the example values file has no `hostAliases` block.

### 6. VM workspaces (optional)

The VM tier (KubeVirt VMs as agent workspaces, with gated `sudo`) runs on the same k3d
cluster. Install KubeVirt + CDI with the bootstrap script — it picks the KubeVirt line for the
cluster's Kubernetes minor, uses your host's KVM when `/dev/kvm` is visible inside the node,
patches the `local-path` StorageProfile, and ends with a boot + import smoke test:

```bash
./scripts/local-kubevirt-up.sh
kubectl -n srw create secret generic srw-vm-lifecycle-hmac \
  --from-literal=VM_LIFECYCLE_HMAC_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Then uncomment the `vm:` / `vmController:` block in `deployment/values-local.yaml` (pin a
published `agent-vm-base` tag) and let Tilt apply it on the next image rebuild. Prerequisites,
sizing and troubleshooting: `helm/README.md` → "VM workspaces on your cluster".

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

Tilt is opt-in but is now the **default development workflow**. The `helm install` path above still works standalone for people without Tilt installed. Design and rationale: [`knowledge-base/knowledge/features/tilt_inner_loop_dev.md`](knowledge-base/knowledge/features/tilt_inner_loop_dev.md).

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

This bootstrap is idempotent — it runs `scripts/local-dev-up.sh` underneath
(cluster + cert-manager + namespace + vm-ssh-key Secret), then adds the
`srw-session-jwt` Secret, syncs the current Traefik ClusterIP into
`values-local.yaml`'s `opencloud.hostAliases` entry, and runs `tilt up` in the
foreground. The chart deploys and bootstraps its bundled single-node Garage for
the `virtual` workspace tier and workspace snapshot/IDE-session storage; no
separate MinIO image mirror is required.

**Run Tilt — subsequent sessions**: the cluster, secrets, and Helm release persist across `k3d cluster stop/start`, and the Traefik ClusterIP is stable for the life of the cluster, so you don't need the bootstrap again. Just bring the cluster back and start Tilt directly (always cluster first, then Tilt — Tilt deploys *into* a running cluster):

```bash
k3d cluster start srw   # if it was stopped
tilt up                 # from the repo root; Tilt UI at https://localhost:10350
```

Press Ctrl-C to stop Tilt (the cluster keeps running; use `k3d cluster stop srw` to stop that too). The bootstrap script is always safe to re-run if you're unsure — it skips anything already in place.

#### The Plan → Develop → Verify workflow

The point of having Tilt + a local prod-parity cluster is that **every change can be verified locally before it ships**. The loop:

1. **Plan**: design doc under `knowledge-base/knowledge/features/` or `knowledge-base/knowledge/issues/` (whatever fits). Get alignment on scope + acceptance before you start editing.
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
- **MCP** (`orchestrator/mcp/*.py`, `src/shared/`): mcp pod log shows `watchfiles: N changes detected` followed by the FastMCP banner with the (potentially updated) server name. `/health` returns 200 within ~10 s.
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
- **Keycloak in `CreateContainerConfigError` for missing secret keys** — the realm import references env vars for every OIDC client (even disabled ones). Check that all keys from `values-local.yaml.example` are present in `values-local.yaml`.
- **Every Tilt deploy fails with `Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress`** — a previous `helm upgrade` was killed mid-flight (superseding build, Ctrl-C, or the `k8s_upsert_timeout_secs` deadline) and left its release Secret in `pending-upgrade`. The lock lives in the cluster, so **restarting Tilt does not clear it**. `scripts/tilt-helm-apply.sh` now clears this automatically on the next deploy, once the pending revision is >60 s old (`SRW_HELM_STALE_AFTER`). To clear it by hand:
  ```bash
  helm history srw -n srw            # find the pending revision N
  kubectl -n srw delete secret sh.helm.release.v1.srw.v<N>
  ```
  The previous revision stays `deployed` and becomes the head again; `--take-ownership` re-adopts anything the killed run already applied. **Do not use `tilt trigger srw` to recover** — a Force Update runs `delete_cmd` (`helm uninstall`) first, which reinstalls the release from scratch and restarts every workload. Data survives (PVCs and the `resource-policy: keep` Secret are preserved), but it costs a multi-minute full-stack restart for nothing.
- **`https://localhost/` returns 404 right after `k3d cluster start srw`** — Traefik's endpoint discovery can go stale through a long idle (the pod restart cascade after `k3d cluster stop`/`start` sometimes leaves Traefik holding empty endpoint state). Kick it: `kubectl --context=k3d-srw -n kube-system rollout restart deploy/traefik`.
- **Login lands in a 401-refresh loop in Brave/Firefox** — the chart defaults to `auth.bff.sameOriginApi: true` in `values-local.yaml.example` so the cockpit and BFF share an origin and the session cookie is first-party. If you flipped that off, either flip it back on or allowlist `[*.]localhost` for cookies in the browser. Symptom in orchestrator logs: `GET /auth/callback 302` immediately followed by `GET /api/auth/me 401` on repeat.
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

Running the Python tooling on your host — tests, lint, `init.py`, one-off
scripts, or an orchestrator/agent under a debugger — against the databases of
a cluster you already have up. This is not a way to run the system; the
cluster from the previous section is a prerequisite.

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

### 3. Reach the Databases

Every database Service is ClusterIP. Tunnel them to localhost (app DB on
5432, pgvector on 5433, audit on 5434) and point `DATABASE_URL` /
`VECTOR_DB_URL` at the tunnels:

```bash
scripts/port-forward-dbs.sh                       # Ctrl-C to stop
KUBE_CONTEXT=k3d-srw KUBE_NAMESPACE=srw scripts/port-forward-dbs.sh
```

Read the credentials out of the cluster Secret rather than guessing them:

```bash
kubectl --context=k3d-srw -n srw get secret srw \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d; echo
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
is fine in Kubernetes (the pod restarts) but kills the dev loop when you run
the agent by hand. Jobs, documents, descriptions, git URLs, feedback, and
freeze approvals are all submitted to the orchestrator (REST API or Cockpit
UI) and dispatched to the running agent — not passed as CLI flags.

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
python -m pip install -r requirements-dev.txt  # One-time test dependencies
./scripts/pytest-fast.sh                   # All tests, bounded parallel runner
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
 │ PostgreSQL│ │  AuditDB  │ │   Neo4j   │ │Datasources│
 │ (system)  │ │  (audit)  │ │(knowledge)│ │ (per-job) │
 └───────────┘ └───────────┘ └───────────┘ └───────────┘
```

### Expert Lineup

| Expert | Config | Mode | Role |
|--------|--------|------|------|
| **General Worker** | managed DB expert seeded from `config/experts/general-worker/` | Worker | Application default for general jobs |
| **Scholar** | `config/experts/scholar/` | Worker | R&D exploration, idea generation, web research, paper analysis |
| **Critic** | `config/experts/critic/` | Worker | Code review, proposal review, codebase audits, test execution |
| **Developer** | `config/experts/developer/` | Worker | Claude Code delegation, PR factory, implementation |
| **Curator** | `config/experts/curator/` | Worker | Knowledge extraction from job artifacts into project KB |
| **Designer** | `config/experts/designer/` | Worker | UI/UX design, HTML/CSS mockups, design specifications |
| **Assistant** | managed DB expert seeded from `config/experts/assistant/` | Persistent | Application default for conversational sessions |
| **Designer-Interactive** | `config/experts/designer-interactive/` | Persistent | Collaborative design iteration in real-time sessions |

All experts share the same universal agent codebase and one config root,
`config/expert_base.yaml`, with a role overlay in between: worker experts
extend `worker_base` (`config/overlays/worker.yaml`), session experts extend
`session_base` (`config/overlays/session.yaml`), and any expert can be
re-rooted onto another role at resolve time. Those files are conservative
inheritance fallbacks, while the user-facing defaults are database expert
pointers selected by the administrator, project, or user. See
[config/README.md](config/README.md) for details.

### Two Operating Modes

**Worker mode** (job-based) — Phase-alternating execution for batch tasks:

- **Strategic phase** — Reviews progress, reflects on what worked, updates the plan, creates the next batch of tasks
- **Tactical phase** — Executes tasks using domain-specific tools until all todos are complete
- This loop continues until the job is done. Each phase boundary creates a snapshot for recovery.

**Persistent mode** (session-based) — Continuous tool-calling loop for interactive use:

- No phase/todo structure. The agent stays online and responds in real time via WebSocket.
- Supports idle timeout handling and memory injection across turns.
- Used by Assistant and Designer-Interactive experts.

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

- **Workspace files** (inside the workspace container, SSH in to look): `plan.md`, `todos.yaml`, `notes/`, `datasources.md`, `output/`. From your own terminal, editor, or IDE — not just Cockpit's file browser — see [docs/ssh-access.md](docs/ssh-access.md).
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
…) arrive under their own upstream licenses and are not redistributed by
this project.
