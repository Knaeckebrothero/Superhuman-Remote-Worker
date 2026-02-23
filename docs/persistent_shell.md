---
tags:
  - agent-architecture
  - tool-development
  - coding-tools
  - interactive
aliases:
  - persistent terminal
  - shell sessions
  - async tools
related:
  - "[[coding_agent]]"
  - "[[universal_shell_command]]"
  - "[[cloud_workspace]]"
  - "[[cli_wrapper]]"
---

# Persistent Shell Sessions

Concept document for replacing the synchronous `run_command` model with persistent, multiplexed terminal sessions — giving the agent an interactive, async-capable workspace similar to how a human developer uses multiple terminal tabs.

## Problem

The agent's current tool interaction model is **synchronous RPC**:

```
Agent → run_command("pytest tests/") → blocks → waits → receives 50K chars → next thought
Agent → claude_code(prompt="...") → blocks → waits → receives 50K chars → next thought
```

This creates several problems:

1. **No async work.** The agent cannot start a build and do something else while it runs. Every command blocks the entire reasoning loop.

2. **Output flooding.** A test suite dumps its full output into context. The agent gets 50K chars of pytest logs when it only needed "3 tests failed" from the summary. Truncation helps but is a blunt instrument — it keeps the tail, which is often the right choice for tests but wrong for other tools.

3. **No interactive sessions.** Claude Code is always used in one-shot print mode (`-p`). The agent can't enter plan mode, review a plan, approve it, check progress, and course-correct. Same for REPLs, debuggers, or any interactive program.

4. **No persistent connections.** Every SSH command reconnects. Every database CLI restarts. There's no way to maintain a session with a remote server, GPU box, or Jupyter kernel.

5. **No process supervision.** The agent can't monitor a dev server, watch a training run, or check if a background job is still alive.

These are all things a human developer does naturally with a terminal multiplexer. The agent should too.

## Prior Art

This is a solved problem in the agent ecosystem. The industry has converged on persistent terminal sessions as the standard for capable coding agents:

| Project | Shell Approach | Key Insight |
|---------|---------------|-------------|
| **OpenHands** | tmux via libtmux (migrated from pexpect in v0.19) | pexpect's prompt-matching broke on interactive commands, password prompts, conda activations. tmux fixed all of it. |
| **SWE-agent** | Stateful shell via SWE-ReX | Persistent session inside sandboxed container. Commands sent, output captured through the session. |
| **mini-swe-agent** | Stateless `subprocess.run` | Deliberately chose statelessness — scores 74% on SWE-bench Verified. Proves persistent state isn't always needed. |
| **Devin** | Full cloud VM per task | Persistent bash + VS Code + Chrome. Memory layer with vectorized snapshots and replay timelines. |
| **agent-infra/sandbox** | tmux sessions via REST API | All-in-one container: browser, shell, files, MCP, VS Code. Shell commands execute in persistent tmux sessions. Closest to our architecture. |
| **Claude Code** | Semi-persistent (cwd persists, shell state resets) | Middle ground: `cd` persistence without state accumulation fragility. Background tasks via `run_in_background`. |

**The OpenHands migration is the most instructive.** They started with pexpect (same subprocess-based approach we use now), hit exactly the problems we're hitting, and moved to libtmux. Their [PR #4881](https://github.com/All-Hands-AI/OpenHands/pull/4881) documents the migration and rationale.

**The mini-swe-agent counterpoint matters too.** For simple command execution (run tests, check lint, grep for patterns), stateless `subprocess.run` works fine and is maximally robust. Our `shell_run` convenience tool preserves this path — persistent tabs are for when you actually need persistence.

**tmux MCP servers** already exist in the ecosystem ([persistent-shell-mcp](https://mcpservers.org/servers/TNTisdial/persistent-shell-mcp), [tmux-mcp](https://github.com/bnomei/tmux-mcp), [lox/tmux-mcp-server](https://github.com/lox/tmux-mcp-server)), confirming that tmux is the standard backing technology for exposing terminal sessions to LLM agents.

## Design

### Mental Model

The agent starts every job with a **terminal multiplexer** (backed by tmux) containing two pre-opened tabs: a **default shell** and (if configured) a **Claude Code session**. The agent can open additional named tabs for SSH, REPLs, dev servers, etc. — just like a human with multiple terminal windows.

The key shift from `run_command`: commands and their output are decoupled. Sending a command is one action; reading the result is another. And the default shell accumulates history — when the agent looks at it, it sees its command history just like a human scrolling up in their terminal.

```
┌──────────────────────────────────────────────────────────────────┐
│  Agent's Terminal Multiplexer                                    │
│                                                                  │
│  ┌── shell ──────┐  ┌── claude-code ─┐  ┌── ssh - gpu-box ─────┐│
│  │ $ pytest -x   │  │ claude         │  │ $ ssh user@gpu-box   ││
│  │ 2 failed      │  │ > planning..   │  │ $ nvidia-smi         ││
│  │ $ ruff check  │  │               │  │ GPU 0: A100 80GB     ││
│  │ All clean.    │  │               │  │                      ││
│  └───────────────┘  └───────────────┘  └──────────────────────┘│
│  (always open)      (auto-started)     (agent-opened)           │
└──────────────────────────────────────────────────────────────────┘
```

### Default Tabs

**`shell`** — Always open. This is the agent's workspace shell, equivalent to a developer's main terminal. All `shell_run` commands execute here, so the scrollback accumulates a natural command history. The agent can scroll up to review previous output. The injection shows the last 5 lines by default — enough to see the most recent result without bloating context. The agent uses `shell_read("shell", lines=50)` to see more when needed.

**`claude-code`** — Auto-started when the agent config includes `claude_code` in its tools. Launches an interactive Claude Code session (`claude --model <configured_model>`). The agent interacts with it across the entire job lifecycle — sending prompts, reading responses, using `/compact` to manage context, and `/plan` for complex subtasks. Claude Code has built-in auto-compacting, so it won't break from long sessions, but the agent should proactively use `/compact` when Claude Code's responses become slow or repetitive. See [Claude Code Interaction Guide](#claude-code-interaction-guide) below.

Default tabs cannot be closed by the agent (closing returns an error). They persist for the entire job.

### Tab Naming Convention

Tabs use a **type-prefixed naming scheme**:

| Tab Type | Name Format | Examples |
|----------|-------------|---------|
| Default shell | `shell` | `shell` (always this exact name) |
| Claude Code | `claude-code` | `claude-code` (always this exact name) |
| Additional shells | `shell - <name>` | `shell - build`, `shell - tests` |
| SSH sessions | `ssh - <name>` | `ssh - gpu-box`, `ssh - prod-server` |
| Python REPLs | `python - <name>` | `python - analysis`, `python - debug` |
| Jupyter consoles | `jupyter - <name>` | `jupyter - data`, `jupyter - ml` |
| Other | `<type> - <name>` | `node - repl`, `psql - prod` |

The type prefix is system-enforced (based on the command or explicit parameter). The `<name>` suffix is agent-chosen. This keeps the injection readable while indicating what each tab is:

```
--- shell (last 5 lines) ---
--- claude-code (running, 8m) [NEW OUTPUT] ---
--- ssh - gpu-box (idle, 15m) ---
```

Validation: lowercase alphanumeric + hyphens for the agent-chosen part, max 20 chars, duplicates rejected.

### Tool Surface

| Tool | Parameters | Description |
|------|-----------|-------------|
| `shell_open` | `name`, `command?`, `type?`, `cols?`, `rows?` | Open a new tab, optionally start a process |
| `shell_send` | `name`, `input`, `enter?` | Send keystrokes or a command to a tab |
| `shell_read` | `name`, `lines?`, `since_cursor?`, `wait?`, `timeout?` | Read output from a tab |
| `shell_close` | `name` | Close a tab (not allowed for default tabs) |
| `shell_list` | — | List open tabs with status |
| `shell_run` | `command`, `timeout?`, `working_dir?` | Run a command in the default shell (replaces `run_command`) |

#### `shell_open(name, command?, type?, cols?, rows?)`

Creates a new tmux window. If `command` is provided, it's executed immediately (e.g., `shell_open("gpu-box", command="ssh user@gpu-server", type="ssh")`). If not, opens a bash shell. The `type` parameter sets the prefix (auto-detected from `command` when possible: `ssh` for ssh commands, `python` for python/ipython, etc.).

Returns: confirmation with full tab name and PID.

Max open tabs: configurable, default 6 (including the default tabs). Opening beyond the limit returns an error listing current tabs.

#### `shell_send(name, input, enter=True)`

Sends text to the named tab via `tmux send-keys`. By default appends Enter. Set `enter=False` for partial input or interactive prompts (e.g., answering `y/n`).

Returns: confirmation only (read output separately via `shell_read`).

#### `shell_read(name, lines=50, since_cursor=False, wait=0, timeout=30)`

Reads output from the named tab's scrollback buffer via `tmux capture-pane`.

- `lines`: Number of lines to return from the end of the buffer (default 50, max 200). Acts like scrolling up in the terminal — the agent can request more lines to see earlier output.
- `since_cursor`: If true, return only output since the last `shell_read` call on this tab. This is the "show me what's new" mode.
- `wait`: Seconds to wait before reading. For "send command, wait 2s, read result" patterns. Uses the sentinel-marker polling approach (see Implementation) for reliability rather than a blind sleep.
- `timeout`: Max seconds to wait if `wait` is set.

Returns: the output text, plus a status header:

```
[tab: shell | status: idle | pid: 12345 | lines: 50/1203 | scroll: use lines=100 to see more]
--- output ---
...
```

#### `shell_close(name)`

Kills the process (if running) and destroys the tmux window. Returns an error for default tabs (`shell`, `claude-code`).

#### `shell_list()`

Returns a summary of all open tabs:

```
Open shells (4/6):
  shell             | idle    | pid 12345 | uptime 25m   | last output 10s ago
  claude-code       | running | pid 12400 | uptime 25m   | last output 1m ago
  ssh - gpu-box     | idle    | pid 12500 | uptime 15m   | last output 3m ago
  shell - build     | running | pid 12600 | uptime 30s   | last output 5s ago
```

#### `shell_run(command, timeout=120, working_dir=None)`

Executes a command **in the default `shell` tab** using the sentinel marker pattern. Waits for completion, then returns the output. The command and its output remain in the shell's scrollback — the agent (or a human reviewing the workspace) can scroll up to see the full history.

This is the primary tool for simple commands and the drop-in replacement for `run_command`. The key difference from the old model: output accumulates in a persistent tab rather than vanishing after each call.

### State Injection

Open shell status is injected into the agent's context as a transient message every turn, using the same synthetic AIMessage + ToolMessage mechanism as workspace.md and todo injection.

The injection shows the **last 5 lines** of each tab by default — enough to see the most recent command result without bloating context. Tabs with new output since the last turn are flagged with `[NEW OUTPUT]`. Tabs with no changes show a one-line summary.

The default `shell` tab always shows its last 5 lines (the agent's recent command history). This is the terminal equivalent of glancing at your shell — you see what just happened. If the agent needs more detail, it calls `shell_read("shell", lines=50)` to scroll up.

Example injection:

```xml
<open_shells>
[4 tabs open]

--- shell (idle, 25m) [NEW OUTPUT] ---
$ pytest tests/ -x
FAILED tests/test_auth.py::test_login - AssertionError
======= 1 failed, 49 passed in 8.2s =======
$ ruff check src/
All checks passed!

--- claude-code (running, 25m) [NEW OUTPUT] ---
Working on implementing auth module...
  Created src/auth/middleware.py
  Modified src/routes/login.py
  Running tests...

--- ssh - gpu-box (idle, 15m, no new output) ---

--- shell - build (exited, code=0, 2m ago) ---
Build completed successfully.
</open_shells>
```

The agent sees its command history in the `shell` tab, Claude Code's progress in `claude-code`, and can tell at a glance that the GPU SSH session is idle and the build finished. If it needs more detail on any tab, it uses `shell_read` with a higher line count.

**Context budget math:** At 5 lines per active tab and 1 line per idle tab, worst case with 6 tabs (4 active, 2 idle) is ~22 lines — less than the todo injection. Acceptable.

Config:

```yaml
shell:
  max_tabs: 6
  inject_lines_per_tab: 5       # Lines shown per active tab in injection
  max_read_lines: 200           # Max lines from shell_read tool
  scrollback_limit: 5000        # Max scrollback buffer per tab (tmux history-limit)
  default_timeout: 120          # Default for shell_run
  idle_timeout: 1800            # Auto-close idle tabs after 30min (0 = disabled, default tabs exempt)
```

### Notification System (Future)

While the transient injection gives the agent a snapshot every turn, a notification system would enable **reactive** behavior — the agent gets explicitly told "something happened" rather than having to notice it in the injection.

Possible implementation:

1. A background monitor watches tmux panes for configurable events:
   - Process exited (with exit code)
   - Output matches a regex pattern (e.g., `ERROR`, `FAILED`, `Plan ready`)
   - Tab has been idle for N seconds after activity
   - Output volume spike (sudden burst of errors)

2. Events are queued and injected as a `<shell_notifications>` block in the next turn:

```xml
<shell_notifications>
[!] build: process exited with code 1 (2 tests failed)
[!] claude: output matched pattern "needs your input"
</shell_notifications>
```

3. The agent can configure watches via a tool:
   - `shell_watch(name, pattern="FAILED|ERROR", on_exit=True)`
   - `shell_unwatch(name)`

This is a v2 feature. The injection approach works well enough for the initial implementation, and real usage will tell us whether explicit notifications add meaningful value over the `[NEW OUTPUT]` delta flags.

## Use Cases

### 1. Default Shell — Command History Like a Human

The default `shell` tab is always open. `shell_run` executes commands there and returns output, but the scrollback keeps everything:

```
shell_run("pytest tests/ -x")
→ Returns: "1 failed, 49 passed in 8.2s" (plus failure details)
→ The shell tab now shows: $ pytest tests/ -x ... all output ... $

shell_run("ruff check src/")
→ Returns: "All checks passed!"
→ The shell tab now shows both commands in history

→ Later, the agent wants to review what it ran:
shell_read("shell", lines=100)
→ Sees the full command history, like scrolling up in a terminal
```

The injection shows the last 5 lines of the shell at all times — the agent always knows what its most recent command was and what it returned.

### 2. Async Work — Start and Continue

For long-running commands, the agent can use `shell_send` on any tab and continue working:

```
shell_open("build", command="", type="shell")
shell_send("shell - build", "pytest tests/ -v --slow")
→ Immediately returns
→ Agent continues editing files
→ Next turn: injection shows "shell - build: exited, code=1, 2 failed"
→ shell_read("shell - build", lines=30, since_cursor=True)
→ Reads just the failure summary
```

### 3. Interactive Claude Code (Supervisor Pattern)

The `claude-code` tab is auto-started. The agent uses it throughout the job:

```
→ claude-code tab is already running from job start

shell_send("claude-code", "Read src/routes/ and analyze the current auth patterns. Don't make changes yet.")
→ Agent continues working on other todos
shell_read("claude-code", since_cursor=True)
→ Sees Claude Code's analysis

shell_send("claude-code", "Good analysis. Now create src/auth/middleware.py with JWT validation based on those patterns.")
→ Agent monitors progress, course-corrects
shell_read("claude-code", since_cursor=True)
→ Claude Code created the file, shows diff

shell_send("claude-code", "/compact")
→ Agent manages Claude Code's context proactively

shell_send("claude-code", "/plan Let's implement the login redirect. Here are the requirements: ...")
→ Agent puts Claude Code into plan mode for a complex subtask
shell_read("claude-code", since_cursor=True)
→ Reviews the plan
shell_send("claude-code", "Looks good, proceed.")
```

The agent becomes a **supervisor** that steers Claude Code iteratively. This is the interaction pattern that motivated the entire design.

### 4. Remote Server Management (SSH)

```
shell_open("gpu-box", command="ssh user@gpu-box", type="ssh")
shell_send("ssh - gpu-box", "nvidia-smi")
shell_read("ssh - gpu-box", wait=2)
→ Sees GPU status
shell_send("ssh - gpu-box", "nohup python train.py > train.log 2>&1 &")
shell_send("ssh - gpu-box", "echo $!")
→ Gets PID, continues other work
→ Later:
shell_send("ssh - gpu-box", "tail -20 train.log")
shell_read("ssh - gpu-box", wait=2)
→ Checks training progress without reconnecting
```

No agent tooling needed on the remote machine. The agent uses the same SSH session a human would.

### 5. Jupyter / REPL Sessions

```
shell_open("analysis", command="jupyter console --kernel python3", type="jupyter")
shell_send("jupyter - analysis", "import pandas as pd; df = pd.read_csv('data.csv'); df.shape")
shell_read("jupyter - analysis", wait=3)
→ (1000, 25)
shell_send("jupyter - analysis", "df.describe()")
shell_read("jupyter - analysis", wait=2)
→ Gets statistical summary
→ Iterative data exploration, just like a human in a notebook
```

### 6. Dev Server + Live Testing

```
shell_open("frontend", command="npm run dev", type="shell")
→ Dev server starts in background
shell_read("shell - frontend", wait=5)
→ "Server running on http://localhost:3000"
shell_run("curl -s http://localhost:3000/api/health | jq .")
→ Tests the running server via the default shell
→ Agent edits code, server hot-reloads
shell_read("shell - frontend", since_cursor=True)
→ Checks for reload errors
```

## Claude Code Interaction Guide

When `claude_code` is in the agent's tool config, a `claude-code` tab is started automatically at job begin. The agent should treat Claude Code as a **junior developer** it supervises — giving specific instructions, reviewing output, and course-correcting.

### Core Principles

1. **Give focused tasks, not giant prompts.** Instead of "implement the entire auth module," break it into steps: "Read src/routes/ and summarize the patterns" → "Create src/auth/middleware.py with JWT validation" → "Write tests for the middleware."

2. **Read before sending the next instruction.** Always `shell_read("claude-code", since_cursor=True)` to see what Claude Code did before sending the next task. Course-correct if it went off track.

3. **Use `/compact` proactively.** When Claude Code's responses become slow, repetitive, or it starts losing track of earlier context, send `/compact` to compress its conversation history. Claude Code has auto-compacting, but proactive compaction produces better results.

4. **Use `/plan` for complex subtasks.** When delegating something that requires multiple files or an architectural decision, put Claude Code into plan mode: `shell_send("claude-code", "/plan <description>")`. Review the plan, provide feedback, then approve.

5. **Don't babysit.** After sending an instruction, the agent should continue its own work (editing files, running tests in the default shell, etc.) and check back on Claude Code in the next turn via the injection or `shell_read`.

### When to Compact

- Claude Code's output is getting noticeably slower
- It repeats information it already stated
- It starts making errors on files it previously handled correctly
- After completing a significant subtask (good checkpoint for compaction)

### When to Start a Fresh Session

If Claude Code gets badly confused (contradicting itself, stuck in a loop, producing broken code repeatedly), the agent should:

```
shell_send("claude-code", "/exit")
→ Wait for exit
shell_send("claude-code", "claude --model <model> --resume")
→ Or start fresh without --resume if the context is poisoned
```

### Example Workflow

```
Phase 1 (Strategic): Agent plans the auth module, creates todos
Phase 2 (Tactical):
  Todo 1: "Implement JWT middleware"
    → shell_send("claude-code", "Create src/auth/middleware.py with JWT validation. Use PyJWT. Read src/routes/login.py first to understand the existing patterns.")
    → Agent works on Todo 2 while Claude Code works
    → shell_read("claude-code", since_cursor=True) → Reviews output
    → shell_run("pytest tests/test_auth.py -x") → Tests Claude Code's work
    → If tests fail: shell_send("claude-code", "The test failed: <paste error>. Fix the issue.")
  Todo 2: "Update API routes"
    → Agent does this directly (simple edit)
  Todo 3: "Integration tests"
    → shell_send("claude-code", "/plan Write integration tests for the auth flow. Cover: login, token refresh, logout, expired token.")
    → shell_read("claude-code", since_cursor=True) → Reviews plan
    → shell_send("claude-code", "Good plan, but also add a test for invalid token format. Proceed.")
```

## Implementation

### Backend: libtmux

[libtmux](https://github.com/tmux-python/libtmux) (v0.53+, actively maintained, Python 3.10+) provides a typed, object-oriented API over tmux. This is the same library OpenHands migrated to after hitting reliability issues with pexpect.

Why tmux + libtmux over alternatives:

| Alternative | Why Not |
|-------------|---------|
| **pexpect** (raw PTY + prompt matching) | OpenHands tried this, migrated away. Interactive commands, password prompts, and unusual output break prompt matching. |
| **pyte** (in-process VT100 emulator) | Dormant (last release Nov 2023). Works for output parsing but does not provide session management, persistence, or multiplexing. |
| **subprocess.run** (stateless) | Our `shell_run` preserves this path. But it can't do async, interactivity, or persistent connections. |
| **pymux** (pure Python tmux clone) | Abandoned. Required old prompt_toolkit versions. |
| **Custom PTY manager** | Reinventing tmux poorly. No session persistence, no multiplexing, every edge case (SIGWINCH, job control, zombie processes) must be handled manually. |

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  ShellManager (src/tools/coding/shell_manager.py)                │
│                                                                  │
│  DEFAULT TABS (always open, cannot be closed)                    │
│  ┌────────────────┐  ┌──────────────────┐                        │
│  │ ShellTab       │  │ ShellTab         │                        │
│  │ name: shell    │  │ name: claude-code│                        │
│  │ type: shell    │  │ type: claude-code│                        │
│  │ closeable: no  │  │ closeable: no    │                        │
│  │ cursor: 342    │  │ cursor: 89       │                        │
│  └───────┬────────┘  └────────┬─────────┘                        │
│          │                    │                                   │
│  AGENT-OPENED TABS                                               │
│  ┌──────────────────┐  ┌───────────────────┐                     │
│  │ ShellTab         │  │ ShellTab          │                     │
│  │ name: ssh-gpu-box│  │ name: shell-build │      ... (max 6     │
│  │ type: ssh        │  │ type: shell       │       total)        │
│  │ closeable: yes   │  │ closeable: yes    │                     │
│  │ cursor: 42       │  │ cursor: 0         │                     │
│  └───────┬──────────┘  └────────┬──────────┘                     │
│          │                      │                                │
│          └──────────┬───────────┘                                │
│                     │                                            │
│           ┌─────────▼─────────┐                                  │
│           │  libtmux.Session  │                                  │
│           │  "agent_<job_id>" │                                  │
│           └───────────────────┘                                  │
└──────────────────────────────────────────────────────────────────┘
```

**`ShellManager`** — Lifecycle-managed singleton per job. Created when the agent starts (opens default tabs immediately), destroyed when the job ends. Owns the libtmux `Session` object.

**`ShellTab`** — Tracks per-tab state: name, type (shell/claude-code/ssh/python/jupyter/...), libtmux `Window`/`Pane` reference, read cursor position (for `since_cursor`), creation time, last activity timestamp, closeable flag.

**Default tabs** — `shell` and `claude-code` (if configured) are opened at job start. They cannot be closed by the agent and are exempt from `idle_timeout`.

**tmux session** — One detached tmux session per job (`agent_<job_id>`), with one window per tab. Sessions are created detached (`new-session -d`) so no TTY allocation is needed — critical for running inside containers and headless agent processes. Isolated from other jobs. Cleaned up on job completion.

### Key Implementation Details

**Reliable output capture — the sentinel marker pattern:**

The industry-standard approach for knowing when a command has finished in a persistent terminal. Used by OpenHands, SWE-agent, and others:

```python
import time
import uuid

SENTINEL = f"__DONE_{uuid.uuid4().hex[:8]}__"

def run_and_capture(pane, command, timeout=30.0):
    """Send a command and reliably capture its output."""
    pane.send_keys(f'{command}; echo "{SENTINEL} $?"')
    start = time.time()
    while time.time() - start < timeout:
        output = pane.capture_pane(start=-1000)  # last 1000 lines
        text = '\n'.join(output)
        if SENTINEL in text:
            # Extract output between command and sentinel
            # Parse exit code from sentinel line
            return parse_output(text, SENTINEL)
        time.sleep(0.2)
    raise TimeoutError(f'Command did not complete within {timeout}s')
```

The sentinel is a unique marker (UUID-based to avoid collisions with program output) appended after the command. Polling `capture_pane` until the sentinel appears guarantees we capture complete output regardless of timing. The exit code is embedded in the sentinel line.

This is what `shell_run` (the synchronous convenience tool) uses internally. For async usage (`shell_send` + later `shell_read`), the agent polls manually.

**Output capture (plain text, no ANSI):**

```python
# libtmux capture_pane strips ANSI by default
output_lines = pane.capture_pane()  # Returns list of strings, clean text

# For explicit control:
output_lines = pane.capture_pane(
    start=-50,                # Last 50 lines (negative = from end)
    escape_sequences=False,   # Default: strips ANSI (True would preserve them)
)
```

Known edge cases with `capture_pane`:
- Long lines wrap based on pane width — use `pane.capture_pane(join_wrapped=True)` to rejoin them (requires recent libtmux)
- First line can be missing in edge cases on Alpine Linux (tmux issue #1663) — mitigated by not using `remain-on-exit`
- Trailing whitespace on lines — cosmetic, stripped during post-processing

**Process status detection:**

```python
# libtmux provides pane properties
pane_pid = pane.pane_pid
is_dead = pane.pane_dead  # "1" if process exited

# Or via tmux format strings for more detail
info = pane.cmd('display-message', '-p',
    '#{pane_pid} #{pane_dead} #{pane_current_command}')
```

**Cursor tracking for `since_cursor`:**

```python
class ShellTab:
    def __init__(self, name, pane):
        self.name = name
        self.pane = pane
        self.read_cursor = 0  # Lines read so far

    def read_since_cursor(self, max_lines=200):
        all_output = self.pane.capture_pane(start=0)
        new_output = all_output[self.read_cursor:]
        self.read_cursor = len(all_output)
        if len(new_output) > max_lines:
            new_output = new_output[-max_lines:]
        return new_output
```

**Scrollback buffer management:**

Set `history-limit` at session creation time (cannot be changed after pane creation):

```python
server = libtmux.Server()
session = server.new_session(
    session_name=f"agent_{job_id}",
    kill_session=True,  # Clean up any stale session with same name
    x=200, y=50,        # Pane dimensions
)
# Set scrollback limit for the session
session.set_option('history-limit', 5000)
```

Memory impact at 5000 lines: ~2-5 MB per pane. With 6 tabs, worst case ~30 MB — negligible.

**Cleanup:**

```python
def cleanup(self):
    """Kill the entire tmux session. Called on job end."""
    try:
        self.session.kill()
    except libtmux.exc.LibTmuxException:
        pass  # Session already dead
```

### Container Requirements

tmux must be installed in the agent container. It runs headless (detached sessions only), so no terminal emulator or display server is needed.

```dockerfile
# Add to Dockerfile.agent
RUN apt-get update && apt-get install -y tmux && rm -rf /var/lib/apt/lists/*
# Fedora/RHEL: RUN dnf install -y tmux && dnf clean all
```

tmux inside containers works reliably when sessions are created detached (`new-session -d`). No TTY allocation needed. This is confirmed by OpenHands, agent-infra/sandbox, and multiple tmux MCP servers running in Docker.

The tmux server process starts automatically on first session creation and exits when all sessions are destroyed. In our case, `ShellManager.cleanup()` kills the session on job end, and tmux exits on its own.

### Integration Points

**Tool Registry:**
- New tools registered under the `coding` category (same as `run_command`)
- `shell_run` replaces `run_command` as the synchronous convenience tool
- `run_command` kept as an alias for backward compat (maps to `shell_run` internally)
- Phase availability: all shell tools in both strategic and tactical phases

**Transient Injection:**
- `ShellInjector` (new class in `src/core/`) generates the `<open_shells>` block
- Follows the same synthetic AIMessage + ToolMessage pattern as workspace.md injection
- Called from the same injection point in the execute node
- Skipped if no tabs are open (zero overhead when not using shells)
- Tabs with no new output since last injection get condensed to one line

**Config Schema:**

```yaml
# In config/schema.json and defaults.yaml:
shell:
  max_tabs: 6
  inject_lines_per_tab: 5        # Lines shown per active tab in injection
  max_read_lines: 200            # Max lines from shell_read tool
  scrollback_limit: 5000         # tmux history-limit per pane
  default_timeout: 120           # Default for shell_run
  idle_timeout: 1800             # Auto-close idle agent-opened tabs (default tabs exempt)
  blocked_commands: [sudo, reboot, shutdown, poweroff, halt, init, systemctl]
  sandbox: true                  # Same workspace sandbox as run_command
  auto_start_claude_code: true   # Start claude-code tab if claude_code is in tools
```

**Workspace State:**
- `ShellManager` state (open tab names and metadata, NOT tmux session handles) is serialized into the LangGraph checkpoint
- On resume (`--resume`), tabs are NOT restored (tmux session is gone) — agent gets a clean slate with a note in the injection: `"Previous shell sessions were closed due to job restart. Open new tabs as needed."`

**Dependency:**
- `libtmux>=0.50` added to `requirements.txt`
- `tmux` added to `Dockerfile.agent` (system package)

## Migration Path

### Phase 1: Core Infrastructure

1. Add `libtmux` to `requirements.txt`, `tmux` to `Dockerfile.agent`
2. Implement `ShellManager` and `ShellTab` in `src/tools/coding/shell_manager.py`
   - Default `shell` tab auto-created at init
   - Tab type system with naming conventions
   - Closeable flag (default tabs cannot be closed)
3. Implement tool functions: `shell_open`, `shell_send`, `shell_read`, `shell_close`, `shell_list`, `shell_run`
4. `shell_run` executes in the default `shell` tab (sentinel marker pattern)
5. Register all tools in `TOOL_REGISTRY` under `coding` category
6. Alias `run_command` → `shell_run` for backward compat
7. Add `shell` config section to `config/schema.json` and `defaults.yaml`
8. Write tool documentation for `workspace/tools/` auto-generation
9. Test: basic command execution, command history in default shell, multiple tabs, sentinel-based completion detection

### Phase 2: State Injection

1. Implement `ShellInjector` in `src/core/shell_injection.py`
2. Wire into the execute node's injection pipeline (alongside workspace.md and todos)
3. Smart injection: 5 lines per active tab, 1-line summary for idle tabs, `[NEW OUTPUT]` flags
4. Default shell always shows last 5 lines (recent command history)
5. Test context budget with multiple concurrent tabs
6. Tune `inject_lines_per_tab` based on real usage

### Phase 3: Claude Code Integration

1. Auto-start `claude-code` tab when `claude_code` is in agent's tool config
2. Add Claude Code Interaction Guide to coding agent instructions (from this document)
3. Teach agent: focused tasks, read before next instruction, proactive `/compact`, `/plan` for complex subtasks
4. Handle session recovery (Claude Code crash → agent detects via injection → restart)
5. Test supervisor pattern: agent opens Claude Code, steers it across multiple turns, manages its context
6. Evaluate: keep old `claude_code` tool as a convenience wrapper, or deprecate entirely?

### Phase 4: Notifications (Optional)

1. Background monitor thread watching tmux panes for events
2. `shell_watch` / `shell_unwatch` tools for pattern-based alerts
3. Notification injection block (`<shell_notifications>`)
4. Evaluate: does this meaningfully outperform the ambient injection with `[NEW OUTPUT]` flags?

## Design Decisions

Decisions made based on prior art research and our architecture:

### D1: tmux + libtmux, not pexpect or raw PTY

**Decision:** Use tmux as the backend, libtmux as the Python API.

**Rationale:** OpenHands migrated from pexpect to libtmux ([PR #4881](https://github.com/All-Hands-AI/OpenHands/pull/4881)) because pexpect's prompt-matching broke on interactive commands, passwords, and conda activations. We'd hit the same problems. tmux gives us session persistence, ANSI stripping, multiplexing, and decades of edge-case handling for free. libtmux (v0.53, actively maintained, Python 3.10+) provides a typed API.

### D2: Sentinel marker pattern for command completion

**Decision:** Use UUID-based sentinel markers to detect when a command has finished, not timing-based waits.

**Rationale:** This is the standard pattern across OpenHands, SWE-agent, and others. `send_keys(f'{cmd}; echo __DONE_abc123__')` followed by polling `capture_pane` until the sentinel appears. Reliable regardless of command duration. Timing-based waits (`sleep(2)`) are fragile and either too slow or too fast.

### D3: Persistent default shell with command history

**Decision:** All `shell_run` commands execute in a persistent default `shell` tab. The scrollback accumulates the agent's command history, just like a human's terminal. The injection shows the last 5 lines.

**Rationale:** mini-swe-agent scores 74% on SWE-bench Verified with stateless execution, proving that simple commands don't need dedicated tabs. But running everything in one persistent shell gives the agent (and human reviewers) a natural command history to scroll through. The agent uses `shell_run` for most commands (synchronous, returns output) and `shell_open` only when it needs a separate concurrent process (SSH, dev server, REPL). This is how a human works — one main terminal for most things, extra tabs only when needed.

### D4: Detached tmux sessions, no TTY required

**Decision:** Always create tmux sessions detached (`new-session -d`). Never attach.

**Rationale:** Agent processes run headless inside containers without a TTY. Detached sessions work reliably in this context — confirmed by OpenHands, agent-infra/sandbox, and tmux MCP servers. All interaction is through `send-keys` and `capture-pane`, never through an attached terminal.

### D5: Scrollback limit of 5000 lines

**Decision:** Default `history-limit` of 5000 lines per pane.

**Rationale:** Memory impact is ~2-5 MB per pane at this setting. With 6 tabs, worst case ~30 MB. Going higher (50K+) risks memory bloat, especially with programs that emit heavy RGB color sequences (a [tmux bug in 2025](https://github.com/tmux/tmux/issues/4859) caused 48GB memory usage from RGB color data in scrollback). 5000 lines is enough to review any reasonable command output. The agent can always re-run a command if it needs to see earlier output.

### D6: Smart injection — 5 lines per active tab, 1 line per idle

**Decision:** Inject last 5 lines for tabs with new content since last turn. Condense idle tabs to a one-line summary. The agent uses `shell_read` to see more when needed.

**Rationale:** 5 lines is enough to see the result of the most recent command or the current state of a running process. With 6 tabs (4 active, 2 idle), that's ~22 lines — less than the todo injection. The `[NEW OUTPUT]` flag draws the agent's attention to tabs that need it. The agent calls `shell_read("shell", lines=100)` to scroll up when it needs more context, just like a human scrolling in their terminal.

### D7: Always start Claude Code as a persistent tab

**Decision:** When `claude_code` is in the agent's tool config, a `claude-code` tab is auto-started at job begin and persists for the entire job.

**Rationale:** The main motivation for this feature is enabling interactive Claude Code supervision. Having the agent manually open a Claude Code session every time it needs one adds friction and wastes turns. By pre-starting it, the agent can delegate work to Claude Code at any point during the job without setup overhead. Claude Code has built-in auto-compacting, so long sessions don't break — but instructions guide the agent to use `/compact` proactively and `/plan` for complex subtasks.

### D8: Raw text output, no structured JSON modes

**Decision:** The tool layer does NOT request structured output from CLI tools (no `--json-report`, `--output-format json`). Output is always raw text with ANSI stripped.

**Rationale:** Research shows no major agent (OpenHands, SWE-agent, Devin, Claude Code) uses structured JSON from CLI tools. Aider's benchmark ([LLMs are bad at returning code in JSON](https://aider.chat/2024/08/14/code-in-json.html)) demonstrated that JSON mode actively hurts code quality — models make more syntax errors when dealing with JSON-escaped content. JSON also doubles token consumption. SWE-agent's Agent-Computer Interface (ACI) improved performance 3x through smart truncation and formatting of raw text, not by switching to JSON. The right approach: strip ANSI, truncate intelligently, add status prefixes — but keep the underlying output as raw text. The agent can choose verbose flags itself when needed (e.g., `pytest -v` or `pytest --tb=short`).

### D9: Type-prefixed tab naming with agent-chosen suffixes

**Decision:** Tabs use the format `<type> - <agent-name>` (e.g., `ssh - gpu-box`, `shell - build`). Default tabs use fixed names (`shell`, `claude-code`). Type is auto-detected from the command or set explicitly.

**Rationale:** Hybrid naming gives both readability and semantic meaning. The type prefix tells the agent (and human reviewers) what kind of session it is at a glance. The agent-chosen suffix indicates the purpose. This makes the injection self-documenting: `ssh - gpu-box` is obviously an SSH connection to a GPU server. Validation: lowercase alphanumeric + hyphens, max 20 chars for the suffix, duplicates rejected.

## Open Questions

### Resolved

- ~~**tmux availability in containers.**~~ Hard requirement. tmux runs detached without a TTY, works reliably in Docker/Podman. Small footprint (~500KB). Added to Dockerfile. (D4)

- ~~**Output ANSI handling.**~~ libtmux's `capture_pane()` strips ANSI by default. No secondary processing needed. (D1)

- ~~**Structured output (JSON) for CLI tools.**~~ No. Raw text with smart truncation and ANSI stripping. No agent in production uses `--json-report`. Aider's benchmark showed JSON mode hurts code quality. The agent can choose verbose flags itself. (D8)

- ~~**Security of persistent connections.**~~ `idle_timeout: 1800` (30 min) as default, not mandatory. Default tabs are exempt. The container sandbox is the real security boundary — persistent connections inside the sandbox have the same blast radius as reconnecting ones.

- ~~**Claude Code practical limits.**~~ Always start as persistent tab. Auto-compacting prevents crashes. Instructions guide `/compact` usage and `/plan` mode. See [Claude Code Interaction Guide](#claude-code-interaction-guide). (D7)

- ~~**Tab naming conventions.**~~ Type-prefixed with agent-chosen suffixes: `ssh - gpu-box`, `shell - build`. Default tabs use fixed names. (D9)

### Open

1. **Agent cognitive overhead (empirical).** Does the persistent shell model actually produce better results than pure `shell_run`? The design mitigates this (default shell for most things, extra tabs only when needed), but we should A/B test: run the same coding tasks with and without persistent tabs to measure the impact on task completion rate and quality.

2. **Output preprocessing in the tool wrapper.** SWE-agent's ACI improved performance 3x through smart output processing (empty output → "Command completed successfully", search results → condensed listing, file views → capped at 100 lines). Should `shell_run` apply similar preprocessing? Options:
   - Add a status prefix based on exit code ("Command failed (exit 1):" vs "Command succeeded:")
   - Replace empty stdout with an explicit success message
   - Apply head+tail truncation (keep first 20 + last 50 lines, drop middle) instead of tail-only
   - This can be iterated on post-launch based on observed failure modes.

3. **Claude Code session recovery.** What happens if the Claude Code process inside the `claude-code` tab crashes or exits unexpectedly? The agent needs to detect this (via injection showing "exited") and restart. Should `ShellManager` auto-restart default tabs, or should the agent handle it explicitly? Leaning toward explicit — the agent should notice and decide whether to restart clean or with `--resume`.

## Related

- [[coding_agent]] — Existing coding agent design (uses `run_command`)
- [[universal_shell_command]] — Security model for shell access
- [[cloud_workspace]] — Container architecture (where tmux would run)
- [[cli_wrapper]] — Claude Code integration (precursor to interactive sessions)
