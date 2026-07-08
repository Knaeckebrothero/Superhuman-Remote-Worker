# Memory and knowledge — what agents remember

Two systems make agents smarter over time, both scoped to a project so
context stays where it belongs.

## Memory — automatic recall

Agents remember automatically. As they work, a background process extracts
durable facts — your preferences, decisions made, things that turned out to
be true or false — and stores them as memories. On later jobs and sessions
in the same project, relevant memories are retrieved and injected into the
agent's context each turn. You don't manage this; it just makes the third
job in a project noticeably less repetitive than the first.

Memory is **project-scoped**: a project's memories are shared across its
jobs and sessions (there's a "share memories across jobs" toggle in project
settings). Stale or contradicted memories are retired over time.

## The knowledge base — deliberate, browsable notes

Where memory is automatic and behind the scenes, the **knowledge base (KB)**
is deliberate and visible. Agents write structured notes — decisions,
learnings, goals, plans, open questions, code insights, retrospectives — and
read them back in later work. In loops, the KB is the blackboard the roles
coordinate through: the scholar's findings, the critic's verdicts, and the
developer's outcomes all land here.

**Browse it** on the project page's **Knowledge** tab:

- Summary stats and full-text **search**.
- Filter by **type** (decision, learning, goal, plan, code, question, state,
  source, retrospective) and **status** (active, resolved, superseded,
  archived).
- Open a note to read its content, tags, and relationships; change its
  status or delete it.
- **Export** the whole knowledge base.

Notes have lifecycle: agents supersede outdated notes and resolve answered
questions, so the KB converges on what's currently true rather than piling
up history.

## How they differ

| | Memory | Knowledge base |
|---|---|---|
| Written | automatically, in the background | deliberately, by agents as part of the work |
| Visible | injected into agent context; not a browsing surface | browsable, searchable, editable on the project page |
| Best for | preferences, corrections, small durable facts | findings, decisions, plans — the project's shared brain |

If you want an agent to "remember" something specific, the reliable move is
to say it in a session or put it in the project's goal/description — it will
land in memory and/or the KB from there.
