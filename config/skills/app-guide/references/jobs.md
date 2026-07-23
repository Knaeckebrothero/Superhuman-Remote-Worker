# Jobs — autonomous work

A **job** is a task an agent works through on its own: you describe the goal,
pick an expert, and the agent plans, executes, and comes back with results.
Jobs run in isolated workspaces and everything they do is versioned, so you
can always inspect what happened.

## Creating a job

**Jobs → New Job** (or ask a session with **Fleet Management** enabled to
create one for you). Fields:

- **Project** — attach the job to a project (or none) for shared knowledge,
  connectors, and repos.
- **Description** — the task brief. This is the main input; be concrete about
  the goal and what "done" looks like.
- **Kick-off message** (optional) — an opening prompt to the agent.
- **Expert** — who does the work (see the experts guide).
- **Documents** — upload files the agent should work with.
- **Priority** — Low (backfill), Normal, or High (may preempt lower).
- **Agent Settings** — autonomy, models, tool categories, connectors, custom
  instructions, and advanced limits. Defaults are sensible; the most
  important knob is autonomy.

## Autonomy — when the job comes back to you

- **Full** — never asks; completes on its own.
- **Review** — runs all the way through, then asks you to review the finished
  work before it counts as completed.
- **Partial** — checks in once after its initial plan, then again at the end.
- **Guided** — checks in before every new phase of work.
- **Dependent** — checks in at every boundary; maximum hand-holding.

## Statuses you'll see

**Created** (queued, waiting for an agent) → **Processing** (an agent is
working) → then one of: **Pending review** (waiting on you), **Reviewing**
(the built-in critic is checking the work before you see it), **Completed**,
**Failed**, **Cancelled**. **Paused** jobs are parked and resume
automatically when an agent frees up; **Waiting** means the job is holding
for a subtask (like a research pre-pass) or a reply.

## How the agent works — and how to watch it

Agents alternate **planning phases** (write/update a plan, stage a todo list)
with **execution phases** (work through the todos, write results to files).
You can watch this live: the job's workspace shows `plan.md`, the current
todo list, and an `output/` folder where deliverables accumulate. Use the
**Workspace** button to browse the job's git repository (every phase is
committed), or **IDE** to open the workspace in an in-browser editor.

## When a job needs you

Everything that needs your attention lands in the **Inbox**: reviews, agent
messages, and permission requests. From there (or the job's Review page) you
can:

- **Approve** — accept finished work (or press **Continue** at a phase-boundary
  check-in). Optionally leave notes.
- **Continue with feedback** — send the agent corrections. It condenses its
  prior context, takes your feedback on board, and re-plans. This also works
  on failed or paused jobs to get them going again.
- **Reply to messages** — agents can ask you questions mid-job; blocking
  questions hold the job until you answer, async ones are picked up at the
  next planning phase.
- **Approve or deny privileged commands** — if the agent needs elevated
  (sudo) access, you get a request showing the exact command. You can also
  maintain auto-approve/deny rules for command patterns.
- **Upgrade to a VM** — some work needs a full virtual machine instead of the
  standard sandbox; you'll be offered the upgrade and can accept or resume
  without it.
- **Pause / Cancel** — from the Jobs list at any time.

For project jobs that change project files, the review can include a **diff
view**: a file-by-file comparison of what the agent wants to change, which
you **Accept** (applied to the project) or **Reject** (discarded, with the
work still preserved in git for audit).

## What you get out

- **Deliverables** — named files in the job's `output/` folder, listed in the
  review together with the agent's **summary** and a **confidence** score.
- **Git history** — every phase committed and tagged in the job's repository.
- **Citations and sources** — everything the agent cited, browsable and
  searchable.
- **Knowledge notes** — on project jobs with curation enabled, reusable
  findings are filed into the project knowledge base automatically.
- **Promote** — turn a finished job into a full project to keep building on
  it. **Export to Cloud** copies deliverables to a shared cloud folder.

## Built-in quality control

By default, when a job finishes, a **Critic** agent reviews the work first
(status **Reviewing**). If it finds problems, it sends the job back with
feedback for another round — several rounds if needed — before the result
reaches you. A research pre-pass (**Scholar**) can also run before big jobs
so the worker starts informed. Both are configurable per job.

## Automations — jobs on a schedule

The **Automations** page stores a recurring schedule and job template. Its
trigger, retry, connector, and safety boundaries differ from a manually
created job, so route schedule questions to the focused automations guide
instead of treating an automation as just another job form.
