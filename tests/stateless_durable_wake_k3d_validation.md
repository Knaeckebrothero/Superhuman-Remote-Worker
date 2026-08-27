# Stateless durable-wake k3d acceptance gate

Status: executable rollout gate; no local-cluster run is claimed by this note.

This gate proves that a truthful `role=event` session wake survives the
stateless execution lane. It exercises the production jobs outbox,
`thread_input_deliveries`, `run_queue`, and stateless executor rather than
posting a human-shaped substitute. It is deliberately destructive only to its
own disposable database rows and to exact idle/fixture-owning stateless Pod
UIDs.

## Preconditions

- The current `kubectl` context must be exactly `k3d-srw`; the namespace is
  exactly `srw`. The wrapper refuses every other current context even though it
  also passes `--context=k3d-srw` on each call.
- The final integration image must be deployed to every orchestrator and
  stateless executor. Both Deployments must have one observed generation,
  every desired replica updated/Ready/available, no extra old or terminating
  selected Pod, and one actual image digest per component. The wrapper checks
  symbols inside every running container. A successful rollout or tag alone is
  not evidence.
- Migrations `0189_stateless_input_deliveries.sql`,
  `0190_stateless_input_delivery_validate.sql`,
  `0195_non_pinned_workspace_process_zero.sql`, and
  `0196_non_pinned_workspace_lifecycle_authority.sql` must be successful, with
  all constraints validated and trigger fences present. The migration ledger
  must have no `success=false` row. Later additive migrations are allowed.
- At least two stateless executor Pods must be Ready. No unrelated queued or
  leased `run_queue` work may exist during either fault injection. Run this on
  an otherwise idle disposable k3d cluster.
- Supply the UUID of an existing approved local test user. The gate does not
  create, alter, print, or delete identity-provider state.
- `WORKSPACE_CLEANUP_RECONCILIATION_ENABLED`,
  `WORKSPACE_REATTACH_FRESH_FALLBACK`, and
  `OFFICER_AUTO_PULL_RELEASE_ENABLED` must all be the literal `false` in
  `srw-config` and the running orchestrators. `auto_pull` must be false in
  every durable Post and thread mirror. The operator checks database state
  before and after the run and never writes an Officer Post.
- The configured model for `session_base` must be reachable. This is an
  execution gate and deliberately spends the small bounded set of fixture
  turns.

## Step 0: read-only preflight

From the repository root:

```bash
scripts/stateless-durable-wake-k3d-gate.sh
```

The wrapper prints one `artifact-pass` line per running orchestrator, including
the real container image ID, then a JSON `preflight` record. It prints no DSN,
token, prompt body, message content, repository coordinate, or Secret value.

## Execute

Choose a new lowercase run ID (8-48 characters) and run:

```bash
scripts/stateless-durable-wake-k3d-gate.sh \
  --execute \
  --run-id wake-gate-20260826-001 \
  --owner-user-id '<approved-local-user-uuid>' \
  --timeout-seconds 600 \
  --confirm k3d-srw-disposable-stateless-wake
```

Mutation requires all of `--execute`, the exact confirmation phrase, the run
ID, the approved owner, the wrapper's environment attestation, and the real
kube-context check. The module itself defaults to read-only inspection.

The scorecard must prove:

1. `warm_fifo`: a human input committed before a wake is answered first; the
   event retains `role=event` and executes on the warm executor.
2. `fresh_attach`: after exact-UID deletion of the idle warm executor, the same
   thread is restored by another process and the event executes once.
3. `lease_handoff`: an exact claimant is deleted after its lease but before
   provider admission; a different Pod UID and higher queue lease/delivery
   claim generation settle the same delivery identity once.
4. `lost_response_historical_lane`: execution commits while the jobs outbox is
   intentionally left `sending`; after a `stateless -> pinned` lane change, the
   retry recognizes the immutable terminal receipt, creates no queue/message/
   delivery row, and marks the outbox `sent` exactly once.
5. `scorecard`: every fixture delivery is `settled`, stamped `stateless`, has
   source `officer_wake`, joins exactly one `role=event` transcript row, and all
   fixture outboxes are `sent`.
6. `cleanup`: zero selected threads, jobs, queue rows, deliveries, messages,
   Pods, and PVCs remain; `auto_pull_enabled` is zero.

The gate always runs cleanup in `finally`, including after a failed assertion.
It uses the normal job deletion helper and supported thread End funnel. Its only
direct lane update is the intentionally bounded historical-terminal-replay
seam; the migration trigger refuses that change while any delivery is pending.

## Interrupted-run cleanup

If the operator process itself is killed before `finally`, rerun only the exact
run ID:

```bash
scripts/stateless-durable-wake-k3d-gate.sh \
  --cleanup-only \
  --run-id wake-gate-20260826-001 \
  --confirm k3d-srw-disposable-stateless-wake
```

Cleanup finds fixtures only by the unique server-side JSON marker. It refuses
an ambiguous run ID and does not search by title, partial UUID, user, or age.
Do not use SQL deletion, transcript surgery, deployment scaling, or broad
Kubernetes selectors as a substitute.

## Stop conditions

Stop and preserve the JSON evidence if any artifact, migration, trigger,
constraint, approved-owner, capacity, quiet-pool, lane, lease, exact-once,
outbox, cleanup, or auto-pull assertion fails. Do not weaken a fence or retry
with a reused run ID until `--cleanup-only` returns a zero-residue record.
