"""Durable cloud push-generation contract for stateless session handoff.

The database records what generation *must* exist. The cloud mount itself
records what generation actually committed after its bytes. A successor reads
both before pull: an older/missing resource marker replays only the paths that
differ from the durable turn-start content baseline; a marker ahead of the
database fails closed.

Only the database half is enforced. These statements combine the mutation
with a live ``run_queue`` lease check, so a stale cooperative executor cannot
reserve or acknowledge state. The resource marker is intentionally classified
separately in ``src.services.cloud_sync``: it is stored in user-writable cloud
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
        required_at = now()
    WHERE thread_cloud_sync_generations.acknowledged_generation =
              thread_cloud_sync_generations.required_generation
      AND thread_cloud_sync_generations.required_generation <=
              EXCLUDED.required_generation
    RETURNING mount_id, required_generation, acknowledged_generation,
              required_lease_token, workspace_generation, sync_scope_sha256,
              baseline_manifest, baseline_sha256
)
SELECT mount_id, required_generation, acknowledged_generation,
       required_lease_token, workspace_generation, sync_scope_sha256,
       baseline_manifest, baseline_sha256
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
       generation.baseline_sha256
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
        requirement = CloudSyncRequirement(
            mount_id=str(row["mount_id"]),
            required_generation=int(row["required_generation"]),
            acknowledged_generation=int(row["acknowledged_generation"]),
            required_lease_token=int(row["required_lease_token"]),
            workspace_generation=str(row["workspace_generation"]),
            sync_scope_sha256=str(row["sync_scope_sha256"]),
            baseline_manifest=manifest,
            baseline_sha256=stored_sha,
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
) -> bool:
    """Mirror a verified resource marker under the live queue lease."""

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


async def cloud_sync_lease_is_current(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    workspace_generation: str,
) -> bool:
    """Cheap cooperative recheck immediately before external writes."""

    return bool(
        await conn.fetchval(
            _CURRENT_SQL,
            _uuid(thread_id),
            int(lease_token),
            str(workspace_generation),
        )
    )
