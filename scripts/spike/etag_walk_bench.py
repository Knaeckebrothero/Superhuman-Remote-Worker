# scripts/spike/etag_walk_bench.py — Phase 0 spike (protected cloud mode), Task 5.
#
# Times the mount-time etag baseline (design §3.4): a full-tree
# ``list_project_folder`` walk through the orchestrator's main-cloud backend,
# exactly as the overlay mount path would run it. On Nextcloud/OpenCloud this
# is a breadth-first series of ``Depth: 1`` PROPFINDs (one per directory);
# the script also counts those HTTP requests by wrapping the backend's
# per-directory helper.
#
# Run IN the orchestrator pod (code lives flattened at /app):
#   kubectl -n srw exec -i deploy/srw-orchestrator -- python3 -u - \
#     < scripts/spike/etag_walk_bench.py <backend_id> <handle_db> [runs]
#   # or, if the file exists in the image:
#   kubectl -n srw exec deploy/srw-orchestrator -- python3 -u \
#     /app/scripts/spike/etag_walk_bench.py <backend_id> <handle_db> [runs]
#
# ``handle_db`` is the projects.main_cloud_folder_handle column value, e.g.
#   '{"backend": "nextcloud", "native_id": "1", "vendor_meta": {"mountpoint": "my-project"}}'
#
# NOTE: ``import main`` boots the FastAPI module (~20 s) but does NOT start
# background loops (those hang off the lifespan handler); router state still
# needs an explicit ``ensure_initialized()``.
import asyncio
import sys
import time

sys.path.insert(0, "/app")

import main  # noqa: E402  — module-level main_cloud_router lives here
from services.cloud import ProjectFolderHandle  # noqa: E402


async def bench(backend_id: str, handle_db: str, runs: int) -> None:
    router = main.main_cloud_router
    ok = await router.ensure_initialized()
    if not ok:
        print("FATAL: main-cloud backend failed ensure_initialized()", flush=True)
        raise SystemExit(1)
    backend = router.for_backend(backend_id)
    handle = ProjectFolderHandle.from_db(handle_db, backend=backend_id)

    # Count per-directory PROPFINDs without touching product code. Both the
    # Nextcloud and OpenCloud adapters walk the tree by calling a single
    # depth-1 helper once per directory (``_propfind_depth_one``).
    counter = {"n": 0}
    helper_name = "_propfind_depth_one" if hasattr(backend, "_propfind_depth_one") else None
    if helper_name:
        orig = getattr(backend, helper_name)

        async def counting(*a, **kw):
            counter["n"] += 1
            return await orig(*a, **kw)

        setattr(backend, helper_name, counting)

    for run in range(1, runs + 1):
        counter["n"] = 0
        t0 = time.monotonic()
        entries = await backend.list_project_folder(handle)
        dt = time.monotonic() - t0
        files = [e for e in entries if not e.is_dir]
        dirs = [e for e in entries if e.is_dir]
        with_etag = sum(1 for e in files if e.etag)
        reqs = counter["n"] if helper_name else 1 + len(dirs)
        print(
            f"[run {run}] backend={backend.backend_id} files={len(files)} "
            f"dirs={len(dirs)} entries={len(entries)} etags={with_etag} "
            f"requests={reqs} walk={dt:.3f}s "
            f"({len(files) / dt:.1f} files/s, {dt / max(reqs, 1) * 1000:.1f} ms/req)",
            flush=True,
        )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__ or "usage: etag_walk_bench.py <backend_id> <handle_db> [runs]")
        raise SystemExit(2)
    asyncio.run(
        bench(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
    )
