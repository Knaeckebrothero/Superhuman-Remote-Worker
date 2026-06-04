---
tags:
  - agent-architecture
  - tooling
  - go-rewrite
  - planning
aliases:
  - Tool CLI Audit
  - CLI-First Tools
  - Tool Surface Audit
related:
  - "[[go_rewrite]]"
  - "[[git]]"
  - "[[tools_description]]"
  - "[[universal_shell_command]]"
  - "[[persistent_shell]]"
  - "[[shell]]"
---
# Tool CLI-Replaceability Audit

## Status

**Decision captured; implementation deferred to the [[go_rewrite|Go rewrite]].**
No Python change now — removing tools from a codebase that is about to be rewritten
wholesale is wasted effort. The lasting artifact is *this decision*, which becomes an
input to the rewrite's tool surface. Captured 2026-06-04.

## Why this doc exists

Three things converge here:

1. **The principle was never written down.** We already let the agent use the shell for
   everything the bespoke tools don't cover. The implicit rule — *git is a first-class
   CLI, so prefer the shell over a wrapper when the CLI is equivalent* — deserves to be
   explicit.
2. **`go_rewrite.md` has a gap.** Its *Tool Registry* section specs the `Tool` interface
   but says nothing about *which* tools should exist, or which should collapse into the
   shell. This doc fills that gap.
3. **It matches a trend the rewrite doc already names.** `go_rewrite.md` calls out the
   *"broader trend of LLMs moving away from rigid tool-call schemas toward natural code
   generation (e.g., agents writing `python -c` instead of calling structured tools)."*
   CLI-first tooling is that same trend applied to the tool layer.

## Principle

> Prefer the shell over a bespoke tool when (a) a canonical CLI is equivalent in power and
> (b) the model is fluent in it. Keep a structured tool only when it earns its place.

The agent already has a full Linux shell on the workspace pod/VM (see
[[persistent_shell]]). Any tool that is a thin wrapper over a command the agent could
just *run* is pure overhead: extra schema in every prompt, extra code to maintain, and a
narrower interface than the real CLI.

## Decision criteria

**Keep a structured tool when at least one holds:**

1. **No real CLI equivalent** — the capability is a service/API call, not a shell command
   (web search, browser automation, citation engine, knowledge graph, orchestrator).
2. **It enforces an invariant the shell cannot bypass** — e.g. the sudo-approval gate.
   *Caveat:* if the agent can already run the raw command via the shell, the "guardrail"
   was illusory. This is exactly why exposing `git_log` but withholding `git_reset` never
   actually restricted anything — the shell could `git reset --hard` all along.
3. **It does real shaping the raw command doesn't** — validated/structured output,
   document parsing, line-targeted edits, or idempotency. (The rewrite wants
   `tool_call_id` dedup; that is easier to guarantee on a typed tool than on raw shell.)

**Send it to the CLI when:**

4. **It is a thin pass-through that just shells out anyway** — pure overhead.
5. **The canonical interface is a well-known CLI the model is fluent in, and we expose
   only a fraction of it** — the agent hits the CLI for the rest regardless.

## First-pass category audit

Verdicts: **Keep** · **→ CLI** · **Hybrid** (keep a structured core, CLI the thin parts)
· **Revisit** (CLI exists but a real concern — credentials, injection — needs a look
during the rewrite). Source: `config/defaults.yaml` `tools:` section.

| Category | Tools | Verdict | Why |
|---|---|---|---|
| `git` | git_log, git_show, git_diff, git_status, git_tags | **→ CLI** | Thin pass-through to a world-class CLI the model is fluent in; we wrap 5 of git's hundreds of subcommands (criteria 4 + 5). Worked example below. |
| `workspace` | read/write/edit_file, list/search_files, move/rename/copy_file, file_exists, create/delete_directory, get_document_info | **Hybrid** | Keep `read_file`/`write_file`/`edit_file` (line-targeted, validated, idempotent — criterion 3) and `get_document_info` (document parsing — criterion 1). The thin filesystem verbs (`ls`/`mv`/`cp`/`mkdir`/`rmdir`/`test -f`/`grep`) are → CLI candidates. |
| `core` | next_phase_todos, todo_complete, todo_list, todo_rewind, mark_complete, job_complete | **Keep** | Agent control plane — drives the phase state machine and job lifecycle. Not OS operations; no CLI equivalent (criterion 1). |
| `shell` | run_command, shell_read (+ shell_execute in persistent mode) | **Keep** | This *is* the substrate everything else migrates toward. |
| `research` | web_search, extract_webpage, crawl_website, map_website, search_papers, get_paper_info, research_topic, browse_website, download_paper, download_from_website | **Hybrid** | Search/extract/crawl need backing services (criterion 1) → keep. The pure fetchers (`download_paper`, `download_from_website`) are `curl`/`wget` → CLI candidates. |
| `browser_direct` | browser_navigate, snapshot, click, type, select, scroll, screenshot, back, close | **Keep** | CDP/Playwright control — no CLI equivalent (criterion 1). |
| `citation` | cite_document, cite_web, list/get/edit_citation, list_sources, annotate/tag_source, get_annotations, search_library, generate_bibliography | **Keep** | Backed by the citation engine/store — no CLI equivalent. |
| `knowledge` | kb_write, kb_update, kb_read, kb_list, kb_search, kb_related, kb_contradictions, kb_provenance, kb_unanswered, kb_export | **Keep** | Project KB over Neo4j + pgvector — structured API, no CLI equivalent. |
| `communication` | send_message | **Keep** | Agent→human messaging with recipient/approval policy — an app-level action, not a shell command. |
| `delegation` | delegate_work, resume_delegation_child | **Keep** | Spawns child jobs as workspace branches via the orchestrator — control plane. |
| `orchestrator` | (injected for persistent agents) | **Keep** | Orchestrator MCP/API surface — no CLI equivalent. |
| `evaluation` | (injected for critic agents) | **Keep** | Critic approve/return verdicts — control plane for reviewer agents. |
| `graph` · `sql` · `mongodb` · `cloud` | (injected per attached datasource) | **Revisit** | CLI shells exist (`cypher-shell`, `psql`, `mongosh`, WebDAV clients), but these tools inject credentials and shape results. Whether the shell can get the same connection/credential handling safely is a rewrite-time question. |

## The pattern

The migration is **surgical, not sweeping.** Git is the standout. The only secondary
candidates are the thin filesystem verbs in `workspace`, the pure fetchers in `research`,
and *maybe* the datasource DB tools (pending the credential question). Everything else
earns its keep under criteria 1–3. "Move more stuff to the CLI" is real but small — it
does **not** mean deleting most of the tool surface.

## Worked example: `git` → CLI

The five agent-facing git tools are read-only inspectors. Their CLI equivalents are
exactly what the agent already runs for everything they don't cover:

| Tool | CLI equivalent |
|---|---|
| `git_log` | `git log --oneline -10` |
| `git_show` | `git show <ref> [--stat]` |
| `git_diff` | `git diff [ref1] [ref2] [-- <path>]` |
| `git_status` | `git status` |
| `git_tags` | `git tag -l "<pattern>"` |

**What does NOT get removed.** Auto-commit-on-todo and phase tagging are *not* agent
tools — they are internal machinery. `TodoManager.complete()` and the phase-transition
logic call `GitManager` methods directly (`commit()`, `tag()`, `init_repository()`); the
agent never invokes them. They stay, and port to Go as internal infrastructure, not as
tools. Removing the five inspectors does not touch versioning. See [[git]].

**What moves from code into the prompt (or a small shell helper):**

- **Job-scoping of tags.** `git_tags` auto-prefixes the pattern with `job_id[:8]-`. Raw
  `git tag -l` in a shared project repo shows *all* jobs' tags. Preserve this as prompt
  guidance ("your phase tags are prefixed with your job id") or a workspace `git`
  alias — not as a wrapped tool.
- **Output truncation.** The tools cap at 500 lines / 10k words. The shell has its own
  caps (`max_read_lines: 200`, ~50k chars) plus `shell_read` paging, so large `git log`
  / `git diff` output is already bounded. Limits differ but the protection exists.

**Rewrite action items:**

- Do **not** port `src/tools/git/` to Go.
- Drop the `git:` category from the tool config.
- Reword the strategic/tactical prompts: the review instructions currently say "use
  `git_log` / `git_diff`" — change to the `git` CLI equivalents. (See the prompt blocks
  in [[git]].)
- Keep `GitManager` (auto-commit + phase tags) as internal infrastructure.

**Dogfooding validates this for free.** The rewrite is the next major effort, and the
agent will help build it — meaning it will do a *lot* of real-repo git through the shell
during the rewrite itself. That is a live test of "CLI git is enough" *before* we commit
to it in Go. If the agent struggles, we keep a thin tool; if it sails through (likely —
it already uses CLI git for everything the five tools don't cover), the decision
validates itself.

## Open items / revisit during the rewrite

- **Datasource DB tools** (`graph`/`sql`/`mongodb`/`cloud`): can `psql`/`mongosh`/
  `cypher-shell` get connection + credential injection as safely as the wrapped tools? If
  yes, they become CLI candidates; if not, keep.
- **`workspace` split**: confirm the keep-core (`read`/`write`/`edit`/`get_document_info`)
  vs → CLI (filesystem verbs) line during the rewrite, when the Go tool surface is
  designed deliberately rather than ported 1:1.
- **Per-tool depth**: this is a first pass at the *category* level. Per-tool detail gets
  filled in as the rewrite touches each category.
