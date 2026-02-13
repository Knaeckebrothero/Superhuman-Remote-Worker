# Autonomy Levels & Human-in-the-Loop Review

Design document for the autonomy level system — a configurable human-in-the-loop mechanism that controls when the agent pauses for human review during job execution.

## Motivation

### The Instructions/Plan Overlap

The current job creation flow inherited an "instructions" concept from the university project this agent was built on. That model worked well for a two-agent system with slightly different tasks within the same domain, but it falls short for a general-purpose agent that should be able to do anything.

The core issue: the user writes detailed instructions so that the agent can create a plan. This is redundant — the user is effectively writing a plan so the AI can write a plan. For power users who know what they want, it would be better to just create the plan directly. Meanwhile, for simpler or repetitive tasks where the plan is straightforward, the agent should retain the ability to plan autonomously without human involvement.

### The Real Control Point

The instruction builder chat helps users craft instructions, but it operates in isolation — without access to the actual documents, datasources, or tools the agent will use. The agent then re-interprets those instructions during its first strategic phase, effectively planning twice.

By letting the agent run its first strategic phase with full context (uploaded documents, attached datasources, all configured tools) and then pausing for human review, we:

- Ground the plan in reality rather than the user's description of their materials
- Eliminate the gap between "what I asked for" and "what the agent understood"
- Give the user meaningful control at the point where it matters most — the plan, not the prompt
- Let the instruction builder chat serve a new, more valuable role as a review companion

### The Solution: Autonomy Levels

Rather than a binary "interactive planning on/off", we introduce a graduated autonomy model. Different tasks need different levels of oversight — from fully autonomous execution to step-by-step guided control.

## Autonomy Levels

Five levels, from most autonomous to most controlled:

| Level | Stops After Initial Strategic | Stops After Each Strategic | Stops After Each Tactical | Stops After job_complete | Description |
|-------|:---:|:---:|:---:|:---:|-------------|
| **full** | - | - | - | - | Fully autonomous. Runs from start to finish without any human review. Job is marked `completed` immediately after `job_complete`. |
| **review** | - | - | - | yes | Current default behavior. Agent works autonomously but submits the job for human review after calling `job_complete`. User can approve or resume with feedback. |
| **partial** | yes | - | - | yes | Agent plans, then pauses for human review before execution begins. After approval, runs autonomously until `job_complete`, then pauses again for final review. |
| **guided** | yes | yes | - | yes | Agent pauses after every strategic phase for review. The user sees the plan/retrospective and upcoming todos before each execution cycle. Tactical phases run uninterrupted. |
| **dependent** | yes | yes | yes | yes | Maximum oversight. Agent pauses after every phase — both strategic and tactical. The user reviews every piece of work before the agent continues. |

### Choosing the Right Level

| Use Case | Level | Why |
|----------|-------|-----|
| Automated debugging, batch processing, simple templated tasks | **full** | Plan is predictable, no review needed |
| Standard jobs where you trust the agent but want to check the result | **review** | Current behavior, minimal friction |
| Important jobs where the plan matters but execution is routine | **partial** | Review the plan once, then let it run |
| New expert configs, high-stakes tasks, unfamiliar domains | **guided** | Stay in the loop at every decision point |
| Learning/training, critical compliance work, tuning prompts | **dependent** | Maximum visibility into every step |

### Future: Custom Autonomy

In the future, we also want to support a **custom** level that allows defining specific milestones at which the agent should stop — for example, stopping every N phases, stopping at named plan milestones, or stopping only when the agent's confidence drops below a threshold. This is noted here for future reference but is not part of the initial implementation.

## Configuration

### Where the Value Lives

Autonomy level is part of the **agent config** stored in the database. The expert config sets the default, and the user can override it at job creation via `config_override`.

**Expert config default (`config/experts/*/config.yaml`):**
```yaml
autonomy: partial    # Default for this expert type
```

**Framework default (`config/defaults.yaml`):**
```yaml
autonomy: partial    # System-wide default
```

**Per-job override (via cockpit UI or API):**
```json
{
  "config_override": {
    "autonomy": "guided"
  }
}
```

### Expert Defaults

| Expert | Default | Rationale |
|--------|---------|-----------|
| debugger | full | Debugging is typically autonomous and iterative |
| researcher | partial | User should review the research plan but execution is straightforward |
| writer | partial | Plan review ensures the outline matches expectations |
| coder | partial | Code changes benefit from plan review |
| validator | review | Validators produce a report for review at the end |

All other / new experts inherit the framework default of `partial`.

### Database

The autonomy level is resolved from the agent's effective config (expert default + `config_override`). No new database column is needed — `config_override` JSONB on the `jobs` table already supports this:

```sql
-- Query effective autonomy for a job
SELECT COALESCE(
    config_override->>'autonomy',
    -- fallback: resolved from expert config file at runtime
) as autonomy_level
FROM jobs WHERE id = $1;
```

## How It Works

### Job Lifecycle with Autonomy Levels

```
User creates job (description, documents, datasources, autonomy override)
        |
        v
Agent starts, runs first strategic phase
  - Reads uploaded documents
  - Explores datasources
  - Builds workspace.md, plan.md
  - Creates todos for first tactical phase
        |
        v
[partial/guided/dependent] -----> FREEZE for plan review
        |                              |
        |                    User reviews in cockpit
        |                    (approve / feedback loop)
        |                              |
        v                              v
First tactical phase begins <---- On approval
        |
        v
Tactical phase completes
        |
        v
[dependent] -----> FREEZE for tactical review
        |              |
        |              v
        |          User reviews work done
        |          (approve / feedback)
        |              |
        v              v
Strategic phase (retrospective, re-plan)
        |
        v
[guided/dependent] -----> FREEZE for strategic review
        |                      |
        |                      v
        |                  User reviews updated plan
        |                  (approve / feedback)
        |                      |
        v                      v
Next tactical phase <---- On approval
        |
        v
    ... (repeat until job_complete) ...
        |
        v
Agent calls job_complete
        |
        v
[review/partial/guided/dependent] -----> FREEZE for final review
        |                                      |
        |                               User examines results
        |                               (approve / resume with feedback)
        |                                      |
        v                                      v
[full] ---> Job marked completed       On approve: completed
            immediately                On feedback: resume cycle
```

### What the Agent Produces for Review

The agent's strategic phase creates the key artifacts the user needs to review:

| Artifact | Purpose |
|----------|---------|
| `workspace.md` | Long-term memory — agent's understanding of the task, context, constraints |
| `plan.md` | Strategic plan — phased approach, methodology, success criteria |
| `todos.yaml` | Concrete task list for the upcoming tactical phase |
| `tools/*.md` | Auto-generated tool documentation (shows what capabilities the agent has) |
| `archive/` | Phase retrospectives and archived todos (available after phase 2+) |

These files are the plan. The user reviews them directly — no translation layer needed.

### Freeze Mechanism

Freezes reuse the existing `pending_review` job status and the checkpoint/resume infrastructure. The graph checks autonomy level at phase boundaries:

**In `handle_transition` (`src/graph.py`):**

```python
def should_freeze(state, config) -> bool:
    """Determine if the agent should freeze at the current phase boundary."""
    autonomy = config.autonomy  # resolved from expert + override
    phase_type = state["current_phase"]  # "strategic" or "tactical"
    phase_number = state["phase_number"]
    job_complete_called = state.get("job_complete_called", False)

    if autonomy == "full":
        return False

    if job_complete_called and autonomy != "full":
        return True  # review, partial, guided, dependent all freeze here

    if autonomy == "dependent":
        return True  # freeze after every phase

    if autonomy == "guided" and phase_type == "strategic":
        return True  # freeze after every strategic phase

    if autonomy == "partial" and phase_type == "strategic" and phase_number == 1:
        return True  # freeze only after initial strategic phase

    return False
```

### Phase-Aware Resume

When the user resumes from a freeze, the resume behavior depends on the freeze context:

| Freeze Context | Resume With Approval | Resume With Feedback |
|---------------|---------------------|---------------------|
| After initial strategic | Continue to first tactical phase | Re-enter strategic phase with feedback todos |
| After later strategic | Continue to next tactical phase | Re-enter strategic phase with feedback |
| After tactical | Continue to next strategic phase | Enter special feedback strategic phase |
| After job_complete | Mark job as `completed` | Enter feedback-driven strategic phase (existing resume flow) |

The existing resume-with-feedback mechanism (`strategic_todos_resume.yaml`) already handles the feedback injection — the agent gets a special strategic phase with todos for processing feedback, evaluating outputs, adapting the plan, and creating corrective todos.

## Git Branching & Pull Request Model

### Current State & Problems

The current review flow is shallow:
- After `job_complete`, the job enters `pending_review`
- The user can clone the workspace git repo and examine files
- The user can approve (marks `completed`) or resume with a feedback message
- The feedback message is injected as a HumanMessage + written to `feedback.md` in the workspace
- The agent gets a special resume strategic phase with predefined todos for processing feedback

Two major problems with this approach:
1. **Feedback is a blunt instrument.** A single text message doesn't let the user comment on specific changes, approve some parts while rejecting others, or have a structured back-and-forth about individual files.
2. **Workspace changes don't propagate.** Even if a user clones the repo, edits files, and pushes — the agent wouldn't notice. The resume flow doesn't include a `git pull` or any mechanism to detect external modifications.

Experience with the current implementation showed that feedback-driven resume cycles tend to get stuck on the same issues after ~2 cycles, partly because the feedback channel is too coarse.

### The Solution: Phase Branches and Pull Requests

Instead of working directly on main and using text messages for feedback, each phase that requires review becomes a **branch with a pull request**. This gives the user a proper code review interface — inline comments, file-level approvals, change requests — and the agent can read and respond to PR feedback using its existing git tools.

#### How It Works

```
Job starts
    |
    v
Agent creates branch: phase-1-strategic
    |
    v
Agent does strategic work on branch
  - workspace.md, plan.md, todos.yaml
    |
    v
Phase completes → Agent creates PR to main
    |
    v
[autonomy check] Should this PR require approval?
    |
    +---> YES (partial/guided/dependent for this phase type)
    |         |
    |         v
    |     Agent freezes, job status = pending_review
    |     User reviews PR in cockpit or Gitea
    |       - Inline comments on specific files
    |       - Approve, request changes, or comment
    |       - Optionally edit files directly (push to branch)
    |         |
    |         +---> User approves PR
    |         |         |
    |         |         v
    |         |     PR merged to main
    |         |     Agent resumes, creates next phase branch
    |         |
    |         +---> User requests changes (with comments)
    |                   |
    |                   v
    |               Agent resumes on same branch
    |               Reads PR comments via git tools
    |               Makes corrections, pushes new commits
    |               PR updated automatically
    |               Agent freezes again for re-review
    |
    +---> NO (full, or autonomy allows auto-merge for this phase type)
              |
              v
          Agent auto-merges PR to main
          Continues to next phase immediately
```

#### Branch Naming

Branches follow a predictable naming convention:

```
phase-{N}-{type}          # e.g., phase-1-strategic, phase-2-tactical
phase-{N}-{type}-rev-{M}  # e.g., phase-1-strategic-rev-2 (after feedback cycle)
```

#### Autonomy Levels and PR Behavior

| Level | Strategic PRs | Tactical PRs | Final PR (job_complete) |
|-------|:---:|:---:|:---:|
| **full** | Auto-merge | Auto-merge | Auto-merge, mark completed |
| **review** | Auto-merge | Auto-merge | Requires approval |
| **partial** | First requires approval, rest auto-merge | Auto-merge | Requires approval |
| **guided** | All require approval | Auto-merge | Requires approval |
| **dependent** | All require approval | All require approval | Requires approval |

#### What the User Can Do During PR Review

The PR interface (Gitea or cockpit-embedded) provides:

- **Inline comments** — comment on specific lines in plan.md, workspace.md, output files
- **File-level review** — approve or request changes per file
- **Push to branch** — the user can edit files directly and push to the phase branch before approving (the agent sees these as part of the branch state on resume)
- **Conversation threads** — structured discussion per change, not a single feedback blob
- **Diff view** — see exactly what the agent changed in this phase vs. main

#### Agent Response to PR Feedback

When the agent resumes after PR feedback (request changes):

1. Agent reads PR comments via git tools (`git log`, reading comment data)
2. Agent examines any commits the user pushed to the branch
3. Agent addresses each comment — making corrections, responding, or explaining
4. Agent pushes new commits to the branch (PR updates automatically)
5. Agent freezes again for re-review

The agent's existing git tools (`git_log`, `git_diff`, `git_show`, `git_status`) are sufficient to inspect what changed. For reading PR comments specifically, we may need a lightweight tool that queries the Gitea API (or the orchestrator proxies this).

#### Merge Strategy

- PRs merge to main using **squash merge** by default — keeps main history clean with one commit per phase
- The squash commit message summarizes the phase work (auto-generated from the phase retrospective)
- Branch is deleted after merge
- On `full` autonomy, branches are still created for history/auditability but merged immediately without a PR

### User-Facing Review Interfaces

The user has multiple ways to interact with the PR review flow:

#### Option A — Gitea PR Interface (Power Users)

Users who are comfortable with git can review PRs directly in Gitea (already running on port 3000). Full PR review capabilities: diffs, inline comments, approvals, push to branch.

#### Option B — Cockpit Embedded Review (All Users)

The cockpit embeds the key PR review elements:

- **Diff view** — rendered markdown diffs for workspace files
- **Comment panel** — add inline or general comments
- **File browser** — navigate workspace files on the branch with edit capability
- **Action buttons** — "Approve & Merge" or "Request Changes"

Changes made through the cockpit are committed to the branch on behalf of the user. The cockpit communicates with Gitea's API (or the orchestrator proxies it).

#### Option C — Builder Chat Review Session (Assisted Review)

The builder chat UI opens a **special review session** where an AI assistant can either clone the workspace repo or directly access the paused agent's workspace. This assistant helps the user:

- Navigate and understand the agent's work — "What does the plan say about phase 3?"
- Debug issues — "Why did the agent structure the output this way?"
- Make changes — the review assistant can edit workspace files on behalf of the user, committing to the phase branch
- Draft PR comments — help the user articulate specific, actionable feedback
- Search workspace content — uses vector search over the vectorized workspace (see below)

The review session assistant has access to:

| Tool Category | Tools | Purpose |
|---------------|-------|---------|
| **Workspace** | read_workspace_file, list_workspace_files, search_workspace, vector_search | Navigate and search the agent's workspace |
| **Git** | git_log, git_diff, git_show | Understand what changed and when |
| **Edit** | write_file, edit_file | Make changes to workspace files (commits to phase branch) |
| **Debug (MCP)** | get_audit_trail, get_chat_history, get_todos, get_llm_request, search_audit | Inspect agent reasoning and execution |
| **Job Context** | get_job, get_graph_changes, get_job_progress | Understand job state and progress |

This is the most accessible review path — non-technical users interact through natural language, and the AI navigates the workspace, git history, and audit trail for them.

### Workspace Vectorization

When the agent freezes for review, the workspace is vectorized to enable semantic search during the review session. This allows the builder chat to answer questions like "Find where the agent discusses the methodology" or "What sections reference the compliance requirements?" without needing exact keyword matches.

**Indexing trigger:** Vectorize on freeze (when the job enters `pending_review`). This is the point where the user will be searching, so freshness is guaranteed.

**What gets indexed:**
- All markdown files (workspace.md, plan.md, output/*.md)
- Phase retrospectives in archive/
- Todo history (archived and current)
- Uploaded documents (if not already indexed)

**Storage:** Uses the existing CitationEngine vector store infrastructure. Index is scoped per-job and cleaned up when the job completes.

### The Builder Chat Across the Job Lifecycle

With the branching model and review sessions, the builder chat is useful at every stage:

| Stage | Role | Key Tools |
|-------|------|-----------|
| **Pre-job** (current) | Instruction crafting — help user describe what they want | web_search, update_instructions |
| **Plan review** (new) | Review companion — navigate plan, discuss approach, edit files, draft PR comments | workspace tools, git tools, vector search, edit tools |
| **Mid-execution** (future) | Progress monitor — "How's it going?", "What phase is it on?" | MCP tools (get_todos, get_job) |
| **Result review** (new) | Output navigator — explore results, identify issues, make corrections, draft PR feedback | workspace tools, git tools, MCP debug tools |
| **Post-mortem** (future) | Debugging assistant — "Why did it fail?", "Where did it go wrong?" | MCP debug tools (audit trail, chat history, LLM requests) |

Same chat UI, same session continuity, but with context-appropriate tools activated at each stage. The review session is a special mode where the chat gets direct workspace access (clone or mount) rather than just read-only MCP queries.

## Implementation

### 1. Config & Schema

- Add `autonomy` key to `config/defaults.yaml` (default: `partial`)
- Add `autonomy` to `config/schema.json` with enum validation: `["full", "review", "partial", "guided", "dependent"]`
- Set expert-specific defaults in `config/experts/*/config.yaml`
- Ensure `config_override` merging correctly resolves the `autonomy` value

### 2. Git Branching in Workspace

- Modify `GitManager` (`src/managers/git_manager.py`) to create phase branches at phase start
- Add branch naming convention: `phase-{N}-{type}`
- Add PR creation via Gitea API at phase end (squash merge on approval)
- Add auto-merge logic for phases that don't require review (based on autonomy level)
- Extend git tools to read PR comments / review state

### 3. Freeze Logic in Graph

- Add freeze check at phase boundaries in `handle_transition` (`src/graph.py`)
- The `should_freeze()` function determines whether to pause based on autonomy level, phase type, and phase number
- On freeze: create PR, set job status to `pending_review`, persist checkpoint, exit graph
- On resume: check PR state (approved? changes requested?), read comments, merge if approved, then continue

### 4. Workspace Vectorization on Freeze

- Trigger vectorization when job enters `pending_review`
- Use CitationEngine vector store infrastructure
- Index workspace markdown files, archive, and uploaded documents
- Scope index per-job, clean up on job completion

### 5. Cockpit Review UI

- Embed PR diff view and comment interface for frozen jobs
- Add approve / request-changes actions that map to Gitea PR operations
- Display autonomy level in job metadata
- Allow autonomy level override at job creation
- Integrate builder chat as a review session with workspace access

### 6. Builder Chat Review Session

- New session mode where the builder chat assistant gets workspace access (clone or direct mount)
- Assistant can read, search (text + vector), and edit workspace files
- Edits are committed to the phase branch
- MCP debug tools available for inspecting agent reasoning
- Session is scoped to the current job's workspace

### 7. Agent Awareness

- The agent should know its autonomy level — include it in workspace.md during init
- **Deferred:** Modifying the agent's system prompt based on autonomy level (e.g., being more explicit about trade-offs at `partial`/`guided`) is a potential future enhancement but not part of the initial implementation. The agent should produce clear plans regardless of autonomy level.

## Open Questions

- **Gitea API integration depth:** How much of the Gitea PR API do we need to wrap? Minimal (create PR, check status, read comments, merge) vs. full (labels, reviewers, CI checks). Start minimal.
- **PR comment format for agent consumption:** PR comments are human-written. Should we structure them for agent parsing (e.g., prefixed with action tags like `[FIX]`, `[QUESTION]`, `[APPROVE]`) or let the agent interpret free-form text? Free-form is more natural but less reliable.
- **MCP authentication for builder chat:** The builder chat backend needs access to the orchestrator MCP and the workspace. Since both run server-side this is straightforward, but tool calls need to be scoped to the current job. Will figure this out during implementation.
