---
tags:
  - issue
  - auxiliary-llm
  - memory
  - minimax
  - structured-output
  - observability
related:
  - "[[surface_silent_aux_failures]]"
  - "[[overnight_minimax_m3_scholar_batch_2026-08-03]]"
  - "[[agent_memory_overhaul]]"
aliases:
  - MiniMax auxiliary health flapping
  - auxiliary structured output validation failures
---

# MiniMax-M3 auxiliary structured-output tasks repeatedly fail and recover

**Filed:** 2026-08-04 from the five-job main-cluster overnight Scholar batch.

**Status:** **OPEN. P2 quality/overhead and model-capability mismatch.** The
non-fatal contract worked and all jobs completed, but memory extraction and
assembly repeatedly disappeared during the runs.

## Live evidence

Every job used MiniMax-M3 for the configured LLM tiers. Archived logs recorded:

| Job | Memory-extraction validation failures | Memory-assembly validation failures | Degraded → recovered cycles |
|---|---:|---:|---:|
| control | 16 | 3 | 3 |
| 10-turn readers | 17 | 5 | 3 |
| 24-turn readers | 16 | 6 | 2 |
| paper review | 17 | 3 | 1 |
| web comparison | 9 | 2 | 1 |
| **Total** | **75** | **19** | **10** |

The recurring error was:

```text
Structured-output validation failed for ExtractMemoriesTask
after raw fallback recovery
```

After three consecutive auxiliary failures, the existing health tracker emitted
`AUXILIARY MODEL DEGRADED`; a later success emitted
`AUXILIARY MODEL RECOVERED`. This happened ten times. The observability and
swallow-and-continue behavior designed in `surface_silent_aux_failures.md`
therefore worked as intended.

What did not work reliably was the auxiliary product. During degraded periods,
memory extraction, memory assembly, knowledge curation, and titles are disabled
for the process. The main model kept running, so terminal job success does not
show how much memory context was missing.

## Interpretation boundary

This batch deliberately spent expiring MiniMax-M3 capacity and pushed that model
through the job configuration. The evidence therefore does **not** prove that
all auxiliary models or transports are unhealthy. It proves that the currently
accepted MiniMax-M3 route cannot reliably satisfy these structured-output tasks,
even after the raw fallback repair path, under realistic concurrent load.

It also reveals an MCP telemetry gap. The connector's `list_llm_requests`
formatter omits `call_type`, `status`, and `error`, and exposes no filter for the
REST endpoint's auxiliary/error fields. The 58.77M raw-token total derived from
that MCP listing should therefore be treated as recorded main/light-reader usage
and a **lower bound**, not complete job-wide LLM consumption.

## Consequences

- Memory/knowledge continuity changes within one job as the health latch flaps.
- Repeated failed structured-output calls spend tokens without producing state.
- A report can be correct while the memory system is substantially degraded,
  hiding the reliability problem in terminal success rates.
- Operators can find the events in archived pod logs/agent health, but the MCP
  request-accounting view cannot isolate or total them.

## Fix direction

1. Declare structured-output capability in model/catalog metadata and resolve
   auxiliary tasks to a compatible model by default, independently of the main
   prose/research model.
2. Preserve an explicit per-job auxiliary override for experiments, but reject
   or loudly warn about a model-route combination that has failed a startup
   structured-output probe.
3. Keep the schema validator fail-closed; improve the repair prompt/parser only
   where it can do so without inventing memory records.
4. Add a bounded failure policy per task so health does not oscillate on every
   isolated success; expose time degraded and lost task counts.
5. Complete the MCP read surface: pass `call_type`/`status` filters through,
   render those fields, and include auxiliary consumption in job-wide totals.

## Acceptance criteria

- The selected default auxiliary model passes repeated extraction and assembly
  schemas under concurrent-job load.
- A deliberately incompatible model produces a preflight warning/rejection or a
  stable, operator-visible degraded state rather than ten silent functional
  gaps.
- Failed auxiliary attempts and their tokens are queryable through MCP by job,
  call type, and status.
- Main+reader+auxiliary usage totals reconcile to the archived request rows.
- Non-fatal auxiliary failure still cannot fail or discard an otherwise-valid
  job deliverable.
