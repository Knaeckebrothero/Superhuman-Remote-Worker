---
guide_id: datasources.email.connect
content_type: how_to
capability_ids:
  - datasources.email
  - datasources.email.send
journey_ids:
  - datasources.email.create
---

# Connect an Email mailbox

An **Email connector** lets an agent search and read selected mailbox folders,
move or flag messages, create drafts, and—under the strictest settings—send
mail. Creating the connector does not give every agent access: you must also
select it for a project, job, or session.

Sharing only part of a mailbox therefore has two required parts: restrict the
connector with **Folder allowlist**, then explicitly select that connector for
the project, job, or session that needs it. Do not stop after saving the
allowlist.

The folder allowlist scopes the saved connector; it does **not** attach the
connector. Finish by selecting it for this session or other intended run.

## Shortest safe setup

1. Open **Connectors** in Cockpit and choose **New Connector**.
2. Enter a name and select **Email (IMAP/SMTP)**.
3. Choose a provider preset, then enter the mailbox username and a
   provider-issued **app password**. Use the custom option for another
   IMAP/SMTP service.
4. Pick the least-powerful access tier that fits the task.
5. In **Folder allowlist**, enter comma-separated folders such as `AI` or
   `AI, AI/Processed`. An entry includes its subfolders. Leaving this empty
   shares the whole mailbox.
6. For drafts or sending, set the Drafts folder, From address, and any
   recipients the agent may use for a new message.
7. Choose **Test Connection** before relying on the connector. On a new form,
   this saves the private connector first and then tests it; **Create** saves
   without running the test.
8. Select the connector when creating a job or session, or link it to a
   project and select it for the work that needs it.

Email connectors are private to their owner and cannot be published to every
user.

## Choose an access tier

| Tier | Agent access |
|---|---|
| **Read** | List folders, list/search messages, and read messages without marking them read |
| **Read/write** | Read access plus moving messages and changing flags such as read/unread or starred |
| **Draft** (default) | Read/write access plus saving plain-text drafts for you to review and send |
| **Send** | Draft access plus SMTP sending, subject to the send controls below |

A project-level read-only attachment clamps an Email connector to **Read**,
even if its stored tier is broader.

## Share only selected mail

The folder allowlist is the practical sharing boundary. A useful pattern is to
create an `AI` folder in your mail client and move only messages you want the
agent to see into it. Configure `AI`; its subfolders are included
automatically. Keep the list empty only when whole-mailbox access is intended.

The allowlist controls browsing and message mutations. Drafts are saved to the
mailbox's special-use Drafts folder (or the configured fallback).

## Drafting and sending safely

- For a new composition, every To/Cc address must match the **Recipient
  allowlist**. Entries may be exact addresses or domains such as
  `@example.org`.
- For a reply, recipients are derived from the original thread and do not need
  to be duplicated in that allowlist.
- Prefer **Draft** unless direct sending is essential. The agent creates a
  draft; you review and send it in your normal mail client.
- The current runtime does not yet provide the planned human-approval queue
  for a gated send. With **Send without human approval** off, the agent refuses
  direct sending and should create a draft instead.
- Direct sending requires **Send without human approval** and the
  administrator-granted `email_autonomous_send` permission. Recipient limits
  and per-run rate limits still apply.

## Provider notes

Cockpit has presets for personal Gmail, Fastmail, iCloud Mail, Yahoo Mail,
mailbox.org, and GMX. Most providers require two-factor authentication and an
app-specific password; a normal account password often will not work.

Google Workspace custom-domain Gmail, Microsoft 365/Outlook.com, and Proton
need OAuth or a bridge and are not supported by the built-in Email connector
yet. Provider policies can change, so use **Test Connection** to verify the
actual account.

## If the agent still cannot use it

Check that the connector was selected for this specific job or session, not
only created. For project work, project linkage makes a connector eligible but
does not silently attach it to every run. Also check that the requested action
fits the effective tier and configured folders.
