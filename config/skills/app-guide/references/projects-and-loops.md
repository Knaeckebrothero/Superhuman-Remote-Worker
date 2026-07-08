# Projects and loops — organizing and compounding work

A **project** is the container that makes work compound: it bundles a team,
shared knowledge, datasources, git repositories, and jobs around one goal.
Sessions and jobs attached to a project share its context — what one job
learns, the next one inherits. Every user also has a **personal project** as
their default home for unattached work.

Create one from **Projects → New Project** (name, description, goal), or
**promote** a finished job into a project to keep building on its results.

## Inside a project

The project page has tabs:

- **Overview** — description, goal, and stats; edit them anytime. The goal
  matters: agents read it to understand what the project is for.
- **Jobs** — the project's jobs, plus a **New job in project** shortcut.
- **Knowledge** — the shared knowledge base agents read and write: searchable
  notes typed as decisions, learnings, goals, plans, questions, and more (see
  the memory-and-knowledge guide).
- **Datasources** — link shared connections; per-project access can be
  overridden (read-only vs read-write).
- **Repos** — the project's git repositories: the managed jobs repo, plus any
  source or reference repos you add.
- **Experts** — experts available to this project.
- **Members** — add people as **Owner**, **Editor**, or **Viewer**; membership
  also grants matching access to the project's git repositories.
- **Loop** — see below.
- **Settings** — rename, default agent config, memory sharing across jobs,
  cloud storage folder and its read-only toggle, archive/delete. Workspace
  network access is admin-controlled.

## The self-improvement loop

The **Loop** tab runs jobs *continuously* against the project goal — no human
in the loop between iterations. Agents take turns in a cycle, coordinating
through the project knowledge base: researchers propose, critics evaluate,
an execution role builds. Each iteration compounds on the last.

**Starting a loop**: pick a model and workspace type, choose a cycle —
presets **Build** (scholar → critic → developer), **Write** (scholar → critic
→ general), **Research** (scholar → critic) — or build a **Custom** sequence.
Custom steps can fan out **analysis roles in parallel** (e.g. scholar and
product-qa investigating simultaneously, shown as `scholar ∥ product-qa`);
the execution step always runs alone. Then set the stop conditions:

- **Max iterations** and/or a **time limit** — a loop is always bounded.
- **Definition of done** — what outcome would finish the work.
- **Extra steering** — standing guidance injected into every iteration.

**While it runs**: the tab shows status, the current role and job, iterations
left, and the jobs of the current run. **Pause**, **Resume**, or **Stop** at
any time — stopping is graceful (the in-flight job finishes; nothing new is
spawned). Loops also stop themselves after too many consecutive failures.

The loop is the app's flagship "agents improving a thing over time" feature —
and it's marked **experimental**; expect rough edges and keep budgets modest
at first.

## Practical guidance

- Put ongoing work in a project even if you're solo — the shared memory and
  knowledge base are what make later jobs smarter.
- Write the goal as an outcome ("users can X"), not a task list; agents plan
  the tasks themselves.
- Attach datasources at the project level when several jobs need them.
