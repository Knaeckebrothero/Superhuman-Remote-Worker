# Research corpus for `docs/features/stateless_agents.md`

Raw subagent reports from the 2026-08-07 night run that produced the doc's v2
(research fan-out) and v3 (adversarial panel). The doc folds in everything
load-bearing; these files carry the full evidence trails — complete state
inventories with file:line, source URL lists, per-finding evidence and fix
sketches — for whoever implements S1/S2/S3.

Provenance: two Workflow runs in session `41820805` — research
`wf_b2438ff5-cc0` (8 agents, ~1.69M tokens), critics `wf_d66ce6e9-5c4`
(6 agents, ~0.97M tokens). Line numbers reference the working tree as of
2026-08-07 and will drift.

## Research fan-out (v1 → v2)

| File | Mission |
|---|---|
| `code_session-residue.md` | Complete inventory of in-process session-agent state (module globals, PersistentSession fields, ToolContext, every `create_task` site, queued-input path, journal epoch/seq mechanics, attach/rebuild map) with per-item stateless classification |
| `code_worker-batch-seams.md` | How the worker graph is actually driven; freeze machinery end-to-end; the two resume lanes (Lane A/B); minimal-diff `batch_boundary` design; shipped job lease 0054; dispatch/fairness traps; worker state not covered by the checkpoint |
| `code_control-plane-audit.md` | Deletion ledger for the agent-lifecycle control plane; existing lock/claim primitives (`datasource_reconciliation.py` template); credential injection at dispatch; session pod k8s lifecycle |
| `code_load-cost-journal.md` | Per-turn cold-load cost decomposition with measured numbers; the epoch-per-attach client cascade; seq allocation; journal-only streaming stress test; P5/P6 convergence |
| `web_langgraph-serverless.md` | LangGraph Platform architecture (the vendor's version of this design); AsyncPostgresSaver production guidance; astream-and-break semantics; CVE-2025-64439; compile-once thread-safety |
| `web_prior-art.md` | 2024–2026 survey: OpenAI Assistants→Responses, OpenHands, Cloudflare Durable Objects steelman, Temporal/Restate/Inngest replay contracts, Vercel, Modal/E2B, Cognition, Manus KV-cache rules |
| `web_pg-queue-lease.md` | SKIP LOCKED queue state of the art (River/Graphile/Solid Queue/pgmq/Oban/pg-boss); LISTEN/NOTIFY pitfalls; Kleppmann fencing applied here; recommended lease schema + SQL; KEDA |
| `web_streaming-multitenancy-cache.md` | Journal streaming latency math and write-rate analysis; Python multiplexing sizing (contextvars, shared clients); provider prompt-cache semantics per vendor |

## Adversarial panel (v2 → v3)

| File | Lens |
|---|---|
| `critic_races.md` | Concurrency correctness: completion half missing, dual lease authority, epoch/rewind races, fence implementability, reaper head-of-line blocking, poison-unit loop |
| `critic_migration-ops.md` | Coexistence/rollout: runner_kind partition, lite-PVC discovery, control-verb transport gap, freeze-registry skew (loop phantom-complete), chart/KEDA/Tilt reality |
| `critic_perf-cost.md` | Number recomputation: TTFT budget, journal commit-rate regime, Path-A re-summarization loop, worker overhead at small batches, Erlang sizing, takeover bound |
| `critic_security.md` | Tenancy inversion: pull-claim credential trust, scrub-on-claim (verified env residue), fencing ≠ isolation, revocation bounds, session-JWT gating |
| `critic_product-ux.md` | Cockpit seat: S1 client workstream, steal UX contract, capacity/queueing UX, mid-turn mode semantics, gate-resume flow, media-degradation notice |
| `critic_completeness.md` | System-boundary sweep: queue admission/producers, metering attribution, job-log archive trigger, VM-mesh capability, admin/fleet surface, acceptance-criteria gaps |
