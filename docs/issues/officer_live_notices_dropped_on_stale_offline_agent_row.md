---
tags:
  - issue
  - officers
  - agents
  - liveness
  - heartbeat
status: open
priority: P2
created: 2026-08-15
aliases:
  - LF-1
  - offline row drops live officer notices
related:
  - "[[officer_backlog_pools_resavio_livefire]]"
  - "[[officer_post]]"
---

# Live officer notices are dropped while the agents row reads offline

**Status:** OPEN. Found live during the Resavio O6 release (2026-08-15).

## Observed

Officer agent `ea8dd2ee` (thread `d67ee261`, pod `persistent-d67ee261-334`,
15 days old, Running) heartbeats every 60 s — `last_heartbeat` was 19–25 s
old on every read — yet `agents.status` stayed `offline` for the whole
observation window. The heartbeat handler passes the agent's self-reported
status through `update_agent_heartbeat`, so either the persistent agent
self-reports a status that maps to offline, or an effective-status override
keeps offline sticky until re-registration. Root cause not yet traced.

## Impact

`session_wake._resolve_live_agent` guards `status != 'offline'` **before**
its port probe, so while the row is stale:

- `_inject_officer_notice` fails for every caller — hold-release
  (`notified: false` on the O6 release response), post-update notices from
  officer PATCH, and any other one-liner. All silent, all best-effort by
  design, so nothing pages.
- The officer wake drain routes sitreps through its durable branch
  (`save_thread_message` + finish) instead of live injection. A LIVE
  officer then reads his sitrep only at the next turn trigger — Legate
  input or the ~2 h agent-local backstop — instead of immediately. The
  sitrep watermark deliberately does not advance on durable delivery, so
  fingerprints re-diff until a live delivery lands.

Degraded-but-functional: the durable path carried the whole O6 release.
But the design intent — live inject when the pod is live — is defeated
indefinitely, not for the documented ~4-minute staleness lag.

## Direction

- Trace why a heartbeating persistent agent's row reads offline (agent
  self-report vs orchestrator effective-status vs a stale-marking sweep
  that heartbeats never reverse).
- Either make heartbeat acceptance flip `offline` back to the reported
  status, or make `_resolve_live_agent` trust its own port probe over the
  row for agents with a fresh heartbeat (the probe exists precisely
  because the row lags).

## Acceptance

- A live, heartbeating officer pod receives hold-release and post-update
  notices (`notified: true`) and live sitrep injection with watermark
  advance.
- A genuinely dead pod still takes the durable branch and the watchdog
  respawn path.
