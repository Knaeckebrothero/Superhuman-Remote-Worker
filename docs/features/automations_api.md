---
tags:
  - reference
  - api
  - automations
  - integrations
aliases:
  - external API guide
  - api keys quickstart
  - n8n integration
  - zapier integration
related:
  - "[[automations]]"
  - "[[automations_v0]]"
  - "[[auth_bff_and_api_tokens]]"
---

# Orchestrator API — External Integration Guide

This page is the user-facing entry point linked from the **"Need more
than schedules?"** panel on the Automations page in the cockpit. It
documents how to drive the orchestrator from n8n, Zapier, your own
scripts, or `curl` for everything native automations can't do — webhooks,
inbound email, multi-step branching, and so on.

If your need is "run this thing on a schedule," use the **Automations**
page in the cockpit. Everything else lives here.

## TL;DR

```bash
# 1) Mint a PAT in the cockpit at /settings/api-keys
#    (the prefix is `ak_`; you'll see the full token exactly once).

# 2) Create a job:
curl -X POST https://api.<your-host>/api/jobs \
    -H "Authorization: Bearer ak_<your-token>" \
    -H "Content-Type: application/json" \
    -d '{"description": "Summarize this week's PRs", "config_name": "scholar"}'
```

That's the whole thing. The job lands in the same queue as cockpit-
created jobs and the existing autonomy / project / model preferences
apply.

## 1. Personal Access Tokens (PATs)

PATs are managed at **Settings → API Keys** in the cockpit.

- **Prefix:** `ak_` (32 URL-safe characters of entropy).
- **Display:** the full token is revealed exactly once at creation;
  afterwards only `prefix…last_four` is shown.
- **Default expiry:** 1 year. Selectable: 30 days, 90 days, 1 year,
  never (with an explicit warning).
- **Scopes:** v0 ships permissive — any PAT can hit any endpoint the
  owner can reach. Per-endpoint scope enforcement is on the PR 4 roadmap
  in [`auth_bff_and_api_tokens.md`](./auth_bff_and_api_tokens.md).
- **Rotation:** Settings → API Keys → "Rotate". Issues a successor; the
  old token stays valid for a 24-hour grace window so an automation can
  switch over without an outage.
- **Revocation:** "Revoke" on the row. Effective immediately.

PATs back onto the same `auth_tokens` table as the legacy MCP tokens
(`srw_*` prefix) used by Claude Code. See
[`auth_bff_and_api_tokens.md`](./auth_bff_and_api_tokens.md) §3.6 for
the consolidation history.

## 2. Auth header

All endpoints documented here accept either:

```
Authorization: Bearer ak_<your-token>
```

or (legacy, deprecated for new integrations):

```
X-MCP-Token: srw_<your-token>
```

For n8n, Zapier, and curl, **use the `Authorization: Bearer ak_…` form**.
`X-MCP-Token` exists only so the Claude Code CLI doesn't break.

## 3. Endpoints worth knowing

Full Swagger docs at `/api/docs` (FastAPI auto-generated). What follows
is the curated subset most external integrations need.

### Create a job

```
POST /api/jobs
Content-Type: application/json
Authorization: Bearer ak_…

{
  "description": "What the agent should do (becomes the prompt)",
  "config_name": "scholar",
  "config_override": {"autonomy": "full"},
  "project_id": "<uuid or null>",
  "priority": 5
}
```

- `config_name` is the expert preset (`scholar`, `developer`, `critic`,
  `curator`, `interactive`). Defaults to `default`.
- `project_id` scopes the job to a project; jobs you create without one
  fall into your default project (if you have one) or float standalone.
- `priority` is 0-10; defaults to 5. Higher = preempts lower at dispatch.
- The full schema lives in `JobCreate` (`orchestrator/main.py`) and is
  reflected in Swagger.

### List / inspect jobs

```
GET  /api/jobs?limit=20
GET  /api/jobs/{id}
GET  /api/jobs/{id}/progress
```

### Approve a paused job

Useful for `autonomy=review` automations — the job pauses for human
approval after completing. From an external tool:

```
POST /api/jobs/{id}/approve
```

### Cancel / pause / resume

```
POST /api/jobs/{id}/cancel
POST /api/jobs/{id}/pause
POST /api/jobs/{id}/resume
```

### Automations API (drive the same surface as the cockpit)

You can create / list / manage automations from external tools too —
useful for scripted onboarding or backup/restore:

```
POST /api/automations
GET  /api/automations
GET  /api/automations/{id}
PATCH /api/automations/{id}
DELETE /api/automations/{id}
POST /api/automations/{id}/run-now
POST /api/automations/{id}/pause
POST /api/automations/{id}/resume
GET  /api/automations/{id}/runs
```

Body shape mirrors `AutomationCreate` (see Swagger).

## 4. Recipes

### n8n — "When new email arrives, kick off a scholar job"

Two-node flow:

1. **IMAP Trigger** node (n8n built-in) — fires on new email.
2. **HTTP Request** node:
   - Method: `POST`
   - URL: `https://api.<your-host>/api/jobs`
   - Header: `Authorization: Bearer ak_<your-token>`
   - Body (JSON):
     ```json
     {
       "description": "Read this inbound email and reply: {{$json.subject}}\n\n{{$json.text}}",
       "config_name": "scholar",
       "config_override": {"autonomy": "review"}
     }
     ```

Optional follow-up node: poll `GET /api/jobs/{id}` until `status` is
`completed` or `pending_review` and surface the result.

### Zapier — Slack DM → builder

Trigger: **Slack — New Direct Message**. Action: **Webhooks by Zapier —
POST** to `/api/jobs` with the same body shape as above.

### curl — daily backlog summary

```bash
#!/usr/bin/env bash
set -euo pipefail
TOKEN="${ORCHESTRATOR_TOKEN}"  # ak_…
HOST="https://api.<your-host>"

JOB_ID=$(curl -sS -X POST "$HOST/api/jobs" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "description": "Summarize all jobs that completed in the last 24h with their goals and final status.",
      "config_name": "scholar",
      "config_override": {"autonomy": "full"}
    }' | jq -r '.id')

echo "Submitted job: $JOB_ID"
```

### Python — minimal client

```python
import os
import httpx

BASE = "https://api.<your-host>/api"
TOKEN = os.environ["ORCHESTRATOR_TOKEN"]  # ak_…
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def create_job(description: str, *, config_name: str = "default",
               autonomy: str = "review", project_id: str | None = None) -> dict:
    body = {
        "description": description,
        "config_name": config_name,
        "config_override": {"autonomy": autonomy},
    }
    if project_id:
        body["project_id"] = project_id
    r = httpx.post(f"{BASE}/jobs", json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

print(create_job("Draft 3 Instagram posts about physiotherapy",
                 config_name="scholar"))
```

## 5. Rate limits and quotas

v0 ships with the same per-orchestrator capacity as cockpit traffic —
no separate rate-limit layer for PATs. The dispatcher backpressures
naturally when agent capacity is exhausted (jobs queue in `created`
status until an agent picks them up).

Per-user limits are tracked under M1.D in the multi-tenancy roadmap
([`docs/features/orchestrator_multi_tenancy.md`](./orchestrator_multi_tenancy.md))
— rate limit + pod quotas + egress controls land before the hosted
product moves out of demo.

## 6. Webhooks (planned, not yet shipped)

v0 has **no outbound webhooks**. If your automation needs "fire HTTP
when this job completes," today you poll `GET /api/jobs/{id}` from your
external tool. Event triggers ship in automations v0.5; outbound
webhook delivery is on the v1 backlog.

## 7. Reporting issues

If a documented endpoint behaves differently than this guide describes
(or Swagger is wrong), open an issue at
<https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/issues>.
For security issues with PATs (lost token, suspected compromise),
revoke the token immediately in the cockpit, then file a private report.
