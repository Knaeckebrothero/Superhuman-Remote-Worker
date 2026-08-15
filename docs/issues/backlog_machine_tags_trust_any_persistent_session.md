---
tags:
  - issue
  - officers
  - backlog
  - knowledge
  - authorization
  - security
status: resolved
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

**Status:** **RESOLVED on `develop`** (2026-08-15), pending deployment. Audit finding
**BP-09**.

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

## Resolution

BP-09 consumes the same server-derived `RuntimeActorContext` and authorization service as
OC-02. The context is attached only to the exact writable native knowledge binding, and the
orchestrator revalidates its project, current human role, thread, and officer incarnation
before either a machine-tag or charter mutation. A thread ID or Centurion configuration flag
has no authority by itself.

The policy awaiting explicit Legate confirmation is centralized as the single named constant
`SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY`: project `owner` and global `admin` are allowed;
`viewer` and `editor` are denied. This implements the specified safe default without silently
choosing editor authority. Conference actors pass through this same authenticated-human role
matrix. A future Legate decision is one constant edit plus its matrix tests, not an audit of
multiple write paths.

Sensitive requests are authorized before any graph, vector, `ready_at`, tag, or canonical-file
mutation. A denial returns a structured authorization code/actor from the server and an
explicit tool result ending in “No changes were made.” Worker-side tag stripping remains as
defense in depth, but the sensitive request itself now fails atomically.

| Acceptance gate | Automated evidence |
|---|---|
| Full caller matrix for `ready`, removal, re-ready, and `parallel-safe` | `TestOfficerOnlyTags::test_sensitive_tag_human_role_matrix` |
| Full caller matrix for charter creation/writes | `TestCharterWriteAuthority::test_charter_human_role_matrix` |
| Officer for another project or an old incarnation is denied | shared server tests `test_project_a_credential_cannot_act_on_project_b` and `test_recommission_invalidates_old_incarnation_immediately` |
| Denied tag write changes no tags/`ready_at`, graph, vector row, or canonical file | `test_denied_machine_tag_write_has_zero_projection_or_file_side_effects` |
| Denied charter write changes no graph, vector row, or canonical file | `test_denied_charter_update_has_zero_side_effects` |
| A graph type-read failure cannot bypass charter authorization | `test_graph_type_read_failure_refuses_before_any_update` |
| Denials are explicit and actor-audited | runtime-actor authorization denial tests plus both zero-side-effect tool tests |

## Dependencies

The trusted actor context can share infrastructure with
[[officer_message_actions_trust_shared_transport_identity]]. Close before exposing
[[officer_post_cannot_enable_auto_pull]].
