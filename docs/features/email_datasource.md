---
tags:
  - data-management
  - credential-management
  - tool-development
  - agent-tools
---

# Email Datasource (IMAP/SMTP Managed Connector)

Design document for adding `email` as a datasource type so users can attach a mailbox
to projects and jobs with tiered access: read-only, read/write, draft-only composition,
and (gated) send.

> **Status (2026-07-11): PROPOSED.** No implementation yet.

## Motivation

The user story:

- Attach an email inbox as a datasource, the same way a database or WebDAV share is attached.
- Restrict the agent to **read-only** access, or allow **read/write** (move, flag, file drafts).
- Restrict access to **specific folders** — e.g. a permanent `AI` folder the user moves
  mails into when they want to share them with the agent. The folder becomes the share
  boundary and the user becomes the triage layer.
- Eventually: let the agent **send** mail on the user's behalf, with appropriate gating.

This follows the managed-connector pattern from [[datasource_redesign]]: structured tools
as the security boundary, credentials never exposed to the agent or the workspace pod,
read-only enforcement in the tool layer. It is the sixth managed connector after
`postgresql`, `neo4j`, `mongodb`, `webdav` (and the credential-file family).

**Why not CLI tools** (himalaya, mbsync, curl `imaps://`): CLI tools execute in the
workspace pod, which would require the IMAP password in the workspace environment —
violating the hard rule that internal credentials never enter the workspace pod
([[credential_file_datasources]] trust model, `feedback_internal_creds_not_in_workspace`).
It would also make the folder allowlist decorative: a CLI holding the account password can
read the entire mailbox. Structured agent-side tools are the only place the permission
model is actually enforceable.

## Provider reality (why v1 is IMAP/SMTP)

Plain-password IMAP is dead at the large providers; the protocol is not.

| Provider | v1 path | Notes |
|----------|---------|-------|
| Gmail | **App password** over IMAP/SMTP | Requires 2FA on the account. "Less secure apps" is gone; app passwords remain. |
| Fastmail, mailbox.org, GMX, self-hosted | **App password / password** over IMAP/SMTP | Straightforward. |
| Microsoft 365 | ❌ v1 | Basic auth is dead; IMAP requires XOAUTH2 + tenant-admin-consented app registration. Later backend (MS Graph preferred). |
| Proton | ❌ v1 | No server-side IMAP (E2E encryption). Needs Proton Mail Bridge (desktop) or a `hydroxide` sidecar + paid plan. Later. |
| Gmail via OAuth | ❌ v1 | Gmail API scopes are "restricted": app verification + paid security assessment for a published app. Viable per-tenant later. |

**Decision**: v1 ships one backend, `imap_smtp`, behind a neutral tool surface. Gmail API
and MS Graph become additional backends of the same `email` type later (`credentials.backend`
discriminator) — they map cleanly onto the same tiers (Graph `Mail.Read`/`Mail.ReadWrite`/`Mail.Send`,
Gmail `gmail.readonly`/`gmail.modify`/`gmail.send`; Graph `mailFolders` / Gmail labels ↔ folder allowlist).
The OAuth apparatus (client registration, consent UI, refresh-token storage) is the real cost
of those backends and is explicitly out of v1 scope.

## Trust and permission model

**Posture: managed connector.** The IMAP/SMTP connection object lives in the agent
process, created by `datasource_setup.create_datasource_connection()` and reached via
`ToolContext.get_datasource("email")`. Credentials are injected by the orchestrator at
dispatch, encrypted at rest in the `credentials` JSONB column (shipped, see
[[credential_file_datasources]]), and are never materialized into the workspace pod,
env vars, or tool output.

**Folder scoping is app-level enforcement.** IMAP has no folder-scoped credentials — the
app password can read the whole mailbox. The allowlist is therefore checked inside every
tool call (same defense-in-depth shape as phase-restricted tools: LLM schema binding is
the primary gate, the runtime check is the backup). An empty allowlist means the whole
mailbox is in scope — allowed, but see the injection rule below.

**Access tiers** (per datasource, set in the cockpit form):

| Tier | Capabilities | Mechanism |
|------|-------------|-----------|
| `read` | list folders, list/read/search messages | IMAP `EXAMINE` (read-only select) — cannot even flip `\Seen` flags |
| `read_write` | + move, flag messages | IMAP `SELECT`, `MOVE`/`COPY`+`STORE` |
| `draft` (default) | + compose drafts the user sends from their own client | MIME compose + IMAP `APPEND` to the drafts folder — **no SMTP involved** |
| `send` | + send mail as the user | SMTP submission; additionally runtime-gated (below) |

`draft` is the intended default and the sweet spot: the agent does the writing, the human
does the sending, and no message leaves without a human click in their own mail client.

The existing link-level `project_read_only` flag composes on top: a read-only project link
clamps any tier down to `read` (the orchestrator's per-category read/write tool map at
`orchestrator/main.py:14254` already implements this pattern for webdav).

**Send gating.** Sending as the user is the highest-blast-radius capability in the system
short of shell access. `email_send` is tactical-only and double-gated: the datasource tier
must be `send`, **and** unless the job runs at autonomy `full`, each send goes through the
existing approval machinery (same pattern as sudo requests) rather than executing directly.

**Prompt injection rule.** Inbound email is the canonical injection vector; full-inbox read
plus ungated send is the textbook exfiltration lever. Hard policy, enforced at dispatch
validation: a datasource with tier `send` **and** an empty folder allowlist is rejected —
autonomous send requires a curated folder scope. Tool output additionally labels message
bodies/snippets as untrusted third-party content.

**Context discipline.** Emails are huge (HTML bodies, attachments). Following the
web_search fix (`75fcba8d`): list/search return envelopes only; `email_read` writes the
parsed body and attachments to workspace files and returns headers, a bounded plain-text
snippet, and file pointers. Attachments are never inlined into context.

## Design

### Data model

No new table. One datasource row = one mailbox account.

- `datasources.type = 'email'` (type is `text`; whitelist lives in the API layer,
  `orchestrator/main.py:4940`).
- **Secrets** stay in `credentials` (encrypted):

```json
{
  "backend": "imap_smtp",
  "username": "user@example.com",
  "password": "app-password",
  "imap": { "host": "imap.example.com", "port": 993, "security": "ssl" },
  "smtp": { "host": "smtp.example.com", "port": 465, "security": "ssl" }
}
```

- **Non-secret scoping config** must be readable/editable in the UI without round-tripping
  secrets, so it does not belong in `credentials`. New migration
  `orchestrator/database/migrations/app/00XX_datasource_config.sql` adds
  `config jsonb NOT NULL DEFAULT '{}'` to `datasources` (generic — future connector types
  get non-secret settings for free):

```json
{
  "access": "draft",
  "folders": ["AI", "AI/Processed"],
  "drafts_folder": "Drafts",
  "from_address": "user@example.com"
}
```

Folder matching: exact name or subtree (`AI` also allows `AI/…`). SMTP block optional —
omitted unless tier is `send`.

### Tool surface

`src/tools/email/tools.py`, mirroring the webdav module shape (`EMAIL_TOOLS_METADATA` +
`create_email_tools(context)` factory, registry gate on `context.has_datasource("email")`
in `src/tools/registry.py`).

| Tool | Tier | Phases | Behavior |
|------|------|--------|----------|
| `email_list_folders` | read | both | Allowed folders only, with message/unseen counts |
| `email_list` | read | both | Envelopes in a folder (uid, from, subject, date, flags, size), newest first, paginated |
| `email_search` | read | both | IMAP `SEARCH` (from/subject/text/date range) within allowed folders |
| `email_read` | read | both | Fetch + MIME-parse one message → body to `emails/<folder>/<uid>/body.txt`, attachments to `emails/<folder>/<uid>/att/`; returns headers + snippet + pointers |
| `email_move` | read_write | tactical | Move between two *allowed* folders |
| `email_flag` | read_write | tactical | Set/clear `\Seen`, `\Flagged` |
| `email_draft` | draft | tactical | Compose MIME (to/cc/subject/body, optional workspace-file attachments) → `APPEND` to drafts folder |
| `email_send` | send | tactical | SMTP submission; approval-gated per trust model |

Eight tools, read tools available in both phases, mutating tools tactical-only —
consistent with the webdav precedent. Library: `imap-tools` (high-level, folder/message
API, avoids hand-rolled `imaplib` state machines) + stdlib `smtplib`/`email.message`.

### Touchpoints

| Component | Change |
|-----------|--------|
| `orchestrator/database/migrations/app/` | `00XX_datasource_config.sql` (+ regen `schema_current.sql` via `scripts/schema-snapshot.sh` — CI gate) |
| `orchestrator/main.py:4940` | Add `email` to the type whitelist; accept/validate `config` on create/update |
| `orchestrator/main.py:14254` | Add `email` entry to the per-category read/write tool map (read = 4 read tools; write = all 8, clamped by tier) |
| `orchestrator/main.py:14698` (`POST /api/datasources/{id}/test`) | IMAP login + verify each allowlisted folder exists (+ SMTP `EHLO`/auth if tier `send`) |
| `src/core/datasource_setup.py` | `email` branch in the connection factory (~line 602 pattern); KB summary line (~line 905) listing allowed folders + tier so the agent knows its scope without probing |
| `src/tools/email/` | New module per above |
| `src/tools/registry.py` | Registry gate + metadata import (webdav precedent at `:462`) |
| `cockpit` | `datasource-list.component.ts` form (host/port/security fields with provider presets for Gmail/Fastmail, folder allowlist chips, tier radio incl. app-password hint text); `api.model.ts` type |
| `requirements.txt` (both) | `imap-tools` |

### Testing

- **Unit**: `tests/test_email_tools.py` — fake mailbox object injected via `ToolContext`
  (same style as existing datasource tool tests); cover allowlist enforcement (folder
  outside scope → refusal), tier enforcement, snippet/pointer output shape, MIME parse of
  multipart + attachments.
- **Integration/k3d**: `greenmail/standalone` container (IMAP+SMTP in one image) in the
  dev compose/k3d values; seed a mailbox, attach as datasource, drive a session through
  list → read → draft.
- **Live smoke**: real Gmail app-password mailbox with an `AI` folder, walk the same path.

## Implementation roadmap

1. **P0 — plumbing**: migration + schema snapshot, type whitelist, `config` handling,
   test endpoint, cockpit form.
2. **P1 — read tier**: connection factory, the four read tools, registry, KB summary,
   unit tests, greenmail integration test.
3. **P2 — write + draft tiers**: move/flag/draft tools. This is the end of the default
   product experience (`draft` = default tier).
4. **P3 — send tier**: SMTP path + approval-gate wiring + the send/allowlist dispatch
   validation rule.
5. **Later, separate docs**: Gmail API + MS Graph backends (OAuth apparatus), Proton via
   `hydroxide` sidecar, automations trigger ("new mail in folder → job" — slots into
   automations v0 cron polling, deliberately out of scope here).

## Open questions

- Does the cockpit datasource form currently support per-type dynamic fields cleanly, or
  does the email form force the same refactor the credential-file types needed?
- `email_read` output for HTML-only mail: ship text extraction v1 (html→text), or also
  save raw HTML alongside? (Proposal: save both, snippet from text.)
- Should `email_list` surface `List-Unsubscribe`/bulk-mail heuristics to help agents skip
  newsletters, or is that the agent's problem? (Proposal: agent's problem, v1.)
