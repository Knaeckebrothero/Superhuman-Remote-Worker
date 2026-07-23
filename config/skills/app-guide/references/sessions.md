# Sessions — working with an agent interactively

A **session** is a live, turn-by-turn conversation with an agent. Unlike a
job (which runs off on its own), a session keeps you in the loop: you steer,
the agent works, you see everything as it happens. New sessions use the
**Assistant** expert by default — a generalist for research, writing,
analysis, planning, and light coding.

Start one from **Sessions → New Session**: pick a title, projects to attach,
an expert, and adjust the agent settings (model, permission mode, tools,
connectors) if the defaults don't fit.

## What the session agent can do

- **Work with files** in its own isolated workspace (`documents/`, `notes/`,
  `output/`) — read, write, edit, search; the workspace is git-versioned.
- **Run shell commands** in a sandbox.
- **Research the web** — search, read pages, crawl sites, find and download
  academic papers.
- **Drive a browser** step by step (navigate, click, type, screenshot) for
  pages that need real interaction.
- **Cite as it writes** — sources are tracked and referenced inline as `[N]`
  markers you can click.
- **Read and write the project knowledge base**, and read git history.
- **Create and manage jobs for you when Fleet Management is enabled** — the
  agent can hand heavy work to the autonomous worker pool (create, check,
  approve, resume, pause, cancel jobs) while you keep chatting. The guide does
  not grant those tools; enable the group in Agent Settings and check the
  current session's actual tool list.
- **Use skills** — bundled how-to procedures it loads when relevant.

## Permission modes — how much it asks first

- **Supervised** — every tool call waits for your approval. An inline card
  shows what the agent wants to do, with **Approve**, **Auto-accept**
  (approve and switch modes), or **Stop**.
- **Auto-accept** — file reads and writes run without asking; shell commands
  still wait for approval.
- **Autonomous** — everything runs unattended.

Switch modes anytime in the chat's settings panel or with `/supervised`,
`/auto`, `/autonomous`. Higher modes may be restricted by your account's
grants — options you can't use appear disabled.

## The chat view

- **Composer**: attach files (documents, images, audio, video — drag-and-drop
  and paste work too; on mobile you can take a photo), and use slash commands:
  `/compact` (condense the conversation), `/done` (end the session), `/undo`,
  `/auto`, `/supervised`, `/autonomous`, `/silent`, `/verbose`.
- **Voice**: a mic button dictates your message (speech-to-text), and
  finished agent replies have a read-aloud button. Both depend on your
  deployment having voice models configured; the voice and speaking style are
  set in Settings.
- **Send / Stop**: the send button becomes a stop button while the agent is
  working — interrupt it anytime.
- **Progress**: a collapsible checklist shows the agent's current todos; a
  token-usage panel shows context fill; reasoning and tool calls can be
  expanded or collapsed.
- **Header buttons**: **Settings** (model, temperature, narration, display,
  permission mode), **Citations** (the source panel, once anything is cited),
  **Files** (the session's cloud folder), **Git** (the workspace's commit
  history), and **IDE** (an in-browser code editor on the agent's workspace).

## Session lifecycle

Sessions idle out after a period of inactivity (default 30 minutes) but are
not lost: resume any session from the Sessions list. Ended sessions show a
resume card in the chat. Rename sessions inline from the list or chat header;
delete them from the list.
