---
tags:
  - feature
  - agent-tool
  - communication
  - email
  - notification
aliases:
  - notify_user
  - agent email tool
  - user notification tool
related:
  - "[[email_and_mobile]]"
  - "[[interactive_agent]]"
---

# Feature: `notify_user` Agent Tool

Design document for giving agents the ability to proactively send email messages to the user who owns the job.

**Status:** Design phase.
**Parent design:** `docs/email_and_mobile.md` (Section 1 + Section 3)

## Motivation

Agents run autonomously for hours across dozens of phases. Today, the user has two options: watch logs in real time, or check the cockpit periodically. Neither scales.

The agent itself is best positioned to know *when* something is worth the user's attention. A phase transition is routine — but discovering that a critical dependency is missing, or that two requirements contradict each other, or that the job is complete: these are moments the agent should be able to reach out.

The `docs/issues/task_clearance_user_feedback.md` case study shows the failure mode: an agent spent 6 phases and 220 tool calls on a task it could have resolved in minutes if it could simply ask the user a question. Without a communication channel, it called `job_complete` with fabricated confidence instead.

This tool is the **first concrete deliverable** from the broader notification system designed in `docs/email_and_mobile.md`. It is scoped narrowly: one tool, one channel (email), one direction (agent → user). The bidirectional `ask_user` tool (Section 3 of the parent doc) builds on top of this later.

## Industry Context

### How Others Do It

| System | Pattern | Channel | Agent Control | Safety |
|--------|---------|---------|---------------|--------|
| **Google Workspace CLI** | Agent composes free-form email via `+send` skill | Gmail API (OAuth) | Full — agent writes subject, body, recipients | Model Armor prompt/response sanitization, granular tool exposure |
| **OpenAI Codex** | `notify` config fires external script on `agent-turn-complete` | Desktop, webhook, any script | None — notification is hardcoded trigger, not agent-initiated | Async, no credential passing to agent |
| **Claude Code** | Hooks system (`Notification`, `Stop`, `TaskCompleted` events) | Desktop (`notify-send`), HTTP webhook, any command | None — hooks fire on events, not by agent choice | Scoped settings, env var allowlist |
| **Devin** | Slack DM on status changes, per-session toggle | Slack | Partial — agent sends status updates, not arbitrary messages | Thread isolation, user toggle |
| **AgentMail** | Purpose-built email API for agents (Y Combinator S25) | Email (dedicated addresses, SPF/DKIM/DMARC) | Full — agent composes and sends | SOC 2, TLS, rate limits |
| **Composio / Arcade.dev** | Pre-built actions (`GMAIL_SEND_EMAIL`, `SLACKBOT_SEND_MESSAGE`) | 500+ apps via OAuth | Full — agent selects action and parameters | Managed OAuth, per-tool permission scoping |
| **LangGraph** | `interrupt()` pauses execution, no push notification | None native | N/A | Checkpointer state preservation |
| **n8n / Trigger.dev** | Workflow nodes route to Slack/email/webhook based on urgency | Multi-channel | Workflow-defined | Node-level configuration |
| **OWASP AI Agent Cheat Sheet** | Classifies `send_email` as `RiskLevel.HIGH` | Any (with approval) | Requires human approval | Rate limiting, PII redaction, audit logging |

### Key Takeaways

1. **Event-driven vs agent-initiated.** Most systems (Codex, Claude Code, Trigger.dev) use event-driven notifications — the system fires on state changes, the agent doesn't decide. Google Workspace CLI and Composio go the other direction: the agent decides when and what to send. Our design is agent-initiated, which is more powerful but requires stronger guardrails.

2. **Rate limiting is universal.** Every production system caps notification volume. OWASP suggests `100 calls / 60 seconds` as a concrete starting point. For email specifically, much lower limits are appropriate (e.g., 5 per hour per job).

3. **The agent should not know the recipient.** Google Workspace CLI and Composio let agents pick recipients — that's appropriate for general-purpose email tools. For our scoped use case (agent notifies *its* user), the orchestrator resolves the recipient from job ownership. The agent never sees or controls the email address.

4. **LangGraph has a gap here.** `interrupt()` pauses execution but has no push notification mechanism. LangSmith webhooks only support run completion, not interrupt events. This is an active feature request in the LangChain community. Our `notify_user` tool fills this gap.

5. **Security is the main concern.** The OpenClaw Gmail incident (Feb 2026) — where an AI agent deleted hundreds of emails while ignoring stop commands — demonstrates why email tools need strict guardrails. OWASP classifies `send_email` as high-risk requiring human approval. Our design mitigates this by: (a) the agent can only notify the job owner, not arbitrary recipients, (b) rate limiting at the orchestrator, (c) audit logging.

## Design

### Tool: `notify_user`

```
notify_user(subject: str, message: str, urgency: str = "normal") -> str
```

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | str | yes | Email subject line (max 200 chars) |
| `message` | str | yes | Message body in markdown (max 5000 chars) |
| `urgency` | str | no | `"low"`, `"normal"`, `"high"` — controls email priority headers and future routing |

**Returns:** Confirmation string with delivery status.

**Phase availability:** Both strategic and tactical.

**What it does NOT do:**
- Choose the recipient (resolved from `jobs.user_id` by the orchestrator)
- Pause execution (that's `ask_user`, a separate tool — see parent doc Section 3)
- Send to arbitrary addresses (security boundary)
- Attach files (keep it simple; the email links to the cockpit for details)

### Architecture

```
Agent calls notify_user("Build failed", "Dependency X is missing...", "high")
    │
    ▼
Tool sends POST /api/jobs/{job_id}/notify to orchestrator
    │
    ▼
Orchestrator:
  1. Validates rate limit (per-job, per-hour)
  2. Looks up jobs.user_id → users.email
  3. Renders email template (subject, message, job context)
  4. Sends via EmailService (existing SMTP infrastructure)
  5. Logs to notification_log table
  6. Returns success/failure
    │
    ▼
Agent receives confirmation, continues execution
```

The agent never touches SMTP credentials, never sees the user's email address, and never controls routing. The orchestrator is the gatekeeper.

### Rate Limiting

| Scope | Limit | Window | Behavior on exceed |
|-------|-------|--------|-------------------|
| Per job | 5 | 1 hour | Tool returns error: "Rate limit reached. You can send another notification in X minutes." |
| Per job | 15 | 24 hours | Same |
| Per user (all jobs) | 30 | 24 hours | Same |

Rate limits are enforced at the orchestrator endpoint, not in the agent tool. This prevents circumvention and centralizes policy.

The agent receives a clear error message when rate-limited, so it can adjust behavior (e.g., batch multiple items into one notification next time).

### Email Template

The orchestrator renders the email, not the agent. The agent provides `subject` and `message` (markdown); the orchestrator wraps it in a consistent HTML template with:

- Job ID and description
- Agent config name
- Current phase number
- Direct link to the cockpit job detail page
- Urgency-based styling (high = red accent, normal = default, low = muted)

This ensures consistent branding and prevents the agent from crafting phishing-like emails.

```
Subject: [SRW] {urgency_badge} {subject}

Body:
┌─────────────────────────────────┐
│  SRW Agent Notification         │
│  Job: {description} ({job_id})  │
│  Agent: {config_name}           │
│  Phase: {phase_number}          │
├─────────────────────────────────┤
│                                 │
│  {rendered_markdown_message}    │
│                                 │
├─────────────────────────────────┤
│  [View in Cockpit →]            │
└─────────────────────────────────┘
```

### Orchestrator Endpoint

```
POST /api/jobs/{job_id}/notify

Body:
{
    "subject": "Build failed — missing dependency",
    "message": "The `poppler-utils` package is not installed...",
    "urgency": "high"
}

Response (200):
{
    "status": "sent",
    "notification_id": "uuid",
    "recipient": "user@example.com"  // masked: "u***@example.com"
}

Response (429):
{
    "status": "rate_limited",
    "error": "Rate limit exceeded: 5 per hour per job",
    "retry_after_seconds": 1800
}

Response (404):
{
    "status": "no_recipient",
    "error": "Job has no associated user or user has no email"
}

Response (503):
{
    "status": "smtp_unavailable",
    "error": "Email service not configured"
}
```

### Notification Log Table

```sql
CREATE TABLE notification_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    urgency VARCHAR(10) DEFAULT 'normal',
    status VARCHAR(20) NOT NULL,         -- sent, failed, rate_limited
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_notification_log_job ON notification_log(job_id);
CREATE INDEX idx_notification_log_user_created ON notification_log(user_id, created_at);
```

This table serves dual purpose: audit trail and rate limit enforcement (count recent rows per job/user).

### Agent Tool Implementation

New tool category: `communication` at `src/tools/communication/`.

```python
# src/tools/communication/notify.py

NOTIFY_TOOLS_METADATA = {
    "notify_user": {
        "module": "communication.notify",
        "function": "notify_user",
        "description": "Send an email notification to the user who owns this job",
        "category": "communication",
        "phases": ["strategic", "tactical"],
        "short_description": "Email the job owner with a status update or question.",
    },
}
```

The tool is synchronous from the agent's perspective — it makes an HTTP POST to the orchestrator and returns the result. It uses `httpx` (already a project dependency) with the `ORCHESTRATOR_URL` environment variable.

### Config Integration

```yaml
# config/defaults.yaml
tools:
  # ... existing categories ...
  communication:
    - notify_user
```

Agents that should not send emails can override:
```yaml
# config/experts/silent_worker/config.yaml
tools:
  communication: []   # No notification tools
```

## When the Agent Should Use This Tool

The tool docstring and system prompt guidance should make clear when notification is appropriate:

**Good uses:**
- Job completed successfully (summary of deliverables)
- Job blocked by external dependency (missing package, unreachable API, credentials expired)
- Ambiguity that the agent cannot resolve autonomously (contradicting requirements)
- Significant milestone in a long-running job (e.g., "Phase 1 of 5 complete, found 47 issues")
- Error recovery failed after multiple attempts

**Bad uses (the agent should learn to avoid):**
- Routine phase transitions (these are low-value; use notification events for those)
- Every todo completion
- Asking a question and continuing without waiting (that's what `ask_user` is for)

The rate limit naturally discourages overuse. The tool's docstring should include guidance like: "Use sparingly. The user will see this as an email. Reserve for significant events that the user needs to know about."

## Relationship to Parent Design

This feature implements a focused subset of `docs/email_and_mobile.md`:

| Parent Doc Section | This Feature | Status |
|--------------------|-------------|--------|
| 1. Email Notifications | `notify_user` tool + orchestrator endpoint | **This doc** |
| 3. Agent Questions (`ask_user`) | Not included — separate follow-up | Future |
| 4. User-to-Agent (pause/resume/feedback) | Not included | Future |
| 7. `notification_log` table | Included (for audit + rate limiting) | **This doc** |

The `notify_user` tool is one-directional (agent → user). The `ask_user` tool (Section 3 of parent doc) adds bidirectionality by pausing execution and waiting for a response. That tool will reuse the same orchestrator notification infrastructure but adds the `agent_questions` table and `waiting_for_user` state.

## Implementation Plan

### Files to Create

| File | Purpose |
|------|---------|
| `src/tools/communication/__init__.py` | Category boilerplate (create/get pattern) |
| `src/tools/communication/notify.py` | `notify_user` tool implementation + metadata |

### Files to Modify

| File | Change |
|------|--------|
| `src/tools/registry.py` | Import + register `communication` category |
| `config/defaults.yaml` | Add `communication: [notify_user]` to tools |
| `orchestrator/main.py` | Add `POST /api/jobs/{job_id}/notify` endpoint |
| `orchestrator/services/email.py` | Add `send_job_notification()` method + HTML template |
| `orchestrator/database/schema.sql` | Add `notification_log` table |
| `orchestrator/database/postgres.py` | Add notification log + rate limit query methods |

### Implementation Order

1. **Database** — Add `notification_log` table to schema
2. **Orchestrator email** — Add `send_job_notification()` to EmailService
3. **Orchestrator DB** — Add `log_notification()` and `check_rate_limit()` to PostgresDB
4. **Orchestrator API** — Add `POST /api/jobs/{job_id}/notify` endpoint
5. **Agent tool** — Create `src/tools/communication/` with `notify_user`
6. **Registry** — Wire up in `registry.py`
7. **Config** — Add to `defaults.yaml`
8. **Test** — Manual test with a running job

## Future Extensions

These are explicitly **not in scope** but the design accommodates them:

- **Webhook transport** — The orchestrator endpoint already abstracts the transport. Adding Slack/Discord/Ntfy means adding transport backends, not changing the agent tool.
- **User notification preferences** — Per-user settings for channel (email, Slack, webhook URL), urgency thresholds, quiet hours. Stored in a `notification_preferences` table, checked by the orchestrator before sending.
- **`ask_user` tool** — Reuses the same endpoint infrastructure but adds a `wait_for_response: true` flag that triggers the `waiting_for_user` state from the parent design doc.
- **Notification digest** — Batch low-urgency notifications into periodic summaries rather than individual emails.
- **Cockpit notification feed** — In-app notification panel showing all agent messages, independent of email delivery.
- **MCP integration** — Expose `notify_user` as an MCP tool for the cockpit builder chat to trigger notifications from the instruction builder.
