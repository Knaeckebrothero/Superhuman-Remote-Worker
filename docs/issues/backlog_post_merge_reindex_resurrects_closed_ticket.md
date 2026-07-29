---
tags:
  - issue
  - loops
  - backlog
  - knowledge-base
  - reindex
  - race
related:
  - "[[project_backlog_pipeline]]"
  - "[[loop_campaign_scheduling]]"
  - "[[okf_knowledge_base]]"
---

# A detached post-merge reindex can resurrect a backlog ticket the advance just closed

**Filed:** 2026-07-29. Found by the project-backlog pipeline's final whole-branch review
(as "M6") and **not recorded at the time** — this doc is the recovery of a dropped
finding, re-verified against develop @ 2026-07-29. Line numbers are current.

## Symptom

A campaign is disposed, its ticket is correctly closed to `resolved` in both the note
file and the index — and then the ticket **reappears in the backlog pool**, `status`
back to `active`, with nothing in the logs marking the reversal as an error.

It self-heals at the next post-merge reindex or the sweeper tick. But the next loop
job's kickoff is rendered in the window between, so an agent can be handed a shipped
ticket as available work, in the one block the loop tells it to trust.

## Mechanism

Two writers touch the same index row, and the slower one starts first:

1. `_merge_and_retro_loop_job` (`orchestrator/main.py`) squash-merges the job branch and
   fires the post-merge reindex **detached**:
   ```python
   asyncio.create_task(
       _kb_reindex_after_merge(str(_kb_project), job.get("repo_name"))
   )
   ```
   (`orchestrator/main.py:13253`). It is deliberately fire-and-forget and non-fatal.
2. That reindex reads the ticket's note file from the merged tree, where `status:` is
   still `active`.
3. The advance continues and calls `close_backlog_ticket`
   (`orchestrator/main.py:13660`), which writes `resolved` to the note file **and** the
   index row.
4. The reindex — still running — reaches that note and upserts its **stale** read,
   putting `status='active'` back over the close.

Nothing forces a correction afterwards: `close_backlog_ticket`'s own Gitea commit
triggers no reindex.

## Scope, honestly

- **Verified:** the ordering is real in current code — the `create_task` at `:13253`
  precedes the close at `:13660` on the same advance, and the close triggers no reindex.
- **Reasoned, not measured:** the width of the window, and how often step 4 actually
  lands after step 3.
- **Narrowed by a since-landed fix, not removed.** With the watermark bug fixed
  ([[kb_reindex_watermark_never_advances]], now in `docs/done/`), reindexes are
  incremental rather than full, so the race needs the merged branch to have *changed the
  ticket file*. That is not exotic: any agent that `kb_update`s the ticket during the
  campaign — adding findings, adjusting priority — produces exactly that change.

## Suggested directions

Ordered by cost, not preference — pick after measuring, since the window is unmeasured:

1. **Make the close win.** Have `close_backlog_ticket` (or its caller) re-assert the
   status after the in-flight reindex could plausibly have finished, or have the reindex
   skip rows whose index `modified_at` is newer than the blob it read.
2. **Order the operations.** Await the post-merge reindex before the advance's close
   rather than detaching it — cheapest to reason about, but it puts a slow embedding
   pass on the advance path, which is why it was detached in the first place.
3. **Trigger a reindex from the close**, so the durable file write is always the last
   word.

## Reproduction sketch

Run a campaign whose members `kb_update` the initiative ticket, then dispose it. Watch
`knowledge_index.status` for that `note_id` across the merge and the advance; the bug is
a transition `active → resolved → active` with no error logged. Compare the row's
`indexed_at` against the close's timestamp to confirm the reindex wrote last.
