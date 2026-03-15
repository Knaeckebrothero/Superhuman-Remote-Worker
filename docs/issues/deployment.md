# Home Lab Deployment Status (2026-03-14)

## Cluster Status

**Main cluster** (`superhuman-remote-worker` namespace) — all 15 pods running healthy:
- Orchestrator, 2 agent pods, cockpit, Gitea, PostgreSQL (app + vector), MongoDB, Neo4j, pgAdmin, Mongo Express, Dozzle, 3 VPN containers

**Agent cluster** (`vms` context) — KubeVirt installed, VM controller running:
- VM controller connected to NATS, subscribed to `vm.lifecycle.{create,delete,get}` since March 11
- NATS leaf node bridging the two clusters

## The Job

| Field | Value |
|-------|-------|
| ID | `74b871dd-2fa1-4e71-a832-db2146ce0a5c` |
| Description | "Test job" (simple calculator app) |
| Status | `reviewing` (completed, awaiting human review) |
| Model | `openai/gpt-oss-120b` |
| Duration | ~14 minutes (12:08 → 12:22 on March 12) |
| Output | `calculator.py` (2,718 bytes) + `README.md` (1,881 bytes) |
| Phases completed | 4 (requirements → implementation → docs → verification) |

The job completed successfully — the agent built a Python calculator with CLI, docstrings, and documentation.

## VM Status: Not Used

The VM controller has been idle since startup — no VM creation request was ever sent:

```
Subscribed to vm.lifecycle.{create,delete,get} — waiting for requests
```

### Why

The orchestrator's dispatcher (`_job_needs_vm()` in `orchestrator/main.py:567`) checks two conditions before auto-provisioning a VM:

1. `context.vm.requested == true` — explicit VM request in job context
2. `config_override.workspace.backend == "remote"` — config override specifies remote workspace

The test job had **neither**:

```
config_override: {"llm": {"tactical": {"model": "openai/gpt-oss-120b"}, "strategic": {"model": "openai/gpt-oss-120b"}}}
context: {"git_remote_url": "...", "kickoff_message": "Hey, build a small calculator app."}
```

No `workspace.backend: remote` and no `vm.requested` flag. So the dispatcher treated it as a local-workspace job and assigned it directly to a pod agent.

### How the VM System Actually Works

The VM workspace backend is a **transparent proxy**, not a "run the agent inside a VM" pattern:

```
Agent Pod (main cluster)              VM (agent cluster)
  ├─ LLM calls                        ├─ Filesystem (workspace files)
  ├─ Tool logic                        ├─ tmux sessions (shell commands)
  └─ WorkspaceManager ──SSH/SFTP──►    └─ /home/agent-host/workspace/
```

The agent pod runs on the main cluster. All file I/O and shell commands are forwarded to the VM over SSH/SFTP via `RemoteBackend` (`src/core/backends/remote.py`). This gives fault isolation — VM destruction doesn't crash the agent, and phase snapshots are pulled to the pod at phase boundaries for recovery.

**The expected flow:**

1. Job created with `config_override.workspace.backend: "remote"` or `context.vm.requested: true`
2. Dispatcher sees `_job_needs_vm() == True`, calls `vm_provisioner.create_vm()` via NATS
3. VM controller on agent cluster creates KubeVirt VM
4. Management daemon inside VM boots, registers via NATS with its IP
5. Dispatcher injects `workspace.backend: remote` + VM SSH host into config_override
6. Job dispatched to a pod agent, which connects to VM via `RemoteBackend`

### Fix

To test the VM path, create a job with the VM flag. Either:

- Set `context.vm.requested: true` when creating the job (via cockpit or API)
- Set `config_override.workspace.backend: "remote"` when creating the job

Example via API:
```bash
curl -X POST http://orchestrator:8085/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Test VM workspace",
    "config_name": "default",
    "context": {"vm": {"requested": true}},
    "config_override": {"llm": {"model": "openai/gpt-oss-120b"}}
  }'
```

Alternatively, check if the cockpit UI has a "use VM workspace" toggle when creating jobs.
