# A session's cloud folder is invisible until its owner signs into the cloud once

**Status**: OPEN — root-caused, deliberately not built. Deferred 2026-08-05.
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

The owner of this deployment has now signed in, so every existing and future
thread of theirs shares correctly. The gap only bites a *second* user's first
session — and by the time there is one, onboarding will likely cover the cloud
sign-in anyway. Revisit when the first non-owner account is provisioned.
