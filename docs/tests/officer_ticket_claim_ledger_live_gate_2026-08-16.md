---
tags:
  - test
  - verification
  - officers
  - backlog
  - postgresql
  - live-gate
status: run-1-failed
created: 2026-08-16
related:
  - "[[deleting_a_job_releases_its_backlog_ticket_claim]]"
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
  - "[[officer_backlog_pools_resavio_livefire]]"
---

# Officer ticket-claim ledger — main-dev post-deployment smoke

**Status: RUN 1 FAILED 2026-08-16 (Phase A).** BP-05 is **not deployed** on main dev:
migration `0162_officer_ticket_claims.sql` fail-closed on real historical data, so the
BP-05 orchestrator replica never became serving. Behavioral gates B3 and C1–C7 were never
reached and no fixture was created. See [Run 1 result](#run-1-result-2026-08-16) below.

This is the narrow live gate for BP-05 and app migration
`0162_officer_ticket_claims.sql` after their main-dev deployment. It is not a general
Officer qualification and it does not authorize `auto_pull=true`.

The existing long-running Officer is explicitly out of scope. Use a disposable project,
Officer Post, ticket, and jobs for every mutating check. The one compatibility probe
against a historical backfilled job runs inside a database transaction that is always
rolled back.

## Verdict boundary

This run answers only these questions:

1. Did the intended orchestrator build and migration 0162 actually reach main dev?
2. Did every deployed ticket-bearing job cross into the durable claim ledger?
3. Can a genuine pre-0162 backfilled job still receive an unrelated context merge?
4. Does the trigger refuse removal of claim provenance from a live job?
5. Does a fresh manual Officer ticket claim remain one-shot across physical job deletion?
6. Does the deletion API report retained-claim truth without failing after deletion?
7. Does an ordinary job truthfully report that it had no retained claim?

A complete pass permits closing the BP-05 deployment checkpoint. It does **not** close or
waive BP-01, BP-06, BP-07, BP-08, BP-10, BP-11, ES-01, or the remaining umbrella audit.
Keep every Officer Post used by this run at `auto_pull=false`.

## Authority and safety rules

- Confirm Kubernetes context `main` and namespace `superhuman-remote-worker` before any
  cluster call. If either differs, stop.
- Start with read-only inspection. The operator handing off this runbook must explicitly
  authorize creation and cleanup of the disposable main-dev fixtures before Phase C.
- Do not hold, release, restart, recommission, message, edit, or delete the user's existing
  Officer or project.
- Do not enable `auto_pull`, even briefly. Do not create a database-only ready ticket or
  forge `ready_at`, Officer identity, claim source, or admission context.
- Create ready/category machine tags only through the supported, server-authorized
  Officer/owner tool path. If that path cannot be driven, report the gate blocked; do not
  bypass it with SQL.
- Database inspection must use the existing trusted in-cluster path. Never print a
  connection string, token, internal key, environment dump, job prompt/context, or secret.
- Direct SQL writes are forbidden except for the explicitly bounded Phase B probe. That
  probe must be enclosed in `BEGIN`/`ROLLBACK`, target one terminal backfilled job, and
  leave no committed state.
- Never delete from `officer_ticket_claims` directly. Claims disappear during cleanup only
  through deletion of the disposable project and its `ON DELETE CASCADE` relationship.
- Do not implement a fix, push, deploy, or alter shared configuration during this run. On
  any failure, preserve sanitized evidence, clean up safe disposable state, and stop.

## Required evidence record

Record the following without secrets:

| Field | Value |
|---|---|
| Started/finished UTC | 2026-08-16 11:19Z / 2026-08-16 11:40Z |
| Kubernetes context / namespace | `main` / `superhuman-remote-worker` (context passed per-call; global current-context left on `k3d-srw`) |
| Deployed orchestrator image digest(s) | **Serving (2 pods):** `sha-2afbf95` → `sha256:88def915…33860`. **Intended (never served):** `sha-20c9154` → `sha256:61c50bf4…3be81` |
| Git/build revision represented by the image | Serving = `2afbf956` (**pre-BP-05**). Intended = `20c91546` (contains `014fff49`). Repo HEAD `d7bcda63` targets `sha-b0fd195`, which has not reached the cluster. |
| Orchestrator pod names and UIDs | `…-6cf5b68d68-cjcd5` `1da1914d-cb64-4933-9407-0df07f002521` (Ready, serving); `…-6cf5b68d68-lhgqc` `853d8cf8-14c9-46a6-8cd6-84ff7235ade8` (Ready, serving); `…-6c84d59f74-jh6pl` `287674a4-9d96-4818-9897-f54ddaff586b` (0/1 CrashLoopBackOff, 8 restarts) |
| Migration 0162 `applied_at` / `execution_ms` | `2026-08-16 11:01:52.175663+00` / `33` ms — **`success=false`**, error recorded |
| Disposable project ID | none — Phase C never authorized or reached |
| Disposable Officer thread ID | none |
| Ticket slug and first/second ready generations | none |
| Claimed job IDs | none |
| Ordinary job ID | none |
| Cleanup verification | N/A — run created zero mutating state; all queries read-only |
| Final verdict | **FAIL — Phase A (artifact + migration health)** |

IDs are acceptable evidence. Do not paste complete database rows, job context, auth
headers, pod environment, or model transcripts containing unrelated project data.

## Phase A — prove the deployed artifact

1. Record `kubectl config current-context`; require exact context `main`.
2. Resolve the namespace and all orchestrator replicas. Record image digests, pod UIDs,
   readiness, restart counts, and rollout completion.
3. Prove every serving orchestrator replica uses the same intended BP-05-capable image.
   The source lineage must contain commit `014fff49` or a descendant carrying the same
   migration/trigger/deletion implementation. Do not infer this from a mutable tag alone.
4. Inspect orchestrator/migration logs for the rollout interval. There must be no dirty
   migration, preflight rejection, lock-timeout retry left unresolved, integrity-trigger
   error, or repeated startup failure.
5. Confirm the existing Officer remains healthy with `auto_pull=false`. This is observation
   only; do not send it a test message or restart it.

**Fail Phase A** for mixed serving images, an incomplete rollout, unknown source lineage,
or unresolved migration/startup errors.

## Phase B — schema, ledger, and historical compatibility

### B1. Migration and catalogue checks

Run equivalent read-only queries against the app database:

```sql
SELECT filename, applied_at, execution_ms, success, error IS NULL AS no_error
  FROM schema_migrations
 WHERE filename = '0162_officer_ticket_claims.sql';

SELECT COUNT(*) AS dirty_migrations
  FROM schema_migrations
 WHERE success IS NOT TRUE;

SELECT to_regclass('public.officer_ticket_claims') IS NOT NULL AS ledger_exists;

SELECT COUNT(*) AS installed_job_triggers
  FROM pg_trigger
 WHERE tgrelid = 'public.jobs'::regclass
   AND NOT tgisinternal
   AND tgname IN (
       'officer_ticket_claim_job_integrity',
       'officer_ticket_claim_job_delete_audit'
   );
```

Required result: one successful 0162 row, zero dirty migrations, the ledger exists, and
both named jobs triggers are installed.

### B2. Ledger completeness checks

```sql
SELECT COUNT(*) AS ticket_jobs_without_claim
  FROM jobs AS job
  LEFT JOIN officer_ticket_claims AS claim ON claim.job_id = job.id
 WHERE COALESCE(job.context, '{}'::jsonb) ? 'ticket_note_id'
   AND claim.job_id IS NULL;

SELECT COUNT(*) AS deleted_claims_without_audit
  FROM officer_ticket_claims AS claim
  LEFT JOIN jobs AS job ON job.id = claim.job_id
 WHERE job.id IS NULL
   AND claim.job_deleted_at IS NULL;

SELECT source, COUNT(*) AS claims
  FROM officer_ticket_claims
 GROUP BY source
 ORDER BY source;
```

Required result: both anomaly counts are zero. Record only aggregate source counts.

### B3. Rolled-back compatibility and integrity probe

First count extant terminal backfilled jobs:

```sql
SELECT COUNT(*) AS probe_candidates
  FROM officer_ticket_claims AS claim
  JOIN jobs AS job ON job.id = claim.job_id
 WHERE claim.source = 'backfill'
   AND claim.job_deleted_at IS NULL
   AND job.status IN ('completed', 'failed', 'cancelled');
```

If the count is zero, record **NOT APPLICABLE — no extant backfilled job**. Do not fabricate
one. The migration had no historical compatibility surface to exercise on this cluster.

If at least one candidate exists, run the following logical probe using one terminal row.
It must execute on one connection and always end in `ROLLBACK`. The harmless merge proves
the authentic source-less pre-0162 stamp is accepted. The nested failed update proves the
same claimed job cannot remove its ticket stamp.

```sql
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '15s';

DO $gate$
DECLARE
    target UUID;
BEGIN
    SELECT job.id
      INTO target
      FROM officer_ticket_claims AS claim
      JOIN jobs AS job ON job.id = claim.job_id
     WHERE claim.source = 'backfill'
       AND claim.job_deleted_at IS NULL
       AND job.status IN ('completed', 'failed', 'cancelled')
     ORDER BY claim.claimed_at, claim.job_id
     LIMIT 1
     FOR UPDATE OF job;

    IF target IS NULL THEN
        RAISE EXCEPTION 'BP-05 live gate has no eligible backfill probe row';
    END IF;

    UPDATE jobs
       SET context = COALESCE(context, '{}'::jsonb)
                     || jsonb_build_object(
                         '_bp05_live_gate_probe',
                         statement_timestamp()::text
                     )
     WHERE id = target;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'BP-05 live gate lost probe job %', target;
    END IF;

    BEGIN
        UPDATE jobs
           SET context = context - 'ticket_note_id'
         WHERE id = target;
        RAISE EXCEPTION 'BP-05 live gate failed: provenance removal succeeded';
    EXCEPTION
        WHEN check_violation THEN
            IF SQLERRM NOT LIKE
               'claimed job % cannot remove its server-owned ticket/admission provenance'
            THEN
                RAISE;
            END IF;
    END;

    RAISE NOTICE 'BP-05 rolled-back trigger probe passed for job %', target;
END
$gate$;

ROLLBACK;
```

Afterward, prove no probe key committed:

```sql
SELECT COUNT(*) AS leaked_probe_keys
  FROM jobs
 WHERE COALESCE(context, '{}'::jsonb) ? '_bp05_live_gate_probe';
```

Required result: zero. If the SQL client disconnects before the explicit rollback, verify
the transaction was aborted and the probe count remains zero before proceeding.

## Phase C — disposable end-to-end claim and deletion flow

### C1. Create the isolated fixture

Through normal authenticated APIs and Officer tools:

1. Create a non-default project named `BP05 Live Gate <UTC timestamp>` owned by the test
   principal.
2. Commission one disposable Officer with one `research` slot:
   `count=1`, `category=researcher`, `backend=sandbox`. Keep `auto_pull=false`.
3. Confirm the durable post links exactly that thread and the effective config still says
   `auto_pull=false`.
4. Through the server-authorized Officer/owner knowledge tool path, create one active
   `feature` ticket with a unique title, minimal harmless content, and tags
   `category:researcher`, `ready`, and `bp05-live-gate`.
5. Read the note back from the same project. Record its slug and database-owned `ready_at`.
   Require a finite non-null timestamp. If a tool reports success but the indexed row is
   absent or unstamped, stop and report a separate KB truth failure; do not repair it by
   SQL.

The ticket should request a tiny bounded result such as returning a fixed sentence and
creating no external side effects. Do not point it at an existing project repository or
cloud folder.

### C2. Claim exactly once through the manual Officer path

1. Instruct the disposable Officer to call its normal `create_job` tool with the exact
   ticket slug and `slot=research`. Do not put claim/admission fields in raw context.
2. Record the returned job ID. Read back only these safe fields:
   `project_id`, `created_by_thread_id`, status, `context.ticket_note_id`,
   `context.officer_slot`, `context.work_category`, and the names—not full values—of the
   `context.officer_admission` fields.
3. Verify exactly one ledger row exists for the project/ticket/ready generation, with
   `source=manual`, the same job ID, Officer thread/incarnation, slot, and category.
4. Ask for the same ticket/slot again without changing `ready`. Require a normal conflict
   (HTTP/tool-level `ticket_claimed`/409), no second claim, and no second job.
5. Cancel the tiny job through the supported job endpoint if it has not already reached a
   terminal state. Require final status `completed`, `failed`, or `cancelled` before the
   deletion test. Infrastructure failure is not an acceptable substitute for a terminal
   worker outcome in this smoke.

### C3. Delete the claimed job and prove the claim survived

Delete the terminal claimed job through `DELETE /api/jobs/{job_id}` as its owner/admin.
Require a successful response equivalent to:

```json
{
  "status": "deleted",
  "ticket_claim_retained": true,
  "ticket_rearmed": false
}
```

Then verify:

- the `jobs` row is absent;
- exactly one ledger row still exists for that job ID;
- `job_deleted_at` is non-null;
- `job_status_at_delete` is the terminal status observed above;
- `deletion_reason` is populated; and
- retrying the unchanged ticket generation still conflicts and creates no job.

Any 500 after the job row disappeared is a hard failure: it reproduces the post-commit
truth bug this checkpoint repaired.

### C4. Prove explicit re-ready is the only re-arm

Through the same authorized knowledge path:

1. Remove `ready`, verify the note is ineligible, then explicitly add `ready` again.
2. Require a new `ready_at` strictly greater than the first generation.
3. Manually claim the ticket once more. Require exactly one new job and one new ledger row
   for the newer generation; the original claim must remain unchanged.
4. A second attempt at the newer generation must conflict.
5. Cancel/finish and delete the second job. Its retained claim response must also be true.

### C5. Ordinary deletion control

Create one tiny ordinary job in the disposable project with no `ticket` argument and no
raw Officer claim keys. Cancel/finish it, then delete it through the same endpoint.

Require:

```json
{
  "status": "deleted",
  "ticket_claim_retained": false,
  "ticket_rearmed": false
}
```

Verify no ledger row exists for the ordinary job ID. This control is necessary: returning
`true` for every successful deletion would not prove truthful claim detection.

## Phase D — logs, cleanup, and final state

1. Search orchestrator logs covering Phases B–C for the disposable IDs and for:
   `officer_ticket_claim_job_integrity`, `ticket-bearing job`, `rolling upgrade`,
   `CheckViolation`, and deletion 500s. The one expected provenance-removal rejection in
   the rolled-back SQL probe is allowed; unexplained occurrences are failures.
2. End/decommission only the disposable Officer.
3. Ensure every disposable job is terminal and physically deleted through the API.
4. Delete the disposable knowledge note/project through supported APIs. Project deletion
   should remove its durable claims through the project FK; never delete claims directly.
5. Verify by aggregate/ID lookup that no disposable project, post, thread, job, note,
   claim, route, wake, credential, pod, repo, or cloud fixture remains.
6. Reconfirm the user's existing Officer is still linked, healthy, and
   `auto_pull=false`. Do not wake it to prove this.

If cleanup cannot complete, the final verdict is **FAIL — cleanup residue**, even when the
behavioral assertions passed. List exact non-secret IDs and the safe remediation needed.

## Acceptance scorecard

| Gate | Required result | Result |
|---|---|---|
| A1 — artifact identity | Uniform intended orchestrator image, rollout complete | **FAIL** — mixed: 2 serving pods on pre-BP-05 `sha-2afbf95`; BP-05 `sha-20c9154` CrashLoopBackOff. Deployment `Progressing=False / ProgressDeadlineExceeded`. |
| A2 — migration health | 0162 successful, zero dirty migrations, two triggers | **FAIL** — 0162 `success=false`; `dirty_migrations=1`; `ledger_exists=false`; `installed_job_triggers=0`. |
| B1 — ledger completeness | Zero ticket jobs without claims | **NOT RUN** — vacuously violated: 7 ticket-bearing jobs exist, ledger table absent, so all 7 lack claims. |
| B2 — deletion audit completeness | Zero absent jobs with unstamped deletion | **NOT RUN** — no ledger table. |
| B3 — historical merge | PASS, or N/A only when candidate count is zero | **NOT RUN** — probe requires the ledger. Not recordable as N/A. |
| B4 — provenance removal | Named check violation; rolled back | **NOT RUN** — integrity trigger not installed. |
| C1 — trusted ticket | Ready/category ticket has server `ready_at` | **NOT RUN** — stopped at Phase A. |
| C2 — first claim | One manual claim and one job | **NOT RUN** |
| C3 — duplicate refusal | Same generation creates nothing | **NOT RUN** |
| C4 — claimed deletion | API true; job gone; audited claim retained | **NOT RUN** |
| C5 — unchanged retry | Still refused after physical deletion | **NOT RUN** |
| C6 — explicit re-ready | Newer generation wins exactly once | **NOT RUN** |
| C7 — ordinary control | API false; no ledger claim | **NOT RUN** |
| D1 — logs | No unexplained integrity/deletion errors | **FAIL (explained)** — the only orchestrator error is the repeated 0162 preflight abort; it is the root cause, not an unexplained one. No `CheckViolation` or deletion 500s observed. |
| D2 — cleanup | No disposable residue | **PASS (vacuous)** — zero mutating operations performed. |
| D3 — existing Officer | Unchanged, healthy, auto-pull off | **PASS** — post `a572e4a0…` / thread `6ce5bc4c…` still linked; `config_override.auto_pull` unset (default false). Observed only; never woken, messaged, or edited. |

## Final verdict rules

- **PASS:** every required row passes; B3 may be N/A only with recorded zero candidates.
- **BLOCKED:** the deployed identity or authorization needed to create a trusted disposable
  ticket cannot be established before any behavioral mutation. Do not improvise around it.
- **FAIL:** any assertion differs, an API reports success after contradictory persistence,
  deletion returns 500 after succeeding, a duplicate job/claim appears, or cleanup leaves
  residue.

On PASS, update the BP-05 deployment wording in
`docs/done/deleting_a_job_releases_its_backlog_ticket_claim.md` and
`docs/issues/officer_control_plane_post_implementation_audit.md` with the deployed revision,
UTC window, sanitized IDs, and scorecard result. Do not change the umbrella verdict: the
remaining auto-pull blockers are outside this gate.

## Run 1 result (2026-08-16)

**Verdict: FAIL — Phase A.** Stopped before any mutation, per the authority rules. No
disposable fixture was created, so there is no cleanup residue. The user's existing Officer
was observed only.

### What happened

Migration `0162_officer_ticket_claims.sql` ran on main dev at `11:01:52Z` and aborted in
its strict backfill preflight:

```text
dirty migration '0162_officer_ticket_claims.sql': BP-05 preflight rejected ticket job
660a8eec-…-3f72b526eef5: officer_admission is missing or malformed; manual repair required
```

The migration is fully transactional (`BEGIN` line 18 / `COMMIT` line 445), so it rolled
back cleanly — `officer_ticket_claims` does not exist and neither jobs trigger is
installed. It left a `success=false` row in `schema_migrations`, which makes the BP-05
replica CrashLoop at startup. The two pre-BP-05 replicas (`sha-2afbf95`) started at ~08:00Z,
before the migration attempt, and are still serving normally (`/api/health` → 200).

### Root cause — the backfill cannot accept any genuine pre-0162 job

The preflight requires `context.officer_admission` to be an object carrying a finite
`ticket_ready_at`, and explicitly refuses a `job.created_at` fallback. The later code audit
clarified the history: pre-0162 automatic tick admission could stamp `ticket_ready_at`, but
manual `create_job(ticket=...)` did not, and `auto_pull` was disabled on main dev. The
observed manual/live-fire history therefore has no trusted generation even though that
field existed in one dormant code path.

Classifying all 7 ticket-bearing jobs on main dev against the preflight:

| Jobs | Shape | Preflight outcome |
|---|---|---|
| 6 (`660a8eec`, `a9a468df`, `05c18dc9`, `9cf42783`, `2fbe1f99`, `fcda6532`) | `ticket_note_id` + `officer_slot` + `work_category`, **no `officer_admission` key at all** | rejected: `officer_admission is missing or malformed` |
| 1 (`c4849fa1`) | has `officer_admission` with `project_id, thread_id, slot, category, incarnation, lineage_size, config_fingerprint` — **no `ticket_ready_at`** | rejected: `ticket_ready_at is missing or not finite` |

So **0 of 7** historical jobs are backfillable. Repairing job `660a8eec` alone just moves
the abort to the next row; the migration can never complete on this cluster as written.

This is the gap between the done document's acceptance evidence and production reality: the
real-PostgreSQL test constructs a "pre-migration" job that already carries a complete
admission stamp including `ticket_ready_at`. Actual pre-0162 history has no such stamp, so
the tested backfill path does not exist in the field.

### Operational risk — both images now refuse to start

`run_migrations()` reads `schema_migrations WHERE success = FALSE` **before** it compares
anything to disk, and raises on the first row not listed in `NOTX_RECOVERIES`
(which contains only `0132_jobs_verification_uniq.notx.sql`). The old image `2afbf956`
ships migrations only through `0161` but runs the identical gate, so it hits the same dirty
row.

Consequence: the two serving pods survive only because they started before `11:01:52Z`.
Any eviction, node drain, restart, or Reloader bounce takes the main-dev orchestrator
fully down until the dirty row is cleared. This is the most urgent item from this run.

### Remediation and local repair status

Per `docs/db_migration.md` §Operational runbook, `0162` has no recovery recipe, so the
sequence is: fix the root cause, then clear the flag, then redeploy.

1. **Restore restart safety first** (unblocks the outage risk):
   `DELETE FROM schema_migrations WHERE filename = '0162_officer_ticket_claims.sql' AND success = FALSE;`
   Nothing was applied, so this deletes a flag, not schema. Pin the deployment back to
   `sha-2afbf95` until step 2 lands, otherwise the new replica immediately re-dirties it.
2. **Fix 0162's backfill to accept the real pre-0162 shape.** This is complete locally.
   Genuine history with no provable generation becomes `source=legacy_unversioned`, with
   `ready_generation_at=NULL` and the database cutover timestamp as a fail-closed re-arm
   barrier. No timestamp or admission provenance is copied into the seven jobs or guessed
   from `jobs.created_at`.
3. Re-run this gate from Phase A once a corrected migration is deployed.

Deleting the 7 historical jobs would also unblock the migration, but they are the O6
live-fire evidence set; that is a data-loss decision for the operator, not a repair.

### Local repair checkpoint

The corrected 0162 checksum is
`3383746fe9a488b39ca4964744981e4e9e8863afbe969fe05ed22b1da6aa675d`; the failed deployed
version was `fedfd5b94a34bb4d8eec1337f5db32b09561650a1e4fc86fec9008456c5932e1`.
Local PostgreSQL evidence on 2026-08-16:

- the exact six stamp-less plus one partial-stamp population migrates successfully;
- all seven rows have NULL generation/incarnation/fingerprint/lineage authority and one
  server cutover barrier;
- equal/older readiness remains consumed, strict post-cutover re-ready wins exactly once,
  and deleted non-terminal legacy work remains blocking;
- the complete transaction file passed 66 tests, the expanded Officer checkpoint passed
  547, and migration discovery/head/replay passed 34; and
- the app chain replayed from zero through all 136 transactional migrations and regenerated
  `schema_current.sql`.

Before clearing the dirty row, update the GitOps source of truth back to the serving
pre-BP-05 image and wait until the crash-looping old-0162 replica is gone; otherwise it can
immediately recreate the failure. Then clear only the known `success=FALSE` 0162 row,
deploy the corrected image, and restart this runbook at Phase A. Do not deploy this edited
migration to any database that reports `success=TRUE` for the old checksum: the immutable
migration guard will correctly reject checksum drift and that environment needs an
explicit forward-repair plan.

### Not closed

BP-05's deployment checkpoint stays **open**. The local implementation is repaired, but
main dev still serves pre-BP-05 code and retains the failed migration row until an operator
performs the recovery above. BP-01, BP-06, BP-07, BP-08, BP-10, BP-11 and ES-01 are
untouched by this run. `auto_pull` remains false everywhere.

## Dedicated-agent handoff

The agent executing this document should:

1. read `AGENTS.md`, this file, the BP-05 done document, and the umbrella audit;
2. show the operator a concise preflight plan and exact disposable scope before Phase C;
3. run Phases A–D in order, stopping at the first hard failure;
4. update only this result table and the two deployment-evidence documents on PASS;
5. preserve unrelated worktree changes and all existing Officer/project state;
6. run `git diff --check` after documentation updates; and
7. report the final scorecard, cleanup proof, remaining blockers, and whether any file is
   modified—but do not commit, push, deploy, or enable auto-pull.
