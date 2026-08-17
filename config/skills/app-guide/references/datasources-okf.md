---
guide_id: datasources.okf.connect
content_type: how_to
capability_ids:
  - datasources.okf
journey_ids:
  - datasources.okf.create
---

# Connect an external OKF Knowledge Base

An **OKF Knowledge Base** connector makes a Git repository of Markdown/OKF
notes searchable through SRW's knowledge tools. The repository files remain
the source of truth; the orchestrator builds a disposable search index from
them. Unlike a normal Repository connector, the repository is not cloned into
the agent workspace.

External OKF knowledge bases are read-only to agents in this release. Their Git
credentials remain on the orchestrator's indexing path, and agents receive
search/read access rather than repository credentials.

A complete setup has two separate checks: **Test Connection** validates the
source, then the connector's index state must reach **Ready** (or the user must
account for **Indexing** or **Partial**). Creating or successfully testing the
connector does not mean that every note is searchable yet.

## Connect one

1. Open **Connectors** in Cockpit and choose **New Connector**.
2. Enter a name and select **OKF Knowledge Base**.
3. Enter the Git **Repository URL** and, if needed, a branch.
4. Optionally set **OKF Root Path** to the folder containing the notes, such as
   `knowledge` or `docs`. Leave it blank to index Markdown from the repository
   root.
5. Choose HTTPS token or SSH-key authentication and provide a scoped,
   read-only credential when the repository is private.
6. Save with **Create**, or use **Test Connection** to save a new connector and
   then confirm that the branch resolves and Markdown notes are present. The
   test inspects the source; it does not embed or modify notes.
7. Watch the connector's index state. Initial indexing starts in the
   background, so the connector can exist before all notes are searchable.
8. Link it to a project or select it for a job/session. Project linkage makes
   it eligible; the individual run still uses explicit connector selection.

OKF connectors also work with shell-less session tiers because agents query
the central index instead of cloning the Git repository.

## Readiness and refresh

Cockpit shows **Pending**, **Indexing**, **Ready**, **Partial**, or **Failed**
along with progress when available. A still-indexing connector may be attached
and queried, but empty or partial results are not proof that the source itself
is empty.

When the upstream repository changes:

- **Reindex** performs the normal incremental refresh.
- **Full rebuild** re-embeds every note. Use it for a changed root, embedding
  or parsing pipeline changes, or recovery—not as the everyday refresh. It may
  incur embedding cost and requires confirmation.
- A periodic orchestrator sweep also checks for changes; its interval is an
  operator setting, so do not promise an exact refresh time.

If indexing fails, first use **Test Connection** and verify the repository URL,
branch, credential, root path, and presence of Markdown files. Then retry
Reindex. Reserve Full rebuild for cases where an incremental retry is not
enough.

## How this differs from project knowledge

The existing native project knowledge base remains the writable destination
for project knowledge. A selected external OKF connector is an additional,
reusable read-only library: agents can search, list, and read its indexed notes,
and results identify which knowledge base they came from, but agents cannot
write changes back to its Git repository in this release.

A connector can instead be **attached as one project's knowledge base**, on the
project Knowledge tab or when creating the project. That converts it: the
connector becomes that project's writable vault, is managed by the project
(no delete, relink, or manual reindex from the Connectors page), and stops
being available to anyone else. The connector must use token auth (writes go
through the GitHub API), keep its notes under `knowledge/`, point at GitHub,
and not be linked to any other project. Attaching is one-way and never
replaces a vault a project already has.
