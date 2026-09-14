#!/usr/bin/env python3
"""Hold real Nextcloud requests across adoption by a second k3d agent pod.

Run with the repository venv. Uses only a disposable resilience-test thread.
The test barriers live in these probe processes, never in the serving agent.
Credentials are resolved inside each pod and never returned to the controller.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID


async def probe(role, thread):
    import asyncpg
    import urllib.request

    from agent.api.persistent_app import _build_sync_coordinator
    from agent.services.cloud_sync import nextcloud_sync as transport
    from agent.services.cloud_sync.base import (
        CloudSyncFenceLost,
        CloudSyncMarker,
        _sync_generation_marker_path,
    )
    from shared.cloud_sync_generations import (
        CloudSyncScope,
        acknowledge_cloud_sync_generation,
        adopt_push_ownership,
        arm_cloud_sync_generations,
        cloud_sync_writer_is_current,
        encode_cloud_sync_baseline,
        hand_off_push_ownership,
        heartbeat_push_owner,
        load_cloud_sync_requirements,
        record_push_progress,
    )
    from shared.db_url import build_postgres_url
    from shared.run_queue import claim_unit, complete_unit, enqueue_unit

    tid = UUID(thread)
    directory = Path(f"/tmp/srw-fence-gate-{tid}")
    directory.mkdir(exist_ok=True)
    if role == "origin":
        (directory / "release").unlink(missing_ok=True)
        (directory / "ready.json").unlink(missing_ok=True)
    pool = await asyncpg.create_pool(
        build_postgres_url("POSTGRES", fallback_env="DATABASE_URL"),
        min_size=1,
        max_size=4,
    )
    row = await pool.fetchrow("SELECT title FROM threads WHERE id=$1", tid)
    assert row and row["title"] == "resilience gate stale writer"
    assert os.environ.get("STATELESS_CLOUD_PUSH_RECOVERY_ENABLED") == "false"
    request = urllib.request.Request(
        os.environ.get("ORCHESTRATOR_URL", "http://srw-orchestrator:8085")
        + f"/api/agents/threads/{tid}/workspace",
        headers={"X-Internal-Key": os.environ["MCP_INTERNAL_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    workspace = payload["workspace_generation"]
    pod = os.environ["HOSTNAME"]
    pod_uid = os.environ["POD_UID"]
    sync = _build_sync_coordinator(
        workspace_path=directory,
        workspace_backend=None,
        cloud_cfg=payload["cloud_sync"],
        thread_id=thread,
        workspace_generation=workspace,
    )
    mount = next(m for m in sync.mounts if m.generation_id == "legacy-session")
    cloud = mount.sync
    assert isinstance(cloud, transport.NextcloudWorkspaceSync)
    scope = mount.sync_scope_sha256
    marker_path = _sync_generation_marker_path(thread, scope)
    paths = [f"output/fence-{kind}.txt" for kind in ("replace", "create", "delete")]

    async def claim():
        async with pool.acquire() as conn, conn.transaction():
            state = await conn.fetchrow(
                "SELECT * FROM run_queue WHERE unit_id=$1 FOR UPDATE", tid
            )
            assert state["state"] == "done"
            await enqueue_unit(
                conn,
                unit_id=tid,
                unit_kind="session_turn",
                input_seq=state["input_seq"],
            )
            result = await claim_unit(
                conn,
                unit_kind="session_turn",
                pod_name=pod,
                prefer_unit_id=tid,
                lease_ttl_seconds=600,
            )
            assert result and result.unit_id == tid
            return result

    async def fence(*, lease=None, push=None):
        if not await cloud_sync_writer_is_current(
            pool,
            kind="push" if push is not None else "lease",
            thread_id=tid,
            workspace_generation=workspace,
            lease_token=lease,
            push_owner_token=push,
        ):
            raise CloudSyncFenceLost("probe writer lost its DB fence")

    async def put(path, content, guard, **conditions):
        target = directory / hashlib.sha256(path.encode()).hexdigest()
        target.write_bytes(content)
        await cloud._ensure_remote_dirs(str(Path(path).parent), before_write=guard)
        etag = await cloud._upload_file(
            path, str(target), before_write=guard, **conditions
        )
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "remote_etag": etag or "",
        }

    def marker(requirement, manifest, lease):
        manifest, _encoded, digest = encode_cloud_sync_baseline(manifest)
        return CloudSyncMarker(
            thread_id=thread,
            mount_id=mount.generation_id,
            generation=requirement.required_generation,
            lease_token=lease,
            workspace_generation=workspace,
            sync_scope_sha256=scope,
            baseline_sha256=requirement.baseline_sha256,
            committed_manifest=manifest,
            committed_manifest_sha256=digest,
        )

    async def arm(lease, baseline):
        baseline, _encoded, digest = encode_cloud_sync_baseline(baseline)
        requirements = await arm_cloud_sync_generations(
            pool,
            thread_id=tid,
            lease_token=lease,
            scopes=[
                CloudSyncScope(mount.generation_id, workspace, scope, baseline, digest)
            ],
        )
        return requirements[mount.generation_id]

    async def ack(requirement, lease):
        assert await acknowledge_cloud_sync_generation(
            pool,
            thread_id=tid,
            lease_token=lease,
            mount_id=mount.generation_id,
            generation=requirement.required_generation,
            workspace_generation=workspace,
            sync_scope_sha256=scope,
            baseline_sha256=requirement.baseline_sha256,
        )

    unit = None
    try:
        if role == "verify":
            current = await cloud.read_sync_generation_marker(
                thread_id=thread, sync_scope_sha256=scope
            )
            print(json.dumps({"generation": current.generation}), flush=True)
            return
        unit = await claim()
        lease = unit.lease_token

        async def live_lease():
            await fence(lease=lease)

        if role == "successor":
            requirement = (
                await load_cloud_sync_requirements(
                    pool,
                    thread_id=tid,
                    lease_token=lease,
                    workspace_generation=workspace,
                )
            )[mount.generation_id]
            adopted = await adopt_push_ownership(
                pool,
                thread_id=tid,
                lease_token=lease,
                workspace_generation=workspace,
                pod_name=pod,
                pod_uid=pod_uid,
            )
            assert mount.generation_id in adopted
            # Settle the prior generation before arming this claim's newer one.
            baseline = requirement.baseline_manifest
            await cloud.write_sync_generation_marker(
                marker(requirement, baseline, lease), before_write=live_lease
            )
            await ack(requirement, lease)
            requirement = await arm(lease, baseline)
            newer = dict(baseline)
            for path in paths:
                previous = await cloud._remote_etag(path)
                newer[path] = await put(
                    path,
                    b"successor bytes must survive",
                    live_lease,
                    if_match=previous,
                    if_none_match=previous is None,
                )
            committed = marker(requirement, newer, lease)
            await cloud.write_sync_generation_marker(committed, before_write=live_lease)
            await ack(requirement, lease)
            assert (
                await complete_unit(
                    pool, unit_id=tid, lease_token=lease, consumed_seq=unit.input_seq
                )
                == "done"
            )
            print(
                json.dumps(
                    {"generation": requirement.required_generation, "lease": lease}
                ),
                flush=True,
            )
            return

        initial = await cloud.read_sync_generation_marker(
            thread_id=thread, sync_scope_sha256=scope
        )
        assert initial is not None
        baseline = dict(initial.committed_manifest)
        created_etag = await cloud._remote_etag(paths[1])
        if created_etag:
            await cloud._delete_remote_file(
                paths[1], before_write=live_lease, if_match=created_etag
            )
        baseline.pop(paths[1], None)
        for path in (paths[0], paths[2]):
            etag = await cloud._remote_etag(path)
            baseline[path] = await put(
                path,
                b"predecessor bytes",
                live_lease,
                if_match=etag,
                if_none_match=etag is None,
            )
        requirement = await arm(lease, baseline)
        push = await hand_off_push_ownership(
            pool,
            thread_id=tid,
            lease_token=lease,
            workspace_generation=workspace,
            pod_name=pod,
            pod_uid=pod_uid,
        )
        assert push is not None
        assert (
            await complete_unit(
                pool, unit_id=tid, lease_token=lease, consumed_seq=unit.input_seq
            )
            == "done"
        )

        async def live_push():
            await fence(push=push)

        # Pause inside the transport thread, AFTER the real final DB check.
        # Capture all requests before the successor is allowed to claim.
        raw_put, raw_delete = transport._conditional_put, transport._conditional_delete
        arrived = set()
        mutex = threading.Lock()

        def pause(path):
            with mutex:
                arrived.add(path)
            end = time.monotonic() + 600
            while not (directory / "release").exists():
                if time.monotonic() > end:
                    raise TimeoutError("stale-writer barrier timed out")
                time.sleep(0.1)

        def paused_put(client, path, *args):
            pause(path)
            return raw_put(client, path, *args)

        def paused_delete(client, path, *args):
            pause(path)
            return raw_delete(client, path, *args)

        transport._conditional_put = paused_put
        transport._conditional_delete = paused_delete
        pending = [
            asyncio.create_task(
                put(
                    paths[0],
                    b"predecessor bytes",
                    live_push,
                    if_match=baseline[paths[0]]["remote_etag"],
                )
            ),
            asyncio.create_task(
                put(paths[1], b"predecessor bytes", live_push, if_none_match=True)
            ),
            asyncio.create_task(
                cloud._delete_remote_file(
                    paths[2],
                    before_write=live_push,
                    if_match=baseline[paths[2]]["remote_etag"],
                )
            ),
            asyncio.create_task(
                cloud.write_sync_generation_marker(
                    marker(requirement, baseline, lease), before_write=live_push
                )
            ),
        ]
        while len(arrived) < 4:
            for task in pending:
                if task.done():
                    task.result()
                    raise AssertionError("write escaped its barrier")
            await asyncio.sleep(0.1)
        assert arrived == set(paths) | {marker_path}
        (directory / "ready.json").write_text(
            json.dumps({"generation": requirement.required_generation, "push": push})
        )
        results = await asyncio.gather(*pending, return_exceptions=True)
        assert all(isinstance(result, CloudSyncFenceLost) for result in results), [
            type(r).__name__ for r in results
        ]
        try:
            await live_push()
        except CloudSyncFenceLost:
            pass
        else:
            raise AssertionError("old DB fence remained live")
        assert not await record_push_progress(
            pool,
            thread_id=tid,
            mount_id=mount.generation_id,
            push_owner_token=push,
            files={},
        )
        assert not await heartbeat_push_owner(
            pool, thread_id=tid, push_owner_token=push
        )
        current = await cloud.read_sync_generation_marker(
            thread_id=thread, sync_scope_sha256=scope
        )
        assert current.generation > requirement.required_generation
        for index, path in enumerate(paths):
            target = directory / f"verified-{index}"
            await cloud._download_file(path, str(target))
            assert target.read_bytes() == b"successor bytes must survive"
            assert (
                current.committed_manifest[path]["sha256"]
                == hashlib.sha256(target.read_bytes()).hexdigest()
            )
        print(
            json.dumps(
                {
                    "passed": True,
                    "rejected_http_writes": 4,
                    "generation": current.generation,
                    "db_fence_rejected": True,
                }
            ),
            flush=True,
        )
    finally:
        if unit is not None:
            # These probes enqueue no new input or model turn. Do not leave a
            # failed fixture holding a serving slot; pending cloud state stays
            # intact for diagnosis, guarded by the normal queue CAS.
            await complete_unit(
                pool,
                unit_id=tid,
                lease_token=unit.lease_token,
                consumed_seq=unit.input_seq,
            )
        await sync.aclose()
        await pool.close()


def controller(thread=None):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "resilience_gate", root / "scripts/stateless-resilience-k3d-gate.py"
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert (
        gate.sql("SELECT count(*) FROM run_queue WHERE state IN ('queued','leased')")
        == "0"
    )
    if thread is None:
        client = gate.Gate()
        thread = client.thread(
            "stale writer", "Without tools, reply exactly FENCE-READY."
        )
        gate.wait_for("fixture answer", lambda: client.answered(thread, 1))
    UUID(thread)
    gate.wait_for(
        "fixture generation acknowledged",
        lambda: gate.sql(
            f"SELECT count(*) FROM thread_cloud_sync_generations WHERE thread_id='{thread}' AND required_generation=acknowledged_generation"
        )
        == "1",
        timeout=600,
    )

    def ready_pods():
        try:
            pods = json.loads(
                gate.command(
                    gate.K
                    + ["get", "pods", "-l", "srw/class=agent-stateless", "-o", "json"]
                )
            )["items"]
            pods = [
                p
                for p in pods
                if not p["metadata"].get("deletionTimestamp")
                and p["status"].get("phase") == "Running"
                and p["status"].get("containerStatuses")
                and all(s["ready"] for s in p["status"]["containerStatuses"])
            ]
            if len(pods) < 2:
                return None
            predecessor, successor = [p["metadata"]["name"] for p in pods[:2]]
            expected = hashlib.sha256(
                (root / "src/agent/services/cloud_sync/base.py").read_bytes()
            ).hexdigest()
            for pod in (predecessor, successor):
                gate.command(
                    gate.K
                    + [
                        "exec",
                        pod,
                        "-c",
                        "agent",
                        "--",
                        "python",
                        "-c",
                        "import hashlib; from pathlib import Path; assert hashlib.sha256(Path('/app/src/agent/services/cloud_sync/base.py').read_bytes()).hexdigest()=="
                        + repr(expected),
                    ]
                )
            return predecessor, successor
        except RuntimeError:
            return None

    predecessor, successor = gate.wait_for(
        "two pods running the current source", ready_pods
    )
    source = Path(__file__).read_text()
    directory = f"/tmp/srw-fence-gate-{thread}"
    for pod in (predecessor, successor):
        gate.command(
            gate.K
            + [
                "exec",
                pod,
                "-c",
                "agent",
                "--",
                "python",
                "-c",
                f"from pathlib import Path; p=Path('{directory}'); p.mkdir(exist_ok=True); [(p/n).unlink(missing_ok=True) for n in ('ready.json', 'release')]",
            ]
        )
    args = gate.K + [
        "exec",
        "-i",
        predecessor,
        "-c",
        "agent",
        "--",
        "python",
        "-",
        "probe",
        "origin",
        thread,
    ]
    with tempfile.TemporaryFile(mode="w+") as output:
        writer = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        writer.stdin.write(source)
        writer.stdin.close()
        try:

            def ready():
                if writer.poll() is not None:
                    output.seek(0)
                    raise AssertionError("origin probe exited: " + output.read())
                return gate.command(
                    gate.K
                    + [
                        "exec",
                        predecessor,
                        "-c",
                        "agent",
                        "--",
                        "python",
                        "-c",
                        f"from pathlib import Path; p=Path('{directory}/ready.json'); print(p.read_text() if p.exists() else '')",
                    ]
                )

            gate.wait_for("four real requests paused after their DB check", ready)
            result = subprocess.run(
                gate.K
                + [
                    "exec",
                    "-i",
                    successor,
                    "-c",
                    "agent",
                    "--",
                    "python",
                    "-",
                    "probe",
                    "successor",
                    thread,
                ],
                input=source,
                text=True,
                capture_output=True,
            )
            print(result.stdout, flush=True)
            assert result.returncode == 0, "successor probe failed"
        finally:
            gate.command(
                gate.K
                + [
                    "exec",
                    predecessor,
                    "-c",
                    "agent",
                    "--",
                    "touch",
                    f"{directory}/release",
                ]
            )
            writer.wait(timeout=300)
        output.seek(0)
        result = output.read()
        assert writer.returncode == 0, (
            "origin stale-writer assertions failed: " + result
        )
        print(result, flush=True)
    print(
        f"PASS two-pod stale-writer gate: thread={thread} origin={predecessor} successor={successor}",
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        try:
            asyncio.run(probe(sys.argv[2], sys.argv[3]))
        except Exception as exc:
            import traceback

            print(
                json.dumps(
                    {
                        "error": type(exc).__name__,
                        "marker_error": str(exc)
                        if type(exc).__name__ == "CloudSyncMarkerError"
                        else None,
                        "frames": [
                            f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                            for f in traceback.extract_tb(exc.__traceback__)
                        ],
                    }
                ),
                flush=True,
            )
            raise SystemExit(1) from None
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--thread",
            help="Resume preparation of an existing disposable stale-writer fixture",
        )
        controller(parser.parse_args().thread)
