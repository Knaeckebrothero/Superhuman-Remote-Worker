"""Explicit compatibility and recovery contracts for historical migrations.

Successful migration history normally requires a byte-exact file checksum.
A published edit can exceptionally have the same durable effects as the
original file. Such compatibility must pin the filename and both exact hashes;
it never rewrites the ledger, replays applied SQL, or accepts a failed row.

Non-transactional DDL can commit its physical side effect before the migration
ledger is updated, or leave an unusable catalog object behind when it fails.
Most such failures still need operator judgement.  The small registry below is
only for migrations whose cleanup, replay prerequisite, and final catalog shape
have all been reviewed as safe to retry automatically.

Keeping these contracts outside the immutable SQL files lets an already-applied
migration gain a recovery path without changing its checksum.  It also avoids
trying to infer safety by parsing arbitrary SQL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppliedChecksumCompatibility:
    """One reviewed historical checksum for an exact canonical SQL file."""

    canonical_checksum: str
    historical_checksum: str


# 097b414f2 inserted only comments and a session-local pgvector input cast into
# already-applied vector 0025. Its CREATE FUNCTION bytes and durable effects are
# identical to the original from 734250425 (also shipped in 99169725a). Restore
# the original file; migrate._load_installed_pgvector already loads the library
# on cold upgrade connections. Databases that successfully applied the edited
# artifact retain their actual historical checksum, timestamps, and attribution.
# This exception is directional: only the original exact bytes are canonical.
APPLIED_CHECKSUM_COMPATIBILITIES: dict[str, AppliedChecksumCompatibility] = {
    "0025_knowledge_multi_angle_search.sql": AppliedChecksumCompatibility(
        canonical_checksum=(
            "fc9395f5ca2fd60a48932d8038e92238124ae23d7174d558d22a89a83d791ab8"
        ),
        historical_checksum=(
            "915246808c5714610aeb98faac61d96b5a2a72a81cba26677ba2d9b636325424"
        ),
    )
}


@dataclass(frozen=True)
class ConcurrentIndexRecovery:
    """Reviewed recovery recipe for one ``CREATE INDEX CONCURRENTLY`` file."""

    cleanup_filename: str
    replay_filename: str
    index_name: str
    table_name: str
    access_method: str
    key_definitions: tuple[str, ...]
    predicate: str


# 0130 is deliberately replay-safe: it deterministically retires duplicate
# losers and asserts that none remain.  0131 is deliberately replay-safe: its
# exact DROP INDEX CONCURRENTLY removes either an INVALID shell or an unexpected
# same-name shape.  Those reviewed properties are what make 0132 recoverable;
# do not add an entry here merely because a migration happens to create an
# index concurrently.
NOTX_RECOVERIES: dict[str, ConcurrentIndexRecovery] = {
    "0132_jobs_verification_uniq.notx.sql": ConcurrentIndexRecovery(
        cleanup_filename="0131_drop_jobs_verification_uniq.notx.sql",
        replay_filename="0130_jobs_verification_dedupe.sql",
        index_name="jobs_verification_uniq",
        table_name="jobs",
        access_method="btree",
        key_definitions=(
            "parent_job_id",
            "(context ->> 'verification_round'::text)",
        ),
        predicate=(
            "(((context ->> 'verification_target'::text) IS NOT NULL) "
            "AND jsonb_exists(context, 'verification_round'::text))"
        ),
    )
}
