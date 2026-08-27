---
guide_id: projects.loops.run
content_type: how_to
capability_ids:
  - projects.loops
journey_ids:
  - projects.loops.start
  - projects.loops.monitor
---

# Project loops and campaigns — continuous, bounded improvement

A **project loop** keeps launching autonomous worker jobs against one
project goal. Roles take turns, share the project's knowledge base and
connectors, and compound work in the project repository. This is an
experimental, unattended feature: start with a modest budget and inspect the
jobs and repository rather than assuming every handoff succeeded.

Use **Projects → choose a project → Loop**. Project Editors and Owners can
start or control a loop; Viewers can inspect it. Only one loop can be active
for a project at a time.

## In this guide

- [Configure the run](#configure-the-run)
- [Standard scheduling and parallel stages](#standard-scheduling-and-parallel-stages)
- [Campaign scheduling](#campaign-scheduling)
- [Officer scheduling](#officer-scheduling)
- [Monitor, pause, resume, and stop](#monitor-pause-resume-and-stop)

## Configure the run

The start form lets you choose:

- **Model** — optional. Leave it blank to preserve each expert's own model;
  selecting one overrides every loop role.
- **Workspace** — the Cockpit currently offers a Sandbox container or a VM for
  every spawned job. VM use is heavier and still needs the applicable grant
  and an available provisioner.
- **Cycle** — use **Build** (scholar → critic → developer), **Write** (scholar
  → critic → general), **Research** (scholar → critic), or a Custom sequence
  of worker experts.
- **Scheduling** — **Standard** or **Campaign**, explained below.
- **Max iterations** and/or **Time limit** — at least one is required. An
  iteration is one completed stage/turn. A parallel stage can start several
  jobs while spending one iteration, so its total job count can exceed its
  iteration budget.
- **Definition of done** — a quality bar the Critic evaluates and agents steer
  toward. It does **not** stop the loop automatically; budget, deadline,
  failures, or an explicit Stop do.
- **Extra steering** — standing instructions included in every loop job.

For coding work, attach the repository connector to the project first. Loop
jobs inherit the project's explicitly linked connectors, and their shared
knowledge base is the coordination blackboard.

## Standard scheduling and parallel stages

**Standard** runs the configured stages in order and repeats. A Custom stage
can fan out the built-in analysis roles **scholar**, **critic**, and
**product-qa** in parallel. The next stage waits for every member of that
fan-out. Execution roles such as developer, general, and custom worker experts
run alone so concurrent writers do not race the project artifact.

Parallel members each create a job, but the barrier as a whole spends one
iteration. A fan-out counts as failed for the loop's consecutive-failure
limit only when every member fails.

## Campaign scheduling

**Campaign** lets the checkpoint Critic replace the next single execution job
with a short, sequential investment in one initiative. The cycle must contain
exactly one Critic as its own stage, followed by a single-role execution stage;
the form explains an invalid sequence before start.

At a checkpoint the Critic may:

1. file no campaign, in which case the loop falls back to the normal one-stage
   turn; or
2. file a plan against an existing knowledge-base initiative, with ordered
   member roles and acceptance evidence.

A campaign has up to **5 stages** by default, each stage is one job and spends
one iteration, and the scheduler reserves 2 remaining iterations for the
follow-up analysis and closing Critic. After the members finish, the Critic
checks the recorded evidence and records one disposition:

- **ship** — this campaign's result is good enough;
- **extend** — continue the same initiative with another campaign; or
- **kill** — stop investing in that initiative and record why.

Those outcomes close the campaign, not the overall project loop. The loop
continues until one of its actual stop axes fires. A campaign can be extended
2 times by default. 2 consecutive failed campaign members abort it early and
return control to the Critic for a disposition. The live panel shows the
campaign stages and acceptance list; disposed campaigns remain in its history.
Questions filed by loop agents and campaign dispositions can also appear in
the Inbox.

Campaign versus Standard is fixed when the loop starts. To change between
those two, stop the current loop and start another; converting a live loop to
Officer scheduling is the one exception (below).

## Officer scheduling

**Officer** hands the loop's scheduling judgment to the project's Centurion
(see the projects guide's Centurion tab). Each concluded turn wakes him
instead of auto-spawning the next job: he decides what runs next from the
backlog, his sitreps, and the project charter — and a run of failures never
stops the loop mechanically; he judges whether to press on, change approach,
or escalate. Officer loops need no iteration or deadline budget (the officer
and his own daily limits are the brake), and they require the project to have
an enabled Centurion first. A live Standard or Campaign loop can be converted
to Officer scheduling once — while no campaign is in flight — via the loop
API; converting back means stopping and starting a fresh loop with a budget.

## Monitor, pause, resume, and stop

The Loop tab shows status, sequence, current stage/job, remaining iterations,
deadline, active campaign, campaign history, and jobs from the current run.

- **Pause** is graceful: jobs already in the current stage finish, but the loop
  does not advance. **Resume** continues it and re-kicks the next stage if the
  prior one already finished.
- **Stop** is permanent for that run and also graceful: in-flight jobs finish,
  but no new jobs are queued. Start a new loop for another run.
- Exhausting iterations or reaching the deadline completes the loop.
  Reaching the consecutive-failure limit or failing to spawn the next stage
  fails it.

If this session actually has the **Automations & Loops** tool group, it can
inspect the current or most recent loop, list its jobs, and explain its state.
The default group cannot start, pause, resume, stop, or reconfigure a loop; use
the project Loop tab for those actions. The App Guide itself grants no loop
tools.
