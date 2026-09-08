"""Durable cloud push-generation contract for stateless session handoff.

The database records what generation *must* exist. The cloud mount itself
records what generation actually committed after its bytes. A successor reads
both before pull: an older/missing resource marker replays only the paths that
differ from the durable turn-start content baseline; a marker ahead of the
database fails closed.

Only the database half is enforced. These statements combine the mutation
with a live ``run_queue`` lease check, so a stale cooperative executor cannot
reserve or acknowledge state. The resource marker is intentionally classified
separately in ``agent.services.cloud_sync``: it is stored in user-writable cloud
space and is therefore cooperative (though deletion/rollback is detected), not
a security boundary.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import UUID


MAX_BASELINE_ENTRIES = 10_000
MAX_BASELINE_JSON_BYTES = 4 * 1024 * 1024
EMPTY_BASELINE_SHA256 = hashlib.sha256(b"{}").hexdigest()


def normalize_cloud_sync_baseline(value: Any) -> dict[str, dict[str, str]]:
    """Validate and canonicalize one turn-start content baseline.

    The baseline is intentionally content-based rather than size/mtime based:
    a fresh successor must distinguish a same-length agent edit from an
    untouched file without uploading every workspace file and clobbering
    unrelated cloud-side edits. ``remote_etag`` may be empty when a WebDAV
    server listed the remote path without an ETag. Remote presence is
    represented by the manifest entry itself, independently of whether the
    server supplied an ETag.
    """

    if not isinstance(value, Mapping):
        raise ValueError("cloud sync baseline must be an object")
    if len(value) > MAX_BASELINE_ENTRIES:
        raise ValueError("cloud sync baseline has too many entries")
    normalized: dict[str, dict[str, str]] = {}
    for raw_path, raw_entry in value.items():
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ValueError("cloud sync baseline path must be a non-empty string")
        path = raw_path.replace("\\", "/").strip("/")
        if (
            not path
            or path != raw_path
            or posixpath.normpath(path) != path
            or path == ".."
            or path.startswith("../")
        ):
            raise ValueError(f"invalid cloud sync baseline path: {raw_path!r}")
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"cloud sync baseline entry for {path} must be an object")
        digest = raw_entry.get("sha256")
        remote_etag = raw_entry.get("remote_etag", "")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError(f"cloud sync baseline digest is invalid for {path}")
        if not isinstance(remote_etag, str) or len(remote_etag) > 4096:
            raise ValueError(f"cloud sync baseline etag is invalid for {path}")
        normalized[path] = {"sha256": digest, "remote_etag": remote_etag}
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(encoded) > MAX_BASELINE_JSON_BYTES:
        raise ValueError("cloud sync baseline exceeds the durable size limit")
    return dict(sorted(normalized.items()))


def encode_cloud_sync_baseline(
    value: Any,
) -> tuple[dict[str, dict[str, str]], str, str]:
    """Return normalized object, canonical JSON and its SHA-256 digest."""

    normalized = normalize_cloud_sync_baseline(value)
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return normalized, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CloudSyncRequirement:
    mount_id: str
    required_generation: int
    acknowledged_generation: int
    required_lease_token: int
    workspace_generation: str
    sync_scope_sha256: str
    baseline_manifest: dict[str, dict[str, str]] = field(default_factory=dict)
    baseline_sha256: str = EMPTY_BASELINE_SHA256
    # Commit-then-effects (stateless_turn_resilience.md step 4a): the fence of
    # the push once the run_queue unit has completed, and the durable per-file
    # progress a successor resumes from. Both default to "no hand-off yet".
    push_owner_token: int = 0
    push_progress: dict[str, Any] = field(
        default_factory=lambda: {"planned": None, "files": {}}
    )


@dataclass(frozen=True)
class CloudSyncScope:
    mount_id: str
    workspace_generation: str
    sync_scope_sha256: str
    baseline_manifest: dict[str, dict[str, str]] = field(default_factory=dict)
    baseline_sha256: str = EMPTY_BASELINE_SHA256


_RESERVE_SQL = """
WITH owner AS (
    SELECT queue.unit_id
    FROM run_queue AS queue
    JOIN threads AS thread ON thread.id = queue.unit_id
    WHERE queue.unit_id = $1::uuid
      AND queue.lease_token = $2::bigint
      AND queue.state = 'leased'
      AND thread.metadata -> '_workspace_binding' ->> 'generation' = $8::text
      AND (
          thread.metadata -> '_workspace_binding' ->> 'kind' = 'virtual'
          OR (
              thread.metadata -> '_workspace_binding' ->> 'kind' = 'remote'
              AND thread.metadata -> 'workspace_container'
                      ->> '_canvas_workspace_generation' = $8::text
              AND thread.metadata -> 'workspace_container' ->> 'status' = 'ready'
          )
      )
    FOR SHARE
), requested AS (
    SELECT DISTINCT mount_id, workspace_generation, sync_scope_sha256,
                    baseline_manifest_json, baseline_sha256
    FROM unnest($3::text[], $4::text[], $5::text[], $6::text[], $7::text[])
        AS requested_mounts(
            mount_id,
            workspace_generation,
            sync_scope_sha256,
            baseline_manifest_json,
            baseline_sha256
        )
    WHERE mount_id <> ''
), reserved AS (
    INSERT INTO thread_cloud_sync_generations (
        thread_id,
        mount_id,
        required_generation,
        acknowledged_generation,
        required_lease_token,
        workspace_generation,
        sync_scope_sha256,
        baseline_manifest,
        baseline_sha256,
        required_at
    )
    SELECT owner.unit_id,
           requested.mount_id,
           $2::bigint,
           0,
           $2::bigint,
           requested.workspace_generation,
           requested.sync_scope_sha256,
           requested.baseline_manifest_json::jsonb,
           requested.baseline_sha256,
           now()
    FROM owner CROSS JOIN requested
    ON CONFLICT (thread_id, mount_id) DO UPDATE SET
        required_generation = EXCLUDED.required_generation,
        required_lease_token = EXCLUDED.required_lease_token,
        workspace_generation = EXCLUDED.workspace_generation,
        sync_scope_sha256 = EXCLUDED.sync_scope_sha256,
        baseline_manifest = EXCLUDED.baseline_manifest,
        baseline_sha256 = EXCLUDED.baseline_sha256,
        required_at = now(),
        -- A new generation starts with no push owner and no progress; the
        -- token itself stays monotonic so an old owner can never match it.
        push_progress = '{}'::jsonb,
        push_owner_pod = NULL,
        push_owner_pod_uid = NULL,
        push_heartbeat_at = NULL,
        push_started_at = NULL,
        push_failed_at = NULL,
        push_error = NULL
    WHERE thread_cloud_sync_generations.acknowledged_generation =
              thread_cloud_sync_generations.required_generation
      AND thread_cloud_sync_generations.required_generation <=
              EXCLUDED.required_generation
    RETURNING mount_id, required_generation, acknowledged_generation,
              required_lease_token, workspace_generation, sync_scope_sha256,
              baseline_manifest, baseline_sha256, push_owner_token,
              push_progress
)
SELECT mount_id, required_generation, acknowledged_generation,
       required_lease_token, workspace_generation, sync_scope_sha256,
       baseline_manifest, baseline_sha256, push_owner_token, push_progress
FROM reserved
ORDER BY mount_id
"""

_LOAD_SQL = """
WITH owner AS (
    SELECT queue.unit_id
    FROM run_queue AS queue
    JOIN threads AS thread ON thread.id = queue.unit_id
    WHERE queue.unit_id = $1::uuid
      AND queue.lease_token = $2::bigint
      AND queue.state = 'leased'
      AND thread.metadata -> '_workspace_binding' ->> 'generation' = $3::text
      AND (
          thread.metadata -> '_workspace_binding' ->> 'kind' = 'virtual'
          OR (
              thread.metadata -> '_workspace_binding' ->> 'kind' = 'remote'
              AND thread.metadata -> 'workspace_container'
                      ->> '_canvas_workspace_generation' = $3::text
              AND thread.metadata -> 'workspace_container' ->> 'status' = 'ready'
          )
      )
)
SELECT generation.mount_id,
       generation.required_generation,
       generation.acknowledged_generation,
       generation.required_lease_token,
       generation.workspace_generation,
       generation.sync_scope_sha256,
       generation.baseline_manifest,
       generation.baseline_sha256,
       generation.push_owner_token,
       generation.push_progress
FROM thread_cloud_sync_generations AS generation
JOIN owner ON owner.unit_id = generation.thread_id
ORDER BY generation.mount_id
"""

_ACK_SQL = """
WITH owner AS (
    SELECT queue.unit_id
    FROM run_queue AS queue
    JOIN threads AS thread ON thread.id = queue.unit_id
    WHERE queue.unit_id = $1::uuid
      AND queue.lease_token = $2::bigint
      AND queue.state = 'leased'
      AND thread.metadata -> '_workspace_binding' ->> 'generation' = $5::text
      AND (
          thread.metadata -> '_workspace_binding' ->> 'kind' = 'virtual'
          OR (
              thread.metadata -> '_workspace_binding' ->> 'kind' = 'remote'
              AND thread.metadata -> 'workspace_container'
                      ->> '_canvas_workspace_generation' = $5::text
              AND thread.metadata -> 'workspace_container' ->> 'status' = 'ready'
          )
      )
    FOR SHARE
)
UPDATE thread_cloud_sync_generations AS generation
SET acknowledged_generation = GREATEST(
        generation.acknowledged_generation,
        $4::bigint
    ),
    acknowledged_at = now()
FROM owner
WHERE generation.thread_id = owner.unit_id
  AND generation.mount_id = $3::text
  AND generation.required_generation = $4::bigint
  AND generation.workspace_generation = $5::text
  AND generation.sync_scope_sha256 = $6::text
  AND generation.baseline_sha256 = $7::text
RETURNING generation.acknowledged_generation
"""

_CURRENT_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM run_queue AS queue
    JOIN threads AS thread ON thread.id = queue.unit_id
    WHERE queue.unit_id = $1::uuid
      AND queue.lease_token = $2::bigint
      AND queue.state = 'leased'
      AND thread.metadata -> '_workspace_binding' ->> 'generation' = $3::text
      AND (
          thread.metadata -> '_workspace_binding' ->> 'kind' = 'virtual'
          OR (
              thread.metadata -> '_workspace_binding' ->> 'kind' = 'remote'
              AND thread.metadata -> 'workspace_container'
                      ->> '_canvas_workspace_generation' = $3::text
              AND thread.metadata -> 'workspace_container' ->> 'status' = 'ready'
          )
      )
)
"""


# ---------------------------------------------------------------------------
# Commit-then-effects (stateless_turn_resilience.md step 4a). Once the unit has
# completed, the push's fence is no longer the run_queue lease but the row's
# ``push_owner_token``. Every statement below either requires the still-live
# lease (hand-off, adoption) or the exact push token (progress, heartbeat,
# failure, the push-fenced acknowledgement, the writer check).
# ---------------------------------------------------------------------------

_WORKSPACE_BOUND_SQL = """
      thread.metadata -> '_workspace_binding' ->> 'generation' = {param}::text
      AND (
          thread.metadata -> '_workspace_binding' ->> 'kind' = 'virtual'
          OR (
              thread.metadata -> '_workspace_binding' ->> 'kind' = 'remote'
              AND thread.metadata -> 'workspace_container'
                      ->> '_canvas_workspace_generation' = {param}::text
              AND thread.metadata -> 'workspace_container' ->> 'status' = 'ready'
          )
      )
"""

_CURRENT_PUSH_SQL = (
    """
SELECT EXISTS (
    SELECT 1
    FROM thread_cloud_sync_generations AS generation
    JOIN threads AS thread ON thread.id = generation.thread_id
    WHERE generation.thread_id = $1::uuid
      AND generation.push_owner_token = $2::bigint
      AND generation.acknowledged_generation < generation.required_generation
      AND """
    + _WORKSPACE_BOUND_SQL.format(param="$3")
    + """
) AND NOT EXISTS (
    SELECT 1
    FROM thread_cloud_sync_generations AS other
    WHERE other.thread_id = $1::uuid
      AND other.acknowledged_generation < other.required_generation
      AND other.push_owner_token <> $2::bigint
)
"""
)

_HAND_OFF_SQL = (
    """
WITH owner AS (
    SELECT queue.unit_id
    FROM run_queue AS queue
    JOIN threads AS thread ON thread.id = queue.unit_id
    WHERE queue.unit_id = $1::uuid
      AND queue.lease_token = $2::bigint
      AND queue.state = 'leased'
      AND """
    + _WORKSPACE_BOUND_SQL.format(param="$3")
    + """
    FOR SHARE
), next_token AS (
    SELECT COALESCE(MAX(generation.push_owner_token), 0) + 1 AS token
    FROM thread_cloud_sync_generations AS generation
    WHERE generation.thread_id = $1::uuid
)
UPDATE thread_cloud_sync_generations AS generation
SET push_owner_token = next_token.token,
    push_owner_pod = $4::text,
    push_owner_pod_uid = $5::text,
    push_heartbeat_at = now(),
    push_started_at = COALESCE(generation.push_started_at, now()),
    push_failed_at = NULL,
    push_error = NULL
FROM owner, next_token
WHERE generation.thread_id = owner.unit_id
  AND generation.required_lease_token = $2::bigint
  AND generation.acknowledged_generation < generation.required_generation
RETURNING generation.mount_id, generation.push_owner_token
"""
)

_ADOPT_SQL = (
    """
WITH owner AS (
    SELECT queue.unit_id
    FROM run_queue AS queue
    JOIN threads AS thread ON thread.id = queue.unit_id
    WHERE queue.unit_id = $1::uuid
      AND queue.lease_token = $2::bigint
      AND queue.state = 'leased'
      AND """
    + _WORKSPACE_BOUND_SQL.format(param="$3")
    + """
    FOR SHARE
), next_token AS (
    SELECT COALESCE(MAX(generation.push_owner_token), 0) + 1 AS token
    FROM thread_cloud_sync_generations AS generation
    WHERE generation.thread_id = $1::uuid
)
UPDATE thread_cloud_sync_generations AS generation
SET push_owner_token = next_token.token,
    push_owner_pod = $4::text,
    push_owner_pod_uid = $5::text,
    push_heartbeat_at = now()
FROM owner, next_token
WHERE generation.thread_id = owner.unit_id
  AND generation.acknowledged_generation < generation.required_generation
RETURNING generation.mount_id, generation.push_owner_token,
          generation.push_progress
"""
)

_PROGRESS_SQL = """
UPDATE thread_cloud_sync_generations AS generation
SET push_progress = jsonb_set(
        jsonb_set(
            COALESCE(generation.push_progress, '{}'::jsonb),
            '{files}',
            COALESCE(generation.push_progress -> 'files', '{}'::jsonb)
                || $4::jsonb,
            true
        ),
        '{planned}',
        COALESCE($5::jsonb, generation.push_progress -> 'planned', 'null'::jsonb),
        true
    ),
    push_heartbeat_at = now()
WHERE generation.thread_id = $1::uuid
  AND generation.mount_id = $2::text
  AND generation.push_owner_token = $3::bigint
  AND generation.acknowledged_generation < generation.required_generation
RETURNING 1
"""

_HEARTBEAT_PUSH_SQL = """
UPDATE thread_cloud_sync_generations AS generation
SET push_heartbeat_at = now()
WHERE generation.thread_id = $1::uuid
  AND generation.push_owner_token = $2::bigint
  AND generation.acknowledged_generation < generation.required_generation
RETURNING generation.mount_id
"""

_PUSH_FAILED_SQL = """
UPDATE thread_cloud_sync_generations AS generation
SET push_failed_at = now(),
    push_error = $3::text
WHERE generation.thread_id = $1::uuid
  AND generation.push_owner_token = $2::bigint
  AND generation.acknowledged_generation < generation.required_generation
RETURNING generation.mount_id
"""

_ACK_PUSH_SQL = (
    """
WITH owner AS (
    SELECT generation.thread_id AS unit_id
    FROM thread_cloud_sync_generations AS generation
    JOIN threads AS thread ON thread.id = generation.thread_id
    WHERE generation.thread_id = $1::uuid
      AND generation.mount_id = $3::text
      AND generation.push_owner_token = $2::bigint
      AND generation.acknowledged_generation < generation.required_generation
      AND """
    + _WORKSPACE_BOUND_SQL.format(param="$5")
    + """
    FOR UPDATE OF generation
)
UPDATE thread_cloud_sync_generations AS generation
SET acknowledged_generation = GREATEST(
        generation.acknowledged_generation,
        $4::bigint
    ),
    acknowledged_at = now()
FROM owner
WHERE generation.thread_id = owner.unit_id
  AND generation.mount_id = $3::text
  AND generation.required_generation = $4::bigint
  AND generation.workspace_generation = $5::text
  AND generation.sync_scope_sha256 = $6::text
  AND generation.baseline_sha256 = $7::text
RETURNING generation.acknowledged_generation
"""
)

_PENDING_PUSH_SQL = """
SELECT generation.mount_id,
       generation.push_progress,
       generation.push_heartbeat_at,
       generation.push_owner_pod,
       generation.push_failed_at,
       generation.push_error,
       (generation.push_heartbeat_at IS NOT NULL
        AND generation.push_heartbeat_at
            > now() - make_interval(secs => $2::float8)) AS owner_alive
FROM thread_cloud_sync_generations AS generation
WHERE generation.thread_id = $1::uuid
  AND generation.acknowledged_generation < generation.required_generation
ORDER BY generation.mount_id
"""

PUSH_PROGRESS_STATES = frozenset({"uploaded", "deleted"})
PUSH_OWNER_STALE_AFTER_SECONDS = 90.0


def normalize_push_progress(value: Any) -> dict[str, Any]:
    """Validate the durable per-file push progress of one pending generation.

    Shape: ``{"planned": int | None, "files": {path: {"sha256", "size",
    "remote_etag", "state"}}}``. Paths follow the baseline rules; ``state`` is
    ``uploaded`` (bytes + etag landed) or ``deleted`` (the remote DELETE
    landed). Anything malformed is a hard error: a successor must never
    resume from a manifest it cannot trust.
    """

    if value is None:
        return {"planned": None, "files": {}}
    if isinstance(value, str):
        value = json.loads(value) if value else {}
    if not isinstance(value, Mapping):
        raise ValueError("cloud sync push progress must be an object")
    planned_raw = value.get("planned")
    planned: int | None
    if planned_raw is None:
        planned = None
    elif isinstance(planned_raw, bool) or not isinstance(planned_raw, int):
        raise ValueError("cloud sync push progress planned count is invalid")
    else:
        planned = int(planned_raw)
        if planned < 0:
            raise ValueError("cloud sync push progress planned count is invalid")
    raw_files = value.get("files")
    if raw_files is None:
        raw_files = {}
    if not isinstance(raw_files, Mapping):
        raise ValueError("cloud sync push progress files must be an object")
    if len(raw_files) > MAX_BASELINE_ENTRIES:
        raise ValueError("cloud sync push progress has too many entries")
    files: dict[str, dict[str, Any]] = {}
    for raw_path, raw_entry in raw_files.items():
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ValueError("cloud sync push progress path must be a non-empty string")
        path = raw_path.replace("\\", "/").strip("/")
        if (
            not path
            or path != raw_path
            or posixpath.normpath(path) != path
            or path == ".."
            or path.startswith("../")
        ):
            raise ValueError(f"invalid cloud sync push progress path: {raw_path!r}")
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"cloud sync push progress entry for {path} is invalid")
        state = raw_entry.get("state")
        if state not in PUSH_PROGRESS_STATES:
            raise ValueError(f"cloud sync push progress state for {path} is invalid")
        digest = raw_entry.get("sha256", "")
        if state == "uploaded" and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError(f"cloud sync push progress digest is invalid for {path}")
        size_raw = raw_entry.get("size", 0)
        if isinstance(size_raw, bool) or not isinstance(size_raw, int) or size_raw < 0:
            raise ValueError(f"cloud sync push progress size is invalid for {path}")
        remote_etag = raw_entry.get("remote_etag", "")
        if not isinstance(remote_etag, str) or len(remote_etag) > 4096:
            raise ValueError(f"cloud sync push progress etag is invalid for {path}")
        files[path] = {
            "sha256": str(digest) if state == "uploaded" else "",
            "size": int(size_raw),
            "remote_etag": remote_etag,
            "state": state,
        }
    encoded = json.dumps(
        {"planned": planned, "files": files},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_BASELINE_JSON_BYTES:
        raise ValueError("cloud sync push progress exceeds the durable size limit")
    return {"planned": planned, "files": dict(sorted(files.items()))}


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _requirements(rows: Sequence[Any]) -> dict[str, CloudSyncRequirement]:
    requirements: dict[str, CloudSyncRequirement] = {}
    for row in rows:
        raw_manifest = row["baseline_manifest"]
        if isinstance(raw_manifest, str):
            raw_manifest = json.loads(raw_manifest)
        manifest, _encoded, manifest_sha = encode_cloud_sync_baseline(raw_manifest)
        stored_sha = str(row["baseline_sha256"])
        if stored_sha != manifest_sha:
            raise ValueError("cloud sync baseline digest does not match its manifest")
        try:
            push_owner_token = int(row["push_owner_token"] or 0)
        except (KeyError, TypeError, ValueError):
            push_owner_token = 0
        try:
            push_progress = normalize_push_progress(row["push_progress"])
        except KeyError:
            push_progress = {"planned": None, "files": {}}
        requirement = CloudSyncRequirement(
            mount_id=str(row["mount_id"]),
            required_generation=int(row["required_generation"]),
            acknowledged_generation=int(row["acknowledged_generation"]),
            required_lease_token=int(row["required_lease_token"]),
            workspace_generation=str(row["workspace_generation"]),
            sync_scope_sha256=str(row["sync_scope_sha256"]),
            baseline_manifest=manifest,
            baseline_sha256=stored_sha,
            push_owner_token=push_owner_token,
            push_progress=push_progress,
        )
        requirements[requirement.mount_id] = requirement
    return requirements


async def arm_cloud_sync_generations(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    scopes: Sequence[CloudSyncScope],
) -> dict[str, CloudSyncRequirement]:
    """Arm every configured mount to the current lease before tool work.

    Fence + mutations are one SQL statement/transaction. A previous pending
    generation cannot be overwritten: callers must recover/ack it first. The
    queue lease token itself is the generation, so no independent counter can
    reset or disagree with ownership.
    """

    by_mount: dict[str, tuple[str, str, dict[str, dict[str, str]], str, str]] = {}
    for scope in scopes:
        mount_id = str(scope.mount_id)
        workspace_generation = str(scope.workspace_generation)
        scope_sha256 = str(scope.sync_scope_sha256)
        if not mount_id or not workspace_generation:
            raise ValueError("cloud sync scope identity must be non-empty")
        if len(scope_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in scope_sha256
        ):
            raise ValueError("cloud sync scope digest must be lowercase SHA-256")
        manifest, manifest_json, manifest_sha = encode_cloud_sync_baseline(
            scope.baseline_manifest
        )
        if scope.baseline_sha256 and str(scope.baseline_sha256) != manifest_sha:
            raise ValueError(
                f"cloud sync baseline digest mismatch for mount {mount_id}"
            )
        identity = (
            workspace_generation,
            scope_sha256,
            manifest,
            manifest_json,
            manifest_sha,
        )
        previous = by_mount.setdefault(mount_id, identity)
        if previous != identity:
            raise ValueError(f"conflicting cloud sync scope for mount {mount_id}")
    workspace_generations = {identity[0] for identity in by_mount.values()}
    if len(workspace_generations) > 1:
        raise ValueError("all cloud sync scopes must share one workspace generation")
    clean_scopes = sorted(
        (mount_id, *identity) for mount_id, identity in by_mount.items()
    )
    if not clean_scopes:
        return {}
    rows = await conn.fetch(
        _RESERVE_SQL,
        _uuid(thread_id),
        int(lease_token),
        [scope[0] for scope in clean_scopes],
        [scope[1] for scope in clean_scopes],
        [scope[2] for scope in clean_scopes],
        [scope[4] for scope in clean_scopes],
        [scope[5] for scope in clean_scopes],
        clean_scopes[0][1],
    )
    return _requirements(rows)


async def load_cloud_sync_requirements(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    workspace_generation: str,
) -> dict[str, CloudSyncRequirement]:
    """Read ALL requirements only when the supplied lease is current.

    Callers compare the complete persisted set to current configured scopes.
    Filtering here would hide a pending generation for a removed/rebound mount
    and permit a pull from a different resource over unpushed workspace bytes.
    """

    rows = await conn.fetch(
        _LOAD_SQL,
        _uuid(thread_id),
        int(lease_token),
        str(workspace_generation),
    )
    return _requirements(rows)


async def acknowledge_cloud_sync_generation(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    mount_id: str,
    generation: int,
    workspace_generation: str,
    sync_scope_sha256: str,
    baseline_sha256: str,
    push_owner_token: int | None = None,
) -> bool:
    """Mirror a verified resource marker under the writer's fence.

    Under the live queue lease (``push_owner_token`` None) this is the S2
    contract. After a hand-off the unit is complete and the lease is gone, so
    the push acknowledges under its exact ``push_owner_token`` instead — an
    adopted successor's bump makes a stale owner's acknowledgement a no-op.
    """

    if push_owner_token is not None:
        value = await conn.fetchval(
            _ACK_PUSH_SQL,
            _uuid(thread_id),
            int(push_owner_token),
            str(mount_id),
            int(generation),
            str(workspace_generation),
            str(sync_scope_sha256),
            str(baseline_sha256),
        )
        return value is not None and int(value) >= int(generation)
    value = await conn.fetchval(
        _ACK_SQL,
        _uuid(thread_id),
        int(lease_token),
        str(mount_id),
        int(generation),
        str(workspace_generation),
        str(sync_scope_sha256),
        str(baseline_sha256),
    )
    return value is not None and int(value) >= int(generation)


async def cloud_sync_writer_is_current(
    conn: Any,
    *,
    kind: str,
    thread_id: UUID | str,
    workspace_generation: str,
    lease_token: int | None = None,
    push_owner_token: int | None = None,
) -> bool:
    """Cheap cooperative recheck immediately before external writes.

    ``kind='lease'``: the writer is the claim that holds the run_queue lease
    (turn-start recovery/pull, the push while the turn is still leased).
    ``kind='push'``: the writer is a handed-off or adopted push whose fence is
    the row's exact ``push_owner_token``; it is current only while every
    pending row of the thread carries that token and the workspace
    incarnation is unchanged. An acknowledged generation has nothing left to
    write, so "no pending row" also reads as not current.
    """

    if kind == "lease":
        if lease_token is None:
            raise ValueError("lease writer check requires lease_token")
        return bool(
            await conn.fetchval(
                _CURRENT_SQL,
                _uuid(thread_id),
                int(lease_token),
                str(workspace_generation),
            )
        )
    if kind == "push":
        if push_owner_token is None or int(push_owner_token) <= 0:
            return False
        return bool(
            await conn.fetchval(
                _CURRENT_PUSH_SQL,
                _uuid(thread_id),
                int(push_owner_token),
                str(workspace_generation),
            )
        )
    raise ValueError(f"unknown cloud sync writer kind: {kind!r}")


async def cloud_sync_lease_is_current(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    workspace_generation: str,
) -> bool:
    """Compatibility alias for ``cloud_sync_writer_is_current(kind='lease')``.

    Kept for one release; new callers name the writer kind explicitly.
    """

    return await cloud_sync_writer_is_current(
        conn,
        kind="lease",
        thread_id=thread_id,
        workspace_generation=workspace_generation,
        lease_token=lease_token,
    )


async def hand_off_push_ownership(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    workspace_generation: str,
    pod_name: str,
    pod_uid: str,
) -> int | None:
    """Give this claim's pending push its own fence, under the still-live lease.

    One UPDATE: every pending row this lease armed gets one fresh
    ``push_owner_token`` (max over the thread + 1), the owner pod stamped and
    the heartbeat started. Returns that token, or ``None`` when nothing is
    pending (every mount already acknowledged inline — the common case for a
    text-only turn). Fenced on the run_queue lease so it cannot race a steal.
    """

    rows = await conn.fetch(
        _HAND_OFF_SQL,
        _uuid(thread_id),
        int(lease_token),
        str(workspace_generation),
        str(pod_name),
        str(pod_uid),
    )
    tokens = {int(row["push_owner_token"]) for row in rows}
    if not tokens:
        return None
    if len(tokens) != 1:
        raise RuntimeError("cloud push hand-off produced more than one owner token")
    return tokens.pop()


async def adopt_push_ownership(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    workspace_generation: str,
    pod_name: str,
    pod_uid: str,
) -> dict[str, dict[str, Any]]:
    """The thread's new claimant takes over every pending push, unconditionally.

    Bumping the token fences the previous owner out at its next write; the
    claimant's own writes are lease-fenced. Returns the durable progress per
    pending mount so the recovery delta resumes instead of re-uploading.
    """

    rows = await conn.fetch(
        _ADOPT_SQL,
        _uuid(thread_id),
        int(lease_token),
        str(workspace_generation),
        str(pod_name),
        str(pod_uid),
    )
    adopted: dict[str, dict[str, Any]] = {}
    for row in rows:
        adopted[str(row["mount_id"])] = normalize_push_progress(row["push_progress"])
    return adopted


async def record_push_progress(
    conn: Any,
    *,
    thread_id: UUID | str,
    mount_id: str,
    push_owner_token: int,
    files: Mapping[str, Mapping[str, Any]],
    planned: int | None = None,
) -> bool:
    """Merge landed files into the pending row's progress under the push token.

    ``files`` maps path → ``{sha256, size, remote_etag, state}``; ``planned``
    sets the intended write count once. Returns ``False`` when the token no
    longer owns the row (adopted or acknowledged) — the caller stops.
    """

    normalized = normalize_push_progress({"planned": planned, "files": dict(files)})
    files_json = json.dumps(
        normalized["files"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    planned_json = (
        None if normalized["planned"] is None else json.dumps(normalized["planned"])
    )
    value = await conn.fetchval(
        _PROGRESS_SQL,
        _uuid(thread_id),
        str(mount_id),
        int(push_owner_token),
        files_json,
        planned_json,
    )
    return value is not None


async def heartbeat_push_owner(
    conn: Any,
    *,
    thread_id: UUID | str,
    push_owner_token: int,
) -> bool:
    """Renew the owner heartbeat; ``False`` once nothing pending is ours."""

    rows = await conn.fetch(
        _HEARTBEAT_PUSH_SQL,
        _uuid(thread_id),
        int(push_owner_token),
    )
    return bool(rows)


async def record_push_failure(
    conn: Any,
    *,
    thread_id: UUID | str,
    push_owner_token: int,
    error: str,
) -> bool:
    """Diagnostics only: success is still the acknowledgement + marker."""

    rows = await conn.fetch(
        _PUSH_FAILED_SQL,
        _uuid(thread_id),
        int(push_owner_token),
        str(error)[:2000],
    )
    return bool(rows)


async def pending_push_state(
    conn: Any,
    *,
    thread_id: UUID | str,
    stale_after_seconds: float = PUSH_OWNER_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Owner-facing view of the thread's pending push, for /connection.

    ``pending`` is the number of mounts whose generation is not yet
    acknowledged; ``uploaded``/``total`` sum the durable per-file progress
    (``total`` is ``None`` until a planned count was recorded);
    ``owner_alive`` is whether any owner heartbeat is fresher than
    ``stale_after_seconds``. Read-only; values are diagnostics.
    """

    rows = await conn.fetch(
        _PENDING_PUSH_SQL,
        _uuid(thread_id),
        float(stale_after_seconds),
    )
    uploaded = 0
    total: int | None = None
    owner_alive = False
    failed = False
    for row in rows:
        try:
            progress = normalize_push_progress(row["push_progress"])
        except ValueError:
            progress = {"planned": None, "files": {}}
        uploaded += len(progress["files"])
        if progress["planned"] is not None:
            total = (total or 0) + int(progress["planned"])
        owner_alive = owner_alive or bool(row["owner_alive"])
        failed = failed or row["push_failed_at"] is not None
    return {
        "pending": len(rows),
        "uploaded": uploaded,
        "total": total,
        "owner_alive": owner_alive,
        "failed": failed,
    }
