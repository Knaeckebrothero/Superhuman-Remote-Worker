"""Install rows the previous release wrote, the way the migrations met them.

Migrations 0195-0198 fence a *writer* that publishes Kubernetes or pinned
runtime authority with no durable reservation, protection or receipt behind
it.  Rows written before those migrations were never subject to that fence:
the triggers did not exist when the rows were written.  A real-PostgreSQL
proof about what happens *above* such a row therefore has to reproduce that
ordering -- seed with the exact named triggers absent, then put them back,
which is also the proof that installing the fence over historical data is
accepted.

This is deliberately not ``SET session_replication_role = replica``.  That
switch silently drops foreign keys and *every* other trigger for the whole
statement, including the ones a given test is actually about, so a fence that
stopped firing would read as a passing test.  Naming each trigger keeps the
bypass visible, minimal and reviewable, and keeps every other constraint live.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

# 0196 owner-envelope + creation-reservation authority, and the 0191/0195
# process-zero transition fence they build on.
JOB_RUNTIME_AUTHORITY_TRIGGERS = (
    "trg_jobs_require_workspace_creation_reservation_on_insert",
    "trg_jobs_d_validate_workspace_authority_envelope",
    "trg_jobs_enforce_managed_repository_process_zero",
)
THREAD_RUNTIME_AUTHORITY_TRIGGERS = (
    "trg_threads_require_workspace_creation_reservation_on_insert",
    "trg_threads_d_validate_workspace_authority_envelope",
    "trg_threads_enforce_managed_repository_process_zero",
)
# 0198's planned -> protected pinned bind edge.
PINNED_BINDING_AUTHORITY_TRIGGERS = ("zzz_threads_pinned_warm_binding_authority",)

_DEFAULT = {
    "jobs": JOB_RUNTIME_AUTHORITY_TRIGGERS,
    "threads": THREAD_RUNTIME_AUTHORITY_TRIGGERS,
}


@asynccontextmanager
async def previous_release_writer(conn, table: str, *extra_triggers: str):
    """Run a block with only the named post-tranche fences on ``table`` off."""

    triggers = tuple(_DEFAULT[table]) + tuple(extra_triggers)
    for trigger in triggers:
        await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    try:
        yield conn
    finally:
        for trigger in triggers:
            await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


async def seed_previous_release_row(conn, table: str, query: str, *args):
    """Execute one statement as a writer that predates the tranche fences."""

    async with previous_release_writer(conn, table):
        return await conn.execute(query, *args)
