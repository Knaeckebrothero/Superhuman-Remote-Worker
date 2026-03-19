---
tags:
  - feature
  - cockpit
  - ui
  - communication
  - approval
aliases:
  - approve and reply center
  - inbox
  - action center
  - unified inbox
related:
  - "[[live_communication]]"
  - "[[notify_user_tool]]"
  - "[[sudo_permissions]]"
  - "[[vm_backend]]"
---

# Feature: Action Center (Approve & Reply Hub)

Unified cockpit page that merges sudo approvals, agent message threads, and frozen job reviews into a single human-in-the-loop inbox. Replaces the current `/sudo` and `/review` routes with one `/inbox` route.

**Status:** Design phase.
**Parent designs:** `docs/done/live_communication.md`, `docs/done/sudo_approval_gate.md`

## Current State Audit

Before designing the action center, here is an honest assessment of what exists today and where the gaps are.

### What's functional and accessible

| Surface | Route | Status | Notes |
|---------|-------|--------|-------|
| **Sudo page** | `/sudo` (sidebar) | Fully functional | Real-time SSE, approve/deny with risk badges, countdown timers, auto-approval rule management. Self-contained — works end to end. |
| **Review page** | `/review` (sidebar) | Functional | Shows frozen job data (summary, deliverables, confidence bar), approve button, resume with feedback. Requires knowing which job to look at. |
| **Notification bell** | Sidebar footer | Partially functional | Shows unread count badge, dropdown lists recent notifications. Clicking a notification navigates to `/jobs?id={job_id}` — **ignores `thread_id` entirely**, so message threads are unreachable. |

### What's backend-only (no frontend)

The **entire message thread system** has a complete backend but zero cockpit UI:

| Backend capability | Endpoint | Frontend status |
|-------------------|----------|----------------|
| Agent sends message | `POST /api/jobs/{job_id}/messages/send` | N/A (agent-side) |
| Human replies | `POST /api/jobs/{job_id}/messages/{thread_id}/reply` | **No UI** — only reachable via email reply or `curl` |
| List thread summaries | `GET /api/jobs/{job_id}/messages` | **No API service method**, no component |
| View thread messages | — | **Endpoint doesn't exist yet** (thread listing returns summaries only) |

There is no Angular service method to call the messages API, no component to display a conversation thread, no reply form, and no route to handle message URLs.

### Dead ends

**Email "Reply in Cockpit" button** — When an agent sends a blocking message, the email includes a link:

```
http://localhost:4200/jobs/{job_id}/messages/{thread_id}
```

This URL is generated in `orchestrator/services/email.py` (the `cockpit_link` variable). But the route `/jobs/:job_id/messages/:thread_id` **does not exist** in `app.routes.ts`. The wildcard `**` catches it and redirects to `/` — the operator lands on the builder page with no indication of the message.

**Notification bell `thread_id`** — The `AppNotification` model carries `thread_id`, but `NotificationBellComponent.onNotificationClick()` only uses `job_id`:

```typescript
// notification-bell.component.ts:228-230 — thread_id is available but unused
if (n.job_id) {
  this.router.navigate(['/jobs'], { queryParams: { id: n.job_id } });
}
```

Clicking a message notification takes you to the jobs list filtered to that job, but there is no way to view or reply to the thread from there.

**Debug dashboard** — The debug page has panel selectors for database inspection, LLM requests, jobs, agents, and graph visualization. Neither the sudo panel nor any message component is available there.

### Summary

```
Sudo approvals:    Backend ✓  Frontend ✓  (standalone page, works)
Frozen job review:  Backend ✓  Frontend ✓  (standalone page, works)
Message threads:   Backend ✓  Frontend ✗  (zero UI — biggest gap)
Notification bell:  Backend ✓  Frontend ~  (shows items, can't act on threads)
Email reply links:  Backend ✓  Frontend ✗  (generates dead URLs)
```

The action center is not just a consolidation of existing UI — it is the **first time message threads become viewable and replyable** in the cockpit. It also fixes the dead-end email links and makes the notification bell useful for all action types.

## Motivation

Given the current state above, the problems are:

1. **No in-UI reply path at all.** Agent messages with `mode: blocking` set `job.status = "waiting_for_reply"` until a reply arrives via `POST /api/jobs/{job_id}/messages/{thread_id}/reply`. Today, the only way to reply is email or raw API call. The email "Reply in Cockpit" button links to a URL that doesn't resolve. The action center provides the first-ever reply textarea for agent messages.
2. **Scattered attention.** Sudo approvals and job reviews live on separate pages with no shared awareness. Operators miss time-sensitive sudo requests because they're on the review page, or vice versa.
3. **No unified priority.** A pending sudo request with 30s TTL is more urgent than a frozen job that can wait hours. No current view expresses this cross-type urgency.
4. **Dead notification paths.** The notification bell shows message items but can't act on them. Clicking navigates to the job list, not the thread. The `thread_id` field is carried end-to-end but never used in the frontend.
5. **No cross-cutting filters.** Can't filter by job, by agent, or by urgency across all interaction types.

## Existing Infrastructure

All backend endpoints for the three action types already exist. The action center's primary job is building the **missing message thread frontend** and consolidating the two existing pages (sudo, review) into a unified view — plus two small backend additions.

### Message System — backend complete, frontend missing

The live communication system (`docs/done/live_communication.md`) provides a full backend:

- **Agent → Human:** `POST /api/jobs/{job_id}/messages/send` — agent sends a message, optionally blocking
- **Human → Agent:** `POST /api/jobs/{job_id}/messages/{thread_id}/reply` — reply with optional `urgent` flag
- **Thread listing:** `GET /api/jobs/{job_id}/messages` — returns thread summaries with `thread_id`, `subject`, `message_count`, `sent_count`, `received_count`, `started_at`, `last_message_at`, `mode`, `status`
- **SSE:** `GET /api/notifications/events` — emits `new_message` and `reply_delivered` events
- **Job status:** Blocking sends set `job.status = "waiting_for_reply"` and `job.freeze_data.thread_id`; reply triggers `immediate_resume`
- **Email delivery:** Messages are sent via SMTP with a "Reply in Cockpit" link pointing to `/jobs/{job_id}/messages/{thread_id}`

**What's missing on the frontend:**
- No `ApiService` method to call `GET /api/jobs/{job_id}/messages` or `POST .../reply`
- No Angular component to display a conversation thread or render a reply form
- No route to handle the `/jobs/{job_id}/messages/{thread_id}` URLs that emails link to (currently a dead end — hits wildcard redirect to `/`)
- The `NotificationService` receives `new_message` SSE events but the `thread_id` is never acted on in the UI

**Backend gap:** No endpoint returns the full ordered list of individual messages within a thread. The thread listing endpoint returns only aggregated summaries. A new `GET /api/jobs/{job_id}/messages/{thread_id}` endpoint is needed (see New API Endpoints).

### Sudo Approval — backend and frontend complete

The sudo approval gate (`docs/done/sudo_approval_gate.md`) is fully functional end-to-end:

- **SSE:** `GET /api/sudo/events` — emits `new_request`, `request_decided`
- **CRUD:** `GET /api/sudo/requests`, `POST .../approve`, `POST .../deny`
- **Rules:** `GET/POST/DELETE /api/sudo/rules` — auto-approval pattern management
- **TTL:** Each request has `expires_at`; expired requests are no longer actionable
- **Frontend:** `SudoPageComponent` at `/sudo` with real-time updates, risk badges, countdown timers, deny dialog, rule management

This page works well in isolation. The action center extracts its UI into a detail panel so it can share screen space with other action types.

### Frozen Job Review — backend and frontend complete

- **Frozen data:** `GET /api/jobs/{job_id}/frozen` — returns `freeze_type` (`job_complete` or `phase_boundary`), `summary`, `deliverables`, `confidence`
- **Approve:** `POST /api/jobs/{job_id}/approve` — marks job completed (for `job_complete`) or approved to continue (for `phase_boundary`)
- **Resume:** `POST /api/jobs/{job_id}/resume` — resumes with optional `feedback` string injected as system message
- **Job listing with filter:** `GET /api/jobs?status=pending_review` — returns all jobs awaiting review
- **Frontend:** `ReviewPageComponent` at `/review` using `JobReviewComponent` — shows frozen data, confidence bar, approve/resume buttons

Like sudo, this works but is isolated. It also requires the operator to already know which job to look at.

### Notification Feed — backend complete, frontend partially wired

- **Listing:** `GET /api/notifications` — returns `AppNotification[]` with `id`, `job_id`, `thread_id`, `subject`, `message`, `job_description`, `config_name`, `read_at`, `created_at`
- **Mark read:** `PATCH /api/notifications/{id}`
- **SSE:** `GET /api/notifications/events`
- **Frontend:** `NotificationBellComponent` in sidebar footer — shows badge + dropdown list, marks read on click, navigates to `/jobs?id={job_id}` (ignoring `thread_id`)

## Design

### Core Concept

A **single-page inbox** with a left-side item list and a right-side detail/action panel. Each item in the list is an "action item" — something that needs (or needed) human attention. Three item types share the same list:

```
┌─────────────────────────────────────────────────────────────────┐
│  Action Center                    [filter chips]  [SSE ●]  [↻] │
├──────────────────────────┬──────────────────────────────────────┤
│                          │                                      │
│  ▌● Sudo: apt install.. │  SUDO REQUEST                        │
│    agent-dev · 28s left  │                                      │
│                          │  $ sudo apt-get install libxml2-dev  │
│  ▌● Message: Missing .. │                                      │
│    scholar · job fc2a..  │  Risk: LOW        Expires: 28s       │
│                          │  User: agent → root                  │
│    Review: Job complete  │  VM: agent-dev-vm-1                  │
│    developer · job 8b1.. │  CWD: /home/agent/workspace          │
│                          │                                      │
│    Sudo: chmod 755 /t..  │  ┌─────────┐  ┌──────┐              │
│    agent-dev · approved  │  │ Approve  │  │ Deny │              │
│                          │  └─────────┘  └──────┘              │
│    Message: Status upd.. │                                      │
│    scholar · read        │                                      │
│                          │                                      │
└──────────────────────────┴──────────────────────────────────────┘
```

**Empty state** (no pending items): centered illustration with "All clear — no items need your attention" message. Resolved items still visible below if the time filter is active.

### Action Item Types

| Type | Source | Urgency Signal | Actions Available | Real-time |
|------|--------|---------------|-------------------|-----------|
| **Sudo request** | `SudoService` (SSE + REST) | TTL countdown (seconds) | Approve, Deny (with reason) | Yes — `new_request`, `request_decided` |
| **Agent message** | `NotificationService` (SSE + REST) | Blocking mode (`waiting_for_reply`) vs async | Reply, Mark read | Yes — `new_message`, `reply_delivered` |
| **Frozen job** | `GET /api/jobs?status=pending_review` (event-driven) | Freeze type (phase boundary vs completion) | Approve, Resume with feedback | Re-fetched on SSE notification events, on init, and on `/inbox` navigation |

### Unified Action Item Model

Frontend model that normalizes the three backend types into a single sortable list:

```typescript
type ActionItemType = 'sudo' | 'message' | 'review';
type ActionItemStatus = 'pending' | 'resolved';

interface ActionItem {
  /** Stable ID: prefixed to avoid collisions (e.g., "sudo:uuid", "msg:thread_id", "rev:job_id") */
  id: string;
  type: ActionItemType;
  status: ActionItemStatus;           // pending = needs human action
  urgency: number;                    // 0–100, higher = more urgent (see scoring table)
  timestamp: string;                  // ISO 8601, used for secondary sort
  title: string;                      // command text / message subject / job description
  subtitle: string;                   // agent name · job ID snippet · vm name
  jobId: string | null;               // all types link to a job

  // Discriminated payload — exactly one is populated
  sudo?: SudoRequest;                 // from SudoService
  message?: MessageActionData;        // derived from notifications, grouped by thread
  review?: ReviewActionData;          // derived from job + frozen data
}

interface MessageActionData {
  threadId: string;
  subject: string;
  mode: 'blocking' | 'async';
  lastMessage: string;                // preview (first 200 chars)
  configName: string | null;          // agent config that sent the message
  jobDescription: string | null;
  unread: boolean;                    // based on read_at
}

interface ReviewActionData {
  jobId: string;
  jobDescription: string;
  configName: string | null;
  freezeType: 'job_complete' | 'phase_boundary';
  phaseType: string | null;           // "strategic" | "tactical"
  phaseNumber: number | null;
  summary: string | null;
  confidence: number | null;          // 0.0–1.0
  deliverables: string[];
  frozenAt: string | null;
}
```

### Urgency Scoring

Determines sort order within pending items. Pending items always sort above resolved. Within each group: urgency descending, then timestamp descending.

| Type | Condition | Score | Rationale |
|------|-----------|-------|-----------|
| Sudo | TTL < 30s | 90 | About to expire, immediate action required |
| Sudo | TTL < 120s | 70 | Expiring soon |
| Message | Blocking (`waiting_for_reply`) | 80 | Agent is frozen, can't make progress |
| Review | Phase boundary | 60 | Agent can't continue to next phase |
| Sudo | TTL >= 120s | 50 | Has time, but still actionable |
| Message | Async, unread | 40 | Informational, not blocking |
| Review | Job complete | 30 | Just needs sign-off, no urgency |
| Message | Async, read | 20 | Already seen, reply optional |

**Note:** `waiting_for_reply` jobs do NOT appear as separate review items — they are represented by their blocking message thread. This avoids double-counting. The review category only includes `pending_review` jobs.

### TTL Visual Treatment

Sudo requests show a countdown badge on the list row itself — operators need to see urgency at a glance without clicking into the detail panel. The badge uses color transitions:

| TTL Remaining | Color | Badge Style |
|---------------|-------|-------------|
| > 60% of original TTL | Green (`--green`) | Static countdown |
| 20–60% | Amber (`--peach`) | Static countdown |
| < 20% | Red (`--red`) | Static countdown |
| < 10% | Red (`--red`) | Pulsing animation (respects `prefers-reduced-motion`) |
| Expired | Muted (`--overlay0`) | "Expired" text, no countdown |

The countdown badge updates every second, but **list re-sorting only happens at urgency threshold crossings** (TTL dropping below 120s or 30s) and on expiry — not every tick. This prevents items from jumping around while the operator is scanning the list. On expiry, the item transitions to `resolved` status and dims in the list. The default expiry behavior (auto-deny) is shown in the detail panel: "This request will be auto-denied when the timer expires."

### Notification → Action Item Mapping

Notifications and action items are not 1:1. The mapping logic:

1. **Messages:** Group notifications by `thread_id`. Each unique `(job_id, thread_id)` pair produces one message action item. Use the most recent notification in the thread for the preview and timestamp. A thread is "pending" if any notification in it is unread, OR if the job status is `waiting_for_reply` on that thread.

2. **Sudo:** Direct 1:1 mapping from `SudoRequest`. Status is "pending" if `request.status === 'pending'`, "resolved" otherwise.

3. **Reviews:** One action item per job with `status === 'pending_review'`. Frozen data is fetched lazily when the item is selected (not for every job in the list).

### Detail Panels

Each action item type renders its own detail panel on the right side when selected.

#### Sudo Detail Panel

Reuses the current sudo card design but in the detail panel:

- Full command with monospace formatting
- Risk badge (low/medium/high/critical) — same `riskLevel()` heuristic from current `SudoPageComponent`
- Countdown timer for pending requests (updates every second)
- Metadata: `requesting_user → target_user`, `vm_name`, `working_directory`
- **Pending:** Approve / Deny buttons (deny opens inline reason textarea, required)
- **Resolved:** Decision info — `decided_by`, `decision_reason`, `decided_at`

**Sudo rules** are accessible via a gear icon (⚙) in the sudo detail panel header — opens a slide-over or popover with the rule list + add form. Rules are secondary configuration, not part of the main action flow.

#### Message Detail Panel

Thread-based conversation view with inline reply:

```
┌──────────────────────────────────────┐
│  Missing database credentials        │
│  scholar · job fc2a · ● blocking     │
├──────────────────────────────────────┤
│                                      │
│  ┌─ Agent (10:32 AM) ──────────────┐ │
│  │ I need the PostgreSQL connection │ │
│  │ string for the analytics DB.     │ │
│  │ The job can't continue without   │ │
│  │ it.                              │ │
│  └──────────────────────────────────┘ │
│                                      │
│  ┌─ You (10:45 AM) ────────────────┐ │
│  │ Use the default DS config,      │ │
│  │ credentials are in vault.       │ │
│  └──────────────────────────────────┘ │
│                                      │
│  ┌─ Agent (10:45 AM) ──────────────┐ │
│  │ Thanks, resuming with vault     │ │
│  │ credentials.                    │ │
│  └──────────────────────────────────┘ │
│                                      │
│  ┌──────────────────────────┐  ┌───┐ │
│  │ Type a reply...          │  │ ➤ │ │
│  └──────────────────────────┘  └───┘ │
│  ☐ Mark as urgent                    │
└──────────────────────────────────────┘
```

- Full thread history loaded via `GET /api/jobs/{job_id}/messages/{thread_id}` (new endpoint)
- Messages styled by direction: outbound (agent, left-aligned) vs inbound (you, right-aligned)
- Message bodies rendered as markdown via `ngx-markdown` (`MarkdownComponent`) — already installed and configured in `app.config.ts` with GFM + line breaks. Reuse the markdown styles from `instruction-builder.component.ts` (headings, code blocks with Prism syntax highlighting, lists, tables, blockquotes).
- Reply textarea with send button — calls `POST /api/jobs/{job_id}/messages/{thread_id}/reply`
- "Mark as urgent" checkbox → sets `urgent: true` on the reply request
- Mode badge in header: `blocking` (red accent — job is frozen) or `async` (muted)
- Auto-scroll to bottom of thread on load

#### Review Detail Panel

Adapted from the current `JobReviewComponent`:

- Job description and metadata (ID snippet, created date, config name)
- Freeze type badge: `job_complete` (green) or `phase_boundary` (purple)
- Phase info: type (strategic/tactical) and number
- Summary text from `freeze_data.summary`
- Deliverables list (if present)
- Confidence bar with percentage (if present)
- **For `job_complete`:** Approve button (with optional notes textarea)
- **For `phase_boundary`:** Resume with feedback textarea + Resume button
- Both actions call the existing endpoints (`POST /api/jobs/{job_id}/approve` or `POST /api/jobs/{job_id}/resume`)

### Filter System

Horizontal filter chips below the header:

```
[All (5)]  [Messages (2)]  [Sudo (2)]  [Reviews (1)]     [Resolved ▾]
```

- Chips show count of **pending** items per type
- "All" is the default, shows pending items from all types
- "Resolved" is a dropdown toggle with time window: Last hour / Last 24h / Last 7d / Off (default: Off)
- When a type filter is active + resolved is on, shows both pending and resolved of that type

**Secondary filters** (collapsible row, hidden by default):
- By job (searchable dropdown of jobs with active action items)
- By agent/config name (dropdown)
- Free text search across titles

### SSE Lifecycle

The `ActionCenterService` must provide live counts globally (for the sidebar badge and notification bell), not just on the `/inbox` page. SSE connections are managed at the service level:

```
App Bootstrap
└── ActionCenterService (providedIn: 'root')
    ├── SudoService.connectSSE()       ← started once, stays open
    ├── NotificationService.connectSSE() ← started once, stays open
    ├── computed: items()              ← always up to date
    └── computed: counts()             ← drives badge everywhere
```

Currently, `SudoService.connectSSE()` is called in `SudoPageComponent.ngOnInit()` and disconnected in `ngOnDestroy()`. Similarly for `NotificationService` in `NotificationBellComponent`. After this change, `ActionCenterService` owns the lifecycle — connects on service initialization (app startup), reconnects on error. The individual components no longer manage SSE.

### Notification Bell Simplification

The bell stays in its current location (sidebar footer) but is simplified to a badge + link:

- **Badge count:** `ActionCenterService.counts().total` (all pending action items, not just unread notifications)
- **Click:** Navigate to `/inbox`
- **Tooltip:** "2 messages, 1 sudo, 1 review" breakdown
- **No dropdown, no SSE management** — the `ActionCenterService` handles everything

No new nav item is added for the inbox — the bell is the entry point. The sidebar "Review" and "Sudo" nav items are removed; the bell replaces them as the single access point. This eliminates the dropdown that had the rendering bug (opening off-screen at the bottom of the sidebar), fixes the ignored `thread_id`, and replaces it with a simpler, more reliable interaction.

### Deep-Link Handling

The inbox page reads query parameters to auto-select items on load:

```
/inbox?job={job_id}&thread={thread_id}   → selects the message thread
/inbox?job={job_id}&review=true          → selects the frozen job review
/inbox?sudo={request_id}                 → selects the sudo request
```

`InboxPageComponent.ngOnInit()` reads `ActivatedRoute.queryParams`, finds the matching `ActionItem` in the list (by `jobId` + `threadId`, or by `sudo` ID), and sets it as the selected item. If the item isn't loaded yet (SSE hasn't delivered it), the component retries once after the first data load completes.

This is required for:
- Email "Reply in Cockpit" buttons (generate `/inbox?job=X&thread=Y`)
- The redirect component that catches old-format email URLs (see Route Changes)
- Direct links shared between operators

### Real-Time Update Behavior

When SSE delivers new or updated items:

- **New items** appear at their urgency-sorted position with a brief highlight animation (0.3s background flash, respects `prefers-reduced-motion`). The list does **not** scroll or reflow the viewport — new items above the fold get a "N new items" chip at the top that scrolls to them on click.
- **Resolved items** (sudo decided by another operator, job approved elsewhere) transition to resolved state with a fade. If the currently-selected item is resolved externally, the detail panel shows a "This item was already resolved" banner and the action buttons are disabled. (Richer concurrent-operator UX — showing who resolved it, real-time locking — is deferred to a future iteration.)
- **Focus management:** When the user resolves an item via keyboard (`a` to approve), focus automatically advances to the next pending item in the list.
- **Optimistic UI:** Approve/deny actions reflect immediately in the UI. A brief undo toast (3 seconds) appears for deny actions. If the server rejects the action, the item reverts to its previous state with an error toast.
- **Scroll preservation:** List scroll position is preserved across SSE updates. Items are inserted/removed without shifting the viewport of currently-visible items.

### Accessibility

The action center follows WCAG 2.1 AA guidelines:

**Structure:**
- The item list uses `role="feed"` with `aria-label="Action center items"`
- Each item is a focusable element with `role="article"` and `aria-labelledby` pointing to the title element
- The detail panel uses `role="region"` with `aria-label` set to the selected item's title

**Live regions:**
- New item arrivals: `aria-live="polite"` announcement ("New sudo request: apt-get install libxml2-dev")
- Sudo TTL countdown: `aria-live="polite"` with announcements every 15 seconds. Switches to `aria-live="assertive"` when TTL < 30 seconds
- Action confirmations: `role="status"` for "Approved", "Denied", "Reply sent" announcements

**Keyboard focus:**
- Arrow/j/k navigation moves `aria-activedescendant`, not DOM focus, to keep the list container focused
- When an item is resolved, focus advances to the next pending item
- `Escape` from detail panel returns focus to the previously-selected list item

**Visual:**
- Urgency is conveyed through icon + text label + color — never color alone
- The countdown badge includes a screen-reader-only text alternative ("28 seconds remaining")
- All animations respect `prefers-reduced-motion` (pulsing countdown falls back to static red badge)

### What Does NOT Belong Here

The action center is exclusively for items requiring human decision. These do **not** appear:

- Job started / completed / failed status changes (visible on the jobs page)
- Agent heartbeat or connection events
- Phase transition notifications that don't require approval (full autonomy mode)
- Build or CI/CD events
- System health alerts

If a notification does not require approve, deny, reply, or review — it stays in the notification feed only and does not become an action item.

### Responsive Behavior

On narrow viewports (< 768px), the two-panel layout collapses to a single column:
- List view is the default
- Selecting an item navigates to a full-width detail view
- Back button returns to the list
- This matches the existing cockpit patterns (jobs page uses the same approach)

## New API Endpoints

### Thread Detail

```
GET /api/jobs/{job_id}/messages/{thread_id}
```

Returns the full ordered list of individual messages within a thread. The existing `GET /api/jobs/{job_id}/messages` returns only aggregated thread summaries (`sent_count`, `received_count`, `started_at`, `last_message_at`, `mode`, `status`). The reply UI needs the actual message content.

**Response:**
```json
{
  "thread_id": "a7f3b2",
  "subject": "Missing database credentials",
  "mode": "blocking",
  "status": "waiting_for_reply",
  "messages": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "direction": "outbound",
      "subject": "Missing database credentials",
      "message": "I need the PostgreSQL connection string for the analytics DB. The job can't continue without it.",
      "created_at": "2026-03-19T10:32:00Z",
      "read_at": null
    },
    {
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "direction": "inbound",
      "message": "Use the default DS config, credentials are in vault.",
      "created_at": "2026-03-19T10:45:00Z",
      "read_at": null
    }
  ]
}
```

**Implementation:** Query `message_log` by `job_id` + `thread_id`, ordered by `created_at ASC`. Thread-level `subject` is taken from the first outbound message. `mode` and `status` are derived the same way as the thread listing endpoint.

### Pending Actions Summary

```
GET /api/actions/pending
```

Lightweight endpoint for the notification bell tooltip and initial page load. Returns counts across all action types for the authenticated user.

**Response:**
```json
{
  "counts": {
    "sudo": 2,
    "messages": 3,
    "reviews": 1,
    "total": 6
  },
  "most_urgent": {
    "type": "sudo",
    "id": "req-uuid",
    "title": "apt-get install libxml2-dev",
    "expires_in_seconds": 28
  }
}
```

**Implementation:** Three queries, each lightweight:
1. `SELECT COUNT(*) FROM sudo_requests WHERE status = 'pending'`
2. `SELECT COUNT(*) FROM jobs WHERE status = 'waiting_for_reply'` — each blocked job has exactly one blocking thread; the simpler job-level count is sufficient
3. `SELECT COUNT(*) FROM jobs WHERE status = 'pending_review'`

All queries are scoped to the authenticated user's jobs when multi-user auth is in place. Cached server-side for 5 seconds to avoid hammering the DB on rapid refreshes.

## Existing API Endpoints (no changes)

| Endpoint | Used by |
|----------|---------|
| `GET /api/sudo/events` | SSE: sudo request lifecycle |
| `GET /api/sudo/requests` | List sudo requests (filterable by job_id, status) |
| `POST /api/sudo/requests/{id}/approve` | Approve sudo request |
| `POST /api/sudo/requests/{id}/deny` | Deny sudo request (reason required) |
| `GET/POST/DELETE /api/sudo/rules` | Auto-approval rule CRUD |
| `GET /api/notifications/events` | SSE: message and reply events |
| `GET /api/notifications` | List notifications (filterable by unread_only, limit) |
| `PATCH /api/notifications/{id}` | Mark notification read |
| `GET /api/jobs/{job_id}/messages` | List thread summaries for a job |
| `POST /api/jobs/{job_id}/messages/{thread_id}/reply` | Reply to agent (with optional `urgent` flag) |
| `GET /api/jobs/{job_id}/frozen` | Get frozen job data (summary, confidence, deliverables) |
| `POST /api/jobs/{job_id}/approve` | Approve frozen job |
| `POST /api/jobs/{job_id}/resume` | Resume with optional feedback injection |
| `GET /api/jobs?status=pending_review` | List jobs awaiting review |

## Frontend Architecture

### New Files

| File | Purpose |
|------|---------|
| `core/services/action-center.service.ts` | Unified service: composes SudoService + NotificationService, manages SSE lifecycle, exposes merged `items()` and `counts()` signals |
| `core/models/action.model.ts` | `ActionItem`, `MessageActionData`, `ReviewActionData` interfaces |
| `simple/pages/inbox/inbox-page.component.ts` | Page shell: header, filter chips, two-panel responsive layout |
| `shared/components/action-list/action-list.component.ts` | Left panel: sorted, filterable list of `ActionItemCardComponent` instances |
| `shared/components/action-item-card/action-item-card.component.ts` | Single list item: type icon, title, subtitle, urgency indicator, relative time |
| `shared/components/sudo-detail/sudo-detail.component.ts` | Right panel for sudo requests (extracted from `SudoPageComponent`) |
| `shared/components/message-detail/message-detail.component.ts` | Right panel for message threads: conversation view + reply textarea |
| `shared/components/review-detail/review-detail.component.ts` | Right panel for frozen jobs (extracted from `JobReviewComponent`) |
| `shared/components/message-redirect/message-redirect.component.ts` | Tiny redirect component: reads `:jobId/:threadId` from route params, navigates to `/inbox?job={jobId}&thread={threadId}` — needed because Angular `redirectTo` can't transform path params to query params |

### Modified Files

| File | Change |
|------|--------|
| `app.routes.ts` | Replace `/sudo` and `/review` routes with `/inbox`; add redirects for old URLs |
| `layout/sidebar/sidebar.component.ts` | Remove "Review" + "Sudo" nav items (bell in footer is the inbox entry point) |
| `shared/components/notification-bell/notification-bell.component.ts` | Simplify: remove dropdown + SSE, just badge + navigate to `/inbox` |
| `core/services/notification.service.ts` | Remove SSE auto-connect/disconnect from the service; let `ActionCenterService` control lifecycle |

### Deleted Files

| File | Reason |
|------|--------|
| `simple/pages/sudo/sudo-page.component.ts` | Functionality moved to `InboxPageComponent` + `SudoDetailComponent` |
| `simple/pages/review/review-page.component.ts` | Functionality moved to `InboxPageComponent` + `ReviewDetailComponent` |

`JobReviewComponent` is kept if referenced elsewhere (e.g., job detail view); otherwise deleted.

### ActionCenterService Design

```typescript
@Injectable({ providedIn: 'root' })
export class ActionCenterService {
  private readonly sudo = inject(SudoService);
  private readonly notifications = inject(NotificationService);
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  // --- SSE lifecycle (owned here, not by individual components) ---
  constructor() {
    this.sudo.connectSSE();
    this.notifications.connectSSE();
    // Root-provided services are never destroyed during app lifetime,
    // but DestroyRef is the canonical Angular cleanup hook if needed
    this.destroyRef.onDestroy(() => {
      this.sudo.disconnectSSE();
      this.notifications.disconnectSSE();
    });
  }

  // --- Review jobs (event-driven, not SSE — no dedicated frozen-job SSE event exists) ---
  readonly reviewJobs = signal<Job[]>([]);
  loadReviewJobs(): void {
    // GET /api/jobs?status=pending_review
    // Called on: service init, SSE notification events (job freeze creates a notification),
    // SSE reconnect, /inbox navigation, and manual refresh button.
    // No periodic polling — if this proves insufficient, add interval(30000) later.
  }

  // --- Merged action items ---
  readonly items = computed<ActionItem[]>(() => {
    const sudoItems = this.sudo.requests().map(r => this.mapSudo(r));
    const messageItems = this.deduplicateThreads(this.notifications.notifications());
    const reviewItems = this.reviewJobs().map(j => this.mapReview(j));

    return [...sudoItems, ...messageItems, ...reviewItems].sort(actionItemComparator);
  });

  readonly counts = computed(() => {
    const pending = this.items().filter(i => i.status === 'pending');
    return {
      sudo: pending.filter(i => i.type === 'sudo').length,
      messages: pending.filter(i => i.type === 'message').length,
      reviews: pending.filter(i => i.type === 'review').length,
      total: pending.length,
    };
  });

  // --- Actions ---
  loadThread(jobId: string, threadId: string): Observable<ThreadDetail> { ... }
  reply(jobId: string, threadId: string, message: string, urgent: boolean): Observable<any> { ... }
  approveJob(jobId: string, notes?: string): Observable<any> { ... }
  resumeJob(jobId: string, feedback: string): Observable<any> { ... }

  // --- Mapping helpers ---
  private deduplicateThreads(notifications: AppNotification[]): ActionItem[] {
    // Group by (job_id, thread_id), take most recent per group
    // Determine pending/resolved based on read_at and job status
  }
  private mapSudo(req: SudoRequest): ActionItem { ... }
  private mapReview(job: Job): ActionItem { ... }
}
```

### Route Changes

```typescript
// app.routes.ts
export const routes: Routes = [
  { path: '',        component: ShellPageComponent, canActivate: [authGuard] },
  { path: 'jobs',    component: JobsPageComponent, canActivate: [authGuard] },
  { path: 'create',  component: CreatePageComponent, canActivate: [authGuard] },
  { path: 'inbox',   component: InboxPageComponent, canActivate: [authGuard] },  // NEW
  { path: 'projects', component: ProjectListPageComponent, canActivate: [authGuard] },
  { path: 'projects/:id', component: ProjectDetailPageComponent, canActivate: [authGuard] },
  { path: 'settings', component: SettingsComponent, canActivate: [authGuard] },
  { path: 'debug',   component: DebugPageComponent, canActivate: [authGuard] },

  // Redirects for old bookmarks
  { path: 'sudo',   redirectTo: 'inbox' },
  { path: 'review', redirectTo: 'inbox' },

  // Catch old email links — redirectTo can't transform path params to query params,
  // so this uses a tiny redirect component that reads :jobId/:threadId and navigates
  // to /inbox?job={jobId}&thread={threadId}
  { path: 'jobs/:jobId/messages/:threadId', component: MessageRedirectComponent },

  { path: '**', redirectTo: '' },
];
```

## Keyboard Shortcuts

The action center supports keyboard-driven workflows:

| Key | Action | Context |
|-----|--------|---------|
| `↑` / `↓` or `j` / `k` | Navigate action items in list | List focused |
| `Enter` | Select item / open detail panel | List focused |
| `Escape` | Deselect / return to list | Detail focused |
| `a` | Approve (sudo request or frozen job) | Detail focused, item pending |
| `d` | Start deny flow (sudo) | Sudo detail, item pending |
| `r` | Focus reply textarea | Message detail |
| `Ctrl+Enter` | Submit reply / confirm action | Textarea focused |
| `1` / `2` / `3` | Filter: Messages / Sudo / Reviews | Any |
| `0` | Clear filter (show all) | Any |
| `?` | Show/hide keyboard shortcut overlay | Any |

Shortcuts are disabled when a textarea or input has focus (except `Ctrl+Enter`, `Escape`, and `?`). The `?` overlay is a semi-transparent modal listing all shortcuts — dismissed with `Escape` or another `?`.

## Implementation Plan

### Phase 1 — Backend: Thread Detail + Pending Summary + Email Link Fix

Two new endpoints, one small fix to existing email template.

| File | Change |
|------|--------|
| `orchestrator/main.py` | Add `GET /api/jobs/{job_id}/messages/{thread_id}` — query `message_log` by job_id + thread_id, ordered by `created_at ASC`; derive thread metadata from first outbound row |
| `orchestrator/main.py` | Add `GET /api/actions/pending` — three count queries (sudo_requests, message_log via jobs, jobs), 5s server cache |
| `orchestrator/database/postgres.py` | Add `get_thread_messages(job_id, thread_id)` query; add `get_pending_action_counts(user_id)` query |
| `orchestrator/services/email.py` | Fix `cockpit_link` generation: change from `/jobs/{job_id}/messages/{thread_id}` to `/inbox?job={job_id}&thread={thread_id}` — this makes email "Reply in Cockpit" buttons work once the `/inbox` route exists |

**Testable independently:** Both endpoints can be verified via curl before any frontend work begins. The email link fix takes effect immediately for new messages (already-sent emails with old URLs are handled by a catch route in Phase 2).

### Phase 2 — Frontend: Service + Page Shell + List

| File | Change |
|------|--------|
| `core/services/action-center.service.ts` | New: compose SudoService + NotificationService, manage SSE lifecycle, expose `items()` + `counts()` |
| `core/models/action.model.ts` | New: `ActionItem`, `MessageActionData`, `ReviewActionData`, `ThreadDetail` |
| `simple/pages/inbox/inbox-page.component.ts` | New: page shell with header, filter chips, responsive two-panel layout |
| `shared/components/action-list/action-list.component.ts` | New: left panel with sorted item list |
| `shared/components/action-item-card/action-item-card.component.ts` | New: individual card (type icon, title, subtitle, urgency dot, relative time) |
| `shared/components/message-redirect/message-redirect.component.ts` | New: reads `:jobId/:threadId` from route, navigates to `/inbox?job=X&thread=Y` |
| `app.routes.ts` | Add `/inbox` route, add `/sudo` and `/review` redirects, add `/jobs/:jobId/messages/:threadId` redirect component route |
| `layout/sidebar/sidebar.component.ts` | Replace "Review" + "Sudo" nav items with "Inbox" + badge |

**Milestone:** Page loads, shows merged list from all three sources with correct sorting. Detail panel shows a placeholder. Old routes redirect correctly.

### Phase 3 — Frontend: Detail Panels

| File | Change |
|------|--------|
| `shared/components/sudo-detail/sudo-detail.component.ts` | New: extracted from `SudoPageComponent` — command display, risk badge, countdown, approve/deny actions, sudo rules gear icon + popover |
| `shared/components/message-detail/message-detail.component.ts` | New: thread conversation view, message bubbles by direction, reply textarea with send + urgent checkbox |
| `shared/components/review-detail/review-detail.component.ts` | New: extracted from `JobReviewComponent` — summary, deliverables, confidence bar, approve/resume actions |

**Milestone:** Full functionality — all three item types can be viewed and acted on from the inbox.

### Phase 4 — Simplification + Cleanup

| File | Change |
|------|--------|
| `shared/components/notification-bell/notification-bell.component.ts` | Simplify: remove dropdown template + styles, remove SSE lifecycle, just badge + `router.navigate(['/inbox'])` |
| `core/services/notification.service.ts` | Remove `connectSSE()`/`disconnectSSE()` calls from component lifecycle; `ActionCenterService` now owns this |
| `simple/pages/sudo/sudo-page.component.ts` | Delete |
| `simple/pages/review/review-page.component.ts` | Delete |
| `shared/components/job-review/job-review.component.ts` | Delete if unused elsewhere; keep if referenced in job detail view |

**Milestone:** No dead code, SSE lifecycle centralized, bell simplified.

## Migration & Backwards Compatibility

- `/sudo` and `/review` redirect to `/inbox` — no broken bookmarks or shared links
- **Email dead-end fix:** Update `orchestrator/services/email.py` to generate `cockpit_link` pointing to `/inbox?job={job_id}&thread={thread_id}` instead of the non-existent `/jobs/{job_id}/messages/{thread_id}`. Old-format URLs in already-sent emails are handled by a `MessageRedirectComponent` on the catch route — this is necessary because Angular's `redirectTo` cannot transform path params into query params, so a component reads `:jobId/:threadId` and navigates to `/inbox?job=X&thread=Y`.
- Existing `SudoService` and `NotificationService` are unchanged internally — `ActionCenterService` composes them
- MCP tools (`list_sudo_requests`, `approve_sudo_request`, `deny_sudo_request`) continue to work — same backend endpoints
- Orchestrator SSE event format unchanged — frontend adapts, not backend
- The `GET /api/actions/pending` endpoint is additive — no existing behavior changes

## Future Extensions (not in scope)

- **Desktop notifications** — browser `Notification` API for urgent items when the tab is backgrounded
- **Sound alerts** — configurable audio ping for new pending sudo requests or blocking messages
- **Bulk actions** — approve/deny multiple sudo requests at once
- **Delegation** — forward a message thread to another team member
- **Auto-response templates** — saved replies for common agent questions (e.g., "Use vault credentials", "Approved, continue")
- **Unified SSE** — single `GET /api/actions/events` stream replacing the two separate ones (backend optimization, not needed for MVP since composing two streams client-side is fine)
- **Webhook integrations** — forward pending actions to Slack/Discord for teams that prefer those surfaces
