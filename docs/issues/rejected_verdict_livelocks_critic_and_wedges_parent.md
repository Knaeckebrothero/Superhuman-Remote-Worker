---
tags:
  - issue
  - verification
  - critic
  - watchdog
---

# A critic that cannot produce a valid verdict call livelocks forever, and the unstick watchdog is blind to it

**Filed:** 2026-08-01, from the verification live-gate re-run
(dev target `40efbb39-0890-40fa-a464-6e3d6bd92832`, critic
`245889ac-6d5b-4771-bd5a-5f47fd1b7e31`).
**Status:** Observed live. UNFIXED. Manually cancelled after 105 minutes.
**Severity:** **high** — unbounded cost and an indefinitely wedged parent. The
parent sits in `reviewing` with no deadline and no operator signal.
**Component:** `orchestrator/database/postgres.py:4940`
(`_UNSTICK_REVIEWING_SQL`), verdict-tool retry path.

## What happened

The critic reviewed a target whose deliverable was genuinely missing (see
`git_push_fails_silently_via_workspace_backend.md`). It reached the correct
conclusion, wrote a well-formed finding to disk, and then could not get that
finding into a verdict call.

It called `return_job_with_feedback` with `findings: "[]"` and was rejected:

```
Error: the verdict was NOT recorded and must be corrected and resubmitted.
verdict rejected:
- Cannot return a job with no findings: `opened` is empty and no findings from
  previous rounds are open. If the deliverable has a problem, describe it as a
  finding in `opened`.
```

**The gate behaved exactly as designed.** The call was inconsistent, nothing was
recorded, nothing was silently approved, and the error names the fix. This is
the fail-closed property working.

The critic then repeated the same invalid call. Five rejections. It never
converted "the deliverable is absent" into a structured finding — even though
`output/critic_verdict.json` on its own disk contained a correct, complete F1 at
severity high, with claim and evidence.

By cancellation it had run **189 iterations over 105 minutes**, made **41
commits** cycling the same two strategic todos (`todo_1: REVIEW AND ADAPT` →
`todo_2: PLAN OR COMPLETE`, repeating), and opened **15 shells**
(`workspace-check`, `find-deliverable`, `locate-glossary`, `deep-file-search`,
`thorough-search`, `final-check`, `final-evidence`, `final-confirm`,
`last-confirm`, …) — the names alone chart the livelock.

Its own auxiliary memory diagnosed the trap while it was in it:

> stuck calling return_job_with_feedback repeatedly … the single most important
> pinned memory ('do not loop re-attempting the freeze call — write a KB
> decision note and stop') is at 5 turns TTL and at risk of expiring before the
> agent acts on it

The parent stayed in `reviewing` the entire time.

## Two distinct defects

### 1. No bound on rejected verdict submissions

A rejected verdict is a *correctable* error by design — the model is meant to
fix the call and resubmit. Nothing counts the attempts. A model that cannot
construct a valid call retries until something external stops it. There is no
cap, no escalation after N rejections, and no path that converts "this critic
has failed to render a verdict K times" into an escalation to human review.

The design deliberately refuses to treat a missing verdict as approval. It
should equally refuse to treat one as *pending indefinitely*.

### 2. The unstick watchdog only fires for a critic that DIED

`_UNSTICK_REVIEWING_SQL` moves a wedged parent to `pending_review`, but only
when no live critic exists:

```sql
AND NOT EXISTS (
      SELECT 1 FROM jobs c
       WHERE c.parent_job_id = p.id
         AND c.context->>'verification_target' IS NOT NULL
         AND c.status IN ('created','processing','paused','waiting',
                          'waiting_for_reply')
   )
```

This critic was `processing` throughout, so the watchdog correctly declined to
fire. It covers *"the critic pipeline died"* — its error message says exactly
that — and there is no counterpart for *"the critic is alive and will never
finish."*

From the outside a livelocked critic is indistinguishable from a slow one.
Nothing distinguishes them today, and nothing bounds the wait.

## Fix directions

1. **Count rejected verdict submissions per critic.** After N (3 is
   defensible), stop accepting retries and escalate the parent to
   `pending_review` with the rejection reason — the same escalation the round
   limit already uses. Cheapest fix, and it targets the actual failure.
2. **Give the watchdog a wall-clock arm.** A parent in `reviewing` past a
   generous ceiling should escalate even when a critic is live, with a distinct
   message ("critic did not render a verdict in N minutes"). Do not just widen
   the existing predicate — the "died" and "never finished" cases want
   different messages and different thresholds.
3. **Consider accepting the on-disk verdict as a fallback** — but only with
   care. The critic had a correct `critic_verdict.json` the whole time, and its
   memory recorded the belief that *"on-disk verdict IS the recorded verdict"*.
   That belief is false by design (evidence-only closure: the ledger is the
   record). Treating disk as authoritative would reintroduce exactly the
   ambiguity the rewrite removed. Mentioned to be explicitly rejected, not
   adopted.

1 and 2 are complementary: 1 bounds the common case, 2 is the backstop for
every way a critic can fail to finish that nobody has thought of yet.

## Note on the model

The critic ran on `MiniMax-M3`. The same pod logged repeated
`Structured-output validation failed` for `IngestionVerdictTask`,
`ExtractMemoriesTask` and `AssembleMemoriesTask`, and `memory_inject` latencies
up to 48s. A weaker model's inability to build a structured tool call is a
plausible contributor, and worth checking against the critic-model choice — but
it is not the defect. **Any** model can fail to produce a valid call, and the
system's response to that must be bounded regardless.

## Related

- `docs/issues/git_push_fails_silently_via_workspace_backend.md` — why the
  deliverable was missing, i.e. what put the critic in this position.
- `docs/issues/verification_fail_closed_followups.md` — the live gate.
- `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md` — the
  original wedge this watchdog was built for; this is its uncovered sibling.
