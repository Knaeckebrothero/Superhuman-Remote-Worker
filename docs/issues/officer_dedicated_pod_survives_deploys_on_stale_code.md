---
tags:
  - issue
  - officers
  - deployment
  - agents
status: open
priority: P1
created: 2026-08-15
aliases:
  - LF-2
  - stale officer pod after deploy
related:
  - "[[officer_backlog_pools_resavio_livefire]]"
  - "[[officer_post]]"
  - "[[officer_backlog_pools]]"
---

# A deploy leaves a live officer on pre-deploy agent code indefinitely

**Status:** OPEN. Found live during the Resavio O6 release (2026-08-15).

## Observed

The dedicated officer pod `persistent-d67ee261-334` was 15 days old when
the backlog-pools deploy rolled at 15:52. The rollout recycled every
deployment-managed pod (pool agents, stateless agents, orchestrator); the
officer's pod is watchdog/provisioner-created and survives untouched — on
the pre-deploy agent image.

One turn later, both halves of the skew bit at once:

- **Pre-B5 persona**: never taught the `category:` machine-tag grammar.
  He filed honest backlog tickets tagged bare `tester`/`researcher`/
  `executor`; `classify_ticket` reads only `category:`-prefixed tags, so
  his ready ticket was invisible to the tick and the card reported
  `ready_depth: 0` against it.
- **Pre-B2 kb tools**: cannot stamp `ready_at`. His `ready` tag persisted
  with `ready_at` NULL — which fails closed exactly as designed, so even
  correctly-tagged tickets from a stale pod are never dispatchable.

The officer and the machinery disagree about the state of the backlog and
both are honest from their own code's point of view. Nothing errors,
nothing pages; it presents as "the feature doesn't work".

## Why this is structural, not incidental

Any feature that changes the agent-side half of an officer contract
(persona, kb tools, sleep tool, evidence tools) silently does not exist
for every commissioned officer until his pod happens to die. Officers are
designed to be long-lived — the pod ages precisely because the watchdog
keeps it healthy. The longer the system works as designed, the staler the
officer gets.

Same family as the known "long-lived pods keep pre-fix boot images until
recycled", but worse for officers: pool agents recycle on every deploy,
dedicated officer pods never do.

## Direction (pick one, or layer)

- **Deploy-time recycle**: the orchestrator, on boot after a version
  change, drain-recycles dedicated officer pods the way it already
  handles pool agents (respawn is a designed, continuity-safe path — the
  Resavio live-fire exercised it deliberately).
- **Image-skew detection**: the officer watchdog compares the pod's image
  to the current agent image and schedules a respawn outside quiet hours
  when they diverge.
- At minimum: surface the skew on the officer card ("running image N
  deploys behind") so a Legate can see why an officer misbehaves.

## Acceptance

- After a deploy that changes the agent image, every commissioned
  officer is running the new image within a bounded, observable window,
  with his durable state (charter, KB, RecallStore, timers) intact.
- The recycle respects an active hold and does not fire mid-turn.
