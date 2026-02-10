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

## Completion Gate: Plan Completion vs Task Completion

Before solving *how* deliverables get to the user, there's a prerequisite problem: the agent doesn't reliably know *when* it's done.

### The bug

The agent's `job_complete` condition is anchored to **plan execution**, not **task achievement**. An agent can complete every planned phase and signal `job_complete` even if the plan was incomplete, missed requirements, or drifted from the original goal.

| File | Line(s) | Current Wording |
|------|---------|-----------------|
| `config/templates/strategic_todos_transition.yaml` | 35-36 | "If the plan is fully executed and verified, call job_complete instead." |
| `config/prompts/strategic.txt` | 50 | "Do NOT call job_complete until ALL phases are verified complete" |
| `config/prompts/strategic.txt` | 57 | "Only call `job_complete` when the entire plan is executed AND verified" |

All three measure completion against the **plan**, not the **original task/goal**. The strategic phase's review-reflect-adapt cycle evaluates progress relative to the plan, but the plan itself is never validated against the original request at completion time.

The tactical templates mention "Include a verification todo at the end to confirm phase goals were met" — but this checks **phase** goals, not **task** goals. A phase can "pass" verification by checking its own todos without confirming those todos actually advanced the original task.

### The trust-based delivery pipeline

The `job_complete` tool (`src/tools/core/job.py`) accepts `summary`, `deliverables: List[str]`, `confidence`, and optional `notes`. The `deliverables` parameter is a self-reported list of file paths. When the agent calls `job_complete(deliverables=["output/report.md"])`:

1. **No existence check** — the listed files might not actually exist
2. **No content validation** — files could be empty, truncated, or placeholder content
3. **No requirement matching** — no comparison between what was asked for and what was produced
4. **No delivery manifest** — no structured spec of expected outputs defined at job creation time
5. **No completeness check** — if the task asked for 3 things, the agent could deliver 1 and call it done

The `confidence` parameter (0.0-1.0) is self-assessed and currently unused by any downstream logic.

### Decision: Human-in-the-Loop Review

Rather than trying to make the agent self-verify, we lean into the existing frozen-job mechanism and make the **human** the completion gate. The agent is not yet reliable enough to self-assess task completion, so we keep the current prompt wording as-is and instead build a proper review loop.

#### Existing infrastructure (already implemented)

The backend plumbing for this flow already exists:

| Component | Location | Status |
|-----------|----------|--------|
| `job_complete` freezes the job | `src/tools/core/job.py` — writes `output/job_frozen.json` | Done |
| `check_goal` detects frozen state | `src/graph.py:1064-1095` — stops graph with `should_stop=True` | Done |
| `pending_review` job status | `orchestrator/database/schema.sql:62` — valid DB status | Done |
| `--approve` CLI command | `src/agent.py:1406-1486` — writes `output/job_completion.json` | Done |
| `--resume --feedback` CLI command | `src/agent.py:343-351` — injects feedback into `instructions.md` | Done |
| Git push on freeze | `src/core/phase.py:535-537` — commits and pushes workspace to Gitea | Done |
| Gitea per-job repos | `orchestrator/services/gitea.py` — auto-creates repo on job creation | Done |

#### The review loop

```
Agent calls job_complete
  → job freezes (job_frozen.json), workspace pushed to Gitea
  → DB status set to pending_review
  → user gets notification (TODO: cockpit notification)
  → user reviews workspace in cockpit review component

  Option A: User satisfied → clicks "Approve" button
    → API calls approve endpoint
    → DB status set to completed
    → job is done

  Option B: User not satisfied → writes feedback, clicks "Continue"
    → API calls resume endpoint with feedback text
    → agent resumes in strategic phase
    → feedback injected into instructions.md
    → agent re-reads original instructions + new feedback
    → agent does strategic planning (review-reflect-adapt) to address feedback
    → agent continues with new tactical phase(s)
    → agent calls job_complete again when done
    → loop repeats until user approves
```

#### What was already in place (before this work)

- **Orchestrator resume API** — `POST /api/jobs/{job_id}/resume` with optional `feedback` field (`orchestrator/main.py`)
- **Cockpit resume service** — `api.service.ts` has `resumeJob(jobId, feedback?)` calling the API
- **Cockpit resume button** — basic "Resume" button in job list (`job-list.component.ts`)

#### What was built (Phases 1–3)

1. **Orchestrator approve API** — `POST /api/jobs/{job_id}/approve` endpoint. Runs server-side, writes `job_completion.json`, removes `job_frozen.json`, sets DB status to `completed`. See Phase 1 below.
2. **Cockpit review component** — dedicated review UI with:
   - **Job status visibility** — `pending_review` filter chip, orange badge, "Review" action button in job list
   - **Review panel** — shows frozen job summary, confidence, deliverables, notes; Approve button; feedback textarea + Continue button
   - **Workspace browser** — custom file browser proxying Gitea API through orchestrator; split-pane with file tree + content viewer
   - See Phases 2 and 3 below.

## Git-Based Delivery (Implemented)

The delivery mechanism uses Gitea with per-job repositories. This is already implemented:

- **Orchestrator** auto-creates a Gitea repo on job creation (`orchestrator/services/gitea.py`)
- **GitManager** has `add_remote`, `push`, `pull`, `clone` (`src/managers/git_manager.py:453+`)
- **Phase transitions** auto-commit on todo completion with phase tags
- **Job freeze** commits and pushes the full workspace to Gitea (`src/core/phase.py:535-537`)
- **Pod handoff** clones the workspace from Gitea when resuming on a new pod (`src/agent.py:764-774`)

### Decision: Push the Whole Workspace

We push everything as-is. The user sees the full workspace including internal reasoning files (`workspace.md`, `plan.md`, `todos.yaml`, `archive/`, etc.).

**Rationale:** The agent isn't reliable enough yet to curate outputs. Working documents, reasoning trails, and internal files are just as important as the final result for the human reviewer to understand what happened and decide whether the job is actually complete. Separate deliverables directories or branch-based separation can be revisited later when agent output quality is more consistent.

## Resolved Questions

### Workspace browser approach
**Decision:** Custom file browser via orchestrator proxy. Orchestrator proxies Gitea API calls (`list_contents`, `get_file_content`) so the cockpit doesn't need Gitea credentials and avoids iframe auth issues.

### Orchestrator approve endpoint approach
**Decision:** Replicate the agent's `approve_frozen_job` logic server-side. The approve endpoint runs entirely in the orchestrator — no agent pod needed. It reads `job_frozen.json` from Gitea (falls back to local workspace), writes `job_completion.json`, removes the frozen file, and updates the DB.

## Open Questions

### Notifications
- How should notifications work when a job enters `pending_review`? (Currently the user must check the job list or use the "Review" filter chip.)

### Review diffs
- Should the review component show a diff of what changed since the last review cycle (for jobs that were continued with feedback)?

### Types of deliverables
- Written reports/summaries (Markdown, PDF)
- Structured data exports (CSV, JSON)
- Compliance checklists, requirement matrices
- Template-driven outputs (agent fills in structured templates)

## Implementation Roadmap

### Phase 1: Orchestrator Approve Endpoint — DONE

`POST /api/jobs/{job_id}/approve` added to the orchestrator. Runs entirely server-side — no agent pod needed.

**What it does:**
1. Validates job exists and is in `pending_review` status
2. Reads `job_frozen.json` from Gitea repo (falls back to local workspace)
3. Writes `job_completion.json` (with `approved_at`, `approved_by`, optional `reviewer_notes`)
4. Removes `job_frozen.json`
5. Updates DB: `status → completed`, sets `completed_at`

**Files changed:**
- `orchestrator/main.py` — `JobApproveRequest` model + `approve_job` endpoint
- `orchestrator/services/gitea.py` — `get_file`, `create_or_update_file`, `delete_file` methods

### Phase 2: Cockpit Review Component — DONE

Review UI that ties together the approve and resume-with-feedback flows.

**What was built:**
- `pending_review` added to `JobStatus` type — cockpit now recognizes this status
- Job list: "Review" filter chip, orange `pending_review` badge, "Review" action button
- New `job-review` panel component registered in the layout system:
  - Fetches job details and frozen job data (via `GET /api/jobs/{job_id}/frozen`)
  - Shows summary, confidence bar, deliverables list, agent notes
  - "Approve" button → calls `POST /api/jobs/{job_id}/approve`
  - Feedback textarea + "Continue with Feedback" button → calls `POST /api/jobs/{job_id}/resume`
  - Link to browse workspace in Gitea
  - Graceful states: loading, not-review, empty, success/error messages
- New orchestrator endpoint: `GET /api/jobs/{job_id}/frozen` (reads `job_frozen.json` from Gitea or local workspace)

**Files changed:**
- `cockpit/src/app/core/models/api.model.ts` — added `pending_review` to `JobStatus`
- `cockpit/src/app/core/models/layout.model.ts` — added `job-review` to `ComponentType`
- `cockpit/src/app/core/services/api.service.ts` — `approveJob()`, `getFrozenJobData()` methods
- `cockpit/src/app/components/job-list/job-list.component.ts` — filter chip, badge, Review button
- `cockpit/src/app/components/job-review/job-review.component.ts` — new component
- `cockpit/src/app/app.ts` — registered `job-review` component
- `orchestrator/main.py` — `GET /api/jobs/{job_id}/frozen` endpoint

### Phase 3: Workspace Browser — DONE

Custom file browser that proxies the Gitea API through the orchestrator.

**Decision:** Built a custom file browser via orchestrator proxy (option 3). This avoids iframe auth issues and keeps the experience integrated in the cockpit. The orchestrator proxies Gitea API calls so the cockpit doesn't need Gitea credentials.

**What was built:**
- Orchestrator proxy endpoints:
  - `GET /api/jobs/{job_id}/repo/contents?path=` — list directory contents
  - `GET /api/jobs/{job_id}/repo/file?path=` — get file content
- `GiteaClient.list_contents()` — directory listing from Gitea repos
- `GiteaClient.get_file_content()` — raw text file content (unlike `get_file()` which parses JSON)
- New `workspace-browser` panel component:
  - Split pane: file tree (left) + file content viewer (right)
  - Breadcrumb navigation with clickable path segments
  - Directory listing with dirs first, files sorted alphabetically
  - File icons by extension (md, yaml, json, py, etc.)
  - File sizes displayed
  - Parent directory navigation (..)
  - Raw text file viewer with monospace font and pre-wrap
  - Reacts to job selection changes from DataService

**Files changed:**
- `orchestrator/services/gitea.py` — `list_contents()`, `get_file_content()` methods
- `orchestrator/main.py` — `GET /api/jobs/{job_id}/repo/contents`, `GET /api/jobs/{job_id}/repo/file`
- `cockpit/src/app/core/services/api.service.ts` — `listRepoContents()`, `getRepoFile()` methods
- `cockpit/src/app/core/models/layout.model.ts` — added `workspace-browser` to `ComponentType`
- `cockpit/src/app/components/workspace-browser/workspace-browser.component.ts` — new component
- `cockpit/src/app/app.ts` — registered `workspace-browser` component

### Future: Agent Self-Verification

Once the review loop is battle-tested and we have data on what humans actually reject, revisit the prompt-level completion gate:
- Reword `job_complete` conditions to reference the original task, not just the plan
- Add a delivery manifest (`output/manifest.yaml`) extracted during the initial strategic phase
- Use the `confidence` parameter as a gate (low confidence → require extra verification before freezing)
- Programmatic checks in `job_complete` tool (verify declared deliverables exist and are non-empty)
