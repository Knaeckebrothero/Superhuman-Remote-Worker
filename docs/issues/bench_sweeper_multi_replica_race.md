# Job Bench sweeper: no multi-replica claim — duplicate-submission race

**Status:** Open, low-severity. Found 2026-08-05 during `baseline-02`, the
component's first live run on dev (the k3d smoke was single-replica, so this
never had a chance to show).

## The gap

`orchestrator/services/bench.py` has no locking anywhere (`sweep_tick` just
iterates `list_running_runs()` → `sweep_run()`), and the dev cluster runs
**two orchestrator replicas**. Every replica starts its own sweeper via the
router lifespan, so every running bench run is swept twice per tick interval.

Observed phase alignment made it spicy: the two sweepers started 29s apart
(09:54:52 / 09:55:21), which with `tick=30s` means they fire within ~1s of
each other every tick. Both read the run's `state` ledger, compute the missing
(task, arm, replicate) pairs, and submit up to `max_in_flight` — if replica B
reads the ledger before replica A's post-submission ledger write commits, B
submits the same pairs again.

## Observed behaviour

Tick 1 of `baseline-02` (run `885008dc`, 2026-08-05 09:57) came out clean:
replica `ddt49` logged `submitted 2 job(s)`, replica `jnz8b` stayed silent,
ledger holds exactly 2 entries. Either the status-refresh phase skews the
timing enough, or we got lucky. No duplicates seen so far; the run monitor
counts duplicate pairs continuously.

**Update, same day 15:48Z — the race fired.** `A1-inbox-digest` r1 was
submitted twice, `submitted_at` **2 ms apart** (15:48:33.114 / .116, one per
replica; the replicas' tick phases converged after the 11:09Z pod restarts).
Ledger went to 11 entries for 10 pairs, 3 jobs in flight against
`max_in_flight=2`. The duplicate (`572649b6`) was cancelled manually before
its first LLM call (0 requests — still provisioning), so zero token burn and
the row auto-classifies as `infra` in the report. With ~10-min monitoring
lag, a duplicate D-task would have burned real money. This upgrades the fix
from "nice to have" to "before the next long run".

## Impact

- Worst case: duplicate jobs = wasted LLM spend + an extra unplanned
  replicate. The report can dedupe by (task, arm, replicate) so analysis
  survives; money does not come back.
- Same shape of risk applies to any future sweeper added via router lifespan
  — the established loops (`cron_dispatcher` etc.) live in `main.py`'s
  lifespan and have the same multi-replica property; worth checking what
  convention they rely on.

## Fix options (pick one)

1. **Per-tick advisory lock** — `pg_try_advisory_lock(hashtext('bench_sweep'))`
   around the tick; loser skips. Cheapest, one query.
2. **Claim per run** — `SELECT … FOR UPDATE SKIP LOCKED` on the `bench_runs`
   row inside `sweep_run`, ledger write before release.
3. **Single-replica election** — only run the sweeper when some leader
   condition holds (no precedent in the codebase yet; overkill for this).

Option 1 or 2 is a ~10-line change plus a two-replica test.
