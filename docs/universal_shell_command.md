# Universal Shell Command Access

## Problem

Only `coder` and `debugger` agents have access to `run_command`. Other agents (writer, researcher, custom configs) cannot execute shell commands, even when the task clearly calls for it.

Real examples where this hurts:

- **Writer** needs to compile LaTeX (`pdflatex`, `biber`, `latexmk`), count words (`wc`), or convert formats (`pandoc`)
- **Researcher** needs to run Python scripts for data analysis, use `curl` for APIs, or process files with `jq`
- **Any agent** might need to count files (`ls | wc -l`), check disk usage (`du -sh`), inspect file types (`file`), or decompress archives (`unzip`, `tar`)

Building dedicated tools for every possible action is impractical. A shell tool is the universal escape hatch.

## Current State

`run_command` already exists in `src/tools/coding/coding_tools.py` with solid security:

| Layer | Protection |
|-------|-----------|
| **Command blocklist** | `sudo`, `reboot`, `shutdown`, `poweroff`, `halt`, `init`, `systemctl` |
| **Workspace sandbox** | `WorkspaceManager.get_path()` prevents path traversal outside `workspace/job_<uuid>/` |
| **Timeout cap** | Hard limit of 600s (10 minutes), default 120s |
| **Output truncation** | 50,000 chars per stream (stdout/stderr), keeps tail |
| **Container isolation** | In production (k3s), the container itself is the sandbox |

The tool is registered under the `coding` category and enabled only in `coder` and `debugger` expert configs.

## Proposal

### Phase 1: Add to defaults (minimal change)

Add `run_command` to `config/defaults.yaml` so every agent inherits it. Move it from the `coding` category into a new `shell` category to make it role-neutral.

```yaml
# config/defaults.yaml
tools:
  # Shell tools - command execution (src/tools/shell/)
  shell:
    - run_command
```

Expand the blocked commands list to cover more destructive operations:

```python
BLOCKED_COMMANDS = frozenset([
    # System control
    "sudo", "su",
    "reboot", "shutdown", "poweroff", "halt", "init", "systemctl",
    # Destructive filesystem ops
    "mkfs", "dd", "fdisk", "parted", "mount", "umount",
    # Permission/ownership changes
    "chmod", "chown", "chgrp",
    # Network reconfiguration
    "iptables", "ip6tables", "nft", "ifconfig", "ip",
    # Package managers (prevent system modification)
    "dnf", "yum", "apt", "apt-get", "pacman", "zypper", "snap", "flatpak",
    # Process/service manipulation
    "kill", "killall", "pkill",
])
```

This is safe because:
1. Agents already run in workspace-sandboxed directories — they can't touch system files
2. Production runs in containers — the container is the real sandbox
3. The blocklist prevents the most dangerous operations
4. Timeout + output truncation prevent resource exhaustion

### Phase 2: Per-agent command policies (future, if needed)

If finer control is needed later, add optional per-config allow/deny lists:

```yaml
# config/experts/writer/config.yaml
tools:
  shell:
    - run_command

shell:
  # Only these commands are allowed (if set, acts as whitelist)
  allow:
    - pdflatex
    - biber
    - latexmk
    - pandoc
    - wc
    - sort
    - head
    - tail
    - grep
    - find
    - ls
    - cat
  # These are always blocked (merged with global blocklist)
  deny: []
```

Implementation sketch for the whitelist check:

```python
def _check_allowed(command: str, allow_list: Optional[List[str]] = None) -> Optional[str]:
    """If an allow list is configured, only permit commands on it."""
    if allow_list is None:
        return None  # No whitelist = all non-blocked commands allowed

    first_word = command.strip().split()[0] if command.strip() else ""
    if first_word not in allow_list:
        return f"Command not allowed: '{first_word}'. Allowed: {', '.join(sorted(allow_list))}"
    return None
```

This keeps the default behavior permissive (blocklist only) while letting specific configs opt into a stricter whitelist model.

## Implementation Steps

1. **Rename module**: Move `src/tools/coding/` to `src/tools/shell/` (or create `src/tools/shell/` alongside, keeping `coding/` for backwards compat)
2. **Update registry**: Register under `shell` category in `TOOL_REGISTRY`
3. **Expand blocklist**: Add the extended set of blocked commands
4. **Update defaults.yaml**: Add `shell: [run_command]` to the tools section
5. **Update expert configs**: Remove `coding: [run_command]` from `coder` and `debugger` configs (they inherit from defaults now)
6. **Update CLAUDE.md**: Add `shell` to the tool categories documentation
7. **Tests**: Verify blocked commands, whitelist logic (if Phase 2), and that all expert configs load correctly

## Security Considerations

**What the workspace sandbox already prevents:**
- Reading/writing files outside `workspace/job_<uuid>/`
- The `working_dir` parameter is validated through `WorkspaceManager.get_path()` which uses `Path.resolve()` + `Path.relative_to()` to block traversal

**What the blocklist prevents:**
- Privilege escalation (`sudo`, `su`)
- System-level damage (`reboot`, `shutdown`, `mkfs`, `dd`)
- Permission changes (`chmod`, `chown`)
- Package installation (`apt`, `dnf`, etc.)

**What the container provides (production):**
- Filesystem isolation — agent only sees its own container
- Network policies — no lateral movement
- Resource limits — CPU/memory caps via k3s
- No root access inside the container

**Remaining risks (acceptable):**
- Agent could run a fork bomb or CPU-intensive loop — mitigated by timeout cap (600s) and container resource limits
- Agent could fill disk with output — mitigated by output truncation and workspace quotas (if configured)
- Agent could make network requests via `curl`/`wget` — same risk as existing `web_search` tool, acceptable

## Open Questions

- **Should `shell` replace `coding` entirely, or coexist?** Simplest to just rename/relocate and keep the old `coding` key as an alias for backwards compatibility.
- **Should the tool description change?** Current description is coding-focused ("run tests, linters, build commands"). For a universal tool, the description should be broader ("execute shell commands for file processing, compilation, data transformation, or any other task").
- **Do we need per-agent timeout overrides?** A writer compiling a large LaTeX document might need more than 120s default. Could add `shell.default_timeout` to config.
