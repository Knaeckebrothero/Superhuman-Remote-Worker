# Autonomous Operation: Email, Mobile & User-Agent Communication

## Motivation

The agent can now sustain execution over 13,000+ steps and stay on track across many phases. The bottleneck is no longer the agent's reliability — it's the human overhead: manually setting up jobs, watching logs, and being physically present to intervene. This document outlines the infrastructure needed to let the agent run 24/7 with minimal supervision, while still giving the user full control and visibility from anywhere.

## Goals

1. **Notify** — The user knows what's happening without watching logs
2. **Monitor** — The user can check status from a phone at any time
3. **Communicate** — The agent can ask questions; the user can answer asynchronously
4. **Intervene** — The user can pause, redirect, or give feedback without being at the terminal
5. **Schedule** — Jobs can be queued and started automatically

---

## 1. Email Notification System

### 1.1 Notification Events

| Event | Priority | Content |
|-------|----------|---------|
| Job completed | High | Summary, key findings, link to cockpit |
| Job failed / crashed | High | Error details, last phase, stack trace excerpt |
| Agent asking a question | High | The question, context, reply instructions |
| Phase transition | Low | Phase N complete, entering phase N+1, brief summary |
| Periodic status digest | Low | Progress %, todos completed, phases done, ETA if possible |
| Long stall detected | Medium | Agent hasn't progressed in X minutes |

### 1.2 Architecture

- **Notification service** in the orchestrator (`orchestrator/services/notifications.py`)
- Pluggable **transport backends**: SMTP, SendGrid, or webhook (for Slack/Discord/Ntfy)
- **Configuration** via environment variables or orchestrator config:
  ```
  NOTIFICATION_ENABLED=true
  NOTIFICATION_TRANSPORT=smtp          # smtp | sendgrid | webhook
  SMTP_HOST=smtp.example.com
  SMTP_PORT=587
  SMTP_USER=...
  SMTP_PASSWORD=...
  NOTIFICATION_RECIPIENT=user@example.com
  NOTIFICATION_EVENTS=job_complete,job_failed,agent_question  # comma-separated
  ```
- **Rate limiting** — batch low-priority events into digests (e.g., phase transitions → one email every 30 min)
- **Templates** — HTML email templates with job context, rendered with Jinja2

### 1.3 Implementation Steps

1. Create `NotificationService` with abstract transport interface
2. Implement SMTP transport (covers most self-hosted setups)
3. Add notification hooks to orchestrator job lifecycle events
4. Add agent question notification path (see Section 3)
5. Add webhook transport for Slack/Discord/Ntfy integration
6. Add notification preferences to orchestrator config/API

---

## 2. Mobile-Optimized Cockpit

### 2.1 Approach

Responsive redesign of the existing Angular cockpit rather than a separate app. The cockpit already has the data — it just needs to be usable on a phone screen.

### 2.2 Key Mobile Views

| View | What it shows | Mobile considerations |
|------|--------------|----------------------|
| **Job list** | Active/recent jobs with status badges | Card layout, swipe actions |
| **Job detail** | Current phase, progress, workspace.md | Collapsible sections, sticky status bar |
| **Live log / audit** | Streaming phase progress | Auto-scroll, compact entries |
| **Agent question** | Pending question with reply input | Prominent CTA, quick-reply support |
| **Feedback form** | Text input for course correction | Simple textarea + send |

### 2.3 Implementation Steps

1. Add responsive breakpoints and mobile-first CSS to existing components
2. Create a simplified mobile navigation (bottom tab bar or hamburger)
3. Add pull-to-refresh for job list and detail views
4. Add push notification support (PWA service worker) for critical events
5. Optional: PWA manifest so the cockpit can be "installed" on phone home screen
6. Optimize graph visualization for touch (or hide on small screens, show summary instead)

### 2.4 PWA Considerations

A Progressive Web App setup would allow:
- Install to home screen (feels like a native app)
- Push notifications via service worker (alternative/complement to email)
- Offline caching of the last-known job state
- No app store needed

This is optional but relatively low effort on top of the responsive redesign.

---

## 3. Agent-to-User Communication (Agent Questions)

### 3.1 Concept

The agent sometimes needs clarification. Today, it either guesses or gets stuck. Instead, it should be able to **ask a question and wait for the user to respond asynchronously**.

### 3.2 Flow

```
Agent encounters ambiguity
    ↓
Agent calls `ask_user` tool with question + context
    ↓
Tool creates a pending question record in the database
    ↓
Agent execution PAUSES (enters "waiting_for_user" state)
    ↓
Notification sent to user (email / push / webhook)
    ↓
User sees question in cockpit or email
    ↓
User submits answer via cockpit UI or email reply
    ↓
Answer is injected as a user message into the agent's conversation
    ↓
Agent execution RESUMES
```

### 3.3 Implementation Steps

1. **New tool: `ask_user`** — available in both strategic and tactical phases
   - Parameters: `question` (str), `context` (str, optional), `options` (list, optional for multiple choice)
   - Creates a question record in PostgreSQL with status `pending`
   - Returns a signal that pauses the graph execution loop
2. **New agent state: `waiting_for_user`** — graph node that polls/waits for answer
   - Configurable timeout (default: 24h, then auto-resume with "no answer provided")
   - While waiting, the job status in the orchestrator shows "awaiting input"
3. **Cockpit question UI** — prominent banner on job detail page when a question is pending
4. **Email with reply link** — deep link to cockpit question page
5. **Optional: email reply parsing** — reply to notification email, parse answer from body
6. **Answer injection** — add user answer as a `HumanMessage` to the conversation state
7. **Database schema** — new `agent_questions` table:
   ```sql
   CREATE TABLE agent_questions (
       id UUID PRIMARY KEY,
       job_id UUID REFERENCES jobs(id),
       question TEXT NOT NULL,
       context TEXT,
       options JSONB,           -- optional multiple choice
       answer TEXT,
       status VARCHAR(20),      -- pending, answered, expired
       created_at TIMESTAMPTZ,
       answered_at TIMESTAMPTZ
   );
   ```

---

## 4. User-to-Agent Communication (Feedback & Intervention)

### 4.1 Pause / Resume

- **Pause**: User clicks "Pause" in cockpit → sets a flag in the database → agent checks flag at next tool boundary and enters a paused state
- **Resume**: User clicks "Resume" → clears flag → agent continues
- Implementation: check pause flag in the `execute` or `check_todos` node of the graph
- Useful for: reviewing intermediate results, preventing wasted compute, debugging

### 4.2 Scheduled Feedback Phases

A new mechanism where the agent **periodically stops and waits for user feedback** before continuing.

**Configuration:**
```yaml
feedback:
  enabled: true
  interval_phases: 3          # pause for feedback every N tactical phases
  timeout_minutes: 60         # auto-resume after timeout if no feedback
  prompt: "Review the current state and provide feedback or type 'continue' to proceed."
```

**Flow:**
```
Phase N completes (strategic phase)
    ↓
Is N divisible by feedback.interval_phases?
    ↓ yes
Enter "feedback" state (similar to waiting_for_user)
    ↓
Notify user: "Phase N complete — review and provide feedback"
    ↓
User reviews workspace.md, plan.md, outputs
    ↓
User submits feedback or "continue"
    ↓
Feedback injected as user message → agent adjusts course
    ↓
Resume execution
```

**Implementation:**
- Add feedback check to the `handle_transition` node (after phase archiving, before next phase)
- Reuse the same `waiting_for_user` state and notification infrastructure from Section 3
- Feedback message becomes a `HumanMessage` the agent sees when planning the next phase

### 4.3 Ad-hoc Feedback (Anytime)

Even outside scheduled feedback phases, the user should be able to send a message:
- Cockpit shows a "Send message to agent" input on the job detail page
- Message is queued and injected at the next safe point (between tool calls)
- Agent acknowledges and incorporates the feedback
- Does NOT pause execution — this is a non-blocking nudge

---

## 5. Job Scheduling & Queuing

### 5.1 Concept

Instead of manually running `python agent.py`, users should be able to queue jobs from the cockpit.

### 5.2 Features

- **Job queue** — submit jobs via cockpit/API, they run when a worker is available
- **Worker process** — long-running daemon that picks up queued jobs
- **Concurrency control** — configurable max parallel jobs (default: 1)
- **Retry policy** — auto-retry failed jobs with configurable attempts and backoff
- **Scheduled jobs** — cron-like scheduling for recurring tasks (e.g., daily compliance checks)

### 5.3 Implementation Steps

1. Add job queue status (`queued` → `running` → `completed`/`failed`) to orchestrator
2. Create worker process (`agent_worker.py`) that polls for queued jobs
3. Add "New Job" form to cockpit (config selection, description, document upload)
4. Add retry logic with exponential backoff
5. Optional: cron scheduling with APScheduler or similar

---

## 6. Priority & Phasing

### Phase 1 — Foundation (Minimum Viable Autonomy)
- [ ] Email notifications for job completion and failure (SMTP)
- [ ] Pause / resume from cockpit
- [ ] Basic responsive CSS for cockpit (job list + detail usable on phone)
- [ ] Job submission via cockpit API

### Phase 2 — Communication
- [ ] `ask_user` tool + waiting state
- [ ] Agent question notification + cockpit answer UI
- [ ] Scheduled feedback phases
- [ ] Ad-hoc user messages to running agent

### Phase 3 — Polish & Scale
- [ ] Mobile-optimized cockpit with PWA support
- [ ] Webhook/Slack/Discord notifications
- [ ] Job queue worker with retry and concurrency
- [ ] Scheduled/recurring jobs
- [ ] Email reply parsing for agent questions
- [ ] Notification digest/batching for low-priority events

---

## 7. Database & API Changes Summary

### New Tables
- `agent_questions` — agent-initiated questions with answers
- `user_messages` — ad-hoc user feedback messages to running agents
- `notification_log` — sent notifications for debugging/audit

### New API Endpoints (Orchestrator)
- `POST /api/jobs/{id}/pause` — pause a running job
- `POST /api/jobs/{id}/resume` — resume a paused job
- `POST /api/jobs/{id}/message` — send feedback to running agent
- `GET /api/jobs/{id}/questions` — list pending agent questions
- `POST /api/jobs/{id}/questions/{qid}/answer` — answer an agent question
- `POST /api/jobs` — submit a new job to the queue
- `GET /api/notifications/config` — get notification settings
- `PUT /api/notifications/config` — update notification settings

### New Agent States
- `waiting_for_user` — paused, awaiting answer to agent question
- `waiting_for_feedback` — paused at scheduled feedback point
- `paused` — manually paused by user

### New Config Options
```yaml
notifications:
  enabled: true
  transport: smtp
  events: [job_complete, job_failed, agent_question]

feedback:
  enabled: false
  interval_phases: 3
  timeout_minutes: 60
```
