---
tags:
  - security
  - agent-architecture
  - orchestrator
  - infrastructure
related:
  - "[[sudo_approval_gate]]"
  - "[[sudo_approval_plugin]]"
  - "[[vm_backend]]"
  - "[[cockpit_ds]]"
---

# Sudo Permission Profiles & Advanced Rules

> **Status: Design.** Extends the existing sudo approval gate with job-scoped permission profiles, JIT session grants, command categories, time/use-limited rules, audit enrichment, and notification webhooks.

## Problem

The sudo approval gate works — every privileged command is intercepted and either auto-approved by a rule or held for human decision. But in practice, the current system has friction:

1. **Rules are global.** An `apt-get install *` auto-approve rule applies to every agent, every job. A research job that should only read files gets the same permissions as a DevOps job that legitimately needs to install packages. Industry best practice (OWASP Agentic AI Top 10, NIST AI governance frameworks) calls for task-scoped, least-privilege access — agents should be treated as non-human identities with managed, job-specific permission sets.

2. **Approval fatigue.** An agent building a Node.js project might hit `sudo apt-get install` 5-10 times in one job. The operator approves the first one, then has to approve essentially the same thing 9 more times. JIT Privileged Access Management solves this: grant time-limited, task-specific privileges on first approval, auto-revoke when the task ends.

3. **No expressive categories.** The cockpit shows risk badges (critical/high/medium/low) but these are purely cosmetic — they don't influence auto-approval behavior. There's no way to say "auto-approve all low-risk commands" or "always deny destructive operations."

4. **No temporal limits.** Once a rule exists, it's permanent. There's no "approve package installs for the next hour" or "allow up to 5 installs then require human review." The Zero Standing Privileges (ZSP) model — where no permanent elevated access exists by default — is the direction enterprise PAM is moving.

5. **No external notifications.** Pending approvals are only visible in the cockpit UI. If the operator isn't watching, requests time out. There's no webhook, Slack notification, or PagerDuty integration for urgent approvals.

6. **Limited audit context.** The request table records command, user, and decision, but not the agent's current phase, recent tool calls, or the job's purpose. Operators approve commands without enough context to make informed decisions.

## Design Principles

These features follow established patterns from JIT PAM, OWASP AI Agent Security, and the Microsoft Agent Governance Toolkit:

- **Zero Standing Privileges**: Default state is no elevated access. Every privilege is explicitly granted, scoped, and time-limited.
- **Just-In-Time, Just-Enough Access**: Grant the minimum privilege needed for the specific task, automatically revoke when the task completes.
- **Non-Human Identity Governance**: Agents are managed identities with auditable permission sets — not users with blanket access.
- **Immutable Audit Trail**: Every request, decision, rule match, and revocation is recorded with full context.
- **Fail-Closed**: Any ambiguity, timeout, or system failure defaults to denial.

## Solution

Six features that layer on top of the existing gate without changing its core architecture (C plugin, Go daemon, NATS request/reply). All changes are orchestrator-side and cockpit-side — the daemon and plugin are untouched.

---

### Feature 1: Permission Profiles

A **profile** is a named set of auto-approval rules that can be attached to a job. Rules within a profile are evaluated before global rules, giving job-specific permissions. This implements the JIT PAM concept of "task-based roles" — rather than granting broad access, each job type gets a tailored permission set.

#### Schema

```sql
CREATE TABLE IF NOT EXISTS sudo_profiles (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_by  VARCHAR(255) DEFAULT 'operator',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Extend the existing rules table with an optional profile FK.
ALTER TABLE sudo_auto_rules
    ADD COLUMN profile_id UUID REFERENCES sudo_profiles(id) ON DELETE CASCADE;

-- Rules with profile_id = NULL remain global (current behavior).
-- Rules with profile_id != NULL belong to that profile.

CREATE INDEX IF NOT EXISTS idx_sudo_rules_profile
    ON sudo_auto_rules (profile_id, priority ASC)
    WHERE enabled = TRUE;
```

Jobs reference a profile by name in their config:

```yaml
# In config_override or agent config
sudo:
  profile: developer       # NULL = global rules only (default)
  ttl: 300                 # Override default TTL (seconds)
```

#### Orchestrator column

```sql
ALTER TABLE jobs
    ADD COLUMN sudo_profile_id UUID REFERENCES sudo_profiles(id) ON DELETE SET NULL;
```

The dispatcher resolves `sudo.profile` from config_override → agent config → NULL and sets `sudo_profile_id` on job creation.

#### Evaluation order

```
1. Profile rules (for this job's profile, ordered by priority)
2. Global rules (profile_id IS NULL, ordered by priority)
3. Shell metacharacter check (always forces human review)
4. No match → human review
```

This means a profile can override global rules. A `restricted` profile with a single `deny *` rule at priority 0 would block everything regardless of global allows.

#### Pre-built profiles

| Profile | Description | Example Rules |
|---------|-------------|---------------|
| `developer` | Full dev toolchain access | `apt-get install *` → approve, `pip install *` → approve, `npm install *` → approve, `systemctl restart *` → approve |
| `researcher` | Read-only + safe tools | `cat *` → approve, `ls *` → approve, `find *` → approve, `head *` → approve, `grep *` → approve, `*` → deny (catch-all) |
| `devops` | System administration | Inherits `developer` + `systemctl *` → approve, `journalctl *` → approve, `docker *` → review |
| `restricted` | Everything requires approval | `*` → review (single catch-all) |

#### Profile inheritance

A profile can extend another profile via an optional `parent_id`:

```sql
ALTER TABLE sudo_profiles
    ADD COLUMN parent_id UUID REFERENCES sudo_profiles(id) ON DELETE SET NULL;
```

When evaluating, the chain is: **this profile's rules → parent's rules → global rules**. This avoids duplicating common rules across similar profiles (e.g., `devops` extends `developer`).

#### REST endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sudo/profiles` | List all profiles |
| `POST` | `/api/sudo/profiles` | Create profile (name, description, parent_id) |
| `GET` | `/api/sudo/profiles/{id}` | Get profile with its rules (includes inherited) |
| `PUT` | `/api/sudo/profiles/{id}` | Update profile metadata |
| `DELETE` | `/api/sudo/profiles/{id}` | Delete profile (cascades rules) |
| `POST` | `/api/sudo/profiles/{id}/rules` | Add rule to profile |
| `POST` | `/api/sudo/profiles/{id}/clone` | Clone profile with new name |

Existing rule endpoints (`/api/sudo/rules`) continue to work for global rules. The create-rule endpoint gains an optional `profile_id` field.

#### Cockpit UI

The rules panel on the `/sudo` page gains a profile selector dropdown:
- "Global Rules" (default, current behavior)
- Named profiles listed below
- "New Profile" button
- Each profile shows its rules in the same list format, with inherited rules shown in a muted style
- Job creation dialog gains a "Sudo Profile" dropdown

---

### Feature 2: JIT Session Grants (Approval Caching)

When an operator manually approves a command, they can optionally grant a **session rule** — a temporary, job-scoped auto-approval rule created from the approved command pattern. This implements the JIT PAM pattern: grant access on first request, scope it to the task, auto-revoke when done.

The key difference from a permanent rule: session grants are **ephemeral**. They exist only for the duration of the job and are automatically cleaned up — achieving Zero Standing Privileges by default.

#### How it works

1. Operator approves `sudo apt-get install -y libxml2-dev` for job X
2. The approve dialog shows a checkbox: **"Auto-approve similar commands for this job"**
3. If checked, the operator can edit the pattern (default: generalize to `apt-get install *`)
4. System creates a temporary rule: `{pattern: "apt-get install *", action: "approve", job_id: X, expires_at: job_end_or_ttl}`
5. Next time the agent runs `sudo apt-get install -y libcurl-dev`, it auto-approves
6. When the job completes, the session rule is automatically deleted (CASCADE)

#### Schema

```sql
-- Extend sudo_auto_rules with job scoping and expiration.
ALTER TABLE sudo_auto_rules
    ADD COLUMN job_id     UUID REFERENCES jobs(id) ON DELETE CASCADE,
    ADD COLUMN expires_at TIMESTAMPTZ,
    ADD COLUMN max_uses   INTEGER,
    ADD COLUMN use_count  INTEGER DEFAULT 0;

-- Job-scoped rules index.
CREATE INDEX IF NOT EXISTS idx_sudo_rules_job
    ON sudo_auto_rules (job_id, priority ASC)
    WHERE enabled = TRUE AND job_id IS NOT NULL;
```

Session rules are:
- Scoped to a single job (`job_id IS NOT NULL`)
- Optionally time-limited (`expires_at`)
- Optionally use-limited (`max_uses`, incremented on each match)
- Auto-deleted when the job completes (CASCADE on `jobs.id`)

#### Evaluation order (updated with session rules)

```
1. Session rules (job_id = this job, not expired, uses < max)
2. Profile rules (this job's profile)
3. Global rules (profile_id IS NULL, job_id IS NULL)
4. Shell metacharacter check
5. No match → human review
```

#### REST changes

The approve endpoint gains optional fields:

```json
POST /api/sudo/requests/{id}/approve
{
  "reason": "Agent needs XML parsing library",
  "create_session_rule": true,
  "session_pattern": "apt-get install *",
  "session_max_uses": null,
  "session_ttl_minutes": null
}
```

- `create_session_rule`: If true, creates a job-scoped auto-approval rule
- `session_pattern`: fnmatch pattern (defaults to generalizing the approved command)
- `session_max_uses`: Optional cap (null = unlimited for this job)
- `session_ttl_minutes`: Optional time limit (null = lives until job ends)

#### Cockpit UI

The approve dialog expands:

```
┌─ Approve Sudo Request ──────────────────────────┐
│                                                   │
│  Command: apt-get install -y libxml2-dev          │
│  Job: 74b871dd (Test job)                         │
│                                                   │
│  Reason: [________________________]               │
│                                                   │
│  ☑ Auto-approve similar commands for this job     │
│    Pattern: [apt-get install *_________]          │
│    Limit:   [unlimited ▼]  (or N uses)            │
│    Expires: [when job ends ▼]  (or N minutes)     │
│                                                   │
│              [Cancel]  [Approve]                   │
└───────────────────────────────────────────────────┘
```

---

### Feature 3: Command Categories & Risk Policies

Formalize the cockpit's risk badges into a policy engine that maps categories to default actions. This bridges the gap between individual rules (too granular) and no rules (too permissive) — operators get sensible defaults out of the box.

#### Categories

| Category | Commands | Default Risk | Default Action |
|----------|----------|-------------|----------------|
| `read` | `cat`, `ls`, `find`, `head`, `tail`, `grep`, `less`, `wc`, `file`, `stat`, `du`, `df` | low | approve |
| `package-install` | `apt-get install`, `pip install`, `npm install`, `dnf install`, `cargo install` | medium | review |
| `package-remove` | `apt-get remove/purge`, `pip uninstall`, `npm uninstall`, `dnf remove` | high | review |
| `package-update` | `apt-get update/upgrade`, `dnf update`, `pip install --upgrade` | medium | review |
| `service` | `systemctl start/stop/restart/status/enable/disable`, `journalctl` | medium | review |
| `file-modify` | `chmod`, `chown`, `mv`, `cp`, `mkdir`, `touch`, `ln` | medium | review |
| `file-destroy` | `rm`, `rmdir`, `shred`, `truncate` | high | review |
| `user` | `useradd`, `userdel`, `passwd`, `usermod`, `groupadd`, `groupdel`, `visudo` | critical | deny |
| `network` | `iptables`, `nft`, `ip`, `ss`, `netstat`, `ufw` | critical | deny |
| `network-client` | `curl`, `wget`, `ping`, `dig`, `nslookup` | low | approve |
| `disk` | `mount`, `umount`, `mkfs`, `fdisk`, `dd`, `lvm`, `parted` | critical | deny |
| `system` | `reboot`, `shutdown`, `init`, `telinit`, `halt`, `poweroff` | critical | deny |

Note the split between `network` (firewall/routing — critical) and `network-client` (curl/wget — low risk). The current system lumps these together.

#### Schema

```sql
CREATE TABLE IF NOT EXISTS sudo_command_categories (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(50) NOT NULL UNIQUE,
    patterns        TEXT[] NOT NULL,         -- fnmatch patterns that define membership
    risk_level      VARCHAR(20) NOT NULL,    -- low, medium, high, critical
    default_action  VARCHAR(20) NOT NULL,    -- approve, deny, review
    description     TEXT,
    editable        BOOLEAN DEFAULT TRUE     -- FALSE for built-in categories
);
```

Categories are evaluated **after** explicit rules (session, profile, global) but **before** the fallback human review. They provide sensible defaults without requiring the operator to create individual rules for every common command.

#### Evaluation order (final)

```
1. Session rules (job-scoped)
2. Profile rules (job's profile, including inherited)
3. Global rules
4. Shell metacharacter check → always human review
5. Command category default action
6. No match → human review
```

#### Category matching

The evaluator extracts the base command (first element of argv) and matches against category patterns. A command can only belong to one category — first match wins, categories ordered by specificity (longest pattern first).

#### Cockpit integration

The rules panel gains a "Categories" tab showing the category table with toggleable default actions. Operators can override the default action per category (e.g., flip `package-install` from "review" to "approve" for their environment). Built-in categories (`editable: false`) can have their action changed but not their patterns.

---

### Feature 4: Time & Use-Limited Rules

Extends session rules (Feature 2) to global and profile rules as well, enabling deployment windows and quota-based access.

#### Use cases

- **Deployment window**: "Approve `apt-get install *` until 18:00 today" — time-limited global rule that auto-expires after the maintenance window.
- **Quota per job**: "Allow up to 10 package installs per job" — `max_uses: 10` on a profile rule.
- **One-shot grant**: "Approve this exact command once" — `max_uses: 1`.

#### Per-job use counting

For profile/global rules with `max_uses`, tracking requires a join table since the rule itself is shared across jobs:

```sql
CREATE TABLE IF NOT EXISTS sudo_rule_usage (
    rule_id  UUID NOT NULL REFERENCES sudo_auto_rules(id) ON DELETE CASCADE,
    job_id   UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    count    INTEGER DEFAULT 0,
    PRIMARY KEY (rule_id, job_id)
);
```

When a rule with `max_uses` matches, the evaluator checks `sudo_rule_usage` for the current job. If `count >= max_uses`, the rule is skipped (falls through to the next rule or human review).

#### REST changes

Rule creation and update endpoints gain optional fields:

```json
POST /api/sudo/rules
{
  "pattern": "apt-get install *",
  "action": "approve",
  "priority": 100,
  "expires_at": "2026-03-17T18:00:00Z",
  "max_uses": 10,
  "profile_id": null
}
```

#### Expired rule cleanup

The existing expiration sweeper (`sweep_expired`, runs every 15s) is extended to also disable expired rules:

```python
# In sweep_expired():
await conn.execute("""
    UPDATE sudo_auto_rules
    SET enabled = FALSE
    WHERE expires_at IS NOT NULL AND expires_at < NOW() AND enabled = TRUE
""")
```

---

### Feature 5: Enriched Audit Trail

Improve the context available to operators when making approval decisions and for post-hoc audit.

#### Request context enrichment

When the orchestrator receives a sudo request, it enriches the stored record with job context before broadcasting to SSE:

```sql
ALTER TABLE sudo_approval_requests
    ADD COLUMN job_phase       INTEGER,          -- current phase number
    ADD COLUMN job_description TEXT,             -- from jobs table
    ADD COLUMN recent_commands TEXT[],           -- last 5 sudo commands for this job
    ADD COLUMN rule_matched    UUID REFERENCES sudo_auto_rules(id),
    ADD COLUMN category_matched VARCHAR(50);
```

The orchestrator populates these from the `jobs` table and recent `sudo_approval_requests` rows at insert time.

#### Cockpit request detail

The request card in the cockpit expands to show:
- **Job context**: Description, current phase, agent config name
- **Recent sudo history**: Last 5 commands for this job (approved/denied/auto)
- **Rule/category match**: If auto-decided, which rule or category matched and why
- **Command risk analysis**: Category, risk level, whether shell metacharacters are present

This gives operators the context OWASP recommends: "operators should see the raw command, the agent's current task, and recent activity" before approving.

#### Immutable audit log

All approval events (request, decision, rule match, session grant creation, expiration) are written to an append-only audit table:

```sql
CREATE TABLE IF NOT EXISTS sudo_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ DEFAULT NOW(),
    event_type  VARCHAR(50) NOT NULL,   -- request, approved, denied, auto_approved,
                                        -- auto_denied, expired, session_grant_created,
                                        -- rule_created, rule_deleted, profile_changed
    request_id  UUID REFERENCES sudo_approval_requests(id),
    job_id      UUID,
    actor       VARCHAR(255),           -- operator email, 'system', 'auto-rule'
    details     JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sudo_audit_time
    ON sudo_audit_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sudo_audit_job
    ON sudo_audit_log (job_id, timestamp DESC);
```

This table is never updated or deleted from — only appended. It provides the immutable, complete audit trail that enterprise PAM systems require.

#### REST endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sudo/audit` | Query audit log (filters: job_id, event_type, time range, limit) |
| `GET` | `/api/sudo/audit/export` | Export audit log as CSV/JSON for compliance |

---

### Feature 6: Notification Webhooks

Allow operators to receive approval requests outside the cockpit — via Slack, PagerDuty, email, or any webhook endpoint. This follows the HashiCorp Boundary pattern of integrating with existing approval workflows rather than requiring operators to watch a dedicated UI.

#### Schema

```sql
CREATE TABLE IF NOT EXISTS sudo_notification_channels (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        VARCHAR(100) NOT NULL,
    type        VARCHAR(20) NOT NULL,    -- webhook, slack, email
    config      JSONB NOT NULL,          -- type-specific config (url, token, channel, etc.)
    events      TEXT[] DEFAULT '{new_request}',  -- which events trigger notification
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

#### Channel types

**Webhook** (generic):
```json
{
  "url": "https://hooks.example.com/sudo-approvals",
  "headers": {"Authorization": "Bearer ..."},
  "method": "POST"
}
```

**Slack** (incoming webhook):
```json
{
  "webhook_url": "https://hooks.slack.com/services/T.../B.../...",
  "channel": "#agent-approvals"
}
```

The Slack message includes the command, job context, risk level, and a direct link to the cockpit approval page.

**Email** (SMTP):
```json
{
  "smtp_host": "smtp.example.com",
  "to": ["ops@example.com"],
  "from": "sudo-gate@agents.local"
}
```

#### Integration point

The existing `_broadcast_sse()` method is extended to also dispatch to notification channels:

```python
async def _broadcast_sse(self, event_type: str, data: dict) -> None:
    # Existing SSE push ...
    # New: dispatch to notification channels
    await self._dispatch_notifications(event_type, data)
```

Notifications are fire-and-forget (async, non-blocking). Failures are logged but never block the approval flow.

#### REST endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sudo/channels` | List notification channels |
| `POST` | `/api/sudo/channels` | Create channel |
| `PUT` | `/api/sudo/channels/{id}` | Update channel |
| `DELETE` | `/api/sudo/channels/{id}` | Delete channel |
| `POST` | `/api/sudo/channels/{id}/test` | Send test notification |

---

## Evaluation Pipeline (Complete)

The final evaluation order, incorporating all features:

```
sudo command intercepted by C plugin
        │
        ▼
Go daemon → NATS → Orchestrator receives request
        │
        ▼
   ┌─ Step 1: Session rules ──────────────────────────────────┐
   │  job-scoped, not expired, use_count < max_uses           │
   │  Match → approve/deny/review                             │
   └──────────────────────────────────────────────────────────┘
        │ no match
        ▼
   ┌─ Step 2: Profile rules ──────────────────────────────────┐
   │  this job's profile → parent profile (if any)            │
   │  ordered by priority ASC                                 │
   │  Match → approve/deny/review                             │
   └──────────────────────────────────────────────────────────┘
        │ no match
        ▼
   ┌─ Step 3: Global rules ───────────────────────────────────┐
   │  profile_id IS NULL, job_id IS NULL                      │
   │  ordered by priority ASC                                 │
   │  Match → approve/deny/review                             │
   └──────────────────────────────────────────────────────────┘
        │ no match
        ▼
   ┌─ Step 4: Shell metacharacter check ──────────────────────┐
   │  |, &, ;, `, $, <, >, ||, && detected?                  │
   │  Yes → always human review (cannot be overridden)        │
   └──────────────────────────────────────────────────────────┘
        │ no metacharacters
        ▼
   ┌─ Step 5: Command category ───────────────────────────────┐
   │  Match base command against category patterns             │
   │  Match → category's default_action (approve/deny/review) │
   └──────────────────────────────────────────────────────────┘
        │ no category match
        ▼
   Default: human review
```

At any step, `review` means "forward to human" (same as no match). `approve` and `deny` are final — the response is sent immediately and logged.

---

## Implementation Plan

| Phase | Scope | Effort | Components |
|-------|-------|--------|------------|
| 1 | Permission profiles (+ inheritance) | 2-3 days | Schema, service, REST, cockpit UI |
| 2 | JIT session grants | 1-2 days | Approve dialog, session rule creation, evaluation order |
| 3 | Command categories | 1-2 days | Category table, seed data, evaluation integration, cockpit tab |
| 4 | Time/use limits | 1 day | Schema additions, sweeper extension, counter table |
| 5 | Enriched audit trail | 1-2 days | Request enrichment, audit log table, cockpit detail view |
| 6 | Notification webhooks | 1-2 days | Channel table, Slack/webhook dispatch, cockpit config UI |
| **Total** | | **7-12 days** | |

### Phase dependencies

```
Phase 1 (profiles) ───────────────── independent
Phase 2 (session grants) ─────────── independent (adds job_id, expires_at columns)
Phase 3 (categories) ─────────────── independent
Phase 4 (time/use limits) ────────── depends on Phase 2 (extends its columns)
Phase 5 (audit trail) ────────────── independent (benefits from all others but not blocked)
Phase 6 (webhooks) ───────────────── independent (hooks into existing SSE broadcast)
```

Phases 1, 2, 3, 5, and 6 can be built in parallel. Phase 4 after Phase 2.

## Migration

All schema changes are additive (`ADD COLUMN`, new tables). No breaking changes to existing rules or requests. The evaluation order change (session → profile → global → category → human) is backward-compatible: existing global rules continue to work at the same priority, they just gain new layers around them.

Existing `sudo_auto_rules` rows have `profile_id = NULL`, `job_id = NULL`, `expires_at = NULL`, `max_uses = NULL` — they remain global, permanent, unlimited. No data migration needed.

Command categories are seeded on first startup (like the existing schema init pattern). The seed data uses the category table above with conservative defaults.

## Security Considerations

- **Profile escalation**: A job config specifying `sudo.profile: devops` gets broader permissions. This is intentional — the config is set by the operator (via cockpit or API), not by the agent. The agent cannot change its own profile mid-job.
- **Session rule scope**: Session rules inherit the job's `job_id` FK with CASCADE delete. When the job ends, session rules are automatically cleaned up. An agent cannot create session rules — only the operator-initiated approval flow creates them.
- **Category override safety**: Category defaults are conservative (most default to "review" or "deny"). Flipping a category to "approve" is a deliberate operator choice, logged in the audit trail.
- **Use-count bypass**: An agent could theoretically split one operation into many small commands to stay under `max_uses`. This is acceptable — `max_uses` is a soft limit for approval fatigue, not a security boundary. The hard security boundary remains the approval gate itself.
- **Shell metacharacters**: The metacharacter check remains mandatory at Step 4 and **cannot be overridden** by any rule, profile, session grant, or category. Commands with pipes, redirects, or backticks always require human review. This is the system's hard safety floor.
- **Webhook credential security**: Notification channel configs (tokens, webhook URLs) are stored in the database. The REST API never returns sensitive fields in GET responses (masked with `***`). Only create/update endpoints accept credentials.
- **Audit immutability**: The `sudo_audit_log` table is append-only. No UPDATE or DELETE endpoints are exposed. Database-level protections (e.g., a trigger that rejects UPDATE/DELETE) can be added for environments requiring tamper-evident logging.
- **Inter-agent privilege escalation**: Per OWASP guidance, the system prevents privilege escalation through agent chains — each job has its own profile and session grants. One agent's approvals cannot influence another agent's permissions.

## OWASP Agentic AI Alignment

This design addresses several items from the OWASP Top 10 for Agentic Applications (2026):

| OWASP Risk | How Addressed |
|------------|---------------|
| **Agent Identity & Authorization Abuse** | Profiles bind permissions to job identity. Session grants are non-transferable. |
| **Tool Misuse** | Categories provide default-deny for dangerous commands. Shell metacharacter check is mandatory. |
| **Privilege Escalation** | JIT grants expire with the job. Zero standing privileges by default. No permanent elevated access. |
| **Insufficient Logging & Monitoring** | Immutable audit log, enriched request context, notification webhooks for real-time alerting. |
| **Cascading Failures** | Use-limited rules and rate limiting (existing 5 req/min) prevent runaway privilege requests. |

## Future Considerations

- **OPA integration**: Replace fnmatch evaluation with Open Policy Agent (OPA) for Rego-based policies. Would enable complex rules like "allow apt-get install only if the package is in an allowlist" or "deny if the agent has already installed more than 20 packages total."
- **Argument-level restrictions**: Extend beyond command-level matching to inspect specific flags — e.g., allow `apt-get install` but block `apt-get install --force-*` or `rm` but block `rm -rf /`.
- **Approval delegation to LLM**: A supervisor agent evaluates low-risk requests automatically (using the command category + job context), only escalating ambiguous or high-risk commands to the human. This is the "dynamic risk-based access" pattern from enterprise PAM, where clean low-risk requests auto-approve while higher-risk ones route to a human.
- **Session recording**: Record terminal output during approved sudo commands (via script(1) or tmux capture) and attach to the audit trail. Enterprise PAM systems (CyberArk, BeyondTrust) do this for compliance.
- **Multi-approval for critical commands**: Require two operators to approve critical-risk commands (dual authorization). Prevents a single compromised operator account from approving destructive operations.

## Related

- [[sudo_approval_gate]] — Core implementation (C plugin, Go daemon, NATS, orchestrator, cockpit)
- [[sudo_approval_plugin]] — Original concept design document
- [[vm_backend]] — VM workspace architecture
- [[cockpit_ds]] — Cockpit UI
