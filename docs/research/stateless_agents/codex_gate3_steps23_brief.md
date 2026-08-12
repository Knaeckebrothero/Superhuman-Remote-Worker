# Codex brief — Gate 3 steps 2+3: the completion command substrate

**Date: 2026-08-12 (overnight run). Branch: `develop`, directly — no
subbranches. Commit locally, milestone by milestone. DO NOT PUSH: a push
triggers the full CI → image build → Fleet → dev-cluster rollout chain, and
that decision belongs to the morning review.**

## 0. Ground rules

0.1 **Authoritative sources, in order.** The design you are building is
`docs/features/stateless_agents.md` **§5.4.5** — authoritative again as of
2026-08-12 (`d492442c`), with the adversarial review folded in. Where anything
else (including this brief, older briefs, or `gate3_adversarial_review.md`)
disagrees with §5.4.5, §5.4.5 wins. The evidence base for the completion path
is `docs/research/stateless_agents/completion_path_side_effect_inventory.md`
(~37 effects, classified) — **read it before touching `/complete`; do not
reason about that path from prose or memory.** The DDL you ship is the DDL in
§5.4.5 "(1) The command row is its own table" — copy it from the doc at build
time, do not retype it from this brief (this brief deliberately quotes none of
it, to make divergence impossible).

0.2 **Stop rule (narrowed — the old broad one cost a full session).** Stop
only if a premise is load-bearing for the night's scope AND there is no
alternative route. "The work is bigger than described", "a helper doesn't
exist", "the doc's line number moved" are adapt-and-record, not stop. Record
every deviation in the implementation log (see 0.6).

0.3 **Git hygiene.** `git add <explicit paths>` only — never `-A`, never
`commit -a`. The untracked `HomeLab/` directory must remain untracked. One
commit per milestone, each leaving the tree green and the system
behavior-identical (everything you build tonight ships **dark or
default-off**).

0.4 **Baseline first.** Run `./scripts/pytest-fast.sh` once before touching
anything and record the failure list. The historical baseline was 17 local
failures; the 6 VM-chart ones were fixed 2026-08-12 (`7d72b964`), so expect
~11 (MCP/research/localhost-Postgres env noise). Do not chase baseline
failures; do not count them against your work. Never establish any state with
`-x` — it hides everything after the first failure.

0.5 **Migration rules (all mandatory).**
- New migrations start at **0140** (0134–0139 are deliberately left free;
  0133 is taken). Files: `orchestrator/database/migrations/app/NNNN_*.sql`.
- After ANY migration: regenerate the schema snapshot with
  `scripts/schema-snapshot.sh` and stage `orchestrator/database/*_current.sql`
  **in the same commit** — CI has a drift gate.
- Never edit a migration file that has ever been applied anywhere (checksum
  guard). Fix forward with a new file.
- Put each `CREATE INDEX` in the **same file** as the `CREATE TABLE` it
  serves — squawk only tracks same-file creations, and index-on-new-table in
  the same file needs no exception. If squawk (pinned v2.59.0, run it locally
  over your new files) still finds something inactionable, use the
  `.squawk.toml` `excluded_paths` pattern **with a rationale comment** in the
  file's documented style — never edit applied SQL to appease it.
- `.notx.sql` files are single-statement (the runner sends each file as one
  simple query). You should not need any tonight — all new tables are empty.
- The real-PG test harness for `run_queue` applies its migration files
  explicitly; if you extend that pattern, apply **every** file your schema
  depends on, not just your newest (a one-file harness once passed locally
  and failed on the cluster).

0.6 **Logging.** Append a dated section to
`docs/research/stateless_agents/implementation_log.md` per milestone: what
shipped, what deviated from §5.4.5 and why, what's left. That file is the
morning review's entry point.

0.7 **k3d.** The local cluster is available for verification; Tilt was down
at brief-writing time. If you bring it up, remember: **never `tilt trigger
srw`** (it uninstalls the release), never `git checkout` another branch while
Tilt watches, never manual `helm upgrade` outside the Tilt/values-local flow.
Tilt ships partially-edited images — after any image-dependent change,
`kubectl exec … grep` the running pod to prove the bytes landed before
trusting a smoke result. Harness: `scripts/stateless-lane-probe.sh
turn|burst|kill` drives the stateless session lane.

## 1. What you are building

Rollout steps 2 and 3 of §5.4.5 "(7) Rollout" — the durable completion
command substrate, dark:

- **Step 2 (dead schema, zero behavior change):** `job_completion_commands`,
  `completion_effects`, `jobs.completion_seq_hwm`, the River-style finalizer
  leader-lease row/table, and the sweep-predicate view from decision (6).
  Nothing reads or writes any of it yet.
- **Step 3 (behind a default-off flag):** `/complete`'s accept path writes
  the command row **first, before any guard, for both lanes**, then the
  finalizer runs **inline** for both lanes — behavior identical to today,
  but every report is durably recorded and a crashed completion becomes
  resumable. Ships WITH the finalizer-resume drain (the doc's "live-fuse"
  constraint: starlette cancels handlers on client disconnect at
  `report_completion`'s 60 s timeout, so accept and finalize must be
  separable and stranded `pending` commands must have a drainer from day
  one).

Also fold in the two agent-side ordering items §5.4.5 (7) requires **before**
later steps can ever ship, both tiny and safe now:
- `report_completion` treats `202` as success (agent must understand it
  before any orchestrator ever returns it — step 5 will).
- `client_report_id`: agent mints it **once per stop**, persists it with the
  exact payload in the freeze/checkpoint, resends verbatim on retry; the
  orchestrator treats it as optional with the server-side fallback synthesized
  from `(job_id, report_seq)`.

## 2. Scope boundaries

**IN:** everything in §1; real-PG tests for the new tables and the accept
semantics; unit tests for the finalizer; the k3d soak of milestone M4;
`.squawk.toml`/snapshot/log housekeeping.

**OUT — do not touch tonight:**
- Step 4 (moving the status write, activating sweep routing) and step 5
  (background finalizer for stateless units). If M1–M4 are genuinely done and
  soaked, dark *scaffolding* for step 4 is permitted as a stretch (M5), but
  activation is not.
- Step 6 / worker admission. Session-lane adoption of `completion_effects`
  (the schema is shared-ready by design; adoption is a later workstream).
- Path-A compaction persistence, `prompt_cache_key`, metering attribution —
  owned by the other track.
- Anything VM. Anything in `helm/`/`helm-vm-cluster/` beyond (if strictly
  needed) a values-local flag wire-through.
- Migrations 0134–0139 (reserved), and any already-applied migration file.

## 3. Milestones (commit after each; stop cleanly wherever time runs out)

**M1 — schema (0140+).** The tables/column/lease/view per §5.4.5, with the
CHECK constraints exactly as the doc's DDL states them (the terminal-shape
and fence-exactly-one CHECKs are load-bearing; the review derived them from
live holes). Real-PG tests proving: the CHECKs reject every half-written
shape named in the doc; `uq_job_completion_client` dedups; the drain partial
index exists and `completion_effects` deliberately has none. Regenerate the
snapshot, run squawk, commit.

**M2 — accept.** The thin accept path in `/complete`, per §5.4.5 (2):
- INSERT is the **first DB write of the handler**, before any guard.
- Fence checked **at accept**: `accepted_lease_token` XOR `accepted_agent_id`
  (agent-origin); operator-origin rows carry neither. `JobCompleteRequest`
  gains optional `agent_id` and optional `client_report_id`.
- `report_seq` from incrementing `jobs.completion_seq_hwm` **under the jobs
  row lock** — never an IDENTITY/sequence.
- **Lock order is binding and inverts 0119: run_queue row first, jobs row
  second.** Validate the lease fence (`FOR SHARE` on the run_queue row)
  before taking the jobs row for the hwm bump. Getting this backwards
  deadlocks against every concurrent claim.
- The full retry matrix from §5.4.5 (2): duplicate `client_report_id` with
  equal server-computed `payload_digest` ⇒ replay stored outcome +
  `Idempotent-Replayed`; divergent digest ⇒ **422**; `pending`/`finalizing` ⇒
  **409**; `parked` ⇒ 202-still-pending without a retry promise;
  `superseded`/`force_resolved` ⇒ terminal per the doc. Digest is computed
  **server-side** over canonical JSON excluding transport/fence fields.
- Tests: concurrent same-report race (one wins, one 409, exactly one row);
  the 422-vs-409 split; pinned fence via `agent_id`; stateless fence via
  lease token; stale token rejected at accept.

**M3 — inline finalizer + resume drain.** The finalizer executes the
existing completion effects **in the same order and with the same behavior
as today** (this step does NOT reorder anything or move the status write),
recording one `completion_effects` row per effect as it goes, command row
`pending → finalizing → done`. Leader election per §5.4.5 (3): expiring
lease row (`INSERT … ON CONFLICT DO NOTHING`, term-keyed renewal UPDATE,
expiry reap) — **not** an advisory lock. The resume drain finds commands
stranded `pending`/`finalizing` past their lease and re-executes from the
first unfinished effect row. Backoff dialect = run_queue's
(`attempts`/`max_attempts`/`run_after`, `5s × attempts × (1+U(0,0.2))`,
`parked` as operator worklist).

**M4 — flag + parity proof.** Wire the whole path behind a default-off
config flag (orchestrator config/env; on in `deployment/values-local.yaml`
only). Prove parity per §5.4.5's split acceptance: with the flag ON, a
pinned k3d job (create → process → complete → approve) reaches the same
terminal status, same effect **set** (assert from the effects table), same
response shape, and `completed_at`/critic/dispatch behavior unchanged; a
stateless session turn is unaffected; with the flag OFF, zero rows appear
anywhere new. Kill-test the one window step 3 owns: crash (pod
force-delete, not `kill -9 1` — PID-1 ignores it) between accept and
finalize, restart, drain completes it exactly once. Record numbers in the
log.

**M5 (stretch, only if M1–M4 are green and soaked) — dark step-4
scaffolding.** The decision-(6) sweep routing table + per-actor treatment
map as code with tests, nothing activated; and/or the `superseded`
winner-selection logic in the finalizer with the first-wins semantics of
§5.4.5 (2) "Authority is finalization ORDER" — behind the same flag,
exercised only by tests.

## 4. Traps already hit once — do not rediscover them

- **Read the call, not the keyword**: a grep that matches a trigger string
  is not evidence the behavior exists. Verify by reading the call site.
- The 60 s client timeout means any long inline finalize can be cancelled
  mid-flight; that is exactly why M3's drain ships tonight and not later.
- `determine_job_status`'s "ignore a coincident error on an already-successful
  job" backstop is load-bearing for first-wins — do not "clean it up".
- The freeze payload embeds `datetime.now()`/`head_commit`: never re-derive
  `client_report_id` or the payload on retry; persist and resend verbatim.
- `apply_terminal_job_side_effects` has 4 call sites with NO agent identity
  (approve/Mode-A verdict paths) — that is why operator-origin rows exist in
  the fence CHECK. Do not force them into the agent shape.
- A fresh `git worktree` fails ~19 helm tests spuriously (gitignored deps
  absent) — verify in the main worktree.
- Local admin-cli id_tokens die in ~15 min as silent 401s during long k3d
  sessions — re-mint before trusting a "failure".

## 5. Final gates before the last commit

1. `./scripts/pytest-fast.sh` — same failure set as your 0.4 baseline, plus
   nothing.
2. `ruff check src/ orchestrator/ tests/` and `ruff format --check` on
   touched paths.
3. Squawk (pinned binary) over the new migration files: 0 findings or
   documented exceptions.
4. `scripts/schema-snapshot.sh` idempotent: re-running it after your last
   commit produces zero diff.
5. The M4 parity/kill evidence recorded in the implementation log.

## 6. Report format (morning review reads this first)

Per milestone: shipped/partial/not-started, commit hash, deviations from
§5.4.5 with reasoning, test counts (new + suite), and the M4 parity table.
Then: the exact flag name and where it's wired, anything left `pending` in
the new tables on k3d, and the single most important thing the morning
should verify by hand.
