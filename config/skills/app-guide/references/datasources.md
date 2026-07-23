# Connectors — giving agents access to external systems and data

A **connector** is a stored configuration for something the agent should work
with: a database, MCP server, mailbox, cloud folder, git repository, knowledge
base, or credential file. You create connectors once, then attach them to
projects or individual jobs and sessions. Attaching is what grants access —
agents never see connectors you didn't attach.

## Supported types

- **MCP Server** — tools discovered from a remote HTTP/SSE server or a trusted
  local stdio command, when MCP connectors are enabled on the deployment.
- **PostgreSQL, MongoDB, and Neo4j** — relational, document, and graph
  database access.
- **WebDAV** — cloud file storage (this is how the built-in cloud storage and
  services like Nextcloud/OpenCloud are attached); the agent can list, read,
  and — if allowed — write and delete files there.
- **Email** — mailbox search, reading, drafts, and optionally sending, within
  the configured folders and access tier.
- **Repository** — a git repository the agent can work with.
- **OKF Knowledge Base** — centrally indexed Markdown knowledge the agent can
  search and read.
- **Credential files** — kubeconfig, SSH key, or a generic file the agent
  needs to reach some other system.
- **Generic** — a free-form connection definition for anything else.

Email and OKF Knowledge Base setup have focused guide topics because their
access and indexing models are different from ordinary read-only/read-write
connectors.

## What attaching one does

When a job or session starts with a connector attached, the agent
automatically gets the matching access:

- An MCP server contributes its discovered, namespaced tools.
- A PostgreSQL connector gives it SQL query and schema-inspection tools.
- A MongoDB connector gives it query, aggregation, and schema tools.
- A Neo4j connector gives it graph (Cypher) query and schema tools.
- A WebDAV connector gives it cloud file tools.
- Repository and credential-file connectors prepare the workspace and
  credentials the agent needs.

## Read-only vs. read-write

Managed database and WebDAV attachments can be marked **read-only**. Read-only
connectors expose only querying and reading; read-write connectors additionally
allow writes (SQL execute, document inserts/updates, graph writes, cloud file
writes and deletes). Other connector types use their own boundary: email has
an access tier, MCP follows the server and credentials, and credential files
carry whatever access their underlying credential grants.

## Where to attach

- **On a project** — every job in the project can use it, and it becomes part
  of the project's shared context. Best for the data a team works with
  continuously.
- **On a single job or session** — one-off access for one piece of work.

Jobs an agent creates on your behalf (for example, a session delegating work)
inherit the connector selection of their parent unless overridden.
