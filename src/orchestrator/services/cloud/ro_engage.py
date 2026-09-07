"""Fail-closed RO engage gate for protected cloud mode (design §3.3, §11.4).

Provisions the per-user reader + per-mount grant, then verifies — as the
reader identity — that the mount is genuinely read-only before it may be used:
version floors (CVE side-channel patches) AND a live probe of every mutating
verb, with a positive read control so a dead credential cannot pass by 401ing
everywhere. On ANY failure it revokes the grant and raises RoEngageRefused;
only an ``ok`` probe persists the cloud_ro_mounts row.

This is the first (and only) caller of ``ro_probe``. The write identity's
``seed_canary_fixture`` (nextcloud.py) discovers real version/trash ids when
the server exposes them for the canary, and this module passes them through
to ``probe_read_only`` as ``version_ref``/``trash_ref`` — so the
versions-restore/trash-restore side channels target ids the server actually
knows, turning ``inconclusive`` into a verified ``403`` rejection on a
correctly-RO reader. The uploads-finalize side channel is cured differently
(it can't be by an injectable ref — its URL is reader-namespaced):
``probe_read_only`` self-provisions a REAL reader-owned upload session via
``MKCOL`` before probing it. Remaining inconclusives — the canary's server
exposed no version/trash id, or the uploads MKCOL failed — still correctly
REFUSE: fail-closed, not a config bug.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from orchestrator.services.cloud import ro_probe
from orchestrator.services.cloud.base import RoReaderGrant
from orchestrator.services.cloud.protected_effect_client import (
    ProtectedNextcloudEffectExecutor,
)
from orchestrator.services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)

logger = logging.getLogger(__name__)


def _dav_root_from_webdav_url(url: str) -> str:
    """Derive the true DAV root from a reader's files-namespace WebDAV URL.

    ``grant.webdav_url`` (Slice A ``mint_ro_grant``) is the files-namespace
    URL — ``{origin}/remote.php/dav/files/{reader}/{mount}/`` — which is NOT
    the root the versions/trashbin/uploads side channels live under (they
    hang directly off ``{origin}/remote.php/dav``, not under ``files/...``).
    Passing the files URL as ``dav_root`` builds nonsense side-channel URLs
    like ``.../files/<reader>/<mount>/versions/...``. Splitting at the
    ``/remote.php/dav`` marker and keeping the prefix up to and including it
    recovers the real root. If the marker is absent (unexpected URL shape),
    fall back to the url rstripped of a trailing slash — defensive, not a
    silent success path: a malformed root still only produces requests that
    fail closed (404/409 inconclusive, or a transport error) under the
    strict engage gate, never a false "verified rejected".
    """
    marker = "/remote.php/dav"
    idx = url.find(marker)
    if idx == -1:
        return url.rstrip("/")
    return url[: idx + len(marker)]


class RoEngageRefused(Exception):
    """The RO gate refused to engage — protected mode must NOT mount."""


class RoEngageCleanupPending(RoEngageRefused):
    """The failed attempt remains durably revoking behind its effect fence."""


async def revoke_ro_mount_attempt(
    *,
    backend,
    postgres_db,
    row_id: str,
    thread_id: str,
    runtime_generation: str,
    plan: ProtectedNextcloudReaderGrantPlan,
) -> bool:
    """Contain and settle one exact attempt; never infer remote absence."""

    if not await postgres_db.begin_ro_mount_revocation_if_matches(
        row_id,
        expected_thread_id=thread_id,
        expected_runtime_generation=runtime_generation,
        plan=plan,
    ):
        return False
    await backend.revoke_protected_reader_attempt(plan)
    return await postgres_db.finish_ro_mount_revocation_if_matches(
        row_id,
        expected_thread_id=thread_id,
        expected_runtime_generation=runtime_generation,
        plan=plan,
    )


async def engage_ro_mount(
    *,
    backend,
    plan: ProtectedNextcloudReaderGrantPlan,
    credentials: str,
    selected_mount_id: str,
    thread_id: str,
    user_id: str,
    postgres_db,
    http_client_factory,
    admission_check: Callable[[], Awaitable[bool]] | None = None,
    expected_runtime_generation: str,
    effect_dispatcher: (
        Callable[[str, str, bytes], Awaitable[httpx.Response]] | None
    ) = None,
) -> RoReaderGrant:
    """Persist, provision, and verify one attempt-scoped read-only grant.

    ``http_client_factory(credentials, reader_id)`` returns an httpx-like
    client authenticated AS THE READER (so the probe exercises the reader's
    real rights). The encrypted final password and immutable source/attempt row
    commit before the first Nextcloud mutation. Every authority-creating POST
    then passes through the signed effect executor. A failure first transitions
    the exact row to ``revoking`` and performs attempt-unique containment; it
    becomes ``revoked`` only after every durable dispatch horizon has elapsed.
    """

    async def _require_admission() -> None:
        if admission_check is not None and not await admission_check():
            raise RoEngageRefused("session lifecycle no longer admits a runtime")

    if not isinstance(plan, ProtectedNextcloudReaderGrantPlan):
        raise RoEngageRefused("protected reader attempt authority is malformed")
    handle = plan.to_project_folder_handle()
    grant = backend.build_protected_reader_grant(
        plan,
        credentials=credentials,
    )
    dispatch = effect_dispatcher
    if dispatch is None:
        dispatch = ProtectedNextcloudEffectExecutor(
            postgres_db=postgres_db,
            transport=backend,
            thread_id=thread_id,
            runtime_generation=expected_runtime_generation,
            plan=plan,
        )

    await _require_admission()
    canary = None
    row_id: str | None = None
    try:
        installed = await postgres_db.install_ro_mount_engage_intent(
            thread_id=thread_id,
            user_id=user_id,
            selected_mount_id=selected_mount_id,
            expected_runtime_generation=expected_runtime_generation,
            plan=plan,
            credentials=credentials,
            webdav_url=grant.webdav_url,
            auth_kind=grant.auth_kind,
        )
        if installed is None:
            raise RoEngageRefused(
                "session or protected source changed before intent persistence"
            )
        row_id = str(installed["id"])

        await _require_admission()
        provisioned = await backend.grant_protected_reader_attempt(
            plan,
            credentials=credentials,
            dispatch_effect=dispatch,
        )
        if provisioned != grant:
            raise RoEngageRefused(
                "protected reader effect returned a different credential authority"
            )
        await _require_admission()
        canary = await backend.seed_canary_fixture(handle)
        await _require_admission()
        # Probe AS THE READER using its freshly minted credential.
        client = http_client_factory(grant.credentials, grant.reader_id)

        floors = await ro_probe.check_version_floors(
            client, grant.webdav_url, backend=backend.backend_id
        )
        await _require_admission()
        if not floors.ok:
            raise RoEngageRefused(
                f"version floor check failed: {floors.failures or floors.inconclusive}"
            )

        # probe_read_only(client, base_url, path, *, dav_root=None,
        # username=None, version_ref=None, trash_ref=None) — `path` is the
        # positional target (the canary file); `base_url` stays the
        # reader's files-namespace WebDAV URL (that's the mount being
        # verified read-only), but `dav_root` must be the TRUE DAV root
        # (versions/trashbin/uploads live under it, not under
        # `files/<reader>/<mount>/`) — see `_dav_root_from_webdav_url`.
        # The canary's discovered refs (None if the server exposed none)
        # pass straight through so the side channels target real ids.
        result = await ro_probe.probe_read_only(
            client,
            grant.webdav_url,
            canary.path,
            dav_root=_dav_root_from_webdav_url(grant.webdav_url),
            username=grant.reader_id,
            version_ref=canary.version_ref,
            trash_ref=canary.trash_ref,
        )
        await _require_admission()
        if not result.ok:
            raise RoEngageRefused(
                "read-only probe did not pass: "
                f"failures={result.failures} inconclusive={result.inconclusive} "
                f"skipped={result.skipped}"
            )

        # Etag baseline (design §3.4): without it neither the staged-diff
        # manifest nor the apply conflict gate can classify writes, so a
        # capture failure here is itself an engage refusal — the outer
        # except below revokes the grant AND (row-aware, since the row has
        # already persisted at this point) marks the row revoked, just like
        # every other refusal in this function (no second cleanup path).
        #
        # BUT: this may be a resume re-engage on a thread whose row already
        # carries a live staging — ``create_ro_mount``'s upsert just replaced
        # credentials/status in place and left ``etag_baseline``/
        # ``staged_summary`` untouched. The staged diff classifies its
        # entries against THAT (prior) baseline; capturing a fresh one here
        # would silently absorb whatever changed on the cloud folder since
        # staging into "the baseline", making those changes invisible to the
        # apply conflict gate instead of surfacing as
        # ``external_modifications_detected``. So: skip capture+persist
        # whenever a staging is still live. Only restage (clears staging) or
        # apply/reject (clears staging too) should ever move the baseline
        # forward — re-engage must not.
        existing_row = await postgres_db.get_ro_mount_by_thread(thread_id)
        await _require_admission()
        if existing_row is not None and existing_row.get("staged_summary") is not None:
            baseline = existing_row.get("etag_baseline")
            if not isinstance(baseline, dict):
                raise RoEngageRefused("existing staging has no valid etag baseline")
            logger.info(
                "RO mount re-engage for thread %s: existing staging binds to "
                "prior baseline — preserving",
                thread_id,
            )
        else:
            try:
                baseline = await backend.capture_etag_baseline(handle)
                await _require_admission()
            except Exception as e:
                raise RoEngageRefused(f"etag baseline capture failed: {e}") from e
        if not await postgres_db.activate_ro_mount_attempt_with_baseline(
            row_id,
            baseline,
            thread_id=thread_id,
            user_id=user_id,
            selected_mount_id=selected_mount_id,
            expected_runtime_generation=expected_runtime_generation,
            plan=plan,
        ):
            raise RoEngageRefused(
                "etag baseline publication failed: engage authority changed"
            )

        await _require_admission()
        logger.info("RO mount engaged for thread %s (row %s)", thread_id, row_id)
        return grant
    except BaseException as original_error:
        if row_id is not None:
            try:
                settled = await revoke_ro_mount_attempt(
                    backend=backend,
                    postgres_db=postgres_db,
                    row_id=row_id,
                    thread_id=thread_id,
                    runtime_generation=expected_runtime_generation,
                    plan=plan,
                )
            except BaseException:
                logger.exception(
                    "protected reader attempt cleanup remains pending for %s",
                    plan.engage_attempt,
                )
                settled = False
            if not settled and isinstance(original_error, Exception):
                raise RoEngageCleanupPending(
                    "protected reader cleanup is still pending"
                ) from original_error
        raise
    finally:
        if canary is not None:
            try:
                await backend.remove_canary_fixture(handle, canary)
            except Exception:  # pragma: no cover
                logger.exception("failed to remove canary fixture")
