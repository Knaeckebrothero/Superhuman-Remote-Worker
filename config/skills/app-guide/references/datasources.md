# Datasources — giving agents access to your data

A **datasource** is a stored connection to something the agent should work
with: a database, a cloud folder, a git repository, or a credential file. You
create datasources once, then attach them to projects or individual jobs and
sessions. Attaching is what grants access — agents never see connections you
didn't attach.

## Supported types

- **PostgreSQL** — relational database access.
- **MongoDB** — document database access.
- **Neo4j** — graph database access.
- **WebDAV** — cloud file storage (this is how the built-in cloud storage and
  services like Nextcloud/OpenCloud are attached); the agent can list, read,
  and — if allowed — write and delete files there.
- **Repository** — a git repository the agent can work with.
- **Credential files** — kubeconfig, SSH key, or a generic file the agent
  needs to reach some other system.
- **Generic** — a free-form connection definition for anything else.

## What attaching one does

When a job or session starts with a datasource attached, the agent
automatically gets the matching tools:

- A PostgreSQL source gives it SQL query and schema-inspection tools.
- A MongoDB source gives it query, aggregation, and schema tools.
- A Neo4j source gives it graph (Cypher) query and schema tools.
- A WebDAV source gives it cloud file list/read tools.

## Read-only vs. read-write

Each attachment can be marked **read-only**. Read-only sources expose only
querying and reading; read-write sources additionally allow writes (SQL
execute, document inserts/updates, graph writes, cloud file writes and
deletes). If you want an agent to analyze production data safely, attach the
source read-only.

## Where to attach

- **On a project** — every job in the project can use it, and it becomes part
  of the project's shared context. Best for the data a team works with
  continuously.
- **On a single job or session** — one-off access for one piece of work.

Jobs an agent creates on your behalf (for example, a session delegating work)
inherit the datasource selection of their parent unless overridden.
