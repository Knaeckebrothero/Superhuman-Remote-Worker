---
tags:
  - issue
  - jobs
  - scholar
  - subjobs
---

# Scholar subjob ending `pending_review` silently counts as research success

**Filed:** 2026-07-16, found during the multi-agent code audit for
[`llm_outage_subjob_resilience`](../features/llm_outage_subjob_resilience.md).
Line numbers are develop @ 2026-07-16.

## Symptom

A pre-job scholar subjob that ends in **`pending_review`** — today the landing
state for any LLM outage/cooldown freeze on a subjob
(`determine_job_status` subjob fallback, `completion.py:747-748`), and in
principle any other abnormal non-failed terminal — is treated as a **successful
research run**:

- `_handle_scholar_completion` (`main.py:10874-10951`) classifies failure as
  `status in {failed, cancelled}` only (`is_failure`, `:10905`).
- Everything else — including `pending_review` — takes the success path: the
  parent gets `scholar_completed=True` + `scholar_output_dir` (which may be
  `None` when nothing was grafted) and is unblocked `waiting → created`
  (`:10929-10950`).

Net effect: the parent proceeds **as if research succeeded, with no research**,
and nobody is told. The scholar row lingers in `pending_review` for a human who
has nothing meaningful to review.

## Fix sketch

Treat non-`completed` terminal states as failure-equivalent for the unblock
signal (`scholar_failed=True`, parent proceeds knowingly without research), or
key success on an actual research artifact (graft present) rather than "not
failed". Either way the parent should be unblocked — the scholar is
best-effort by design — but honestly labeled.

## Notes

- Once [`llm_outage_subjob_resilience`](../features/llm_outage_subjob_resilience.md)
  lands, outage-struck scholars will **pause+resume** instead of landing in
  `pending_review`, removing the main path into this bug. This issue covers the
  remaining paths (and the misleading `is_failure` semantics generally).
- Sibling semantics worth a look in the same pass:
  `all_delegation_children_terminal` counts `pending_review` as terminal
  (`postgres.py:1548-1556`), so a delegation parent proceeds with an empty
  child result — arguably intentional, but the same "abnormal state counted as
  done" shape.
