---
tags:
  - feature
  - quality
  - orchestration
  - critic
aliases:
  - verification phase
  - critic follow-up
  - pre-approval review
related:
  - "[[continuous_improvement_loop]]"
  - "[[job_complete]]"
  - "[[autonomy_levels]]"
---

# Verification Phase — Critic Follow-Up Before Approval

> When an agent calls `job_complete`, automatically spawn a critic job that reviews the deliverables against the original requirements. The critic either approves the job or resumes it with feedback. No changes to the agent graph.

## Problem

The agent consistently delivers work that falls short of its actual capability. When a human (or Claude Code) reviews the output afterwards, it takes seconds to spot issues the agent missed — shortcuts taken, edge cases skipped, deliverables not verified against requirements. Resuming the job with this feedback fixes the issue ~90% of the time, proving the model *can* do the work, it just doesn't critically reflect before calling it done.

The root cause isn't capability — it's **premature closure**. Models are optimistic about their own output. The current flow lets `job_complete` freeze the job for human review with no structural checkpoint forcing the agent to verify what it actually produced.

### What happens today

```
Agent calls job_complete(summary, deliverables, confidence)
    ↓
Remaining strategic todos complete
    ↓
finalize_job() → status: "pending_review" → waits for human
    ↓
Human reviews → approves or resumes with feedback
```

The summary and confidence are vibes-based — the agent doesn't re-read deliverables or compare them against requirements. The human catches what the agent missed.

### What we want

```
Agent calls job_complete(summary, deliverables, confidence)
    ↓
finalize_job() → status: "pending_review"
    ↓
Critic job auto-created → reviews deliverables against requirements
    ↓
Issues found?
    ├─ YES → critic resumes original job with feedback → agent fixes → cycle repeats
    └─ NO  → critic approves → original job status based on original agent's autonomy level
```

The critic automates what a human reviewer does today. Same workflow, same mechanisms, just triggered automatically.

## Why Not Modify the Agent Graph?

The initial idea was to insert a "verification phase" inside the agent's phase alternation model — intercepting `finalize_job()` to add a self-review step. But this is unnecessary complexity:

1. **The critic agent already exists** (`config/experts/critic/`) — built specifically for quality gating
2. **The approve/resume mechanisms already work** — `POST /api/jobs/{id}/approve` and `POST /api/jobs/{id}/resume` with feedback
3. **A different agent is a better reviewer** — asking the same model to review its own work in the same context invites the same optimism bias. A fresh agent with a critic persona and clean context is more likely to catch issues.
4. **Zero changes to the core agent** — no new state fields, no graph modifications, no new phase types. The agent doesn't even know it's being reviewed.
5. **Aligns with the three-agent architecture** — the system was designed for developer/scholar/critic to work in a cycle. This is the first step toward that.

## Design

### Trigger

When a job enters `pending_review` status with `freeze_type: "job_complete"`, the system creates a follow-up critic job.

**The trigger problem:** The agent updates the DB status directly (`src/graph.py` handle_transition node) — the orchestrator has no event system, no DB triggers, and no callbacks. It doesn't know a job just froze unless it polls or is told.

**Recommended approach:** The agent's API layer (`src/api/app.py`) already runs `_update_job_status_from_result()` after job processing ends. This function detects whether the job ended in `pending_review` status. Add logic here to call the orchestrator's `POST /api/jobs` endpoint to create the critic job. The agent pod already has `OrchestratorClient` (`src/api/orchestrator_client.py`) configured with the orchestrator URL for heartbeats, so no new configuration is needed.

```python
# In src/api/app.py, after _update_job_status_from_result()
if status == "pending_review" and freeze_type == "job_complete":
    if should_trigger_verification(config, job_context):
        await orchestrator_client.create_verification_job(job_id, freeze_data)
```

This is a small addition to the agent's API layer, not the graph itself. The core agent code (`src/graph.py`, `src/core/phase.py`) remains untouched.

**Alternative approaches considered:**
- Orchestrator polls for `pending_review` jobs — adds latency, wastes resources
- DB LISTEN/NOTIFY — clean but requires new infrastructure
- Heartbeat-based detection — up to 60s delay between job completion and next heartbeat

### Preventing Recursive Verification

The critic's own job will eventually call `job_complete` and enter `pending_review`. Without a guard, this triggers another critic job, ad infinitum.

**Solution:** The trigger checks the job's context for `verification_target`. If present, this job IS a verification — skip creating another critic job:

```python
def should_trigger_verification(config, job_context):
    if not config.verification.enabled:
        return False
    if job_context.get("verification_target"):
        return False  # This is already a verification job, don't recurse
    return True
```

### Critic Job Creation

The trigger creates a regular job via `POST /api/jobs`:

```python
critic_job = create_job(
    description="Review and verify the deliverables of job {original_job_id}. [structured instructions here]",
    config_name="critic",
    parent_job_id=original_job_id,
    context={
        "verification_target": original_job_id,
        "original_description": original_job.description,
        "original_config": original_job.config_name,
        "original_autonomy": original_job.autonomy,
        "deliverables": freeze_data["deliverables"],
        "summary": freeze_data["summary"],
        "confidence": freeze_data["confidence"],
    },
)
```

The job description overrides the critic's default instructions with specific verification instructions telling it what to do: inspect the target job's deliverables via MCP tools, compare against the original requirements, and either approve or return with feedback.

**No auto-assignment.** The critic job is created with status `"created"` and waits for an available agent pod to pick it up. Automatic job assignment/pickup is a separate feature that will handle dispatch. For now, the job sits in the queue until an agent is available or the user manually assigns it.

### The Critic is Just a Regular Job

The critic runs as a normal agent job with the `critic` config. No special tooling beyond what it needs to inspect and act on the target job. By default it runs with `autonomy: full` (from the critic config), meaning it runs to completion without freezing.

This means:
- The critic's own autonomy level can be overridden per-job if needed (e.g., `dependent` for debugging the critic itself)
- No special `auto_approve` setting is needed — the existing autonomy system handles everything
- The critic goes through normal phase alternation (strategic → tactical → strategic → done)

### How the Critic Accesses the Target Job

The critic gets access to the **MCP tools** that already expose job introspection capabilities (`orchestrator/mcp/`). This gives it the ability to:

- Read the target job's workspace files (deliverables, workspace.md, plan.md, archive)
- Get job metadata and status
- Inspect the audit trail if needed

This is the cleanest approach — works across machines, doesn't require filesystem access, and the MCP server already exists on port 8055.

### Critic Verdict Tools

The critic needs two tools to act on its review:

| Tool | Action | Implementation |
|------|--------|---------------|
| `approve_job(job_id, report)` | Approves the target job | Calls `POST /api/jobs/{id}/approve` |
| `return_job_with_feedback(job_id, feedback, issues)` | Resumes target job with feedback | Calls `POST /api/jobs/{id}/resume` with structured feedback |

These are thin HTTP wrappers around existing orchestrator endpoints, living in a new `evaluation` tool category.

### What Happens After the Critic's Verdict

**Critic approves** → calls `POST /api/jobs/{id}/approve` on the original job. The original job's final status follows the **original agent's autonomy level**:
- Original autonomy `full` → status: `completed` (same as if a human approved)
- Original autonomy `review` or below → status: `completed` (approve endpoint always completes)

This works because the approve endpoint already does exactly what we need — it transitions `pending_review` → `completed`. The critic is functionally identical to a human clicking "Approve" in the cockpit.

**Critic returns with feedback** → calls `POST /api/jobs/{id}/resume` with feedback. The original job resumes:
1. Status changes from `pending_review` → `processing`
2. Feedback is injected via the existing `resume_feedback` mechanism
3. The agent resumes with the critic's specific findings in context
4. The agent addresses the issues and eventually calls `job_complete` again
5. The cycle repeats: a new critic job is spawned to review the updated deliverables

There is no limit on how many times this cycle can repeat. The critic and agent keep iterating until the critic is satisfied. If progress stalls or diminishing returns set in, the user can manually cancel either job. In practice, most issues are caught in the first round.

### Critic Flow

```
1. Critic job starts (regular job with critic config + verification instructions)
2. Strategic phase: read original job description, plan review approach
3. Tactical phase:
   a. Inspect target job's deliverables via MCP tools
   b. Read target job's workspace.md and plan.md for context
   c. Compare each deliverable against the original requirements
   d. Write verification report (output/verification_report.json)
   e. Verdict: approve_job() or return_job_with_feedback()
4. Strategic phase: summarize findings, call job_complete
5. Critic job ends
```

### Job Hierarchy: `parent_job_id`

Add a `parent_job_id` column to the jobs table:

```sql
ALTER TABLE jobs ADD COLUMN parent_job_id UUID REFERENCES jobs(id) DEFAULT NULL;
```

- **Root jobs** (user-created) have `parent_job_id = NULL`
- **System-spawned jobs** (critic follow-ups) reference the job that triggered them
- The cockpit UI uses this to display a job hierarchy — a folding/expander view that shows parent jobs with their child jobs nested underneath, so the user can see that a critic job is running against a specific parent job

The `verification_target` in `context` JSONB remains for the trigger logic (identifying what the critic is reviewing), while `parent_job_id` provides the structural relationship for the UI and queries.

### Integration with Autonomy Levels

| Autonomy | Behavior |
|----------|----------|
| `full` | Agent auto-completes (status: `completed`, `goal_achieved: true`). The trigger does NOT fire — there is no `pending_review` state to intercept. Verification is skipped. If verification is desired, change the agent's autonomy to `review`. |
| `review` | Agent freezes → critic reviews → approves or sends back. Primary use case. |
| `partial`+ | Same as `review` for the `job_complete` freeze. Phase-boundary freezes are unaffected. |

Note: Verification only applies to `freeze_type: "job_complete"` freezes. Phase-boundary freezes (`freeze_type: "phase_boundary"`) are never intercepted — they serve a different purpose (human steering at checkpoints).

### Cost and Proportionality

Every verified job spawns an entire critic agent run. For trivial jobs, the review could cost more (in tokens and time) than the original work.

Mitigation: **`enabled: false` as default** — verification is opt-in per agent config. Enable it for agents that handle complex, high-stakes work. A future complexity threshold (e.g., only verify jobs above N phases or M tokens) could gate verification automatically, but config-level opt-in is sufficient for v1.

### Configuration

```yaml
# config/defaults.yaml or per-agent config
verification:
  enabled: false         # Opt-in: spawn a critic job after job_complete
  critic_config: critic  # Which expert config to use for the reviewer
```

That's it. Two settings. The critic's autonomy comes from the critic's own config. The original job's final status follows the original job's autonomy level. No `auto_approve`, no `max_rounds` — the existing systems handle both.

### What Changes and What Doesn't

**Untouched:**
- `src/graph.py` — no graph modifications
- `src/core/phase.py` — no phase system changes
- `src/core/state.py` — no new state fields
- `src/tools/core/job.py` — `job_complete` works exactly as before
- The agent doesn't know it's being reviewed — from its perspective, the job freezes normally

**Small additions:**
- `src/api/app.py` — trigger logic after job completion (calls orchestrator to create critic job)
- `src/api/orchestrator_client.py` — new method to create verification jobs via orchestrator API

## Implementation

### What Already Exists

| Component | Status |
|-----------|--------|
| Critic expert config (`config/experts/critic/`) | Done |
| Job creation API (`POST /api/jobs`) | Done |
| Approve API (`POST /api/jobs/{id}/approve`) | Done |
| Resume with feedback (`POST /api/jobs/{id}/resume`) | Done |
| Freeze data in DB (`freeze_data` JSONB column) | Done |
| Job context JSONB for linking | Done |
| MCP server with job introspection tools (`orchestrator/mcp/`) | Done |
| OrchestratorClient in agent pods (`src/api/orchestrator_client.py`) | Done |

### What Needs to Be Built

1. **Evaluation verdict tools**
   - `approve_job(job_id, report)` — approve target job via orchestrator API
   - `return_job_with_feedback(job_id, feedback, issues)` — resume target with feedback
   - Location: `src/tools/evaluation/` (new tool category)
   - Implementation: HTTP wrappers using `OrchestratorClient`

2. **Critic config: add MCP + evaluation tools**
   - Add `evaluation` tool category to critic config with the verdict tools
   - Add MCP tools to critic config for inspecting target job workspaces
   - Location: `config/experts/critic/config.yaml`

3. **Trigger logic in agent API layer**
   - In `src/api/app.py`: after job ends in `pending_review` with `freeze_type: "job_complete"`, create critic job
   - Guard: skip if job context has `verification_target` (prevent recursion)
   - Guard: skip if config has `verification.enabled: false`
   - New method in `src/api/orchestrator_client.py` for creating the critic job via orchestrator API

4. **DB migration: `parent_job_id` column**
   - Add `parent_job_id UUID REFERENCES jobs(id) DEFAULT NULL` to jobs table
   - Set by trigger when creating critic jobs
   - Location: `orchestrator/database/schema.sql`

5. **Verification instructions template**
   - Structured instructions telling the critic how to review: inspect deliverables via MCP, compare against requirements, write report, call verdict tool
   - Injected as the job description override when creating the critic job
   - Location: `config/experts/critic/` or inline in the trigger logic

6. **Configuration**
   - Add `verification` section to `config/defaults.yaml` and `config/schema.json`
   - Default: `enabled: false`, `critic_config: critic`

7. **Cockpit UI: job hierarchy**
   - Display parent/child job relationships using `parent_job_id`
   - Folding/expander view: root jobs with nested child jobs
   - Show that a critic job is running against a specific parent
   - Location: `cockpit/`

### Implementation Roadmap

```
Phase 1: Foundation (no runtime behavior yet)
├─ Step 1: DB migration — add parent_job_id column
├─ Step 2: Configuration — add verification section to defaults.yaml + schema.json
└─ Step 3: Evaluation verdict tools — new src/tools/evaluation/ category
           ├─ approve_job()
           ├─ return_job_with_feedback()
           └─ Register in src/tools/registry.py

Phase 2: Critic wiring
├─ Step 4: Verification instructions template
│          Write the structured review instructions the critic receives
├─ Step 5: Update critic config — add evaluation + MCP tool categories
└─ Step 6: Manual test — create a critic job by hand via POST /api/jobs
           with a verification_target in context, assign to agent pod,
           verify the critic can inspect the target job and call verdict tools

Phase 3: Automation
├─ Step 7: OrchestratorClient.create_verification_job()
│          New method that calls POST /api/jobs with parent_job_id,
│          context, and verification instructions
└─ Step 8: Trigger in src/api/app.py
           After _update_job_status_from_result(), detect pending_review +
           job_complete freeze, check guards, call create_verification_job()

Phase 4: UI
└─ Step 9: Cockpit job hierarchy
           Query parent_job_id, display folding parent/child view
```

**Dependencies:**

```
Step 1 ──→ Step 7 (parent_job_id column must exist before creating linked jobs)
Step 2 ──→ Step 8 (config must exist before trigger reads it)
Step 3 ──→ Step 5 (tools must exist before critic config references them)
Step 3 ──→ Step 6 (tools must exist before manual test)
Step 4 ──→ Step 6 (instructions must exist before manual test)
Step 5 ──→ Step 6 (critic config must include tools before test)
Step 6 ──→ Step 7, 8 (manual test validates the approach before automating)
Step 1 ──→ Step 9 (column must exist before UI queries it)
```

Phase 1 and 2 can be developed and tested without changing any runtime behavior — no job will auto-spawn a critic until Phase 3 lands. Phase 4 (UI) is independent and can be done in parallel with Phase 3.

The critical validation point is **Step 6**: manually creating a critic job and confirming it can inspect the target, render a verdict, and approve/resume. If this works end-to-end, automating the trigger (Steps 7-8) is straightforward.

### Relationship to Continuous Improvement Loop

This feature implements the **first stage** of the CI loop described in `docs/continuous_improvement_loop.md`:

| CI Loop Stage | This Feature |
|---------------|-------------|
| Evaluator analyzes job behavior | Critic reviews deliverables |
| Evaluation tools inspect target job | MCP tools (existing) + verdict tools (new) |
| Researcher + Coder improve system | Future work (unchanged) |

The evaluation verdict tools and `parent_job_id` hierarchy built here are directly reusable by the full CI loop later. Building them now for the critic establishes the foundation.

## Resolved Design Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Critic config | Use existing `critic` | Agent customization is a separate topic. The existing persona is already built for quality gating. |
| Workspace access | MCP tools | Already exists, works across machines, gives full job introspection. |
| Auto-approve setting | Not needed | The critic is a regular job with its own autonomy. The original job's final status follows the original agent's autonomy. Existing systems handle everything. |
| Infinite disagreement | No cap | Let the cycle continue. The goal is continuous improvement. User cancels manually if progress stalls. |
| Job description | Override critic's default instructions | Regular job with critic config, verification-specific instructions in the description. |
| Agent pod availability | Don't auto-assign | Just create the job. Auto-assignment is a separate feature. |
| Job hierarchy in UI | `parent_job_id` column | Proper FK on jobs table. UI shows folding hierarchy of parent/child jobs. |
