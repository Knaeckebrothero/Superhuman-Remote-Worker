# Overview — what this app is and how the pieces fit

**Superhuman Remote Worker (SRW)** runs AI agents that do real work for you:
research, writing, analysis, design, and software development. Agents work in
isolated, git-versioned workspaces, can be given access to your data, check
in with you as much or as little as you choose, and get smarter over time
through shared project memory.

## The mental model

Three things, one triangle:

- **Sessions** — interactive. A conversation with an agent that works while
  you watch and steer. Best for exploration, quick tasks, and anything you
  want to shape as it happens.
- **Jobs** — autonomous. You describe a goal, an agent plans and executes it
  on its own, and the result comes back for your review. Best for
  well-defined work you don't want to babysit.
- **Projects** — the container. A project ties sessions and jobs together
  with a shared goal, team members, knowledge base, memory, connectors, and
  git repos — so work compounds instead of starting from zero each time.

Two more concepts complete the picture: **experts** (preconfigured agent
roles — who does the work) and **connectors** (stored access to your external
databases, files, and repos — what the agent may touch).

## The map

The sidebar:

- **Sessions** — your conversations; create and resume them here.
- **Jobs** — all autonomous work: create, watch, pause, review.
- **Projects** — project homes: goal, jobs, knowledge, members, loop.
- **Connectors** — access to databases, cloud folders, repos, MCP servers, and
  credentials.
- **Experts** — the agent roster; deployments can allow custom ones.
- **Skills** — reusable how-to procedures agents can load; same story.
- **Automations** — jobs on a schedule (cron).
- **Settings** — your keys, models, voice, notifications, and integrations.

The **Inbox** is where everything that needs *you* collects: job reviews,
questions from agents, and permission requests — with approve/deny/reply
right there. Admins additionally get **Admin** pages (LLM providers and
models, users, configuration, capability grants, usage and cost).

## Your first fifteen minutes

1. **Check Settings first** — agents need language-model access. If your
   deployment doesn't provide it centrally, add a provider key under
   **Settings → API Keys**, and pick default models under Preferences.
2. **Start a session** (Sessions → New Session, defaults are fine). Ask for
   something real — research a question, draft a document. Watch the todo
   checklist and tool calls; approve actions as they come (you start
   supervised).
3. **Create a job** (Jobs → New Job): a concrete description, an expert,
   autonomy **Review**. Watch it move Created → Processing, then handle the
   review from the Inbox — approve it or send it back with feedback.
4. **Make a project** for anything ongoing, and create the next jobs inside
   it. From here on, agents inherit what earlier work learned.

A useful shortcut: when the session's **Fleet Management** tools are enabled,
the agent can create and steer worker jobs for you. Ask it to “run this as a
background job,” then follow the durable progress and review from **Jobs** and
the **Inbox**. Without those tools, use **Jobs → New Job** yourself.

## How much autonomy to give

Start with **supervised** sessions and **review**-autonomy jobs: you see
everything and nothing completes without you. As trust builds, loosen —
auto-accept sessions, full-autonomy jobs, then scheduled automations and
project loops (continuous agent cycles working toward a project goal). The
app's built-in quality control (a critic agent reviews finished jobs before
you see them) is on by default and stays available at every autonomy level.

## Where results live

Agent work lands as files in the job's `output/` folder, git commits you can
browse, citations for anything researched, and knowledge notes on the
project. Deliverables can be exported to shared cloud storage, and any
workspace opens in an in-browser IDE.
