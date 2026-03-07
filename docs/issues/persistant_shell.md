# Persistent Shell: Remote Server Management Issues

## Problem

LLMs are not trained to work with interactive terminal sessions over SSH. The persistent shell system (tmux-backed tabs) works well for local commands, but agents struggle significantly when managing remote servers via SSH. This was observed in job `38527586` (Deploy GPT OSS 20b), where the agent burned 19 phases and 3200+ audit entries largely due to SSH/sudo difficulties.

## Observed Failure Patterns

### 1. Shell Tab Explosion

When agents hit an interactive prompt (SSH password, sudo password), they abandon the tab and open a new one instead of resolving the prompt. In the observed job, the agent opened dozens of tabs:

```
srv, srv-105, srv-105-check, srv-105-status, srv-105-verify,
fresh-srv, check-srv, fresh-check, fw, fw2, fw3, fw4,
restart-router, restart-router-2, restart-router-final,
restart-router-tty, kill-router-sudo, kill-router-2, ...
```

Each abandoned tab left a stuck password prompt, wasting resources and context.

### 2. Interactive Prompt Handling

The shell tool detects interactive prompts (password prompts) and blocks command execution until resolved. Agents don't reliably handle this:
- They fail to use `keys` mode to send passwords to prompts
- They open new tabs instead of resolving blocked ones
- They mix up which tab has which state

### 3. Password/Credential Piping

Agents attempt complex command chains to avoid interactive prompts:

```bash
sshpass -p 'p@ss' ssh -t admin@server "echo 'p@ss' | sudo -S firewall-cmd ..."
```

These fail due to:
- Special characters in passwords getting mangled by shell escaping
- The shell tool's prompt detection intercepting sshpass prompts
- Nested password requirements (SSH password + sudo password) compounding the problem

### 4. SSH Key Setup Not Generalized

The agent set up SSH keys for one user (`routerprod`) but didn't think to do the same for `admin` (which had sudo/wheel access). This would have eliminated most of the prompt detection issues. Models don't generalize solutions across similar problems well in this context.

### 5. Blocked Commands in SSH Sessions

The shell tool's `blocked_commands` filter was blocking `sudo` and `systemctl` even when typed into a remote SSH session — the filter doesn't distinguish between local and remote execution. **Fix applied:** `sudo` and `systemctl` removed from default blocklist (2026-03-07).

## Impact

- Massive token waste (3200+ audit entries for a deployment task)
- 19 phases when ~5 should have been sufficient
- Agent concluded it "cannot do sudo" despite having admin credentials with wheel access
- Critic job also failed, hitting the same SSH/sudo wall and overstepping its role by trying to fix things itself

## Potential Solution: SSH Target Abstraction

Instead of having agents manually manage SSH sessions, implement an SSH target system similar to how sshpass works — the agent specifies a target machine and commands run there transparently.

### Concept

```yaml
# Config-level remote targets
shell:
  remote_targets:
    workstation:
      host: 10.18.2.105
      user: admin
      auth: password  # or key
      credential_ref: WORKSTATION_ADMIN_PASS  # env var or vault ref
      sudo: true       # allow sudo, pipe password automatically
```

Agent tool usage would look like:

```python
shell(command="firewall-cmd --permanent --add-port=8086/tcp", target="workstation", sudo=True)
```

The shell tool would handle SSH connection, authentication, sudo password piping, and prompt management internally — none of which the LLM needs to reason about.

### What to Preserve

Background/interactive shells should still be available for:
- VPN connections that need to stay open
- Long-running scripts (training, builds)
- REPL sessions
- Claude Code delegation tabs

The target abstraction would be an additional mode, not a replacement for the existing persistent shell system.

## Open Questions

- Should targets be defined in config YAML, job-level datasource config, or both?
- How to handle multi-hop SSH (jump hosts)?
- Should sudo password handling be automatic or require explicit opt-in per command?
- How to handle SSH host key verification for new targets?
- Should the agent still be able to fall back to manual SSH for edge cases?
