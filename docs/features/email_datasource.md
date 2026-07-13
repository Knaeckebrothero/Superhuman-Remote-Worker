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

> **Status (2026-07-12): PROPOSED.** No implementation yet. Reconciled against the
> datasource changes on develop @ `f50a1039`: the `config jsonb` column (migration `0055`,
> shipped for OKF `kb`) means **no email migration is needed**; the new `read_only` publish
> flag (`0056`) and grant-gated publishing (`public_datasources.md`) drive the
> email-is-never-published rule below.

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

No new table **and no new migration** — one datasource row = one mailbox account, on
columns that already exist.

- `datasources.type = 'email'` (type is `text`; whitelist lives in the API layer,
  `orchestrator/main.py:5209` — currently `generic, repository, kb, postgresql, neo4j,
  mongodb, webdav, kubeconfig, ssh_key, generic_file`).
- **Secrets** stay in `credentials` (encrypted at rest, per [[credential_file_datasources]]):

```json
{
  "backend": "imap_smtp",
  "username": "user@example.com",
  "password": "app-password",
  "imap": { "host": "imap.example.com", "port": 993, "security": "ssl" },
  "smtp": { "host": "smtp.example.com", "port": 465, "security": "ssl" }
}
```

- **Non-secret scoping** goes in the **existing `config jsonb` column** (migration
  `0055_datasource_config.sql`, landed 2026-07-11 for OKF `kb` datasources, which store
  `config.root_path`). No email-specific migration is required:

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

#### Three "read-only" axes — do not conflate

The datasource system now carries three independent read-only-ish concepts. The email
tier is the *first* one; the doc names all three so implementation doesn't cross wires:

| Concept | Where | Meaning for email |
|---------|-------|-------------------|
| `config.access` (this feature) | `config` jsonb | The tier: `read`/`read_write`/`draft`/`send`. **Tool-layer enforced.** |
| `datasources.read_only` | column, migration `0056` | Declarative **publish** flag for `is_global` org-wide rows; NULL for private. Surfaces in the agent index via `_declared_ro_note()` as "(declared read-only — treat as no-write)". Email is **never published** (see below), so this stays NULL. |
| `project_read_only` | per-project link | Connector-mode switch on a project↔datasource link. For email it **clamps the tier down to `read`** (it must not empty credentials — see the managed-connector caveat in Touchpoints). |

#### Publishing email is rejected

`docs/features/public_datasources.md` (implemented 2026-07-12) establishes that a public
(`is_global`) datasource hands the **publisher's stored credentials** to every other user's
agent that attaches it. For a mailbox that means a third party's agent operating your inbox
with your identity — categorically worse than a shared read-only database. Therefore
`type='email'` **rejects `is_global=true`** at create/update (a typed guard mirroring the
existing `kb`-specific guards at `main.py:15175`/`15198`/`15281`, inverted: `kb` is
publish-friendly, `email` is publish-hostile). Email mailboxes are private, project- or
job-scoped only.

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

Line numbers are as of develop @ `f50a1039` and drift; anchor on the symbol. The OKF
`kb` commit (`e27a9313`) is the most recent end-to-end datasource-type addition and is the
better template than webdav for the *plumbing* touchpoints below (whitelist, `config`
usage, cockpit per-type fields, i18n); webdav remains the template for the *tool module*.

| Component | Change |
|-----------|--------|
| migrations | **None.** `config` (0055) and `read_only` (0056) already exist; latest pushed is `0057_cloud_ro_mounts_staging.sql`. No schema-snapshot regen needed. |
| `orchestrator/main.py:5209` | Add `email` to the type whitelist; accept/validate `config` (`access`, `folders`, `drafts_folder`, `from_address`) on create/update |
| `main.py` create/update (`~15175`–`15281`) | Add the `is_global`-rejection guard for `type='email'` (see "Publishing email is rejected") |
| `main.py:14843` `managed_types` set | Add `email` **with a caveat**: the `if ds_type in managed_types and is_read_only: creds = {}` branch (`:14859`) empties creds for read-only managed connectors. Email needs a live IMAP connection even at `read` tier, so it must be exempt — its tier is driven by `config.access`, not by withholding creds. Simplest: don't empty creds for `email`; instead pass `config.access` (clamped by `project_read_only`) through to the tool factory. |
| `main.py` per-category read/write tool map (`~14254`, webdav entry) | Add `email` entry (read = 4 read tools; write = all 8, then clamped by tier + `project_read_only`) |
| `POST /api/datasources/{id}/test` (`~14698`) | IMAP login + verify each allowlisted folder exists (+ SMTP `EHLO`/auth if tier `send`). Same `managed_types` gate the endpoint already uses. |
| `src/core/datasource_setup.py` | `email` branch in the connection factory (webdav pattern ~`:602`); datasource-index entry (`inject_datasource_index`, ~`:902`) listing tier + allowed folders, honoring `_declared_ro_note()` for consistency |
| `src/tools/email/` | New tool module (webdav module shape) |
| `src/tools/registry.py` | Registry gate + metadata import (webdav precedent `:462`) |
| `cockpit/src/app/views/datasources/datasource-list.component.ts` | Add an `@if (formData.type === 'email')` field block — the per-type pattern the `kb`/`kubeconfig`/`ssh_key` blocks already established (this resolves the old open question). Fields: host/port/security with Gmail/Fastmail presets, folder-allowlist chips, tier radio, app-password hint |
| `cockpit .../api.model.ts` | `email` type + config shape |
| `cockpit/src/assets/i18n/en.json` + `de-DE.json` | Form labels/hints (OKF added ~20 lines each; email is the same shape) |
| `requirements.txt` (both — see [[two_requirements_files]]) | `imap-tools` |

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

1. **P0 — plumbing** (no migration — `config` already exists): type whitelist, `config`
   validation, `is_global` rejection, `managed_types` add + read-only-creds carve-out,
   test endpoint, cockpit form + i18n.
2. **P1 — read tier**: connection factory, the four read tools, registry, datasource-index
   entry, unit tests, greenmail integration test.
3. **P2 — write + draft tiers**: move/flag/draft tools. This is the end of the default
   product experience (`draft` = default tier).
4. **P3 — send tier**: SMTP path + approval-gate wiring + the send/allowlist dispatch
   validation rule.
5. **Later, separate docs**: Gmail API + MS Graph backends (OAuth apparatus), Proton via
   `hydroxide` sidecar, automations trigger ("new mail in folder → job" — slots into
   automations v0 cron polling, deliberately out of scope here).

## Open questions

- ~~Does the cockpit datasource form support per-type dynamic fields cleanly?~~
  **Resolved (2026-07-12)**: the OKF `kb` commit (`e27a9313`) added `@if (formData.type
  === '…')` field blocks for `kb`/`kubeconfig`/`ssh_key`/`generic_file`; email adds one
  more of the same shape.
- `email_read` output for HTML-only mail: ship text extraction v1 (html→text), or also
  save raw HTML alongside? (Proposal: save both, snippet from text.)
- Should `email_list` surface `List-Unsubscribe`/bulk-mail heuristics to help agents skip
  newsletters, or is that the agent's problem? (Proposal: agent's problem, v1.)
