---
tags:
  - feature
  - agent-tool
  - communication
  - email
  - bidirectional
aliases:
  - agent mailbox
  - send_message
  - agent email
  - live communication
related:
  - "[[email_and_mobile]]"
  - "[[interactive_agent]]"
  - "[[sso_and_cloud_storage]]"
---

# Feature: Live Communication (Agent Mailbox)

Bidirectional email communication between agents and humans, with a filesystem-native message store that integrates naturally into the workspace, git versioning, and phase-based review cycle.

**Status:** Phase 1 implemented and tested. Phase 2 in progress.
**Parent design:** `docs/email_and_mobile.md`

## Motivation

Agents run autonomously for hours across dozens of phases. The `docs/issues/task_clearance_user_feedback.md` case study shows what happens without a communication channel: an agent spent 6 phases and 220 tool calls on a task it could have resolved in minutes with a single question, then called `job_complete` with fabricated confidence.

A one-directional notification tool solves the alert problem but misses the real value: **conversations**. An agent that can send a message, receive a reply, and continue the thread — with full history preserved in the workspace — is fundamentally more capable than one that can only broadcast status updates.

### Two Separate Concerns

1. **Automatic notifications** (job completed, job failed, phase transitions, stall detection) — orchestrator-level lifecycle events. The orchestrator already knows when they happen. These should be async hooks, not agent-initiated. No tool needed. No cognitive load on the agent. Covered by `docs/email_and_mobile.md` Section 1.

2. **Agent-initiated communication** (questions, status reports, requests for input, multi-party coordination) — the agent makes a deliberate decision to reach out. Requires a tool, a message store, and reply routing. **This document covers this concern.**

## Existing Infrastructure

This feature builds on infrastructure that is already deployed and operational.

### User Identity (Keycloak SSO)

Users authenticate via Keycloak OIDC (`docs/features/sso_and_cloud_storage.md`). The orchestrator performs JIT user provisioning — on first login, a local `users` row is created with email synced from the Keycloak token claims. Every authenticated user already has a verified email address in the database.

Relevant `users` table fields:
- `email` — synced from Keycloak on login (unique, used for recipient resolution)
- `display_name` — synced from Keycloak `name` or `preferred_username`
- `keycloak_sub` — Keycloak subject ID (primary linking key)
- `settings JSONB` — user preferences (delivery preferences will live here)

No new user provisioning or email verification is needed.

### Email Transport (SMTP + IMAP)

The deployment includes a mail bridge providing both SMTP (outbound) and IMAP (inbound) on the cluster's internal network. See deployment manifests for service addresses and ports.

- **Custom domain** with SPF, DKIM, DMARC — outbound emails pass authentication checks
- **SMTP:** The orchestrator's `EmailService` (`orchestrator/services/email.py`) already sends through this bridge for auth emails. Configured via `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM` env vars.
- **IMAP:** Available on the same bridge with the same credentials. Inbound reply routing is feasible without additional infrastructure.
- **`+` sub-addressing** supported (e.g., `agent+jobid+threadid@example.com`), enabling deterministic routing of replies to the correct job and thread.

### Summary

| Capability | Status |
|------------|--------|
| User emails in DB (Keycloak-synced) | Ready |
| SMTP outbound (EmailService + mail bridge) | Ready |
| IMAP inbound (same bridge, same credentials) | Ready |
| Custom domain with SPF/DKIM/DMARC | Ready |
| User preferences column (`users.settings` JSONB) | Ready |
| Workspace git versioning | Ready |

No new infrastructure deployments needed.

## Industry Context

| System | Pattern | Agent Control | Key Insight |
|--------|---------|---------------|-------------|
| **Google Workspace CLI** | Free-form email via `+send` skill (Gmail API) | Full — agent writes subject, body, recipients | Model Armor sanitization; granular per-tool exposure |
| **OpenAI Codex** | `notify` config fires external script on events | None — event-driven, not agent-initiated | Clean separation: system events vs agent actions |
| **Claude Code** | Hooks (`Notification`, `Stop`, `TaskCompleted`) | None — hooks fire on events | Extensible via user-configured commands |
| **Devin** | Slack DM on status changes | Partial — agent sends status updates | Per-session notification toggle |
| **AgentMail** (YC S25) | Purpose-built email API for agents | Full — agent composes and sends | Dedicated agent addresses with SPF/DKIM/DMARC |
| **Composio / Arcade.dev** | Pre-built actions via managed OAuth | Full — agent selects action and params | OAuth delegation; cherry-pick tools by name |
| **OWASP Cheat Sheet** | Classifies `send_email` as `RiskLevel.HIGH` | Requires human approval | Rate limiting, PII redaction, audit logging |

### Key Takeaways

1. **Event-driven notifications are not tools.** Codex, Claude Code, and Trigger.dev handle notifications as system events, not agent actions. We follow this pattern — lifecycle notifications are orchestrator hooks.

2. **Agent-initiated email needs guardrails.** OWASP classifies it as high-risk. Our mitigations: recipient restrictions (project members only), rate limiting at the orchestrator, audit logging, agent never sees SMTP credentials.

3. **Filesystem-native storage is unique to our architecture.** No other system stores agent messages as workspace files. This gives us git visibility, phase snapshot preservation, and zero-cost message discovery during strategic review — for free.

## Design

### Core Concept: The Workspace Mailbox

Messages are files in the workspace. The agent reads them with `read_file`, discovers new ones via `git_diff` during strategic phases, and sends via a `send_message` tool that writes to the filesystem and triggers email delivery through the orchestrator.

This is a **conversation archive** that lives alongside the agent's other workspace files (`workspace.md`, `plan.md`, `todos.yaml`) and participates in the same git versioning, phase snapshots, and context compaction survival mechanisms.

### Why Filesystem Storage

1. **Zero new tooling for reading.** The agent already has `read_file`, `list_files`. No "check inbox" tool needed.
2. **Git visibility for free.** During strategic phase, `git_diff` shows new files in `messages/`. Replies are discovered naturally during the review-reflect-adapt cycle.
3. **Survives context compaction.** Messages on disk persist even when conversation history gets summarized.
4. **Phase snapshots capture message state.** You can reconstruct exactly what the agent knew at any phase boundary.
5. **Works with both workspace backends.** Local writes directly. VM writes via the workspace backend API. Same abstraction.

### Workspace Structure

```
workspace/job_<uuid>/
  messages/
    <thread_id>/
      001_sent.md
      002_received.md
      003_sent.md
    <thread_id>/
      001_sent.md
      002_received.md
```

**Thread IDs** are short UUID prefixes (e.g., `a7f3b2`), generated when the first message in a thread is sent. The numeric prefix (`001_`, `002_`) preserves chronological order. The suffix indicates direction.

### Message File Format

```markdown
---
from: agent
to: alice@example.com
to_name: Alice Meyer
date: 2026-03-17T14:30:00Z
subject: Missing database credentials
thread: a7f3b2
sequence: 1
mode: blocking
status: delivered
---

I need the PostgreSQL credentials for the staging environment.

The connection string in `.env.example` points to localhost but this job
requires access to the remote database. Could you provide the connection
string or add it to the job's datasources?
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | str | `"agent"` for outbound, sender email for inbound |
| `to` | str | Recipient email for outbound, `"agent"` for inbound |
| `to_name` | str | Display name of recipient |
| `date` | ISO 8601 | Timestamp |
| `subject` | str | Thread subject (set on first message, carried through) |
| `thread` | str | Thread ID (UUID short prefix) |
| `sequence` | int | Message number within thread (1-indexed) |
| `mode` | str | `"async"` or `"blocking"` (outbound only) |
| `status` | str | Outbound: `delivered`, `failed`, `pending`. Inbound: `unread`, `read`. |

## Agent Tool: `send_message`

```
send_message(
    to: str,
    subject: str,
    message: str,
    mode: str = "async",
    thread_id: str | None = None
) -> str
```

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `to` | str | yes | Recipient: `"user"` for job owner, or display name / email of a project member |
| `subject` | str | yes | Subject line (max 200 chars). Carried from first message when replying to a thread. |
| `message` | str | yes | Message body in markdown (max 5000 chars) |
| `mode` | str | no | `"async"` (continue working) or `"blocking"` (pause until reply) |
| `thread_id` | str | no | Reply to existing thread. Omit to start a new thread. |

**Returns:** Confirmation string including thread ID and delivery status.

**Phase availability:** Both strategic and tactical.

### Sending Modes

| Mode | Agent behavior | Job state | Use case |
|------|---------------|-----------|----------|
| `async` | Sends message, continues working | Unchanged | Background question, FYI, status update |
| `blocking` | Sends message, execution pauses | `waiting_for_reply` | Need answer before proceeding, dependency blocker |

For `blocking` mode, the tool triggers a **job freeze** with status `waiting_for_reply`. This reuses the existing freeze/resume mechanism (same pattern as `autonomy: review`). The orchestrator stores the thread ID and message metadata on the frozen job record so it knows what to resume with when a reply arrives.

**Blocking timeout:** Configurable (default: 24h). After timeout, the orchestrator auto-resumes the job with a system message: "No reply received within 24 hours. Proceeding with best judgment."

### Internal Flow

**Sending (agent-side):**
```
Agent calls send_message(to="user", subject="...", message="...", mode="blocking")
    │
    ├─ 1. Tool resolves recipient via orchestrator API
    │     ("user" → job owner email; names validated against project members)
    │
    ├─ 2. Tool writes message file: messages/<thread_id>/NNN_sent.md
    │     Thread ID: UUID short prefix, generated on new threads
    │
    ├─ 3. Tool git-commits the file (if workspace git versioning enabled)
    │
    ├─ 4. Tool calls POST /api/jobs/{job_id}/messages/send
    │     Orchestrator: validates rate limit → renders email → sends via SMTP → logs to message_log
    │
    ├─ 5. mode == "blocking"? → orchestrator freezes job (status: waiting_for_reply)
    │     mode == "async"?    → return confirmation (graph continues)
    │
    └─ Agent receives: "Message sent to a***@example.com (thread: a7f3b2)"
```

**Receiving (reply arrives):**
```
User replies (cockpit UI or email)
    │
    ├─ 1. Orchestrator receives reply via API or IMAP poller
    │
    ├─ 2. Orchestrator resumes the frozen job with the reply as the message body
    │     (same resume mechanism as manual job resume with feedback)
    │
    ├─ 3. Agent pod receives resume → writes reply to messages/<thread_id>/NNN_received.md
    │
    ├─ 4. Reply content injected as the tool result of the original send_message call
    │     (agent sees the reply as the response to its blocking send_message)
    │
    └─ Agent continues execution with the reply in context
```

The tool handles file I/O; the orchestrator handles email delivery, rate limiting, logging, and job freeze/resume. The agent never touches SMTP credentials or sees full email addresses.

**For `async` replies:** the orchestrator resumes the job with the reply body at the next appropriate point (per delivery strategy). The agent pod writes the received message file. The reply shows up in `git_diff` during the next strategic phase review.

### Graceful Degradation

If SMTP is not configured (`EmailService.is_configured == False`), the tool still writes the message file to the workspace and returns a warning: "Message saved to workspace but email delivery unavailable (SMTP not configured)." The conversation archive works even without email — the cockpit can still show threads and accept replies via the API. This matches the existing `EmailService` pattern where unconfigured SMTP logs a warning and returns `False`.

## Reply Delivery

### How Replies Reach the Agent

The orchestrator receives replies (via cockpit UI or IMAP poller) and resumes the agent with the reply content. The agent pod then writes the received message file to the workspace and injects the reply into its conversation context.

### Delivery Strategies

| Strategy | When applied | How it works |
|----------|-------------|--------------|
| **Immediate resume** | Reply to a `blocking` message | Orchestrator resumes frozen job with reply body. Agent writes file, gets reply as tool result. |
| **Immediate interrupt** | User explicitly marks reply as urgent | Orchestrator resumes job with reply body at next tool boundary. Agent writes file. |
| **Next strategic phase** | Default for replies to `async` messages | Orchestrator queues the reply. On next resume/phase boundary, agent writes file. Discovered via `git_diff`. |
| **AuxiliaryLLM triage** | Phase 2. Unprompted user messages. | Aux LLM reads message + agent state, decides: immediate interrupt or queue for strategic phase. |

### Who Controls Delivery?

- **Agent controls sending mode** (`async` vs `blocking`) — determines the *default* delivery strategy for replies.
- **User controls reply urgency** — can override the default by marking a reply as urgent (immediate interrupt) or routine (next strategic phase).

This separation prevents an agent from forcing immediate interrupts on every message while still letting the user escalate when needed.

### Reply Routing

**Phase 1 — Cockpit-only replies:**
- Notification emails include a "Reply in Cockpit" deep link
- User types reply in cockpit UI → `POST /api/jobs/{id}/messages/{thread}/reply`
- Orchestrator resumes the agent with the reply body (blocking) or queues it (async)
- No inbound email processing needed

**Phase 2 — Native email replies:**
- Dedicated agent address on the mail domain (e.g., `agent@{MAIL_DOMAIN}`)
- Outbound emails use `Reply-To` with `+` sub-addressing: `agent+{job_short_id}+{thread_id}@{MAIL_DOMAIN}`
- Orchestrator runs a background IMAP poll loop against the mail bridge
- Parses `+` address to route replies to the correct job and thread
- Strips email signatures and quoted text (e.g., `email-reply-parser` library)
- Feeds cleaned reply into the same resume path as cockpit replies

IMAP environment variables reuse existing SMTP credentials (`IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `AGENT_EMAIL`).

## Multi-Recipient Communication

The agent can message people beyond the job owner, scoped to **project members** (Phase 2).

The `to` parameter accepts:
- `"user"` — job owner (resolved from `jobs.user_id`)
- Display name — matched against `project_members` (e.g., `"Alice Meyer"`)
- Email address — validated against `project_members`

The orchestrator validates every recipient against the job's project membership. Messages to non-members are rejected.

## Rate Limiting

| Scope | Limit | Window | Behavior |
|-------|-------|--------|----------|
| Per job | 5 messages | 1 hour | Tool returns error with retry-after |
| Per job | 15 messages | 24 hours | Same |
| Per user (all jobs) | 30 messages | 24 hours | Same |

Enforced at the orchestrator endpoint, not in the agent tool. The agent receives a clear error message when rate-limited so it can batch items into fewer messages.

A message sent to multiple recipients (Phase 2) counts as **one message** for rate limiting — it's one email with multiple recipients, not multiple emails.

Replies from humans are not rate-limited.

## Security

| Threat | Mitigation |
|--------|------------|
| Agent spams user with emails | Rate limiting (5/hr per job) |
| Agent emails arbitrary people | Recipient validation against project members |
| Agent crafts phishing-like emails | Orchestrator wraps all messages in branded template; agent doesn't control HTML |
| Agent exfiltrates data via email | Audit logging; message content stored in `message_log` |
| Agent sees SMTP credentials | Agent only calls orchestrator API; SMTP config stays in orchestrator |
| Agent discovers user email addresses | Orchestrator masks addresses in responses (e.g., `a***@example.com`) |
| Prompt injection via reply content | Reply written to file, not injected as raw system message. Agent reads it like any document. |

**OWASP alignment:** The OWASP AI Agent Security Cheat Sheet classifies `send_email` as `RiskLevel.HIGH`. Our design addresses this with rate limiting, least-privilege recipient scoping, audit logging, and autonomy-level integration (at `autonomy: dependent`, even `async` messages could require approval).

## Email Template

The orchestrator renders all outbound emails. The agent provides subject + markdown body; the orchestrator wraps it.

```
From: {agent_config_name} Agent <{SMTP_FROM}>
Reply-To: {AGENT_EMAIL}+{job_short_id}+{thread_id}
Subject: [SRW] {subject}

┌─────────────────────────────────────────┐
│  SRW Agent Message                      │
│  Job: {description}                     │
│  From: {config_name} agent, Phase {n}   │
├─────────────────────────────────────────┤
│                                         │
│  {rendered_markdown_message}            │
│                                         │
├─────────────────────────────────────────┤
│  [Reply in Cockpit →]                   │
│  {COCKPIT_URL}/jobs/{id}                │
│  /messages/{thread_id}                  │
│                                         │
│  or reply directly to this email        │
└─────────────────────────────────────────┘
```

Phase 1 uses `SMTP_FROM` (noreply) as `Reply-To` with a prominent cockpit link. Phase 2 switches `Reply-To` to `AGENT_EMAIL` with `+` addressing for IMAP routing.

## Database Schema

### message_log table

Stores all messages (both directions) for audit trail and rate limit enforcement.

```sql
CREATE TABLE message_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    thread_id VARCHAR(12) NOT NULL,
    direction VARCHAR(10) NOT NULL,       -- 'outbound' or 'inbound'
    recipient_email TEXT,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    mode VARCHAR(10),                     -- 'async', 'blocking' (outbound only)
    status VARCHAR(20) NOT NULL,          -- sent, failed, rate_limited, delivered
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_message_log_job ON message_log(job_id);
CREATE INDEX idx_message_log_thread ON message_log(thread_id);
CREATE INDEX idx_message_log_user_created ON message_log(user_id, created_at);
CREATE INDEX idx_message_log_rate ON message_log(job_id, created_at)
    WHERE direction = 'outbound';
```

Rate limit query:
```sql
SELECT COUNT(*) FROM message_log
WHERE job_id = $1
  AND direction = 'outbound'
  AND status != 'rate_limited'
  AND created_at > NOW() - INTERVAL '1 hour';
```

## Orchestrator API

### Send message (agent → human)

```
POST /api/jobs/{job_id}/messages/send

Request:
{
    "to": "user",
    "subject": "Missing database credentials",
    "message": "I need the PostgreSQL credentials for...",
    "mode": "blocking",
    "thread_id": null
}

200: { "status": "sent", "thread_id": "a7f3b2", "sequence": 1,
       "file_path": "messages/a7f3b2/001_sent.md", "recipient": "a***@example.com" }
429: { "status": "rate_limited", "error": "...", "retry_after_seconds": 1800 }
404: { "status": "no_recipient", "error": "Job has no associated user or user has no email" }
503: { "status": "smtp_unavailable", "error": "..." }  // message still saved to workspace
```

### Deliver reply (human → agent)

```
POST /api/jobs/{job_id}/messages/{thread_id}/reply

Request:
{ "message": "The connection string is postgres://...", "urgent": false }

200: { "status": "delivered", "sequence": 2,
       "file_path": "messages/a7f3b2/002_received.md", "delivery_strategy": "next_strategic_phase" }
```

### List threads

```
GET /api/jobs/{job_id}/messages

200: { "threads": [
    { "thread_id": "a7f3b2", "subject": "Missing database credentials",
      "participants": ["agent", "alice@example.com"], "message_count": 3,
      "last_message_at": "2026-03-17T15:45:00Z", "has_unread": true,
      "mode": "blocking", "status": "waiting_for_reply" }
] }
```

## Config Integration

### Agent config

```yaml
tools:
  communication:
    - send_message

communication:
  enabled: true
  blocking_timeout_hours: 24
  max_message_length: 5000
  allowed_recipients: project     # "project" (members only) or "owner" (job owner only)
```

Disable with `communication: []` in an expert config.

### User delivery preferences

Stored in the existing `users.settings` JSONB column. Managed via `GET/PATCH /api/settings/preferences`.

```json
{
    "communication": {
        "delivery": {
            "async_reply": "next_strategic_phase",
            "blocking_reply": "immediate_resume",
            "unprompted_message": "next_strategic_phase",
            "urgent_override": true
        },
        "channels": { "email": true, "cockpit": true },
        "quiet_hours": {
            "enabled": false,
            "start": "22:00",
            "end": "08:00",
            "timezone": "Europe/Berlin"
        }
    }
}
```

## Implementation Plan

### Phase 1 — Core Mailbox (MVP) [IMPLEMENTED]

Agent sends messages, user replies via cockpit, messages stored on filesystem, outbound via existing SMTP.

**Scope:** `send_message` with async/blocking modes, job owner only (`to: "user"`), cockpit-only replies, rate limiting.

**Created:**

| File | Purpose |
|------|---------|
| `src/tools/communication/__init__.py` | Category boilerplate |
| `src/tools/communication/messaging.py` | `send_message` tool with async/blocking modes |
| `orchestrator/services/email.py` | EmailService with `send_agent_message()` + branded HTML template |

**Modified:**

| File | Change |
|------|--------|
| `src/tools/registry.py` | Imported + registered `communication` category |
| `src/tools/context.py` | Added `_freeze_request` field + `request_freeze()`/`consume_freeze_request()` methods |
| `src/graph.py` | Freeze request check in `audited_tools`, `should_stop` route in `route_after_check_todos` → `check_goal`, received message file write in `restore_from_feedback` |
| `config/defaults.yaml` | Added `communication: [send_message]` tool + `communication:` config section |
| `orchestrator/main.py` | Added `POST /messages/send`, `POST /messages/{thread}/reply`, `GET /messages` endpoints |
| `orchestrator/database/schema.sql` | Added `message_log` table with 4 indexes |
| `orchestrator/database/postgres.py` | Added `log_message()`, `check_message_rate_limit()`, `get_message_threads()`, `get_message_sequence()` + `freeze_data` param on `update_job_status()` |

**Key design decisions:**
- Blocking mode uses the existing job freeze mechanism (`job_frozen.json` + `should_stop`) with a new `waiting_for_reply` status
- Tool-initiated freeze via `ToolContext._freeze_request` → consumed by `audited_tools` graph node (follows `_pending_memories` pattern)
- Graph route: `check_todos` → `check_goal` (bypasses `archive_phase`/`handle_transition` for mid-phase freeze)
- Reply delivery reuses `_internal_resume_job()` with reply as feedback → `restore_from_feedback` writes received message file
- Graceful degradation: messages saved to workspace even if SMTP is unconfigured

**Not in Phase 1:** IMAP reply routing, AuxiliaryLLM triage, delivery preferences UI, multi-recipient.

### Phase 2 — Email Replies + Multi-Recipient

All infrastructure exists. This phase is pure code.

- Dedicated agent address + IMAP poller (`orchestrator/services/imap_poller.py`)
- `Reply-To` with `+` sub-addressing for reply routing
- Email signature/quote stripping
- Multi-recipient via project members
- Delivery preference UI in cockpit settings
- AuxiliaryLLM triage for unprompted user messages

### Phase 3 — Extended Channels

- Webhook transport (Slack, Discord, Ntfy)
- MCP integration (messaging as MCP tool for builder chat)
- Cockpit in-app notification feed
- User-introduced external contacts
- Notification digest, quiet hours

## Relationship to Parent Design

| `docs/email_and_mobile.md` Section | Coverage |
|-------------------------------------|----------|
| 1. Email Notifications (lifecycle) | Orthogonal — separate orchestrator hook system |
| 3. Agent Questions (`ask_user`) | **Superseded** by `send_message` with `mode: blocking` |
| 4. User-to-Agent (pause/resume/feedback) | Partial — reply delivery strategies cover feedback injection |
| 5. Job Scheduling & Queuing | Orthogonal |
| 7. `agent_questions` table | **Superseded** — threads replace dedicated question records |

`send_message` with `mode: blocking` is a superset of the original `ask_user` concept. Instead of a separate question/answer mechanism, the agent sends a message and waits for a reply within a thread. The thread model also supports follow-up questions, multi-party conversations, and async background questions — none of which `ask_user` handled.
