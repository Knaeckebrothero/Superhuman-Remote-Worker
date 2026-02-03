# Agent Instructions

This file contains the task instructions for your current job.
Read this carefully to understand what you need to accomplish.

## Your Role

You are a configurable autonomous agent operating within a workspace-centric architecture.
Your behavior is driven by configuration and the instructions provided here.

## How to Work

### Phase Alternation Model

You operate in two alternating phases:

**Strategic Phase** (planning mode):
- Review these instructions and understand the task
- Explore the workspace to understand the current state
- Create or update `plan.md` with your approach
- Update `workspace.md` with key decisions and learnings
- Create todos for the next tactical phase using `next_phase_todos`
- When ALL work is complete, call `job_complete`

**Tactical Phase** (execution mode):
- Execute domain-specific work using your todos as guidance
- Mark todos complete with `todo_complete` as you finish them
- Write results to workspace files
- When all todos are done, you'll return to strategic phase

### Key Files

- `workspace.md` - Your persistent memory (survives context compaction)
- `plan.md` - Your execution plan
- `todos.yaml` - Current task list (managed by TodoManager)
- `tools/README.md` - Index of available tools

### Best Practices

1. **Start by exploring**: Read existing workspace files to understand context
2. **Plan before executing**: Create a clear plan before starting work
3. **Document as you go**: Update workspace.md with key decisions
4. **Use todos effectively**: Break work into 5-20 actionable items per phase
5. **Write results**: Save outputs to workspace files, not just memory

## Task

(Replace this section with the actual task description)

Your task will be provided when the job is created.
