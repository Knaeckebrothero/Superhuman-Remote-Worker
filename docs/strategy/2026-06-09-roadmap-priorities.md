---
tags:
  - strategy
  - roadmap
  - prioritization
  - product
  - planning
related:
  - "[[2026-06-04-strategy-funding-and-next-steps]]"
  - "[[product_capabilities]]"
  - "[[agent_lifecycle]]"
  - "[[persistent_session_swallowed_sends_and_truncated_history]]"
  - "[[workspace_warm_pool_and_async_sessions]]"
  - "[[orchestrator_main_py_monolith]]"
---

# Roadmap Priorities — Consolidation Pass v0

**Date:** 2026-06-09  
**Status:** Working roadmap, intentionally opinionated.  
**Purpose:** Turn the idea pile into a selection mechanism. This is not a complete
backlog and should not try to preserve every idea as an active task.

## Context

The project has crossed the line from hobby/R&D system into product candidate.
The current risk is no longer lack of ideas or lack of capability. The risk is
that too many valid directions compete for the same implementation time.

The system already contains several possible products:

- a remote autonomous worker platform
- an interactive cloud coding/agent session system
- a knowledge-base construction and maintenance tool
- a multi-agent research/build/review pipeline
- a self-hosted/on-prem AI operations stack
- a future open-core agent runtime

Trying to advance all of these at once makes every week feel productive while
reducing the chance that any single path becomes trustworthy enough to sell.

## Strategic Bet

For the next cycle, treat the sellable wedge as:

> **Single-tenant autonomous knowledge-work infrastructure: connect existing
> data and documents, let agents process them in isolated workspaces, and give
> the user a reviewable output with an audit trail.**

This framing is narrower than "general remote worker platform" and easier to
demo than "self-improving agent swarm." It also lines up with the strongest
implemented assets: orchestrator, isolated workspaces, jobs/sessions, review
loop, datasources, audit trail, and Cockpit.

## Decision Rules

Use these rules to accept or reject work for the active roadmap.

1. **Pilot proximity:** Does this help a real pilot install, demo, trust, or pay?
2. **Trust:** Does this reduce the chance that sessions, jobs, workspaces, or
   review state look flaky to a user?
3. **Deployment friction:** Does this make the supported install path easier to
   run or diagnose?
4. **Product clarity:** Does this make the knowledge-work wedge easier to
   understand, sell, or repeat?
5. **Cost of delay:** Would postponing this block active use, or is it mostly
   architectural neatness?

If an item does not score well on at least one of the first four, it goes to the
parking lot by default.

## Active Constraints

- Solo-developer time is the bottleneck. Prefer boring, confidence-building work
  over new capability.
- Do not add a new major surface until one complete happy path is reliable.
- Use the local k3d/Tilt path for verification when a change touches the real
  product workflow.
- Do not start a broad monolith rewrite. Extract only around active work.
- Do not let "one more prerequisite feature" block the roadmap process again.

## Now: Next Two Weeks

Maximum five themes. These are the only things that should be allowed to pull
substantial attention.

### 1. Define And Prove One Pilot Demo Path

Create a short runbook for the exact demo that matters:

1. install/start the supported single-tenant stack
2. log into Cockpit
3. create or open a project
4. attach representative documents/data
5. start a session or job
6. watch the agent work
7. review output and audit trail
8. approve or continue with feedback

Acceptance:

- One documented path exists, with exact commands, URLs, test credentials, and
  expected states.
- The path has been run end-to-end locally or on the target pilot environment.
- Any failure in that path becomes a `Now` bug or a deliberately accepted caveat.

### 2. Stabilize Persistent Session Trust

Persistent sessions are the product's most visible trust surface. Fix the class
of issues where the UI says it is connected but messages, control commands, or
latest history are lost.

Seed issues/docs:

- `persistent_session_swallowed_sends_and_truncated_history`
- `persistent_chat_silent_disconnect`
- `persistent_session_midturn_message_loss`
- `persistent_session_restored_messages_no_ids`
- `persistent_session_empty_chunk_history_corruption`

Acceptance:

- Reconnect loads the latest usable turn history, not only the oldest rows.
- Failed sends are surfaced and recoverable; they are not silently lost.
- Control WebSocket liveness is monitored or revalidated on tab focus/network
  resume.
- Long tool-heavy sessions have a regression test or repeatable manual smoke.

### 3. Stabilize Workspace And Session Lifecycle

Workspaces and agent pods are the execution environment. If they get stuck,
leak, or fail opaquely, everything above them looks unreliable.

Seed issues/docs:

- `workspace_warm_pool_and_async_sessions`
- `workspace_reaper_lifecycle`
- `stuck_thread_workspace_pods`
- `persistent_thread_lifecycle`
- `session_router_ingress_host_helper`
- `agent_app_readiness_drift`

Acceptance:

- A failed workspace/session provision attempt reaches a clear terminal or
  retryable state.
- Stale workspace/session pods are detected and reaped.
- Missing required runtime secrets such as session JWT config fail with a clear
  operator-facing error, not a generic 500.
- Session start latency is measured before deciding whether warm pools are
  necessary for the pilot path.

### 4. Make One Deployment Path The Supported Path

The repo supports native dev, Compose, local k3d/Tilt, production Helm, and VM
clusters. That breadth is useful internally but confusing externally.

For pilots, pick one supported path and make everything else secondary.

Recommended near-term stance:

- **Supported pilot path:** single-tenant Helm install with values overlay.
- **Developer path:** local k3d/Tilt.
- **Fallback/dev convenience:** Compose, documented as secondary.

Seed issues/docs:

- `helm_fresh_deploy_issues`
- `helm_deployment`
- `deprecate_docker_compose_stack`
- `deployment_separation_of_concerns`
- `tilt_inner_loop_dev`

Acceptance:

- The README clearly names the supported pilot path.
- One install/smoke command sequence is current.
- Stale deployment scripts or docs are either fixed, removed, or labeled legacy.
- CI verifies the chart scenario that matches the supported path.

### 5. Package The Knowledge-Base Wedge

The platform needs a concrete use case that a pilot can understand without
learning the whole agent architecture.

Build a repeatable "knowledge base construction and maintenance" workflow:

- input: documents, repo, database, or WebDAV/cloud folder
- agent task template: extract, structure, cite, summarize, flag uncertainty
- output: reviewable report plus structured artifacts
- evidence: audit trail and source/citation links

Seed docs:

- `docs/deliverables/product_capabilities.md`
- `datasource_redesign`
- `project_knowledge_base`
- `credential_file_datasources`
- `webdav_datasource_tools`
- `repo_datasource`

Acceptance:

- One job template exists for this workflow.
- One demo dataset/source set exists.
- The output is understandable to a non-developer buyer.
- The review loop demonstrates why this is safer than a one-shot chatbot.

## Next: Six To Eight Weeks

These become active only after the `Now` path is demonstrably less fragile.

### Operational Visibility And Soft Cost Awareness

Implement the read-side usage/ops layer before billing or hard quotas.

Seed doc: `observability_and_quotas`

Target:

- basic per-job/session cost and resource visibility
- orchestrator/agent health metrics
- operator dashboards for stuck jobs, stuck sessions, workspace age, dispatch
  health, and LLM error rates

Do not implement wallet, billing, or hard quota enforcement yet.

### Orchestrator Decomposition By Active Work

`orchestrator/main.py` is too large, but a broad rewrite is not the next move.
Extract around work already being done.

Seed doc: `orchestrator_main_py_monolith`

Target sequence:

- move small routers first only when touched
- extract session routing/lifecycle code while stabilizing sessions
- extract dispatch/provisioning code while stabilizing lifecycle
- leave unrelated endpoints alone

Acceptance is not "main.py under 1000 lines" in this cycle. Acceptance is fewer
high-risk edits landing directly in the monolith.

### Config And Model Reliability

The config system is powerful and high-risk. Make it observable and testable
before adding more knobs.

Seed docs/issues:

- `config_matrix_db_overrides`
- `settings_default_resolution`
- `hardcoded_model_lists`
- `db_backed_model_catalog`
- `custom_llm_endpoints`
- `model_issues`

Target:

- one clear source of truth for model/provider defaults
- tests for resume/config layering
- operator-visible resolved config for a session/job

### Open-Core / OSS Split Decision

The agent/orchestrator boundary appears clean enough to support an open-source
agent split, but this is a strategic move, not a prerequisite for pilots.

Seed doc: `agent_open_source_split`

Defer implementation until:

- the pilot path is stable
- the license posture is decided
- the minimal reference orchestrator story is clear

## Later / Parking Lot

These are not bad ideas. They are not the active bottleneck.

- public multi-tenant SaaS
- billing wallet and hard quota enforcement
- full orchestrator monolith rewrite
- high availability / active-active orchestrator
- broad memory architecture overhaul
- large visual redesigns
- complete OSS launch package
- every datasource connector at once
- advanced automations beyond pilot support
- broad subagent pipeline expansion
- VM snapshot/IDE polish beyond the demo path
- perfect i18n/style-system cleanup

Parking lot rule: new ideas go here unless they directly support a `Now` item.

## Triage Table

Use this format when converting notes into actionable issues.

| Item | Bucket | Pilot impact | Trust impact | Effort | Decision |
|---|---|---:|---:|---:|---|
| Persistent latest-history reload | Session trust | High | High | M | Now |
| Control WS liveness / failed-send recovery | Session trust | High | High | M | Now |
| Workspace/session reaper clarity | Lifecycle | High | High | M | Now |
| Single Helm pilot runbook | Deployment | High | Medium | S | Now |
| KB construction demo template | Product wedge | High | Medium | M | Now |
| Full `main.py` router split | Architecture | Low | Medium | XL | Next/Later |
| Usage ledger foundation | Ops/cost | Medium | Medium | L | Next |
| OSS reference orchestrator | OSS | Low | Low | L | Later |

## Weekly Operating Rule

At the start of each week:

1. Pick at most three concrete tasks from `Now`.
2. Define the verification command or manual smoke for each.
3. Finish or explicitly cut them before pulling in new work.
4. Move any new idea to the parking lot unless it fixes a current `Now` task.

At the end of each week:

1. Update this document with what moved.
2. Delete or demote tasks that no longer matter.
3. Record the remaining pilot blockers in plain language.

The roadmap is allowed to be wrong. It is not allowed to become another
unprioritized note pile.
