# Request-shape nudge for upstream stream disconnects (TEMPORARY QUICKFIX)

**Status:** shipped + pushed 2026-07-30 · `8e65071c` (nudge, tests, this doc) and
`773a6b20` (codex proxy → `v7.2.110`), both on `develop`.
**LIVE VERIFICATION: still owed** — see *What unit tests cannot tell you* below.
**Remove when:** OpenAI fixes
[openai/codex#9995](https://github.com/openai/codex/issues/9995) (OPEN as of
2026-08-05).

This documents a deliberate hack. It buys job survival against a provider bug we
do not control. It is not a design we want to keep.

> **Filed under `done/` because the work shipped, not because the problem is
> solved.** The quickfix is still live in the codebase, its load-bearing premise
> has never been confirmed in production, and the removal checklist at the bottom
> is still outstanding. Do not read this as resolved.

## What breaks

`gpt-5.6-*` through `srw-codex-proxy` intermittently fails with:

```
Error code: 408 - stream error: stream disconnected before completion:
                  stream closed before response.completed
```

The failure is **deterministic per payload**. Once a given request trips it,
replaying the identical bytes trips it again, indefinitely. Measured on job
`eb0143f8` (2026-07-30): four consecutive re-dispatches, each a fresh pod on a
fresh connection against an idle proxy, each dying on the *first* LLM call after
2–7 s with a fresh 408:

```
07:34:50  attempts=6  initial: 408 stream disconnected before completion
07:38:11  attempts=6  initial: 408 stream disconnected before completion
07:42:17  attempts=6  initial: 408 stream disconnected before completion
07:46:57  attempts=6  initial: 408 stream disconnected before completion
```

Job `d251e513` showed the same across 80 minutes with fully idle hours between
attempts. Waiting does not clear it. Size does not explain it (`c6dd288d` pushed
1.21 MB fine; `d251e513` was 631 KB). Ruled out: the account, the token, the
credential, contention, and the 400k window.

What *does* clear it is changing the request. Same job `d251e513`: the 08:07
re-dispatch rebuilt its context and then ran four successful iterations
(131k → 138k tokens) before the *next* payload got stuck.

## Why the 503s in the logs are a red herring

Pre-`v7.2.110`, CLIProxyAPI charged this **request**-scoped 408 to the
**credential**: the single auth entry flipped to `status: error` and every
following call got an instant `503 auth_unavailable: no auth available`. Retries
2–6 therefore all reported an auth failure that never existed, and the real 408
was overwritten. Fixed upstream by `09da52ad` (2026-07-15,
[CLIProxyAPI#3055](https://github.com/router-for-me/CLIProxyAPI/issues/3055)),
which adds `IsRequestScoped()`. Our images before `v7.2.110` predate it.

**That upgrade fixes attribution and blast radius, not this.** Post-upgrade the
408 is reported honestly — and still kills the job.

## How Codex itself survives this

It doesn't, automatically. Codex CLI retries the stream (`stream_max_retries`
default 5, ~200 ms exponential backoff), those retries resend identical bytes and
frequently fail, and then the task **stops and hands control to the human**. From
[openai/codex#10378](https://github.com/openai/codex/issues/10378):

> "If it counts up to 5 the task is stopped. I can then just type something like
> 'retry' and it usually works again."

The human's keystroke appends a turn, which changes the payload, which is why it
works. Codex users rarely notice, because they *are* the recovery mechanism.

We are autonomous. Nobody types it. Our re-dispatch is a byte-identical replay of
a payload already proven to fail, so we loop until a ceiling.

## The quickfix

Type it ourselves. On the cycle the repeat give-up would first fire, spend **one**
extra backoff cycle with a short synthetic user turn appended, then give up for
real if that fails too.

| Where | What |
|---|---|
| `services/completion.py` | `LLM_OUTAGE_SHAPE_NUDGE` flag; ceiling branch returns `paused` once when `shape_nudge_attempted` is unset |
| `services/completion.py` | `llm_outage_nudge_state()` — the arm/latch state machine, pure so it is testable without a DB |
| `database/postgres.py` | `increment_job_llm_outage_attempt(nudge_at_repeats=…)` calls it and persists `pending_shape_nudge` / `shape_nudge_attempted` |
| `main.py` | passes `nudge_at_repeats=LLM_OUTAGE_REPEAT_CEILING` |
| `agent.py` | `_SHAPE_NUDGE_TEXT` injected via `aupdate_state` on auto-continue resume |

Chosen over force-compaction (`restore_from_feedback`, `force=True`) because it
costs a few dozen tokens and preserves context, where compaction summarises the
history away. Compaction stays available as the operator's manual next step.

**Bounded by design:** one nudge per streak. `shape_nudge_attempted` latches, so a
second identical failure falls through to the real give-up. The 12 h duration
ceiling and the 60-attempt backstop both still outrank it.

**Kill switch:** `LLM_OUTAGE_SHAPE_NUDGE=0` disables it with no deploy.

## Removing it

1. Confirm [openai/codex#9995](https://github.com/openai/codex/issues/9995) is
   fixed and that a job which previously wedged now recovers on plain re-dispatch.
2. Delete `LLM_OUTAGE_SHAPE_NUDGE`, `llm_outage_nudge_state`, and the
   `shape_nudge_attempted` branch in `determine_job_status`.
3. Delete `nudge_at_repeats` + both flags from
   `increment_job_llm_outage_attempt`, and the arg in `main.py`.
4. Delete `_SHAPE_NUDGE_TEXT` and its injection block in `agent.py`.
5. Delete `TestShapeNudgeQuickfix` + `TestShapeNudgeLatch`, and drop
   `nudge_attempted` from `TestRepeatGiveUp._job`.
6. Delete this file.

Grep `TEMPORARY QUICKFIX` — every site is tagged and points here.

## What unit tests cannot tell you

`TestShapeNudgeQuickfix` pins the pause-vs-fail decision and `TestShapeNudgeLatch`
pins the one-shot arming (including the anti-forever-loop property). Neither
covers the two integration seams, so **verify these on a live cluster**:

1. **Does the flag reach the agent?** It travels
   `context.llm_outage.pending_shape_nudge` → orchestrator dispatch
   (`remaining_context`) → `JobStartRequest.context` → `metadata.update(context)`
   in `dual_app.py` → `updated_metadata` in `agent.py`. Four hops, none of them
   asserted. Confirm by grepping the agent log for `Injected request-shape nudge`.
2. **Does an appended turn actually clear the upstream rejection?** This is the
   load-bearing assumption of the whole quickfix and it is borrowed from Codex
   users, not measured here. If the nudge fires and the job still dies with the
   same 408, the premise is wrong and this should be reverted in favour of the
   force-compaction path.

Also worth eyeballing in the first real transcript: whether the agent treats the
notice as inert, or wastes a turn replying to it / re-verifying the workspace.

**As of 2026-08-05 none of this has been checked.** The cluster was unreachable
from the authoring session (kubectl DNS timeout, MCP behind a Cloudflare 1033
tunnel error), so neither seam has been observed even once. Concretely still owed:

```bash
# 1. did the proxy upgrade stop the credential cascade?
kubectl -n superhuman-remote-worker logs deploy/srw-codex-proxy | grep -B1 -A5 "408 |"
#    PASS = a 408 is NOT followed by a run of 503s
kubectl -n superhuman-remote-worker exec deploy/srw-orchestrator -c orchestrator -- \
  curl -s -H "Authorization: Bearer $CODEX_MANAGEMENT_KEY" \
  http://srw-codex-proxy:8317/v0/management/auth-files
#    PASS = status stays "active" through a 408

# 2. did the nudge fire, and did it work?
kubectl -n superhuman-remote-worker logs -l srw/managed-by=agent-provisioner \
  | grep "Injected request-shape nudge"
#    then: did that job continue, or die with the same 408?
```

If the nudge fires and the job still dies identically, the premise is wrong —
revert to the force-compaction path rather than tuning the wording.

## What is NOT in scope

Two adjacent items, deliberately separate:

- **Context budget — covered elsewhere, and my first numbers here were wrong.**
  Superseded by
  [`codex_proxy_context_window_cap.md`](codex_proxy_context_window_cap.md),
  which live-probed the proxy: the input ceiling is **~357–380K** (400K total),
  and exceeding it returns a clean `400 context_too_large`, not the 408 this doc
  is about. **272K is NOT a cap** — it is only OpenAI's long-context *pricing*
  tier; I took it from a secondary blog on 2026-07-30 and it was wrong. That doc
  also fixes the drift by clamping codex-routed models to 400K
  (`CODEX_CONTEXT_WINDOW_CAP`), which puts our 80% threshold at ~320K, safely
  under the measured wall. Nothing left to do here.
- **Loop jobs mask this.** `determine_job_status` maps a loop job that stops with
  no recognised freeze to `completed` (`completion.py`, `is_loop_job` branch), so
  a job that failed 13 times can still report success to the loop. Worse than the
  grinding it replaced. Still open — needs its own fix.
