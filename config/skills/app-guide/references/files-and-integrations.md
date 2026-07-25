---
guide_id: workspaces.files-and-results
content_type: explanation
capability_ids:
  - workspaces.select
journey_ids:
  - workspaces.find-results
---

# Files, git, cloud, and citations — where work lives

Agents can work with isolated files, but the available storage, git, and IDE
surfaces depend on the workspace tier. Deliverables can also flow to shared
cloud storage.

## The agent workspace

Jobs and sessions with file tools commonly use a structure with `documents/`
(inputs you provided), `notes/` (the agent's working notes), and `output/`
(deliverables), though an expert/task may add other files. Virtual workspaces
keep durable cloud-backed files but do not have git. Container and VM
workspaces can be git-versioned. None workspaces have no workspace files.

## Git

The built-in git server hosts repositories for git-capable workspaces:

- **Workspace / Git buttons** (on job rows, in the chat header, in reviews)
  appear when that record actually has a repository and open its files,
  commits, phase tags, and branches.
- Standalone and project jobs can use different repository/branch layouts;
  delegated work may share a parent workspace. Use the links shown on the
  current record rather than assuming one repository per job.
- **Projects** carry repositories too: a managed jobs repo plus any source or
  reference repos you add on the Repos tab. Project membership grants
  matching git access automatically.

You sign into the git server with the same account as the app (single
sign-on).

## Cloud storage

Shared cloud file storage (WebDAV-based) connects agent work to files humans
can open:

- **Projects** can have a cloud folder (see project settings) — agents read
  from and, if allowed, write to it; **Open folder** takes you there.
- **Sessions** can have a files folder — the **Files** button in the chat
  header opens it.
- **Jobs** can **Export to Cloud** — copy deliverables into the shared
  folder — and some project-job reviews happen directly in a cloud folder.

Same single sign-on as the app.

A normal writable session mount is live. On deployments that enable it,
**Protected Cloud** is a separate creation-time session mode: an eligible
Nextcloud project folder is exposed through a private staging layer, and the
user applies or rejects the whole diff from the session's **Cloud changes**
review panel. It currently requires a Container workspace; route setup,
review, and failure questions to the focused protected-cloud guide.

## The in-browser IDE

When a full workspace supports it, **IDE** buttons (jobs list, chat header,
job review) open the agent's workspace in a browser-based code editor—the
fastest way to inspect what an agent produced or make a quick edit. It starts
on demand, so give it a moment on first open. Virtual and None sessions do not
provide this workspace IDE.

## Citations

When agents research, they cite. Sources are captured at cite time (with a
snapshot of what the page/document said), referenced inline as clickable
`[N]` markers, and collected in a **Citations panel** (in sessions) or on
the job's citation views. Citations are verified in the background — each
carries a verification status, a **view original** link, and for cloud
documents an on-demand check for whether the source drifted since it was
cited. Bibliographies can be generated from the collected sources.

## Uploading files

- **Job creation** has a documents dropzone (pdf, doc/docx, txt, md, images,
  zip).
- **Chat** accepts attachments (documents, images, audio, video), including
  drag-and-drop, paste, and mobile camera capture.

## Connecting outside tools (MCP)

Under **Settings → MCP Tokens** you can mint tokens that let external AI
tools (like Claude Code) talk to this app — list jobs, read results, create
work — with copy-paste connection instructions. Scope tokens to yourself or
a project, and set an expiry. Personal access tokens for the REST API are
managed under **Settings → API Keys**.
