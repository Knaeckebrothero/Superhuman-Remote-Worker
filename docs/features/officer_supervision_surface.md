---
tags:
  - feature
  - architecture
  - officers
  - tooling
  - observability
  - security
status: proposed
created: 2026-08-14
aliases:
  - officer eyes and ears
  - officer supervision tools
  - bounded job evidence
related:
  - "[[centurion]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_message_routing]]"
  - "[[unified_orchestrator_tool_surface]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[officer_backlog_pools]]"
  - "[[supervisor_control_plane_and_live_talk]]"
---

# Officer supervision surface — trustworthy eyes and ears without a workspace

> The officer needs enough truth to supervise jobs, not an MCP-sized operator console and
> not a back door into their filesystems. Push him a compact server-computed SITREP; let him
> pull scoped status, audit, messages, logs, todos, and liveness when he investigates; and
> expose only a bounded, immutable evidence manifest for disposition. Every read states
> where it came from, when it was observed, and why it may be incomplete. Arbitrary files,
> repositories, shells, and cloud folders remain object-plane tools and stay denied by
> [[officer_knowledge_plane]].

## Status

**E1–E5 IMPLEMENTED 2026-08-14** on develop: truthful-read envelope + plane metadata +
officer lane/scoping + liveness contract + evidence manifest and tools (`4d501a8f`),
centurion grant/persona/app-guide/cockpit activation (`7bb1d331`). Officer caller
defaults as shipped: 28 tools (7 control, 18 observability, 3 evidence, zero
workspace-plane). Liveness gained a sixth state `terminal`; evidence reads also default
to MCP/session lanes (schema parity, differing defaults per §7). The E5 live-fire
acceptance (an officer dispositioning a real job) rides O6's Resavio release.
Original status for the record: **PROPOSED (2026-08-14).** This refines, rather than replaces, the ratified shared
job-management implementation in [[unified_orchestrator_tool_surface]]. Its shared client,
descriptors, formatters, adapters, and project scope are still the correct substrate. This
feature changes the officer's default grant from "all job inspection" to a deliberate
observability/evidence subset and adds truthfulness requirements to the reads.

Code-audit markers:

- **[A-read]** `src/shared/orch_surface/{client,formatters}.py` and the current job
  inspection routes in `orchestrator/main.py`.
- **[A-live]** `orchestrator/database/postgres.py::get_job_progress` and
  `orchestrator/services/sitrep.py::build_wake_message`.
- **[A-scope]** `src/shared/orch_surface/client.py::set_scope_headers` and
  `orchestrator/security/auth.py`'s `X-MCP-Scope` handling.

## 1. Why the officer is simultaneously blind and over-entitled

The current Centurion config has job writes plus only a few reads, including arbitrary
workspace-file readers. The 2026-08-06 unified surface correctly addressed the first half
by planning MCP/session/officer parity for job tools, but defaulting the entire
`job_inspection` group to the background officer would address blindness by granting an
object plane.

Several nominal reads are also not decision-grade today:

- `PostgresDB.get_job_progress()` always returns `progress_percent=0` and `eta_seconds=None`
  **[A-live]**.
- `format_job_summary()` asks for fields such as `config`, `current_phase`, `elapsed`, and
  `eta` that do not match the current endpoint's `config_name`, `progress_percent`,
  `elapsed_seconds`, and `eta_seconds` shape **[A-read]**.
- `jobs.updated_at` is polluted by unrelated trigger cascades; [[centurion]] already
  rejected it for SITREP liveness and uses audit fingerprints instead **[A-live]**.
- Log, shell, file, todo, and diff routes have different transports and freshness. Gitea
  views may lag an unpushed workspace; live shell state is pod-proxied and unavailable for
  off-mesh VM workspaces. Empty output cannot honestly mean both "nothing happened" and
  "the source was unavailable" **[A-read]**.

The fix is a capability split plus a read contract, not the full 105-tool MCP catalogue.

## 2. Push first, pull on suspicion

The officer keeps two complementary views:

1. **Push — SITREP.** On a wake, the orchestrator computes status transitions, audit-step
   deltas/last-write, pending gates/messages, per-slot capacity, fleet health, and budget.
   Healthy unchanged details stay collapsed. The SITREP is cheap navigation and mechanical
   anomaly detection.
2. **Pull — supervision tools.** The officer expands one suspicious job or thread using
   scoped reads. Pull tools must not force the model to scrape a prose SITREP or remember a
   previous wake.

A combined summary is a triage view, never disposition evidence. `completed` remains a
worker claim. Closing a ticket requires the category checklist plus published evidence or a
tester/recon report.

## 3. Capability taxonomy

The shared descriptor registry gains a machine-readable `plane` (or equivalent generated
subgroup). The existing `job_inspection` label may remain the Cockpit umbrella, but caller
defaults are evaluated at the finer boundary:

### 3.1 `job_control` — bounded orchestrator actions

Background-officer default:

- `create_job`, `pause_job`, `cancel_job`, `steer_job`,
  `resume_job_with_feedback`, `approve_job`;
- `send_message_to_job`, plus the routing tools from [[officer_message_routing]].

`assign_job`, `promote_job`, and `delete_job` remain operator-only. Backlog dispatches use
the one claim/admission funnel in [[officer_backlog_pools]], not a bypassing control tool.

### 3.2 `job_observability` — control-plane truth

Background-officer default:

- `list_jobs`, `get_job`, `get_job_summary`, corrected `get_job_progress`;
- `get_job_log`, `get_frozen_job`;
- current/archive todo reads;
- `list_message_threads`, `get_message_thread`;
- `get_audit_trail`, `search_audit`, `get_chat_history`;
- `get_stuck_jobs`;
- `list_llm_requests`, `get_llm_request` for officer diagnosis.

These reveal execution telemetry and recorded control state, not arbitrary project paths.
Bulk audit/chat exports stay MCP/operator-only.

### 3.3 `job_evidence` — declared, bounded disposition material

Recommended background-officer default, pending the open sizing decision in §10:

- `get_job_completion_report(job_id)`;
- `list_job_evidence(job_id)`;
- `read_job_evidence(job_id, evidence_id, offset=0)`.

Evidence is a manifest, not a filesystem browser. At completion the orchestrator records a
typed, project-scoped entry for each declared deliverable/evidence item:

```text
id · kind · label · media_type · byte_size · sha256
source revision/object version · captured_at · producer · availability
```

The completion contract gains an optional `evidence[]` declaration containing kind, label,
media type, and a job-owned source handle. The server itself creates the completion-report
and deliverable-check entries; for worker-declared entries it resolves the handle inside the
job's allowed repository/artifact stores, pins the exact commit/object version, measures it,
and only then publishes an opaque evidence ID. A raw path from the worker is never copied
into an officer tool call.

Allowed kinds initially: completion report, deliverable-check result, test/axe report,
reproducible screenshot, bounded change summary/diff, deployment probe, and a bounded text
artifact excerpt. A file-backed entry is pinned to a commit/object version and checksum;
`read_job_evidence` resolves the opaque ID server-side. The model cannot provide a new path,
traverse directories, switch revision, or write the source. Binary/screenshots return safe
metadata plus the existing attachment/viewer representation rather than an arbitrary URL.

If a worker did not publish enough evidence, the officer delegates a tester or recon job.
He does not fall back to `get_job_file`, `list_job_files`, repository browsing, or shell.

### 3.4 `job_workspace` — object plane

Not granted to the background officer:

- arbitrary `get_job_file` / `list_job_files`;
- `get_workspace_overview`, `get_shell_state`;
- unbounded/current-revision diff and commit browsing;
- project repository checkout, file tools, browser, shell, git, cloud mounts.

These remain available to interactive sessions and MCP/operator callers according to their
existing grants. A conference may use them because the user is present and that session is
not the background officer runtime.

## 4. The truthful-read envelope

Every supervision/evidence handler returns the same structured envelope before formatting:

```jsonc
{
  "scope": {"project_id": "…", "job_id": "…"},
  "observed_at": "2026-08-14T12:00:00Z",
  "sources": [
    {"name": "control_db", "as_of": "…", "status": "fresh"},
    {"name": "audit_db", "as_of": "…", "status": "degraded",
     "reason": "timeout"}
  ],
  "data": {}
}
```

Formatters preserve that honesty in compact text. Required distinctions:

- `empty`: source was reached and no rows/items exist;
- `unavailable`: source could not be reached or transport is unsupported;
- `stale`: source was reached, but its last known revision/observation is older than the
  declared freshness window;
- `partial`: one section failed while the others remain usable.

No handler manufactures progress or converts missing telemetry into zero. The summary
schema mismatch and progress stub are fixed before either tool is default-on for officers.

## 5. One liveness contract

Introduce one server-side `compute_job_liveness()` (name illustrative) consumed by:

- SITREP active-job fingerprints;
- `get_stuck_jobs`;
- `get_job_progress`;
- [[officer_backlog_pools]] stale-claim age and executor-disposition warnings.

Inputs, in descending authority:

1. terminal/control-plane status and explicit pause/wait/freeze reason;
2. audit last-write, step count, and phase/checkpoint movement;
3. agent binding/readiness and heartbeat;
4. transport-specific evidence freshness.

`jobs.updated_at` is display metadata, not liveness. Output includes
`active | waiting | paused | suspected_stuck | unavailable`, the observed timestamps, and
the reasons. Threshold policy is configured once so the SITREP cannot call a job healthy
while `get_stuck_jobs` calls it stuck.

## 6. Scope and trust

- Every project-bound shared client sends `X-MCP-Scope: project:<uuid>`; the server remains
  the enforcement point **[A-scope]**. A missing/multiple officer project binding is an
  attach error, not an unscoped fleet view.
- Tool results are untrusted worker-produced content. Rendering delimits evidence and never
  treats embedded text as system instructions or a control action.
- Evidence IDs are authorized by `(caller project, job project, evidence job)` on every
  read; opaque IDs alone convey no access.
- Logs and evidence are bounded, paginated, and secret-redacted by the server. The officer
  cannot request a larger raw dump by prompt.
- Summary/evidence reads never mutate `ready`, claims, approval, ticket closure, or job
  status.

## 7. Relationship to the unified job toolset

[[unified_orchestrator_tool_surface]] remains the implementation funnel:

- one `SurfaceClient` and formatter layer;
- one descriptor/handler per operation;
- MCP and LangChain adapters generated from it;
- canonical MCP spellings and project scope header.

This feature is an amendment to its **officer default policy**, not a forked officer client.
The descriptor needs enough metadata to express `plane` and per-caller defaults. Existing
MCP/session grants and external tool names do not need to change. S5 of that plan cannot be
accepted by merely proving the officer can read arbitrary job files; it must pass this
document's boundary and honesty gates.

The S1/S2 byte-compatibility gate still applies to the mechanical move. E1 then makes an
intentional, separately snapshotted behavior change: it fixes fields that are currently
stubbed/misformatted and adds availability/freshness honesty. Do not hide that product
change inside the adapter migration or claim byte-identical output after E1.

## 8. Build slices

| Slice | Contents | Depends on | Gate |
|---|---|---|---|
| E1 | Truthful-read envelope; repair progress/summary schemas; source availability/freshness format | unified surface S1 | unit/contract fixtures distinguish empty, stale, unavailable, partial |
| E2 | Descriptor `plane` metadata and generated officer grant; project scope on every officer call | unified surface S2–S4 | resolved-tool snapshot; cross-project calls denied server-side |
| E3 | Shared liveness computation; SITREP, progress, stuck, and stale-claim consumers | E1 | one fixture produces the same state/reason on every surface |
| E4 | Completion `evidence[]` contract, manifest/immutable source stamp, and three evidence tools | E1–E2 | path traversal/current-revision/oversize/security tests; screenshot and report live read |
| E5 | Centurion config/prompt and Cockpit inspection labels; live supervision acceptance | E2–E4, [[officer_knowledge_plane]] K2–K3 | k3d officer diagnoses and dispositions a live job without an object-plane tool |

E1–E3 may land before evidence. [[officer_backlog_pools]] B3 must not auto-close anything;
its first live acceptance needs either E4 or the explicit tester/recon fallback.

## 9. Acceptance

1. Officer `list_jobs` sees only his project. A guessed job ID from another project is
   denied even if the user can access both projects elsewhere.
2. A live worker's audit advances: SITREP, `get_job_progress`, and `get_stuck_jobs` agree on
   liveness and observed time. Freeze the audit stream; all three cross the same threshold.
3. Take the audit DB down: status remains available, audit/progress says `unavailable`, and
   no `0%` or false-stuck fact is fabricated.
4. Complete an executor ticket with a screenshot, axe report, deliverable check, and pinned
   change summary. The officer can list/read those entries and apply the close checklist,
   but cannot list arbitrary files or inspect a different revision.
5. Complete a ticket without evidence: the officer schedules tester/recon work; no hidden
   fallback tool lets him browse the workspace.
6. Run the same evidence/read descriptors through MCP and an interactive session: canonical
   schema/output remains shared, while caller defaults differ.
7. Hold a conference: the background officer does not act; the conference retains its
   explicitly selected workspace tools; release yields one coalesced supervision wake.

## 10. Open questions (Legate)

1. **Evidence in the first wave?** Recommendation: yes for completion report,
   deliverable-check result, test/axe report, screenshot, and bounded change summary. It is
   the smallest surface that lets the officer judge output without violating the object
   boundary. Defer arbitrary artifact kinds.
2. **Evidence bounds.** Recommendation for an initial ceiling: 256 KiB paginated text per
   item, five images, and 2,000 diff lines per job; larger material requires a worker-written
   KB report and an external artifact link. Tune from live jobs, not model requests.

## 11. Decision log

- **2026-08-06:** one shared job-management implementation for session, officer, and MCP
  was ratified in [[unified_orchestrator_tool_surface]].
- **2026-08-14 (design):** preserve the shared implementation but split officer access by
  control, observability, bounded evidence, and object plane. The background officer gets
  the first three only; exact evidence bounds remain for Legate sign-off.
- **2026-08-14 (design):** summary is navigation, not evidence; every read carries source,
  observed time, and availability; one audit/checkpoint-based liveness contract replaces
  `updated_at`/stub-progress interpretations.
