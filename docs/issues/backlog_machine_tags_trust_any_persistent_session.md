---
tags:
  - issue
  - officers
  - backlog
  - knowledge
  - authorization
  - security
status: open
priority: P0
created: 2026-08-15
aliases:
  - BP-09
  - persistent-session ready escalation
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_backlog_pools]]"
---

# Backlog machine tags trust any persistent session as officer authority

**Status:** OPEN SECURITY/SPEND BLOCKER. Audit finding **BP-09**.

## Problem

`src/tools/knowledge/knowledge_tools.py::_has_officer_authority` returns true whenever
`ToolContext._thread_id` is present. Thread project attachment accepts any member role,
including `viewer`, while `build_knowledge_bindings()` marks the primary native KB writable
without carrying that role. The charter write gate uses the same “has a thread” boundary.

A viewer can therefore open an ordinary persistent session, stamp `ready` or
`parallel-safe`, rewrite standing orders, and trigger owner-funded work or executor
parallelism. Worker jobs are correctly stripped, but “not a worker” is not equivalent to
“the officer or Legate.”

## Required direction

- Carry server-derived caller kind, project role, project ID, thread ID, and officer
  incarnation in hidden trusted runtime context.
- Authorize machine tags and charter mutations against that context at the write boundary;
  never infer authority from thread presence or a public argument.
- Decide and document the human role matrix. Safe default: commissioned officer and project
  owner/admin may authorize dispatch; viewer is denied; editor remains denied until chosen
  explicitly.
- Treat a conference according to its authenticated human project role, not merely its
  Centurion config name.
- Keep worker stripping as defense in depth.

## Acceptance

Test the full matrix for `ready`, removal/re-ready, `parallel-safe`, and charter writes:

| Caller | Expected default |
|---|---|
| Worker job | denied/stripped |
| Viewer session | denied |
| Editor session | denied pending explicit policy |
| Owner/admin session | allowed |
| Commissioned background officer | allowed for its project |
| Officer for another project/old incarnation | denied |
| Conference | follows authenticated human role |

Additionally, a denied write must not change `ready_at`, tags, the vector projection, or
the canonical file; it must return an explicit authorization result and audit actor.

## Dependencies

The trusted actor context can share infrastructure with
[[officer_message_actions_trust_shared_transport_identity]]. Close before exposing
[[officer_post_cannot_enable_auto_pull]].
