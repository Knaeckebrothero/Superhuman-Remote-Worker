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

> **Status: Design.** Extends the existing sudo approval gate with job-scoped permission profiles, approval caching, command categories, and time/use-limited rules.

## Problem

The sudo approval gate works — every privileged command is intercepted and either auto-approved by a rule or held for human decision. But in practice, the current system has friction:

1. **Rules are global.** An `apt-get install *` auto-approve rule applies to every agent, every job. A research job that should only read files gets the same permissions as a DevOps job that legitimately needs to install packages.

2. **Approval fatigue.** An agent building a Node.js project might hit `sudo apt-get install` 5-10 times in one job. The operator approves the first one, then has to approve essentially the same thing 9 more times.

3. **No expressive categories.** The cockpit shows risk badges (critical/high/medium/low) but these are purely cosmetic — they don't influence auto-approval behavior. There's no way to say "auto-approve all low-risk commands."

4. **No temporal limits.** Once a rule exists, it's permanent. There's no "approve package installs for the next hour" or "allow up to 5 installs then require human review."

## Solution

Four features that layer on top of the existing gate without changing its core architecture (C plugin, Go daemon, NATS request/reply). All changes are orchestrator-side and cockpit-side — the daemon and plugin are untouched.

### Feature 1: Permission Profiles

A **profile** is a named set of auto-approval rules that can be attached to a job. Rules within a profile are evaluated before global rules, giving job-specific permissions.

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

#### REST endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sudo/profiles` | List all profiles |
| `POST` | `/api/sudo/profiles` | Create profile (name, description) |
| `GET` | `/api/sudo/profiles/{id}` | Get profile with its rules |
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
- Each profile shows its rules in the same list format
- Job creation dialog gains a "Sudo Profile" dropdown

---

### Feature 2: Approval Caching (Session Grants)

When an operator manually approves a command, they can optionally grant a **session rule** — a temporary auto-approval rule scoped to that job, created from the approved command pattern.

#### How it works

1. Operator approves `sudo apt-get install -y libxml2-dev` for job X
2. The approve dialog shows a checkbox: **"Auto-approve similar commands for this job"**
3. If checked, the operator can edit the pattern (default: generalize to `apt-get install *`)
4. System creates a temporary rule: `{pattern: "apt-get install *", action: "approve", job_id: X, expires_at: job_end_or_ttl}`
5. Next time the agent runs `sudo apt-get install -y libcurl-dev`, it auto-approves

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

Formalize the cockpit's risk badges into a policy engine that maps categories to default actions.

#### Categories

| Category | Commands | Default Risk | Default Action |
|----------|----------|-------------|----------------|
| `read` | `cat`, `ls`, `find`, `head`, `tail`, `grep`, `less`, `wc`, `file`, `stat` | low | approve |
| `package` | `apt-get install`, `pip install`, `npm install`, `dnf install`, `cargo install` | medium | review |
| `package-remove` | `apt-get remove`, `pip uninstall`, `npm uninstall` | high | review |
| `service` | `systemctl start/stop/restart/status`, `journalctl` | medium | review |
| `file-modify` | `chmod`, `chown`, `mv`, `cp`, `mkdir`, `touch` | medium | review |
| `file-destroy` | `rm`, `rmdir`, `shred` | high | review |
| `user` | `useradd`, `userdel`, `passwd`, `usermod`, `groupadd` | critical | deny |
| `network` | `iptables`, `ip`, `ss`, `netstat`, `curl`, `wget` | high | review |
| `disk` | `mount`, `umount`, `mkfs`, `fdisk`, `dd`, `lvm` | critical | deny |
| `system` | `reboot`, `shutdown`, `init`, `telinit` | critical | deny |

#### Schema

```sql
CREATE TABLE IF NOT EXISTS sudo_command_categories (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        VARCHAR(50) NOT NULL UNIQUE,
    patterns    TEXT[] NOT NULL,         -- fnmatch patterns that define membership
    risk_level  VARCHAR(20) NOT NULL,    -- low, medium, high, critical
    default_action VARCHAR(20) NOT NULL, -- approve, deny, review
    description TEXT
);
```

Categories are evaluated **after** explicit rules (profile, session, global) but **before** the fallback human review. They provide sensible defaults without requiring the operator to create individual rules for every common command.

#### Evaluation order (final)

```
1. Session rules (job-scoped)
2. Profile rules (job's profile)
3. Global rules
4. Shell metacharacter check → always human review
5. Command category default action
6. No match → human review
```

#### Cockpit integration

The rules panel gains a "Categories" tab showing the category table with toggleable default actions. Operators can override the default action per category (e.g., flip `package` from "review" to "approve" for their environment).

---

### Feature 4: Time & Use-Limited Rules

Already partially covered by session rules (Feature 2), but extended to global and profile rules as well.

#### Schema additions (on `sudo_auto_rules`)

The `expires_at` and `max_uses` / `use_count` columns added in Feature 2 apply to all rule types:

- **Global time-limited rule**: "Approve `apt-get install *` until 18:00 today" — created during a deployment window, auto-expires.
- **Profile use-limited rule**: "Allow up to 10 package installs per job" — `max_uses: 10` on a profile rule. Since profile rules are shared across jobs, the `use_count` resets per job (tracked in a separate counter table or via session rule cloning).

#### Per-job use counting

For profile/global rules with `max_uses`, tracking requires a join table since the rule itself is shared:

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

## Implementation Plan

| Phase | Scope | Effort | Components |
|-------|-------|--------|------------|
| 1 | Permission profiles | 2-3 days | Schema, service, REST, cockpit UI |
| 2 | Approval caching | 1-2 days | Approve dialog, session rule creation, evaluation order |
| 3 | Command categories | 1-2 days | Category table, seed data, evaluation integration, cockpit tab |
| 4 | Time/use limits | 1 day | Schema additions, sweeper extension, counter table |
| **Total** | | **5-8 days** | |

### Phase dependencies

- Phase 1 (profiles) is independent — can ship alone.
- Phase 2 (session rules) depends on the `job_id` + `expires_at` columns, but not on profiles.
- Phase 3 (categories) is independent.
- Phase 4 (time/use limits) extends columns from Phase 2.

Phases 1 and 3 can be built in parallel. Phase 2 before Phase 4.

## Migration

All schema changes are additive (`ADD COLUMN`, new tables). No breaking changes to existing rules or requests. The evaluation order change (session → profile → global → category → human) is backward-compatible: existing global rules continue to work at the same priority, they just gain a new fallback layer below them.

Existing `sudo_auto_rules` rows have `profile_id = NULL`, `job_id = NULL`, `expires_at = NULL`, `max_uses = NULL` — they remain global, permanent, unlimited. No data migration needed.

## Security Considerations

- **Profile escalation**: A job config specifying `sudo.profile: devops` gets broader permissions. This is intentional — the config is set by the operator (via cockpit or API), not by the agent. The agent cannot change its own profile.
- **Session rule scope**: Session rules inherit the job's `job_id` FK with CASCADE delete. When the job ends, session rules are automatically cleaned up. An agent cannot create session rules — only the approval flow creates them.
- **Category override safety**: Category defaults are conservative (most default to "review"). Flipping a category to "approve" is a deliberate operator choice, visible in the cockpit audit trail.
- **Use-count bypass**: An agent could theoretically try to split one operation into many small commands to stay under `max_uses`. This is acceptable — `max_uses` is a soft limit for approval fatigue, not a security boundary. The hard security boundary remains the approval gate itself.
- **Shell metacharacters**: The metacharacter check remains mandatory and cannot be overridden by any rule, profile, or category. Commands with pipes, redirects, or backticks always require human review.

## Related

- [[sudo_approval_gate]] — Core implementation (C plugin, Go daemon, NATS, orchestrator, cockpit)
- [[sudo_approval_plugin]] — Original concept design document
- [[vm_backend]] — VM workspace architecture
- [[cockpit_ds]] — Cockpit UI
