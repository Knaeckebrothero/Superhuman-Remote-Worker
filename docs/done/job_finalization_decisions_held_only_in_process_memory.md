---
tags:
  - issue
  - fix-spec
  - agent
  - jobs
  - durability
---

# The two decisions that can end a job live only in process RAM, with no write-ahead record

**Filed:** 2026-07-27, generalised from the verification audit.
**Status:** **FIXED 2026-08-06 (batch #3) — journal-before-observe implemented
end-to-end and both k3d kill-tests PASSED.**

- **Durable write inside the tool call:** `job_complete` journals the decision
  through the orchestrator (`POST /api/jobs/{id}/completion-decision`,
  internal-key, `_record_completion_decision_impl`) BEFORE returning to the
  model; idempotent on `(job_id, tool_call_id)` (LangChain
  `InjectedToolCallId`; replay short-circuits, a new id overwrites), CAS-
  guarded against terminal rows (`PostgresDB.set_completion_decision`, one
  atomic statement on the jobs row — the later status transition updates the
  same row, so decision and status cannot diverge). A failed journal write
  reports NOT-recorded to the model; nothing local is populated first.
- **Durable-first read at finalize:** the module dicts are demoted to caches.
  `finalize_job` falls back to the checkpointed mirrors
  (`completion_decision` / `verdict_decision` state channels, written by the
  audited tool node in the same super-step as the tool result), and agent
  resume hydrates the cache from `GET .../completion-decision`
  (`seed_final_phase_data`; never on feedback resumes — those void the
  decision, and `queue_job_for_resume` drops the DB record in the same
  statement, opt-out `void_completion_decision=False` for the
  re-provisioning park).
- **`is_final_phase` is implemented, not deleted:** the audited tool node
  sets it (with the mirrors) once a decision is durably journaled, so the
  finalize trigger survives restarts.
- **The placeholder fabrication is dead:** a worker finalizing with NO
  decision anywhere now gets a loud `reject_transition` telling the model to
  re-issue `job_complete`; a no-verdict critic completes with an HONEST
  zero-confidence report (never "Job completed"/1.0) and the orchestrator
  escalates from the ledger as designed.

**Verified:** `tests/test_completion_decision_journal.py` (20) +
real-Postgres `tests/test_queue_job_for_resume.py` extensions (4). Live k3d
kill-tests 2026-08-06: **worker** — job `965b0935` journaled at 22:30:08,
agent pod force-killed the same second (freeze report never delivered, job
stayed `processing`), re-dispatched at 22:31:57, logged `Hydrated completion
decision … (tool_call_id=chatcmpl-tool-8b6e53f38020c2e7)`, froze with the
ORIGINAL summary field-for-field and reached `reviewing`; **critic** —
verdict round journaled on the target ledger at 22:37:02, critic pod killed
+1s, re-dispatched, logged `Recovered critic verdict from graph state
(process cache empty after restart)`, produced the proper APPROVED verdict
freeze, and the orchestrator resolved the target `pending_review` from the
ledger — no blind escalation, no inversion.
**Severity:** **high** — every restart path converts "I decided" into "no
decision was made", and each consumer has its own (mostly wrong) idea of what
"no decision" means.
**Component:** `src/tools/core/job.py:28`,
`src/tools/evaluation/evaluation_tools.py:28`, `src/core/phase.py:789-846`,
`src/core/phase.py:1071-1090`.

## The defect

Two module-level dicts in the agent process hold the only record of the two
decisions that can terminate a job:

| Store | Anchor | Holds | Written by | Read at |
|---|---|---|---|---|
| `_final_phase_data` | `src/tools/core/job.py:28` | the end-of-job report (summary, deliverables, confidence) | `job_complete` (`job.py:224`), and *also* both evaluation tools (`evaluation_tools.py:138`, `:229`) | `phase.py:1078` — the only live "am I finalizing?" signal |
| `_verdict_data` | `src/tools/evaluation/evaluation_tools.py:28` | a critic's approve/return verdict | `approve_job` (`:128`), `return_job_with_feedback` (`:218`) | `phase.py:796`, `phase.py:1087` |

Both are:

- **process-local** — not a LangGraph channel, so not in the checkpoint, not
  in `checkpoint_writes`, not replayed, not shared across replicas;
- **cleared before the freeze is emitted** (`phase.py:798-800`), so the
  window where the decision exists in exactly one place is not even the whole
  finalization;
- **unbacked** — nothing durable is written first.

Everything less consequential about a job — todos, messages, phase number —
*is* checkpointed. The two irreversible decisions are not.

## Aggravating finding: `is_final_phase` is never `True`

`src/tools/core/job.py:9` documents `job_complete` as:

> 2. Sets is_final_phase=True in state

**It does not, and nothing else does either.** An exhaustive search finds
`is_final_phase` assigned `False` at six sites (`state.py:180`,
`phase.py:755`, `:906`, `:977`, `agent.py:951`, `:1012`, `graph.py:4109`) and
read at `phase.py:1077`. It is never assigned `True` anywhere in the
repository.

So the finalization check at `phase.py:1080`:

```python
if is_final or final_data:
```

reduces to `final_data` alone — job finalization hangs entirely off one
in-memory dict, and the state field that was supposed to make it durable is
vestigial. The docstring is actively misleading anyone reasoning about
recovery.

## Consequences

- **Critic verdicts invert to approvals.** Covered in full by
  `verification_round_reset_spawns_blind_critic.md`; the loss paths are pod
  eviction, OOM, lease expiry, agent re-registration mass-pause
  (`postgres.py:3880-3890`), a cooperative stop that returns before reporting
  (`dual_app.py:557-564`), a status-race 400 on the completion report
  (`main.py:13943`, no retry, no outbox), and the drain overwrite
  (`drain_freeze_overwrites_critic_verdict.md`).
- **Worker jobs silently fail to finalize.** With `_final_phase_data` gone,
  the job simply does not end; if it is present but the process restarted,
  `phase.py:837` substitutes a placeholder report
  (`{"summary": "Job completed", "deliverables": [], "confidence": 1.0}`) —
  a fabricated deliverable list attributed to the agent.
- **The re-entrancy guard resets** (`job.py:166`), so a restarted job can
  re-enter `job_complete`.

## Prior art

The invariant every durable-execution engine enforces is
**journal-before-observe**: the decision is recorded durably *before* the
caller observes the result, and replay returns the recorded value rather than
re-executing (Temporal side effects; Restate's "recorded to a persistent log
before its result is returned"; DBOS checking Postgres before re-running a
step). The gap between "decision made" and "decision committed" is the
canonical **dual-write problem**.

DBOS's strongest form is available to us for free: because job state already
lives in Postgres, the decision write and the status transition can commit in
**one transaction**.

LangGraph's own idiom for a decision produced inside a tool call is to return
a `Command(update=...)` so the value lands in a state channel. Two caveats
before relying on that alone: parallel tool calls need a deliberately chosen
reducer (append-only, not last-write-wins), and channel writes are only
durable at the next super-step boundary — under `durability="exit"` or
`"async"` they are still lost on a pod kill. The module-global is not a
*named* anti-pattern in the LangGraph docs, but it is mechanically excluded
by the design: process memory is not a channel, so it is never checkpointed.

## Fix proposal

1. **Write the decision durably inside the tool call, before it returns.**
   For verdicts, a `verification_rounds` record on the target job (Slice 2 of
   the verification doc). For `job_complete`, the equivalent on the job's own
   context. Idempotency key `(job_id, tool_call_id)` with upsert so replay is
   a no-op.
2. **Read durable-first at finalize.** `finalize_job` should consult the
   durable record and treat the in-memory dict as a cache, not the source of
   truth.
3. **Mirror into graph state** via `Command(update=...)` with an append-only
   reducer, so a resumed graph sees the decision without a DB round-trip —
   *after* confirming the app's compiled `durability` mode. This is a
   convenience mirror, never the source of truth.
4. **Delete or implement `is_final_phase`.** Either set it where the
   docstring claims, or remove the field and fix `job.py:9`,
   `state.py:49`, and `state.py:88`. A state field that is only ever `False`
   is a trap for the next person reasoning about recovery.
5. **Never substitute a placeholder report.** `phase.py:837` should fail
   loudly rather than invent a summary and an empty deliverables list.

## Scope note

This doc covers the durability defect. What each consumer *does* when the
decision is missing — and specifically that missing verdicts are read as
approvals — is a separate defect tracked in
`verification_round_reset_spawns_blind_critic.md` (Layer 2). Both need
fixing: durability alone would still leave the fail-open semantics in place
for genuinely absent decisions.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md`
- `docs/issues/drain_freeze_overwrites_critic_verdict.md`
