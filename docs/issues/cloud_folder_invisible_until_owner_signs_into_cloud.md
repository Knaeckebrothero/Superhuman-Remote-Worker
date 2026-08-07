# A session's cloud folder is invisible until its owner signs into the cloud once

**Status**: OPEN (narrowed) — the historical-thread hole is closed by a sweep
(2026-08-06); the user-facing affordance below is still unbuilt. Deferred 2026-08-05.

> **Correction 2026-08-07.** The "Why it's deferred" section below claimed that
> once the owner signs in, "every existing and future thread of theirs shares
> correctly". That is **false for threads that already ended**, and it cost a user
> a finished deliverable. The share retry lives on the *resume* path
> (`needs_share_only`, `main.py:27180`), so it only fires when a thread is actually
> resumed. An **ended** thread never gets one and stays unshared forever, no matter
> how many times its owner signs in. Found on `1930dec9`, whose folder was created
> 08-03 20:53 — ~16.5 h before the owner's identity first resolved (08-04 13:24:53,
> first successful share 13:25:11). Four sibling threads self-healed because they
> were later resumed; that one had not been.
>
> Swept by `scripts/backfill_session_folder_shares.py` (`af1ed9f8`): dry-run by
> default, idempotent, shares through `backend.share_session_folder` so the
> 20-shares/10-min leaky bucket still applies, and skips owners who still have no
> account so they stay eligible for a later run. Run 08-06 — one candidate, now
> share id 6, re-run reports zero. **Re-run this after any cloud cutover**, since a
> cutover recreates folders in exactly the window that orphans them.
>
> Note also that the measurement below ("`main_cloud_share_handle IS NULL` for all
> 217 threads") counts threads on the *previous* OpenCloud backend. Post-cutover
> only five threads carry a Nextcloud session handle at all. See
> `docs/done/session_cloud_folder_unreachable_when_asleep_and_unshared.md`.
**Split out of**: `docs/done/session_resume_cloud_sync_race_late_provision.md`
(everything else in that investigation shipped; this is the one item left, and
it is a product affordance rather than unfinished work on those defects).

## What happens

The orchestrator provisions a per-session cloud folder under the
`agent-service` account and then tries to share it with the thread's owner. The
share silently no-ops for an owner who has never signed into the cloud, so the
folder exists, syncs, and is completely invisible to the person it belongs to.
Nothing tells them why. Every subsequent resume retries the share and fails the
same way, forever.

Measured on dev 2026-08-04, before the owner signed in: `main_cloud_share_handle
IS NULL` for **all 217 threads**.

## Why it can't be fixed server-side

`NextcloudBackend.ensure_user` is a lookup, not a provisioner, and says so:

> Nextcloud's user_oidc app provisions on first login; no admin API.
> We don't forge a local account from the service side because Nextcloud's
> OIDC-linked accounts only materialise via the login flow.

So there is no admin API to create the account ahead of first login. Confirmed
empirically: before sign-in the instance had only `admin` and `agent-service`;
after the owner opened `cloud.srw.works` once, the OIDC account appeared
(`backend: user_oidc`, found by the email search the resolver uses) and the very
next resume logged `Shared session folder … with user '9edad2a0f55b…'` and
persisted the share handle.

Note the asymmetry that makes this easy to misdiagnose: the **create** path
calls `ensure_user`, the **resume** path calls `resolve_user_identity_cached`
(lookup only). Neither can bootstrap the account — only the human can.

## What to build when it matters

Surface the state instead of retrying silently: when a thread has a session
folder but no share handle, tell the owner "your cloud folder isn't shared yet —
sign in to <cloud> once", with a link. That is the whole fix.

## Why it's deferred

The owner of this deployment has now signed in, so every *future* thread of theirs
shares correctly, and the sweep has repaired the past ones. (This paragraph
originally also claimed existing threads were fine — see the correction at the top;
they were not, and ended threads needed the sweep.) The remaining gap only bites a
*second* user's first session — and by the time there is one, onboarding will likely
cover the cloud sign-in anyway. Revisit when the first non-owner account is
provisioned.

What is still unbuilt is the affordance in "What to build when it matters": a user
whose folder is unshared is told nothing. The sweep repairs the state but does not
explain it, and it is operator-run rather than automatic.
