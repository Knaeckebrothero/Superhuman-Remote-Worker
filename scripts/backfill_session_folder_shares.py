#!/usr/bin/env python3
"""Share session cloud folders that were provisioned before their owner existed.

``_setup_main_cloud`` (``orchestrator/main.py``) creates ``sessions/<id8>`` under
the service account and then shares it with the thread's owner — but the share is
guarded on ``resolved_user_id`` being truthy:

    resolved_user_id = await backend.ensure_user(...)
    if resolved_user_id:
        share_handle = await backend.share_session_folder(...)

For an owner who has never signed into the cloud there is no account to share
with, so that branch is skipped **silently**. The folder is created and its
handle stamped on the thread, but ``main_cloud_share_handle`` stays NULL and the
folder never appears in the owner's Files. The Cockpit's cloud button still
resolves to ``{public_url}/apps/files/?dir=/<id8>`` — a path that exists only
once the share has placed the folder in the owner's root — so the user lands on
an empty view. See ``knowledge-base/knowledge/issues/cloud_folder_invisible_until_owner_signs_into_cloud.md``.

The resume path (``needs_share_only``, ~``main.py:27180``) is the intended
recovery and is correct, but it only fires when a thread is actually resumed.
Threads that ended before their owner's first cloud login never get one and stay
invisible forever. This script is the sweep for those.

Idempotent by construction: it selects only rows whose share handle is NULL, and
writes one via ``update_thread_main_cloud`` on success, so a re-run reports zero
candidates. Owners who *still* have no cloud account are skipped without a write,
which leaves them eligible for a later run rather than burning the row.

Dry-run by default; pass ``--apply`` to write. Run where the app DB env is present
(POSTGRES_* / DATABASE_URL) and the cloud backend is configured. The orchestrator
image has no ``/app/scripts``, so copy it in:

    POD=$(kubectl --context=main -n superhuman-remote-worker get pods \
        --field-selector=status.phase=Running -o name | grep orchestrator | head -1 | cut -d/ -f2)
    kubectl --context=main -n superhuman-remote-worker cp \
        scripts/backfill_session_folder_shares.py $POD:/tmp/bf.py -c orchestrator
    kubectl --context=main -n superhuman-remote-worker exec $POD -c orchestrator -- python /tmp/bf.py
    kubectl --context=main -n superhuman-remote-worker exec $POD -c orchestrator -- python /tmp/bf.py --apply

Sharing goes through ``backend.share_session_folder``, so the client-side leaky
bucket that keeps us under Nextcloud's ``20 shares / 10 min`` ceiling applies here
exactly as it does in the app.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Put the orchestrator package dir on sys.path so bare imports (``database.*``,
# ``services.*``) resolve the same way they do inside the running orchestrator.
# In the image the code is flattened to /app and PYTHONPATH already covers it.
_ORCH = Path(__file__).resolve().parent.parent / "orchestrator"
if _ORCH.is_dir() and str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

from database.postgres import PostgresDB  # noqa: E402
from services.cloud import build_backend  # noqa: E402
from services.cloud.handles import SessionFolderHandle  # noqa: E402
from services.cloud.identity import resolve_user_identity_cached  # noqa: E402

logger = logging.getLogger("backfill_session_folder_shares")

# Threads carrying a session folder but no share record. COALESCE over both the
# new main_cloud_* column and the legacy nc_session_folder one, matching how
# _resolve_cloud_session_url reads them.
_CANDIDATES_SQL = """
    SELECT t.id::text            AS thread_id,
           t.status,
           t.main_cloud_backend  AS backend_id,
           COALESCE(NULLIF(t.main_cloud_session_handle, ''),
                    NULLIF(t.nc_session_folder, '')) AS handle,
           u.id::text            AS user_id,
           u.email,
           u.display_name
      FROM threads t
      JOIN users u ON u.id = t.user_id
     WHERE COALESCE(NULLIF(t.main_cloud_session_handle, ''),
                    NULLIF(t.nc_session_folder, '')) IS NOT NULL
       AND NULLIF(t.main_cloud_share_handle, '') IS NULL
     ORDER BY t.created_at
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform writes")
    args = ap.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"

    db = PostgresDB()
    await db.connect()
    try:
        rows = await db.fetch(_CANDIDATES_SQL)
        print(f"[{mode}] {len(rows)} thread(s) with a session folder but no share\n")
        if not rows:
            return 0

        backends: dict[str, object] = {}
        ok = skipped = failed = 0

        for r in rows:
            tid = r["thread_id"]
            # A NULL backend column predates the multi-backend split; those rows
            # were all Nextcloud. for_backend() applies the same default.
            bid = r["backend_id"] or "nextcloud"
            label = f"{tid[:8]} [{r['status']}] {r['handle']}"

            backend = backends.get(bid)
            if backend is None:
                backend = build_backend(bid)
                if not await backend.ensure_initialized():
                    print(f"  SKIP {label}: backend {bid!r} not initialized")
                    skipped += 1
                    continue
                backends[bid] = backend

            user = {
                "id": r["user_id"],
                "email": r["email"],
                "display_name": r["display_name"],
            }
            try:
                cloud_uid = await resolve_user_identity_cached(db, user, backend)
            except Exception as e:
                print(f"  FAIL {label}: identity lookup errored: {e}")
                failed += 1
                continue

            if not cloud_uid:
                print(f"  SKIP {label}: owner {r['email']} has no cloud account yet")
                skipped += 1
                continue

            if not args.apply:
                print(f"  WOULD SHARE {label} -> {str(cloud_uid)[:16]}…")
                ok += 1
                continue

            try:
                handle = SessionFolderHandle.from_db(r["handle"], backend=bid)
                share = await backend.share_session_folder(handle, cloud_uid)
                await db.update_thread_main_cloud(
                    tid,
                    backend_id=bid,
                    session_handle=handle.to_db(),
                    share_handle=share.to_db(),
                )
                print(f"  SHARED {label} -> share_id={share.native_id}")
                ok += 1
            except Exception as e:
                print(f"  FAIL {label}: {type(e).__name__}: {e}")
                failed += 1

        print(f"\n[{mode}] ok={ok} skipped={skipped} failed={failed}")
        return 1 if failed else 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
