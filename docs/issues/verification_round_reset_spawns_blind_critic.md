---
tags:
  - issue
  - fix-spec
  - jobs
  - orchestrator
  - verification
  - critic
---

# A critic that leaves `waiting` resets verification: the next round spawns a **blind round-1 critic** that can approve what was just returned

**Filed:** 2026-07-27, from job `52949749` ("historische Kernwerke") on dev.
**Status:** CONFIRMED in code + live DB. UNFIXED.
**Severity:** **high** — silently converts "returned twice, severity high" into
"approved". The verification gate can be defeated by a critic-lifecycle
accident, with no error, no warning, and a reset round budget.
**Component:** `orchestrator/main.py` (`_trigger_verification_on_complete`,
~12294–12403), `orchestrator/services/completion.py`
(`format_verification_instructions`, ~1125),
`config/experts/critic/verification_instructions.md`.

## Symptom

Job `52949749` was returned by its critic in round 1 (9 issues, severity
high) and again in round 2 (severity high). In round 3 it was **approved** —
on a deliverable that had not changed by a single byte, by the same model at
the same temperature.

## Evidence (all verified, not inferred)

| Claim | Evidence |
|---|---|
| Deliverable unchanged across all 3 rounds | Every `write_file`/`edit_file` touching `output/kernwerke.md` in the audit DB ends at **2026-07-23 22:33:46Z**; none after. Rounds ran 07-23 21:47, 07-25 23:33, 07-26 13:35. |
| Not sampling variance | Both critic jobs: `resolved_config.agent.llm.model = MiniMax-M3`, `temperature = 0.0`. |
| Round 3 was registered as round **1** | New critic `2ca33236` has `context.verification_round = 0`, `max_verification_rounds = 5`. The prior critic `51a4ce11` sits at `verification_round = 2`, `status = completed`. |
| Both critics got the same generic brief | `jobs.description` is byte-identical for both: *"Verify deliverables of job … Review output against original requirements and either approve or return with feedback."* |
| Round 3 never saw the open issues | Its full reasoning transcript (`chat_history`) has **0 occurrences** of "Hausvater" (issue C3) and 0 of "Akzeptanzkriterium 10". |

## Root cause

`_trigger_verification_on_complete` decides between "next round" and "first
round" purely on the critic's **status**:

```python
critic_row = await conn.fetchrow(
    "SELECT id, status, context FROM jobs "
    "WHERE parent_job_id = $1::uuid AND status = 'waiting'",
    job_id,
)
if critic_row:
    # Subsequent round: resume the SAME critic — its context still holds
    # its own prior findings, so it can check whether they were addressed.
    new_round = await postgres_db.increment_job_verification_round(critic_id)
    await _internal_resume_job(critic_id, feedback="Target job addressed your feedback (round N) …")
else:
    # First round: create a NEW critic, context {"verification_round": 0, …}
```

The design is sound — round continuity is carried by **resuming the same
critic session**, so the prior verdict lives in its conversation context. But
that continuity is anchored to the fragile `waiting` status, and there is no
fallback: the moment a critic leaves `waiting` for any reason, the next
freeze silently takes the `else` branch and starts verification over.

In this incident the round-2 critic session was itself degraded (7 consecutive
`mark_complete` retries, complaints that its toolset was restricted — see
`feedback_resume_restricted_closure_toolset.md`) and finished by calling
`job_complete` at `00:49:01Z`, flipping itself to `completed`. Nothing else
was wrong; that single status transition destroyed the review chain.

**Second, independent defect — a fresh critic *cannot* be told what is open.**
`format_verification_instructions` renders
`config/experts/critic/verification_instructions.md`, whose only placeholders
are:

```
{target_job_id} {target_description} {target_config}
{deliverables_list} {agent_summary} {agent_confidence}
```

There is **no placeholder for prior-round findings**, and no durable record of
them is passed anywhere. Verdicts are handed off by conversation context
alone. So even a deliberately re-created critic starts blind — the `else`
branch is not merely mis-triggered here, it is incapable of continuing a
review.

## Consequences observed

1. **The gate inverted.** Round 3 approved on its own fresh standard, not on
   whether the open issues were closed. From its transcript:
   - C1 passed as `Walnussschalen/Perikarp: explicit in §6.3 Sicherheitshinweise ✅`
     — but §6.3 (lines 636–639 of the deliverable) is a **modern toxicology
     disclaimer** ("hoher Gerbstoffgehalt; potenziell lebertoxisch"),
     explicitly framed as not a pharmacological assessment. No K-/E-title
     covers walnut shells. A safety note was counted as source coverage.
   - C2 passed as `Ratafia/Nocino/Orahovac: E05 (Hovorka/Kronfeld) ✅` — the
     exact substitution round 1 rejected (E05 is a German comparative work,
     not the required Italian primary source).
   - C3 (Hausväterliteratur) was never considered at all.
   - Factual error: round 3 lists `Italian: K05 (original)`; line 44 of the
     deliverable says K05 is `Deutsch (Übers. aus Ital.)`, Prague 1563.
2. **Round budget reset.** The counter lives on the critic, so
   `max_rounds` (5 here) restarted at 0 — a job can exceed its intended
   review budget indefinitely as long as critics keep completing between
   rounds.
3. **Empty structured verdicts are accepted.** Round 2's
   `return_job_with_feedback` was called with `issues: "[]"` while its own
   narrative asserted 9 issues; the orchestrator recorded
   *"Issues: 0, Severity: high"* without complaint.

Incidental but worth knowing: both critics misreported the file length (657
and 658 vs the actual **660** lines). LLM-stated line counts are not evidence.

## Fix proposal

1. **Don't key round continuity on `waiting`.** Look the critic up by
   `parent_job_id` (optionally `context.verification_target`) regardless of
   status, excluding only `cancelled`. If the newest match is terminal,
   either re-open/resume it or create a successor that **inherits its round
   number and findings** — never silently restart at round 0.
2. **Make findings durable and injectable.** Persist each verdict (issues,
   severity, round) — on the parent's context or a small `verification_rounds`
   record — and add a `{prior_findings}` placeholder to
   `verification_instructions.md` so *any* critic, resumed or fresh, is told:
   *"These N issues were open at the end of round K; for each, state
   RESOLVED / STILL OPEN with file:line evidence."* This also upgrades the
   resumed-critic path from "remembers, probably" to "is told, always".
3. **Move the round counter to the parent job**, so it cannot reset with the
   critic.
4. **Validate the verdict call.** `return_job_with_feedback` with
   `severity != none` and an empty `issues` list should be rejected back to
   the model (or repaired from the narrative) rather than recorded as
   "Issues: 0".
5. **Keep the critic in `waiting` after a return.** A critic that has called
   `return_job_with_feedback` for round N should not be able to close itself
   with `job_complete` in the same round; that transition is what broke the
   chain here.

Items 1+2 are the fix; 3–5 are the hardening that keeps it from recurring by
another route.

## Triage rule until fixed

**An approval that follows a return is not automatically a quality signal.**
Compare the approving critic's job id with the returning one — a *different*
id means the approval came from a critic that never saw the findings, and the
verdict should be re-checked by hand.

## Related

- `docs/issues/feedback_resume_restricted_closure_toolset.md` — the degraded
  session that made the round-2 critic close itself.
- `docs/issues/stale_critic_waiting_status_escapes_reaper.md` — the mirror
  image (a critic stuck *in* `waiting`); together these show `waiting` is a
  fragile, load-bearing status in the verification flow.
- `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md`,
  `docs/issues/loop_critic_producer_identity_bias.md` — adjacent
  critic-reliability issues.
- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — the
  incident chain this was discovered in.
