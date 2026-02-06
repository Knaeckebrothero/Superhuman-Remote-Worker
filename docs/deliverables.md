# Agent Deliverables

## Problem

The workspace (`workspace/job_<uuid>/`) was designed as the agent's internal working memory — `workspace.md`, `plan.md`, `todos.yaml`, etc. are all scratchpad files. "Real" outputs go into databases (PostgreSQL for requirements/citations, Neo4j for the knowledge graph).

There is no clean mechanism for the agent to produce **user-facing deliverables** — reports, analysis documents, exported summaries, compliance checklists, etc. — things a user would actually download or read as the end product of a job.

## Current Workspace Contents

| File | Purpose | Internal or Deliverable? |
|------|---------|--------------------------|
| `workspace.md` | Long-term memory / context | Internal |
| `plan.md` | Strategic plan | Internal (possibly interesting to users) |
| `todos.yaml` | Current task list | Internal |
| `archive/` | Phase retrospectives, archived todos | Internal (possibly interesting to users) |
| `tools/` | Auto-generated tool documentation | Internal |
| `documents/` | Input documents | Input data |
| `analysis/` | Working files | Internal |

There is currently no designated place for user-facing outputs.

## Proposed Solution: Git-Based Delivery

The agent already supports git versioning in the workspace (`workspace.git_versioning: true`), with auto-commits on todo completion and phase tags. The idea is to push the agent's work to a git remote when the job completes, so the user can fetch/clone/pull it.

### Why Git?

- Infrastructure is half-built already (workspace git versioning, phase tags, auto-commits)
- Git history shows the agent's progression through phases
- Phase tags mark strategic boundaries
- Users get diffs, blame, history for free
- Multiple delivery formats work (Markdown renders on GitHub/Gitea, etc.)
- Collaboration is natural (users can fork, comment, create issues)
- Familiar tooling — no custom UI needed for basic access

## Approaches

### A) Push the Whole Workspace

Push everything as-is. The user sees the full workspace including internal reasoning.

**Pros:**
- Simplest implementation
- Full transparency — users see how the agent worked
- Git history with phase tags tells the complete story
- No need to distinguish internal vs. deliverable files

**Cons:**
- Cluttered with internal files the user may not care about
- `workspace.md` and `todos.yaml` are not meant for human consumption
- May expose internal reasoning the user finds confusing

### B) Separate Deliverables Directory

The agent writes user-facing outputs to a `deliverables/` folder in the workspace. Only that folder (or a curated subset) gets pushed to the remote.

**Pros:**
- Clean separation of concerns
- User only sees polished output
- Agent can structure deliverables intentionally (table of contents, index, etc.)

**Cons:**
- More complex — agent needs a `create_deliverable` tool or convention
- Loses the transparency of seeing the agent's process
- Requires the agent to explicitly produce deliverables (extra work/tokens)

### C) Branch-Based Separation

The workspace lives on a `work` branch. When the job completes, the agent creates a clean `deliverables` branch with only the final outputs.

**Pros:**
- Best of both worlds — full history on `work`, clean output on `deliverables`
- Users can choose their level of detail

**Cons:**
- Most complex to implement
- Branch management adds overhead

## Related: Existing Git Infrastructure

The workspace already has extensive git versioning support — see [docs/git.md](git.md) for the full design. Key pieces that are relevant to deliverables:

| Existing Feature | Relevance to Deliverables |
|------------------|---------------------------|
| `GitManager` class (`src/managers/git_manager.py`) | Would need extensions for remote operations (`push`, `remote add`) |
| Auto-commit on todo completion | Deliverable files get versioned automatically as the agent works |
| Phase tags (`phase-N-tactical-complete`) | Users can see which phase produced which deliverable |
| Git tools (`git_log`, `git_diff`, etc.) | Currently read-only; delivery requires write operations (`push`) |
| `.gitignore` support | Can be used to exclude internal files from delivery |
| `phase_state.yaml` | Tracks current phase — could include delivery status |

### Required Git Extensions

To support git-based delivery, the `GitManager` would need new capabilities:

| New Method | Purpose |
|------------|---------|
| `add_remote(name, url)` | Configure the delivery remote |
| `push(remote, branch)` | Push to the delivery remote |
| `create_branch(name)` | For branch-based separation (approach C) |
| `checkout(branch)` | Switch branches for delivery preparation |
| `cherry_pick(refs)` | Selectively move deliverable commits (approach C) |

These would be **deferred tools** (v2+) per the git.md design, since they are write operations that affect external state.

## Open Questions

### What gets pushed?
- Whole workspace vs. curated subset vs. dedicated deliverables directory
- Should internal files (workspace.md, todos.yaml) be visible to users?

### Remote repository structure
- One repo per job? Per agent config? Shared repo with job-specific branches?
- Who creates the remote? Orchestrator auto-creates (Gitea/GitHub API)? User pre-configures?
- Config option like `delivery.git_remote: "https://gitea.example.com/org/{job_id}"`?

### Types of deliverables
- Written reports/summaries (Markdown, PDF)
- Structured data exports (CSV, JSON)
- Compliance checklists, requirement matrices
- Template-driven outputs (agent fills in structured templates)

### Integration with cockpit
- Should the cockpit link directly to the git remote?
- Or embed a file browser that reads from the repo?
- Should deliverables also be tracked in PostgreSQL for API access?

### Tooling for the agent
- New tool: `create_deliverable(name, content, format)`?
- Or convention-based: agent writes to `deliverables/` and the system handles the rest?
- Template support: pre-defined deliverable templates the agent fills in?
