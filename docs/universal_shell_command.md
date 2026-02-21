---
tags:
  - tool-development
  - security
  - agent-architecture
  - coding-tools
aliases:
  - run_command
  - shell access
  - coding tools
related:
  - "[[coding_agent]]"
  - "[[security_checklist]]"
  - "[[deployment]]"
  - "[[config_issues]]"
  - "[[cloud_workspace]]"
---

# Universal Shell Command Access

## Problem

Only `coder` and `debugger` agents have access to `run_command`. Other agents (writer, researcher, custom configs) cannot execute shell commands, even when the task clearly calls for it.

Real examples where this hurts:

- **Writer** needs to compile LaTeX (`pdflatex`, `biber`, `latexmk`), count words (`wc`), or convert formats (`pandoc`)
- **Researcher** needs to run Python scripts for data analysis, use `curl` for APIs, or process files with `jq`
- **Any agent** might need to count files (`ls | wc -l`), check disk usage (`du -sh`), inspect file types (`file`), or decompress archives (`unzip`, `tar`)

Building dedicated tools for every possible action is impractical. A shell tool is the universal escape hatch.

## Security Model

### Default Mode (sandbox enabled)

Every agent using `run_command` gets these protections by default:

| Layer | Protection |
|-------|-----------|
| **Command blocklist** | `sudo`, `reboot`, `shutdown`, `poweroff`, `halt`, `init`, `systemctl` |
| **Workspace sandbox** | `WorkspaceManager.get_path()` prevents path traversal outside `workspace/job_<uuid>/` |
| **Timeout cap** | Hard limit of 600s (10 minutes), default 120s |
| **Output truncation** | 50,000 chars per stream (stdout/stderr), keeps tail |
| **Container isolation** | In production, the container itself is the sandbox |

### Unrestricted Mode (sandbox disabled)

For containerized deployments where the agent needs full computer access, both the blocklist and sandbox can be disabled via config:

```yaml
# config/experts/unrestricted/config.yaml
run_command_blocked_commands: []   # Empty list = no restrictions
run_command_sandbox: false          # Allow commands anywhere in the container
```

This is safe because the container IS the sandbox. The agent runs as non-root user `graphrag` with optional `sudo` access (NOPASSWD). There is no host access.

## Implementation

### Configurable `run_command` (`src/tools/coding/coding_tools.py`)

Two config keys control the security posture, read via `ToolContext.get_config()` at tool creation time:

| Config Key | Default | Effect |
|-----------|---------|--------|
| `run_command_blocked_commands` | `None` (use hardcoded blocklist) | List of blocked command prefixes. Set to `[]` to disable. |
| `run_command_sandbox` | `true` | When `true`, `working_dir` must resolve within workspace. When `false`, absolute paths are accepted. |

These use the existing extra-key collection mechanism: YAML root keys flow into the `extra` dict, which becomes `tool_config` on `ToolContext`, accessible via `context.get_config()`. No changes to `loader.py`, `schema.json`, or `defaults.yaml` are required.

At tool creation time, the effective blocklist and sandbox flag are captured in closure variables. Startup warnings are logged when restrictions are disabled:

```
WARNING - run_command: command blocklist is DISABLED — all commands allowed
WARNING - run_command: workspace sandbox is DISABLED — commands can run anywhere
```

### Fat Container (`docker/Dockerfile.agent`)

The agent container includes a full dev environment so agents can actually use their shell access:

**Runtime tools**: curl, git, poppler-utils, ripgrep, jq, vim-tiny, less, tree, htop, zip/unzip, openssh-client
**Dev tools**: build-essential, cmake, python3-dev, libffi-dev
**Networking**: net-tools, iproute2, dnsutils
**Process visibility**: procps (provides `ps`), iproute2 (provides `ss`)
**Runtime**: Node.js 22 (LTS via NodeSource)
**Escalation**: `sudo` with NOPASSWD for `graphrag` user

The container still runs as `USER graphrag` (non-root) by default. Sudo is available when the agent explicitly needs it (e.g., installing a system package).

### Container Monitoring (`GET /system/info`)

A monitoring endpoint on the agent API provides visibility into what the agent is doing inside its container:

```bash
curl http://localhost:8001/system/info | python -m json.tool
```

Returns:

| Field | Content |
|-------|---------|
| `cpu` | percent, core count |
| `memory` | total/used MB, percent |
| `disk` | total/used GB, percent |
| `listening_ports` | list of `{port, address, pid}` |
| `processes` | top 20 by memory: `{pid, name, cmd, memory_mb, cpu_percent}` |
| `network_connections` | established TCP connections (limit 50) |
| `agent` | agent_id, current_job |

This data is also available through the orchestrator proxy and MCP:

```bash
# Via orchestrator
curl http://localhost:8085/api/agents/<agent_id>/system-info

# Via MCP (Claude Code)
get_agent_system_info(agent_id="<agent_id>")
```

The heartbeat now includes `listening_ports` count and `process_count` in agent metrics, flowing into the agents table `metadata` JSONB automatically.

### Expert Configs

| Config | Blocklist | Sandbox | Use Case |
|--------|-----------|---------|----------|
| `coder` | Default (7 commands) | Enabled | Standard coding tasks in workspace |
| `debugger` | Default | Enabled | Debugging within workspace |
| `unrestricted` | Disabled (`[]`) | Disabled | Full computer access in container |

The `unrestricted` expert (`config/experts/unrestricted/config.yaml`) serves as the template for "full computer" mode with coding, workspace, git, and research tools.

## Security Considerations

**What the workspace sandbox prevents (when enabled):**
- Reading/writing files outside `workspace/job_<uuid>/`
- The `working_dir` parameter is validated through `WorkspaceManager.get_path()` which uses `Path.resolve()` + `Path.relative_to()` to block traversal

**What the blocklist prevents (when enabled):**
- Privilege escalation (`sudo`)
- System-level damage (`reboot`, `shutdown`, `poweroff`, `halt`)
- Service manipulation (`init`, `systemctl`)

**What the container provides (always):**
- Filesystem isolation — agent only sees its own container
- Network policies — no lateral movement (when configured)
- Resource limits — CPU/memory caps via container runtime
- Non-root default — `graphrag` user, sudo available but must be explicit

**Remaining risks (acceptable):**
- Agent could run a fork bomb or CPU-intensive loop — mitigated by timeout cap (600s) and container resource limits
- Agent could fill disk with output — mitigated by output truncation and workspace quotas (if configured)
- Agent could make network requests via `curl`/`wget` — same risk as existing `web_search` tool, acceptable
- In unrestricted mode, agent could `sudo` to install packages or modify system state — acceptable because the container is ephemeral and isolated

## Future Considerations

- **Per-agent allowlists**: If finer control is needed, add an `allow` list that acts as a whitelist (only listed command prefixes are permitted). Not implemented yet since the current blocklist + container isolation is sufficient.
- **Per-agent timeout overrides**: A writer compiling a large LaTeX document might need more than 120s default. Could add a `run_command_timeout` config key.
- **Rename `coding` to `shell`**: The tool category is still called `coding` for backwards compatibility. Could rename to `shell` to be more role-neutral and keep `coding` as an alias.

## Related

- [[coding_agent]] — Coding agent configuration that uses run_command
- [[security_checklist]] — Security considerations for the agent system
- [[deployment]] — Container deployment and infrastructure
- [[config_issues]] — Configuration issues including tool access
- [[cloud_workspace]] — Cloud workspace and container architecture
- [[sudo_approval_plugin]] — Sudo approval mechanism for elevated commands
