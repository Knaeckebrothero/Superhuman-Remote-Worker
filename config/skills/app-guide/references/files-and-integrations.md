# Files, git, cloud, and citations — where work lives

Agents work in isolated workspaces, but nothing is locked in: everything is
versioned in git, deliverables can flow to shared cloud storage, and you can
open any workspace in a browser IDE.

## The agent workspace

Every job and session gets an isolated workspace with a simple structure:
`documents/` (inputs you provided), `notes/` (the agent's working notes), and
`output/` (deliverables). Workspaces are git-versioned — the agent commits as
it works, so its history is inspectable.

## Git

The built-in git server hosts every workspace repository:

- **Workspace / Git buttons** (on job rows, in the chat header, in reviews)
  open the repository — browse files, commits, phase tags, and branches.
- Each root job gets its own repository; delegated subjobs work on branches.
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

## The in-browser IDE

**IDE** buttons (jobs list, chat header, job review) open the agent's
workspace in a browser-based code editor — the fastest way to poke through
what an agent actually produced, or to make a quick edit yourself. It starts
on demand, so give it a moment on first open.

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
