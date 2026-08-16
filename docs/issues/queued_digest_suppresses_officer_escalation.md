---
tags:
  - issue
  - officers
  - notifications
  - autonomy
status: open
priority: P1
created: 2026-08-16
aliases:
  - the digest is already queued
  - undelivered digest blocks the page
related:
  - "[[officer_conference_live_fire_findings]]"
  - "[[officer_internal_messages_consume_human_rate_limits]]"
  - "[[deliverable_contract_satisfied_by_a_note_about_failure]]"
---

# A queued-but-undelivered digest becomes the officer's reason not to page

**Status:** OPEN. Observed across ~14 consecutive wake cycles, Better Resavio,
2026-08-15 21:13 → 2026-08-16 05:46, and continuing.

The delivery gap itself is already filed as **F4** in
[[officer_conference_live_fire_findings]] ("digest urgency appends to a metadata
ring surfaced only on the cockpit officer card — no email; digest email delivery is
a known open item"). This issue is the **behavioural consequence**, which F4 does
not cover: the undelivered digest actively prevents the escalation that would have
reached the Legate.

## Observed

The officer filed two `notify_user` digests (turns 13 and 17) correctly describing a
hard blocker it could not clear:

> Unblock requires a build workspace provisioned with the KurortEngine checkout and
> a repository write/publish path.

Neither was delivered. The bodies exist in `threads.metadata` and
`thread_events.payload`; `thread_notifications` contains **420 rows for this thread,
all of kind `permission_pending`, and zero of any other kind**.

From then on, every wake cycle filed the same sleep reason:

> No dispatchable ready work: … build remains de-armed pending a provisioned
> KurortEngine checkout with commit/push/PR capability; test has no reachable
> candidate; **the Legate's digest is already queued.**

That last clause is the defect. The officer treats "I have queued a digest" as
"the Legate has been informed", and correctly concludes that paging again would be
spending a scarce resource on a message already sent. It is reasoning correctly
from a false premise the system gave it.

The post also runs `"conference": false`, so nothing drains the ring.

## The inversion

What the Legate needed — *I am blocked, only you can clear it* — was swallowed.
What the Legate got, in the same window, was **11 `permission_pending` emails**
(409 more suppressed by rate limiting) for routine tool calls like `kb_list`, on a
session whose `permission_mode` is `autonomous`.

The channel delivered the noise and dropped the signal, and then the dropped signal
convinced the officer to stay quiet.

## Why it matters

This is the failure mode that makes unattended operation untrustworthy in a way the
operator cannot detect. The officer was *right* about everything: right diagnosis,
right severity, right decision to notify. Ten and a half hours of correct behaviour
produced no contact, and the officer's own logs read as though contact was made.

Cost of the silence in this instance: ~$23 in a day of wake cycles against a blocker
a human could have cleared in minutes.

## Direction

- **`notify_user` must not report success for a tier it did not deliver.** The
  return string already distinguishes page/digest/downgrade; it should distinguish
  *queued but undeliverable* from *delivered*.
- **An unread digest must expire into an escalation.** A digest that has been queued
  for N wake cycles with no conference to drain it should promote to a page, or the
  officer should be told it is still undelivered so it can decide.
- **Do not let queue state read as delivery state in the officer's own context.**
  The SITREP is the right place to say "1 digest queued, undelivered, 14 cycles".
- Fixing F4's delivery leg would resolve this too, but the reasoning bug is worth
  closing independently: an officer should never conclude it has reported something
  on the strength of a queue write.

## Acceptance

- An officer blocked on the Legate for more than a few cycles reaches the Legate, or
  knows that it has not.
- `notify_user` never returns a success string for a message that was not delivered.
