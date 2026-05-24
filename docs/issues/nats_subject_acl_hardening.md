---
tags:
  - security
  - nats
  - hardening
  - deferred
status: open
priority: medium
created: 2026-05-22
---

# NATS subject-level ACLs for per-pod publish scope

## Context

The [[direct_session_websockets]] design moves notification-event emission from the orchestrator's WS proxy loop into the agent pod, with the pod publishing to NATS subjects `session.events.{thread_id}` and `session.lifecycle.{thread_id}`. The orchestrator's existing `nats_bridge.py` subscribes and broadcasts to the SSE notification feed.

That design accepts the existing NATS trust model — agent pods authenticate with cluster-wide credentials and can publish to any subject they're configured to use. Defense-in-depth is provided by the bridge subscriber filtering incoming events: each event's payload-level `thread_id` must match the pod's currently-bound thread according to the DB.

This issue tracks the gap: we are not enforcing publisher scope at the NATS transport layer. A compromised or buggy agent pod could publish events claiming to be for a thread it doesn't own.

## Threat model

A pod that mints a `session.events.{other_thread_id}` message with crafted notification content could:

- Cause spurious permission-request notifications to appear in another user's cockpit (denial-of-service / phishing surface).
- Inject `approve` / `deny` events that the orchestrator might misinterpret as resolution of another session's pending prompt, depending on how the resolution-tracking logic is wired.
- Tamper with `session.lifecycle.*` phase signals to confuse other sessions' provisioning state.

The actual blast radius depends on how strictly the bridge enforces the payload-level filter. With strict filtering (pod's known binding from the DB must match the event's claimed `thread_id`), most of the impact is contained. Without strict filtering, the impact reaches every user.

## Why deferred

- The existing NATS subjects used for heartbeats, sudo gate, and VM lifecycle already grant agent pods broad publish permissions; this design adds two new subject families to that existing trust boundary without enlarging it.
- The bridge-side filter catches the most likely accidental and most plausible-malicious cases.
- Implementing per-pod NATS credentials with subject-level ACLs requires generating credentials at pod-provisioning time, rotating them with pod lifecycle, and a NATS server configuration change — not a small piece of work.
- The user-explicit decision (2026-05-22) is to defer this hardening so the lighthouse refactor can ship without scope creep.

## Mitigation options when this is picked up

**Option A — Per-pod NATS user with subject ACL.**

For each agent pod, the orchestrator (or the provisioner) creates a NATS user via NATS's account/auth callout API with publish permissions scoped to `session.events.{thread_id}.*` and `session.lifecycle.{thread_id}.*` only. The credentials are mounted into the pod as a Secret. On pod teardown, the credentials are revoked.

Pros: enforces scope at the transport layer; minimum-privilege per-pod.
Cons: credential lifecycle to manage; coordinated with the agent-pod provisioner.

**Option B — Signed event payloads.**

Each pod's events are HMAC-signed with a per-pod secret known only to that pod and the orchestrator. The bridge verifies signatures before broadcasting. The NATS layer stays unchanged.

Pros: no NATS config change; works with shared NATS credentials.
Cons: still allows a compromised pod to publish junk that fails verification but consumes resources.

**Option C — Stricter bridge-side filter (immediate, no infra change).**

Tighten the existing bridge filter: enforce that the payload `thread_id` matches the pod's bound thread according to the DB. Drop events that fail. Log the drops as a security event.

Pros: no infra change; ships with the lighthouse refactor or as a small follow-up.
Cons: defense-in-depth, not transport-layer enforcement; a pod can still spam un-filtered subjects.

**Recommended sequence:** ship C as part of (or immediately after) the lighthouse refactor, even though it's "defense-in-depth"; then plan A for the next hardening cycle if the threat model warrants it.

## Related

- [[direct_session_websockets]] — the change that introduces these subjects
- [[high_availability_setup]] — the broader NATS / control-plane story
