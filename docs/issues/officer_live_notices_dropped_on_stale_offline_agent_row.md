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
observation window.

## Root cause (traced)

`update_agent_heartbeat` makes `offline` sticky on BOTH sides: the UPDATE
uses `CASE WHEN status = 'offline' THEN 'offline' … ELSE <reported> END`,
and the returned `effective_status` mirrors it. Only **registration**
resets an offline row. That is deliberate — an agent the stale-detector
marked offline may have had its jobs orphan-recovered, and a heartbeat
alone must not resurrect its claims.

The assumption that breaks is that every agent re-registers soon after
going offline, because going offline implies the pod died. Pool agents:
true — respawn = re-register. A dedicated officer pod: false — it is
designed to live for weeks, so ONE offline mark (any orchestrator outage
longer than the 3-minute staleness window — e.g. a deploy — marks every
agent offline; the officer's pod survives deploys, see LF-2) leaves it
permanently offline while perfectly healthy. It heartbeats forever into a
row that will never believe it.

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

Do NOT simply un-stick offline on heartbeat — the stickiness protects
against a zombie resurrecting job claims after orphan recovery. The clean
fix uses the channel that already exists: the heartbeat response carries
**intents**. When a heartbeat arrives for an `offline` row, return a
`reregister` intent; the agent re-registers (the path that legitimately
revives a row, re-issuing identity and clearing stale claims), and the
officer is live again within one heartbeat interval instead of never.
Alternatively (or additionally), `_resolve_live_agent` may trust its own
port probe over the row when `last_heartbeat` is fresh — the probe exists
precisely because the row lags.

## Acceptance

- A live, heartbeating officer pod receives hold-release and post-update
  notices (`notified: true`) and live sitrep injection with watermark
  advance.
- A genuinely dead pod still takes the durable branch and the watchdog
  respawn path.
