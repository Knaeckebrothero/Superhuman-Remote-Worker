---
tags:
  - issue
  - officers
  - communication
  - security
  - authentication
status: open
priority: P0
created: 2026-08-15
aliases:
  - OC-02
  - officer route impersonation
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_message_routing]]"
  - "[[officer_post]]"
---

# Officer message actions trust a shared transport key as actor identity

**Status:** OPEN SECURITY BLOCKER. Audit finding **OC-02**.

## Problem

`orchestrator/main.py::_require_officer_route_actor` authenticates the shared internal
transport key, then accepts `officer_thread_id` from the request body and checks whether
that claimed ID currently holds the project post. `X-MCP-Scope` is only defense in depth
when supplied; neither value proves which runtime made the call.

Stateless agent pods receive the same `MCP_INTERNAL_KEY`. That key proves “an internal
client,” not “the commissioned officer incarnation.” A compromised worker that obtains the
thread ID can reply, escalate, or acknowledge a route as the officer.

## Security boundary

- Actor identity must be derived from trusted transport/runtime credentials, never from a
  model-selectable or body field.
- Project scope is not actor identity. A correctly scoped worker is still not the officer.
- The credential must be bound to the post incarnation so decommission/recommission cannot
  transfer old authority.

## Required direction

Mint an officer-runtime credential at attach/respawn, bound at minimum to project ID,
thread ID, incarnation, caller kind, and a short expiry. The job-surface adapter should
send it as hidden context; the route body should no longer carry actor identity. The server
derives the actor, then verifies that the same incarnation still holds the durable post.

An equivalent workload-identity or mutually authenticated design is acceptable. Reusing
the fleet-wide bootstrap secret is not.

## Acceptance

- A stateless worker with the valid shared internal key receives 403 on reply/escalate/ack,
  even when it supplies the correct officer thread ID and project scope.
- The commissioned officer succeeds without placing its thread ID in the public schema.
- A credential for project A cannot act on project B.
- Decommission/recommission invalidates the previous incarnation immediately.
- Expired, missing, duplicated, and malformed actor credentials fail closed and are audited.
- MCP/operator and interactive user routes retain their separately authenticated behavior;
  they do not masquerade as the background officer.

## Dependencies

The server-side “current post” check should use the authoritative post/lock contract in
[[officer_admission_does_not_lock_the_durable_post]], but actor credentialing can land
first.
