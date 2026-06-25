"""Central registry of Postgres advisory-lock IDs (packed-ASCII int64).

Keep every advisory-lock key here so they can never collide. Existing keys
live with their subsystems and are mirrored here for the collision audit:
  LOCK_ID       = 0x5352575F4D4947   # "SRW_MIG"  (database/migrate.py, xact-scoped)
  MAINT_LOCK_ID = 0x5352575F41554454 # "SRW_AUDT" (services/audit_partitions.py, xact-scoped)

Session-scoped locks (held for a task's lifetime) go here too.
"""

# Session-scoped leadership lock — services/leader_election.py.
# "SRW_LEAD" packed into int64. Distinct from LOCK_ID / MAINT_LOCK_ID.
LEADER_ID = 0x5352575F4C454144
