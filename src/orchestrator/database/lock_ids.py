"""Central registry of Postgres advisory-lock IDs (packed-ASCII int64).

Keep every advisory-lock key here so they can never collide. Existing keys
live with their subsystems and are mirrored here for the collision audit:
  LOCK_ID       = 0x5352575F4D4947   # "SRW_MIG"  (database/migrate.py, xact-scoped)
  MAINT_LOCK_ID = 0x5352575F41554454 # "SRW_AUDT" (services/audit_partitions.py, xact-scoped)
  bench sweep   = hashtext('bench_sweep')  # int4, server-computed
                  # (services/bench.py BENCH_SWEEP_LOCK, session-scoped per
                  # sweeper tick; int4 range cannot collide with the packed-
                  # ASCII int64 keys above)

Session-scoped locks (held for a task's lifetime) go here too.
"""

# Session-scoped leadership lock — services/leader_election.py.
# "SRW_LEAD" packed into int64. Distinct from LOCK_ID / MAINT_LOCK_ID.
LEADER_ID = 0x5352575F4C454144

# Session-scoped run-queue reaper lock — the leader-gated per-row lease reaper
# over run_queue (src/shared/run_queue/, wired by the stateless-agents S1
# control plane). "SRW_REAP" packed into int64. The reaper's per-row CAS steal
# is safe without the lock (documented dual-leader window); the lock only
# avoids duplicate sweep work.
RUN_QUEUE_REAPER_ID = 0x5352575F52454150
