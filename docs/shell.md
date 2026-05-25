# Persistent Shell System: Design Rationale & Lessons Learned

## The Underlying Problem

Software engineering and DevOps work frequently requires SSH connections — deploying services, debugging remote servers, managing containers, checking logs. This is not an edge case; it's a core part of the job. Any agent system that aims to handle real-world SWE/DevOps tasks must deal with remote shell sessions.

## How Most Agent Frameworks Handle It (And Why It's Bad)

Most LLM agent frameworks provide a stateless `run_command` tool — fire a command, get the full output back, done. This creates two distinct context-bloating problems:

### 1. Output Bloat

When you run a script or a long-running command, the entire stdout/stderr gets dumped back into the conversation. A `podman pull` downloads 8GB and prints 200 lines of progress bars. A deployment script logs every step. A test suite prints every assertion. The model receives *all of it*, but in 99% of cases you only care about:
- The last few lines (did it succeed?)
- The error line (where did it fail?)

A human developer glances at the bottom of their terminal. They don't read 200 lines of INFO logging from 20 minutes ago. But a stateless tool forces the model to ingest everything, wasting context tokens on irrelevant output.

### 2. Repetition Bloat (sshpass / One-Shot SSH)

The stateless alternative for remote work is `sshpass -p $PASS ssh -o StrictHostKeyChecking=no user@host "command"`. Every single remote command requires repeating the full connection string — IP, username, password, SSH options. For a job that runs 50 remote commands, that's 50 copies of the same boilerplate in the conversation history, burning context tokens on pure repetition.

Both patterns — output bloat and repetition bloat — fill the context window with information that is either irrelevant or redundant, leaving less room for the actual reasoning the model needs to do.

## The Persistent Shell Idea

The idea was simple: give the model a persistent shell window, just like a human developer uses. Open a terminal, keep it open, run commands in it, check the output when you need to.

Concretely, the system uses tmux-backed named tabs (see `docs/persistent_shell.md` for full design). The key properties:

- **Commands and output are decoupled.** Sending a command is one action; reading the result is another. The model can run something, do other work, then come back and check.
- **Only recent output is returned by default.** The model gets the last N lines, not the full history. It can read more if needed. This mirrors how a human uses a terminal — you see the bottom of the screen, and scroll up only when you need to.
- **State persists.** Environment variables, working directory, virtualenvs, command history — all preserved across calls. No need to re-establish context every time.
- **SSH sessions stay open.** Instead of `sshpass` for every command, the model opens an SSH session in a tab and runs commands in it. The connection details appear once, not 50 times.

### What This Was Supposed to Solve

For SSH specifically, the vision was: the model opens two or three tabs (one per machine, or one per user account on a machine), keeps them open, and works in them just like a human would. No `sshpass` repetition, no output flooding, clean separation of concerns.

## What Actually Happened

The persistent shell system works mechanically — tmux sessions are reliable, output capture works, tab lifecycle is solid. But the *model behavior* with it has been problematic (see `docs/issues/job_debug2.md` for a detailed case study):

- **Shell proliferation.** Instead of reusing tabs, the agent spawned ~35 unique shells in a single job, hitting the max-tabs limit (15) seven times and burning tool calls on cleanup cycles.
- **Blocked tab cascading.** When a tab hit a password prompt, instead of resolving it (sending the password or Ctrl+C), the agent opened a *new* tab. The blocked tab was abandoned, consuming a slot until forced cleanup.
- **Helper script proliferation.** Instead of running commands directly in the persistent session, the agent fell back to writing 20+ one-off Python scripts (paramiko wrappers) — each a slight variation because the previous one didn't work. This is the model reverting to its training prior of "write a script, run it once."
- **Credential confusion.** The agent guessed wrong passwords, cycled through credentials, and only found the correct ones after many failed attempts — work that belongs in a credential lookup, not interactive terminal fumbling.

## Why This Happens: The Training-Runtime Mismatch

A March 2026 paper — [Agents Learn Their Runtime: Interpreter Persistence as Training-Time Semantics](https://arxiv.org/abs/2603.01209) — provides the explanation. Their 2x2 study found:

- Models **trained on stateless traces** but deployed in a **persistent runtime** redundantly re-derive state they already have, using **~3.5x more tokens** than necessary.
- Models **trained on persistent traces** but deployed in a **stateless runtime** hit missing-variable errors in **~80% of episodes**.

Most LLM training data consists of one-shot shell examples: Stack Overflow answers, tutorials, documentation, README snippets. The pattern is always "here's the complete command, here's the output." Models have very little training signal for multi-turn stateful terminal interaction — tracking what's running in which tab, handling interactive prompts, managing environment state across commands.

So when given a persistent shell, the model doesn't naturally use it the way a human would. It falls back to what it knows: write a self-contained script, run it, get the output. The persistent session is there, but the model treats it like a stateless executor anyway — or worse, gets confused by the statefulness (tab cascading, credential confusion).

## The Pragmatic Conclusion

The persistent shell idea was sound in theory, and the implementation works. But forcing a stateful workflow onto a model trained on stateless patterns increases cognitive load rather than reducing it. The model has to reason about session state, tab management, prompt resolution — things it has no strong prior for.

The more natural approach for the model is probably:
1. **Stateless command execution** as the primary interface (what the model is trained on)
2. **Tooling-level abstractions** that handle the statefulness invisibly — e.g., an SSH wrapper tool that manages connections internally, handles auth, and presents clean command-in/output-out pairs to the model
3. **Persistent sessions as an opt-in power tool** for specific scenarios (long-running processes, interactive debugging) rather than the default for everything

The ecosystem is moving in this direction. Tools like [Term-CLI](https://github.com/EliasOenal/term-cli) handle interactive scenarios (SSH+MFA, serial consoles, installers) with agent-driven but human-assisted interaction for secrets. [Interactive Shell MCP servers](https://github.com/lightos/interactive-shell-mcp) provide session management with smart output mode detection. The common thread: **abstract away the statefulness from the model, don't expect the model to manage it.**

The `sshpass` pattern is ugly and repetitive, but it might be closer to what the model can actually handle well. The challenge is finding the middle ground — something that doesn't bloat the context *and* doesn't require the model to manage state it wasn't trained for.

## Solution: Dual-Mode Shell System

Rather than replacing the persistent shell system, we add a second mode — a stateless `run_command` tool that uses the persistent tmux infrastructure underneath but hides all session management from the model. The two modes are toggled via config.

### Config Toggle

```yaml
shell:
  mode: stateless    # "stateless" (new run_command) or "persistent" (current shell_execute + shell_read)
```

- **`stateless`** (new default): Exposes `run_command` + `shell_read`. The model fires commands and gets output back. No tab management, no keys mode, no async. Simple command→output interface.
- **`persistent`**: Current behavior. Exposes `shell_execute` + `shell_read`. Full tab management, keys mode, async mode. For agents that need interactive terminal workflows.

### `run_command` Tool Design

The model sees a simple stateless tool:

```python
run_command(
    command: str,      # Shell command to execute
    tail: int = 30,    # Max output lines returned (default 30)
) -> str
```

**What the model gets back:**
```
Exit code: 0
--- stdout ---
[...147 lines truncated...]
line 148
line 149
...
line 177
```

**What happens underneath:** The command runs in a hidden persistent tmux tab (reusing the existing `ShellManager.run_sync()` infrastructure). The tab persists between calls — environment variables, working directory, virtualenvs all survive. But the model doesn't know or care about this.

### Key Design Decisions

**No tab management.** The model doesn't name, create, or close tabs. There's one hidden default tab. The model's mental model is: "I run a command, I get output." This matches training data.

**30-line tail by default.** Most commands produce output where only the end matters (did it pass? what error?). The model gets the last 30 lines. If it needs more, it uses `shell_read` — the same tool that already exists. This prevents output bloat without losing information.

**Quiet/long commands return "still running", not an error.** If a command produces no new output for ~30s (a big `pip install`, a build, a download, data ingestion/embedding), the shell returns an honest "still running" result (`Exit code: -1`) — the command keeps running on the tab. Two distinct messages: the **soft** no-change timeout reports how long output has been quiet; the **hard cap** (the maximum a single call waits, 600s) says only that the limit was reached and does **not** claim silence — a redrawing progress bar can keep emitting while still hitting the cap. Best practice: pass an explicit `timeout` (e.g. 300-600) **up front** for work you expect to be slow/quiet — that waits the full duration and returns the real exit code. Otherwise read the tab once to check progress (not in a tight loop). A tab with a still-running command rejects the next command until it finishes, instead of silently interleaving them; that rejection message is **mode-neutral** (it doesn't advise keys/extra tabs that the stateless `run_command`/`shell_read` set lacks — those options live in the persistent `shell_execute` docstring). (This replaced an earlier heuristic that mislabeled any 5s-quiet command as "waiting for input" — see `docs/issues/shell_stall_detection_false_positive.md`.)

**Interactive prompts → non-interactive form.** Genuine prompts (passwords, y/n) can't be answered from stateless `run_command`, so it returns the prompt text and steers the model to a non-interactive form (e.g., `sshpass`, `yes |`, `-y` flags). This keeps the model in the stateless patterns it's trained on, rather than stateful prompt resolution (which is where things go wrong).

**SSH via sshpass.** Yes, repeating `sshpass -p $PASS ssh user@host "cmd"` every time is verbose. But it's what the model knows how to do. Each command is self-contained, no session state to track, no credential confusion. The context bloat from repetition is real but far less damaging than the bloat from 35 shells, 20+ helper scripts, and credential cycling that happens when the model tries to manage SSH sessions.

**`shell_read` stays available.** The full scrollback history is preserved in the tmux tab. When the model gets 30 lines back and needs to see more (e.g., a test suite with failures at line 50), it calls `shell_read(lines=100)` to get more context. This is the one concession to statefulness — but it's a read-only operation that doesn't require the model to reason about session state.

### What This Eliminates

Compared to the persistent shell issues documented in `docs/issues/job_debug2.md`:

| Problem | Persistent Mode | Stateless Mode |
|---------|----------------|----------------|
| Shell proliferation (35 tabs) | Model creates tabs freely | Single hidden tab |
| Blocked / busy tab cascading | Model opens new tab for prompts | Busy tab rejects new commands; quiet commands report "still running" (poll or set `timeout`); prompts steered to non-interactive form |
| Helper script proliferation | Model writes paramiko scripts to avoid SSH complexity | Model uses sshpass directly (its training prior) |
| Credential confusion | Model guesses passwords interactively | Model must provide credentials in the command (forces lookup first) |
| Max-tabs errors + cleanup cycles | Hits limit, burns calls closing tabs | No tab limit to hit |
| Terminal state injection bloat | All open tabs shown every LLM call | Single hidden tab, minimal injection |

### What We Lose

- **Long-running processes.** Can't start a dev server and come back to check it. For this, use `persistent` mode.
- **Interactive debugging.** Can't step through a debugger, interact with a REPL. Use `persistent` mode.
- **SSH session reuse.** Every remote command pays the SSH connection overhead. Acceptable for most DevOps tasks (individual commands are fast); problematic for high-frequency remote operations.

These are legitimate use cases for persistent mode. The toggle exists precisely because some jobs need it. But for the majority of SWE/DevOps tasks — run tests, deploy, check logs, verify endpoints — stateless mode is sufficient and dramatically reduces agent failure modes.

### Open Questions

1. **Credential management.** The sshpass overhead is accepted for now, but a future credential manager tool (lookup credentials by host/service) would eliminate the repetition without requiring session state. This is a separate feature.
2. **Shell state injection.** In stateless mode, the `<terminal_state>` injection (which shows all open tabs every LLM call) should either be disabled or reduced to a minimal "last command status" line, since there's nothing for the model to manage.

## Related

- `docs/persistent_shell.md` — Full design document for the shell system
- `docs/issues/job_debug2.md` — Detailed case study of shell-related failures (Job 8a202851)
- `docs/issues/job_debug.md` — Earlier job analysis (different issues, same system)
