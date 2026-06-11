# Sudo Freeze Requests Invisible in Cockpit

**Date:** 2026-04-13
**Status:** Open
**Related:** `docs/done/sudo_freeze_bypass_remote_backend.md` (the bypass that prevented freezes from firing at all)

## Summary

When an agent's sudo attempt triggers a freeze (`freeze_type: "vm_upgrade_required"`), the orchestrator sends an email notification but the request is **completely invisible in the Cockpit UI**. The operator has no way to approve or deny the VM upgrade from the Cockpit.

Two sub-problems:
1. The email "Reply in Cockpit" link routes to a non-existent page
2. The Action Center's Sudo tab shows "No sudo items" because it queries a different data source

## Evidence

- **Test job:** `09f3dc0c-b207-4134-876b-b58408b52210` ("Test sudo upgrade to VM")
- Agent ran `sudo -n true` → freeze triggered → email sent at 10:39 → job paused
- Email received with subject: `[SRW] Job 09f3dc0c needs VM upgrade (sudo detected)`
- Clicking "Reply in Cockpit" navigated to the Builder page (default route)
- Action Center Sudo tab: "No sudo items"
- MCP `list_sudo_requests`: empty

## Root Cause: Two Disconnected Sudo Systems

The codebase has two independent sudo-handling mechanisms that don't share state:

### 1. Sudo Freeze (Agent-Level) — what triggered

| Step | Component | Location |
|------|-----------|----------|
| Agent runs `sudo` | `ShellManager._check_blocked()` | `src/tools/shell/shell_manager.py` |
| Sentinel returned | `_check_sudo_freeze()` | `src/tools/shell/shell_tools.py:128-142` |
| Job freezes | `context.request_freeze()` → completion endpoint | `orchestrator/services/completion.py:323-324` |
| Email sent | `_notify_operator_freeze()` | `orchestrator/main.py:3749-3789` |

**Creates:** `freeze_data` in `jobs` table, sets job status to `paused`.
**Does NOT create:** any record in `sudo_approval_requests`.

### 2. Sudo Approval Gate (VM-Level) — what Cockpit queries

| Step | Component | Location |
|------|-----------|----------|
| VM sudo plugin intercepts | C plugin (`sudo_gate.so`) → Go daemon | External to this repo |
| NATS message received | `SudoGateService._handle_request()` | `orchestrator/services/sudo_gate.py` |
| DB record created | `sudo_approval_requests` INSERT | `sudo_gate.py:502-542` |
| SSE broadcast | `_broadcast_sse("new_request", ...)` | `sudo_gate.py:487` |
| Cockpit receives | `SudoService` listens to SSE | `cockpit/.../sudo.service.ts` |

**Creates:** record in `sudo_approval_requests` table + SSE event.

### Why the Cockpit shows nothing

The Action Center assembles items from three sources, none of which capture freeze-based sudo:

| Source | What it fetches | Why freeze is missed |
|--------|----------------|---------------------|
| `sudo.requests()` | `GET /api/sudo/requests` → `sudo_approval_requests` table | Freeze never writes to this table |
| `notifications()` | Message threads | Freeze notification has no `thread_id` |
| `reviewJobs()` | Jobs with status `pending_review` | Frozen jobs have status `paused`, not `pending_review` |

Relevant code: `cockpit/src/app/core/services/action-center.service.ts:55-67`

## Sub-Problem 1: Email Link Broken

The email "Reply in Cockpit" button links to:
```
https://superhuman-remote-worker.com/jobs/{job_id}
```

Constructed at `orchestrator/services/notification_service.py:158` — when `thread_id` is `None` (which it always is for freeze notifications), the fallback URL is `/jobs/{job_id}`.

But the Cockpit has **no `/jobs/:id` route**. Defined routes in `cockpit/src/app/app.routes.ts:18-40`:

| Pattern | Component |
|---------|-----------|
| `''` | ShellPageComponent (Builder) |
| `'jobs'` | JobsPageComponent (list only) |
| `'inbox'` | InboxPageComponent (Action Center) |
| `'jobs/:jobId/messages/:threadId'` | MessageRedirectComponent |
| `'**'` | redirects to `''` |

The URL `/jobs/09f3dc0c-...` doesn't match `'jobs'` (exact) or `'jobs/:jobId/messages/:threadId'` (needs more segments), so it hits the catch-all `**` and redirects to the Builder.

## Sub-Problem 2: No Sudo Item in Action Center

Even if the operator navigates to the Action Center manually, the Sudo tab is empty because:

1. `SudoService.loadRequests()` calls `GET /api/sudo/requests` (`sudo.service.ts:58-72`)
2. That endpoint queries `sudo_approval_requests` via `sudo_gate.list_requests()` (`main.py:4483-4490`)
3. The freeze mechanism never inserts into `sudo_approval_requests` — it only sets `freeze_data` on the job
4. The SSE channel for sudo (`new_request` events) is never triggered for freezes
5. Result: empty array → "No sudo items"

## The Approval Endpoint Exists But Is Unreachable

The orchestrator has a working `POST /api/jobs/{id}/upgrade-to-vm` endpoint (`main.py:4973-5070`) that:
- Provisions a VM workspace
- Resumes the job with `sudo_action="allow"`
- Clears freeze data

But there is **no UI path** to call it. The Cockpit's Sudo tab approve/deny buttons (`inbox-page.component.ts:274-281`) call `POST /api/sudo/requests/{id}/approve` (the gate endpoint), not the job upgrade endpoint.

## Fix Options

### Option A: Bridge the two systems in the orchestrator

When a job freezes with `vm_upgrade_required`, also create a `sudo_approval_request` record:
- Pro: Cockpit Sudo tab works without changes, SSE broadcasts work
- Con: Conflates two conceptually different flows (VM upgrade vs per-command approval). The approve action needs different handling (upgrade-to-vm vs NATS reply)

### Option B: Extend the Action Center to show frozen jobs

Add a fourth data source to `ActionCenterService` that fetches paused jobs with `freeze_type: "vm_upgrade_required"`:
- Pro: Clean separation — freeze requests and gate requests stay distinct
- Con: More frontend work (new API call, new mapping, new approve/deny handlers)

### Option C: Hybrid — use the existing notification/message system

Create a message thread when the freeze happens (give `_notify_operator_freeze()` a `thread_id`):
- Pro: Shows up in Messages tab, email link works (`/inbox?job=...&thread=...`)
- Con: Approve/deny still needs custom handling. Messages tab is for human conversations, not machine approvals

### Recommendation

**Option B** is the cleanest architecturally. The two flows are genuinely different:
- **Freeze**: "You need a VM to run sudo. Approve the upgrade?" → binary decision, no per-command granularity
- **Gate**: "This specific `sudo apt install X` command needs approval" → per-command, time-limited, NATS-based

Mixing them in the same table would require special-casing everywhere. Better to teach the Action Center about frozen jobs directly.

The email link fix is independent — change `notification_service.py:158` to route to `/inbox` (the Action Center) instead of the non-existent `/jobs/{id}`.
