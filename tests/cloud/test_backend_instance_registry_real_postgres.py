from __future__ import annotations

from pathlib import Path
from datetime import timedelta
import hashlib
import json

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.cloud.backend_instance_authority import (
    MainCloudBackendInstanceAuthority,
    main_cloud_installation_proof_sha256,
)
from orchestrator.services.cloud.handles import ProjectFolderHandle
from orchestrator.services.cloud.protected_effect_contract import (
    NextcloudEffectCapability,
    NextcloudEffectFenceIntent,
    NextcloudEffectHorizon,
    NextcloudEffectRequestAuthority,
    adopt_protected_effect_capability,
    sign_protected_effect_capability,
    sign_protected_effect_request,
)
from orchestrator.services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)


_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = _ROOT / "src/orchestrator/database/schema_current.sql"
_MIGRATION_0185 = (
    _ROOT
    / "src/orchestrator/database/migrations/app/0185_thread_runtime_generation_retirement.sql"
)
_MIGRATION_0186 = (
    _ROOT
    / "src/orchestrator/database/migrations/app/0186_protected_cloud_instance_authority.sql"
)
_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_A_RACE = "aaaaaaaa-aaaa-4aaa-8aaa-bbbbbbbbbbbb"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_USER = "11111111-1111-4111-8111-111111111111"
_PROJECT = "22222222-2222-4222-8222-222222222222"
_THREAD = "33333333-3333-4333-8333-333333333333"
_GENERATION = "44444444-4444-4444-8444-444444444444"
_MOUNT = "55555555-5555-4555-8555-555555555555"
_ATTEMPT = "66666666-6666-4666-8666-666666666666"
_EFFECT_KEY = b"e" * 32
_EFFECT_CONFIG_SHA = "e" * 64


def _authority(
    instance_id: str,
    *,
    remote_identity: str,
    base_url: str,
    secret_revision: int = 1,
    admin_ref: str = "env:NEXTCLOUD_ADMIN_PASSWORD",
) -> MainCloudBackendInstanceAuthority:
    return MainCloudBackendInstanceAuthority.capture(
        backend_instance_id=instance_id,
        backend_id="nextcloud",
        routing={
            "version": 1,
            "backend_id": "nextcloud",
            "base_url": base_url,
            "public_url": base_url.replace("internal.", ""),
            "admin_user": "admin",
            "agent_user": "agent-service",
            "protected_effect_url": "http://protected-effect.internal.example",
            "protected_effect_config_sha256": _EFFECT_CONFIG_SHA,
        },
        installation_proof_sha256=main_cloud_installation_proof_sha256(
            backend_id="nextcloud",
            remote_identity=remote_identity,
        ),
        secret_refs={
            "admin_password": admin_ref,
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
            "protected_effect_hmac_key": "env:NEXTCLOUD_PROTECTED_EFFECT_HMAC_KEY",
        },
        secret_revision=secret_revision,
    )


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        container = PostgresContainer("postgres:15")
        container.start()
    except Exception as exc:
        pytest.skip(f"local Postgres container unavailable: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="module")
async def schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(_SCHEMA.read_text())
        await conn.execute(_MIGRATION_0185.read_text())
        await conn.execute(_MIGRATION_0186.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, schema_applied):
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=4,
    )
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute(
            "TRUNCATE cloud_ro_effect_intents, cloud_ro_mounts, "
            "thread_mounts, threads, projects, users, "
            "main_cloud_active_backend, main_cloud_backend_instances CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_first_replica_wins_and_same_installation_replica_adopts(db) -> None:
    authority = _authority(
        _A,
        remote_identity="installation-a",
        base_url="https://a.internal.example",
    )
    result = await db.install_initial_main_cloud_backend_instance(
        authority,
        activated_by="replica-a",
    )
    assert result is not None
    assert result["authority"].canonical_json == authority.canonical_json
    assert result["activation_revision"] == 1

    racing_uuid = _authority(
        _A_RACE,
        remote_identity="installation-a",
        base_url="https://a.internal.example",
    )
    adopted = await db.install_initial_main_cloud_backend_instance(
        racing_uuid,
        activated_by="replica-b",
    )
    assert adopted is not None
    assert adopted["authority"].backend_instance_id == _A
    assert adopted["activation_revision"] == 1

    async with db.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM main_cloud_backend_instances")
            == 1
        )


@pytest.mark.asyncio
async def test_activation_and_secret_rotation_are_exact_cas(db) -> None:
    authority_a = _authority(
        _A,
        remote_identity="installation-a",
        base_url="https://a.internal.example",
    )
    assert await db.install_initial_main_cloud_backend_instance(authority_a)
    authority_b = _authority(
        _B,
        remote_identity="installation-b",
        base_url="https://b.internal.example",
    )
    registered_b = await db.register_main_cloud_backend_instance(authority_b)
    assert registered_b is not None

    assert (
        await db.activate_main_cloud_backend_instance(
            _B,
            expected_activation_revision=2,
        )
        is None
    )
    activated = await db.activate_main_cloud_backend_instance(
        _B,
        expected_activation_revision=1,
        activated_by="admin",
    )
    assert activated is not None
    assert activated["activation_revision"] == 2
    assert activated["authority"].backend_instance_id == _B

    rotated = _authority(
        _B,
        remote_identity="installation-b",
        base_url="https://b.internal.example",
        secret_revision=2,
        admin_ref="env:ROTATED_NEXTCLOUD_ADMIN_PASSWORD",
    )
    assert (
        await db.rotate_main_cloud_backend_secret_refs(
            rotated,
            expected_secret_revision=2,
        )
        is None
    )
    stored = await db.rotate_main_cloud_backend_secret_refs(
        rotated,
        expected_secret_revision=1,
    )
    assert stored is not None
    assert stored.secret_revision == 2
    assert stored.secret_refs["admin_password"] == (
        "env:ROTATED_NEXTCLOUD_ADMIN_PASSWORD"
    )


@pytest.mark.asyncio
async def test_database_triggers_retain_routing_and_active_history(db) -> None:
    authority = _authority(
        _A,
        remote_identity="installation-a",
        base_url="https://a.internal.example",
    )
    assert await db.install_initial_main_cloud_backend_instance(authority)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as immutable:
            await conn.execute(
                """
                UPDATE main_cloud_backend_instances
                   SET routing=jsonb_set(
                       routing,
                       '{base_url}',
                       to_jsonb('https://wrong.invalid'::text)
                   )
                 WHERE id=$1::uuid
                """,
                _A,
            )
        assert immutable.value.constraint_name == (
            "main_cloud_backend_instances_immutable"
        )

        with pytest.raises(asyncpg.CheckViolationError) as retained:
            await conn.execute(
                "DELETE FROM main_cloud_active_backend WHERE singleton=true"
            )
        assert retained.value.constraint_name == "main_cloud_active_backend_retained"


async def _seed_protected_reader_authority(
    db,
) -> tuple[dict, ProtectedNextcloudReaderGrantPlan]:
    authority = _authority(
        _A,
        remote_identity="installation-a",
        base_url="https://a.internal.example",
    )
    assert await db.install_initial_main_cloud_backend_instance(authority)
    handle = ProjectFolderHandle(
        backend="nextcloud",
        native_id="7",
        vendor_meta={"mountpoint": "Project Alpha"},
    )
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id,display_name) VALUES ($1::uuid,'owner')",
            _USER,
        )
        await conn.execute(
            """
            INSERT INTO projects (
                id, name, main_cloud_backend,
                main_cloud_backend_instance_id, main_cloud_folder_handle
            ) VALUES ($1::uuid,'project','nextcloud',$2::uuid,$3)
            """,
            _PROJECT,
            _A,
            handle.to_db(),
        )
        await conn.execute(
            """
            INSERT INTO threads (
                id,user_id,project_id,status,execution_lane,
                runtime_generation,main_cloud_backend,
                main_cloud_backend_instance_id,metadata
            ) VALUES (
                $1::uuid,$2::uuid,$3::uuid,'created','pinned',
                $4::uuid,'nextcloud',$5::uuid,'{}'::jsonb
            )
            """,
            _THREAD,
            _USER,
            _PROJECT,
            _GENERATION,
            _A,
        )
        await conn.execute(
            """
            INSERT INTO thread_mounts (
                id,thread_id,mount_kind,target_path,source_kind,source_ref,
                backend_id,backend_instance_id,cloud_handle,webdav_url
            ) VALUES (
                $1::uuid,$2::uuid,'project','cloud','project_folder',$3::uuid,
                'nextcloud',$4::uuid,$5,'https://ignored.invalid/dav'
            )
            """,
            _MOUNT,
            _THREAD,
            _PROJECT,
            _A,
            handle.to_db(),
        )
    source = ProtectedMountSourceIdentity(
        backend_instance_id=_A,
        source_ref=_PROJECT,
        target_path="cloud",
        native_id="7",
        mountpoint="Project Alpha",
    )
    plan = ProtectedNextcloudReaderGrantPlan(
        engage_attempt=_ATTEMPT,
        backend_instance_id=_A,
        source=source,
    )
    installed = await db.install_ro_mount_engage_intent(
        thread_id=_THREAD,
        user_id=_USER,
        selected_mount_id=_MOUNT,
        expected_runtime_generation=_GENERATION,
        plan=plan,
        credentials="attempt-secret",
        webdav_url="https://a.internal.example/dav/attempt",
    )
    assert installed is not None
    return installed, plan


@pytest.mark.asyncio
async def test_reader_attempt_exact_activation_and_revocation_transitions(db) -> None:
    installed, plan = await _seed_protected_reader_authority(db)

    assert await db.activate_ro_mount_attempt_with_baseline(
        installed["id"],
        {"a.txt": "etag-a"},
        thread_id=_THREAD,
        user_id=_USER,
        selected_mount_id=_MOUNT,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    assert await db.begin_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    assert await db.finish_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    row = await db.get_ro_mount_by_thread(_THREAD)
    assert row is not None
    assert row["status"] == "revoked"
    assert row["credentials"] is None
    assert row["remote_absence_verified_at"] is not None


@pytest.mark.asyncio
async def test_direct_writer_cannot_delete_or_insert_malformed_reader_authority(
    db,
) -> None:
    installed, _plan = await _seed_protected_reader_authority(db)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as retained:
            await conn.execute(
                "DELETE FROM cloud_ro_mounts WHERE id=$1::uuid",
                installed["id"],
            )
        assert retained.value.constraint_name == "cloud_ro_mounts_attempt_retained"

        with pytest.raises(asyncpg.CheckViolationError) as malformed:
            await conn.execute(
                """
                INSERT INTO cloud_ro_mounts (
                    thread_id,user_id,backend,backend_instance_id,
                    reader_id,grant_group_id,grant_handle,grant_handle_sha256,
                    credentials,webdav_url,auth_kind,status,runtime_generation,
                    engage_attempt,source_binding,source_binding_sha256,
                    selected_mount_id
                )
                SELECT thread_id,user_id,backend,backend_instance_id,
                       'shared-reader',grant_group_id,grant_handle,
                       grant_handle_sha256,credentials,webdav_url,auth_kind,
                       status,runtime_generation,engage_attempt,source_binding,
                       source_binding_sha256,selected_mount_id
                  FROM cloud_ro_mounts
                 WHERE id=$1::uuid
                """,
                installed["id"],
            )
        assert malformed.value.constraint_name == "cloud_ro_mounts_authority_shape"


@pytest.mark.asyncio
async def test_direct_writer_cannot_fabricate_effect_intent(db) -> None:
    _installed, _plan = await _seed_protected_reader_authority(db)

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as fabricated:
            await conn.execute(
                """
                INSERT INTO cloud_ro_effect_intents (
                    thread_id,runtime_generation,engage_attempt,
                    backend_instance_id,backend_id,config_sha256,
                    request_authority_sha256,fence_intent
                ) VALUES (
                    $1::uuid,$2::uuid,$3::uuid,$4::uuid,'nextcloud',$5,$6,
                    '{}'::jsonb
                )
                """,
                _THREAD,
                _GENERATION,
                _ATTEMPT,
                _A,
                _EFFECT_CONFIG_SHA,
                "f" * 64,
            )
        assert fabricated.value.constraint_name == (
            "cloud_ro_effect_intents_insert_authority"
        )


async def _begin_never_delivered_retirement(db):
    installed, plan = await _seed_protected_reader_authority(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=$2::jsonb WHERE id=$1::uuid",
            _THREAD,
            json.dumps(
                {
                    "protected_cloud": True,
                    "config_override": {"workspace": {"backend": "sandbox"}},
                }
            ),
        )
    retirement = await db.begin_pinned_thread_retirement(_THREAD, permanent=False)
    assert retirement["state"] == "pending"
    assert await db.authorize_pinned_thread_retirement(
        _THREAD,
        token=retirement["token"],
        generation=retirement["generation"],
        settle_status="ended",
    )
    assert await db.begin_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    assert await db.finish_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    return installed, plan, retirement


@pytest.mark.asyncio
async def test_never_delivered_retirement_receipt_is_source_bound(db) -> None:
    installed, plan, retirement = await _begin_never_delivered_retirement(db)

    receipt = await db.publish_never_engaged_retirement_stage_receipt(
        _THREAD,
        expected_runtime_generation=retirement["generation"],
        expected_retirement_token=retirement["token"],
    )
    assert receipt is not None
    assert receipt["mount_id"] == installed["id"]
    assert receipt["engage_attempt"] == plan.engage_attempt
    assert receipt["source_binding_sha256"] == plan.source_sha256
    event = {
        "thread_id": _THREAD,
        "session_runtime_generation": retirement["generation"],
        "staged_epoch": 0,
        "file_count": 0,
        "counts": {"added": 0, "modified": 0, "deleted": 0},
        "mount_id": installed["id"],
    }
    assert await db.settle_pinned_thread_retirement(
        _THREAD,
        token=retirement["token"],
        generation=retirement["generation"],
        staged_event=event,
    )


@pytest.mark.asyncio
async def test_forged_retirement_receipt_source_cannot_settle(db) -> None:
    installed, plan, retirement = await _begin_never_delivered_retirement(db)
    forged = {
        "version": 1,
        "kind": "never_engaged",
        "runtime_generation": retirement["generation"],
        "retirement_token": retirement["token"],
        "mount_id": installed["id"],
        "engage_attempt": plan.engage_attempt,
        "source_binding_sha256": "f" * 64,
        "workspace_generation": None,
        "workspace_runtime_incarnation": None,
        "expected_staged_epoch": 0,
        "staged_epoch": 0,
        "staged_summary": None,
    }
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET runtime_retirement_stage_receipt=$2::jsonb "
            "WHERE id=$1::uuid",
            _THREAD,
            json.dumps(forged),
        )
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute(
                """
                UPDATE threads
                   SET status='ended', ended_at=now(),
                       runtime_retirement_token=NULL,
                       runtime_retirement_permanent=NULL,
                       runtime_retirement_started_at=NULL,
                       runtime_retirement_authorized_at=NULL,
                       runtime_retirement_context=NULL,
                       runtime_retirement_stage_receipt=NULL,
                       runtime_retirement_local_quiescence=NULL,
                       runtime_retirement_external_cleanup=NULL,
                       agent_id=NULL, control_admission_agent_id=NULL,
                       runtime_attach_token=NULL
                 WHERE id=$1::uuid
                """,
                _THREAD,
            )
        assert refused.value.constraint_name == (
            "threads_runtime_retirement_stage_receipt_source"
        )


@pytest.mark.asyncio
async def test_reader_attempt_effect_intent_blocks_premature_revoke(db) -> None:
    installed, plan = await _seed_protected_reader_authority(db)
    before = await db.get_protected_effect_database_time()
    capability = NextcloudEffectCapability(
        backend_instance_id=_A,
        config_sha256=_EFFECT_CONFIG_SHA,
        queue_bound_seconds=30,
        handler_bound_seconds=1,
        clock_skew_bound_seconds=1,
        safety_margin_seconds=1,
        capability_max_age_seconds=5,
        server_time=before,
    )
    signature = sign_protected_effect_capability(capability, key=_EFFECT_KEY)
    after = await db.get_protected_effect_database_time()
    validated = adopt_protected_effect_capability(
        capability.binding,
        signature=signature,
        key=_EFFECT_KEY,
        db_before=before,
        db_after=after,
        expected_backend_instance_id=_A,
        expected_config_sha256=_EFFECT_CONFIG_SHA,
    )
    assert validated is not None
    dispatched_at = await db.get_protected_effect_database_time()
    body = b"userid=reader"
    request = NextcloudEffectRequestAuthority(
        backend_instance_id=_A,
        config_sha256=_EFFECT_CONFIG_SHA,
        engage_attempt=_ATTEMPT,
        method="POST",
        path="/ocs/v2.php/cloud/users",
        body_sha256=hashlib.sha256(body).hexdigest(),
        effect_not_after=dispatched_at + timedelta(seconds=10),
    )
    request_signature = sign_protected_effect_request(request, key=_EFFECT_KEY)
    intent = NextcloudEffectFenceIntent.capture(
        capability=validated,
        request=request,
        request_signature=request_signature,
        key=_EFFECT_KEY,
        db_dispatched_at=dispatched_at,
    )
    effect_id = await db.install_cloud_ro_effect_intent(
        thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        expected_engage_attempt=_ATTEMPT,
        intent=intent,
    )
    assert effect_id is not None
    assert await db.begin_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    assert not await db.finish_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )

    closed_at = await db.get_protected_effect_database_time()
    horizon = NextcloudEffectHorizon.capture(
        intent=intent,
        db_dispatch_closed_at=closed_at,
    )
    forged_horizon = dict(horizon.binding)
    forged_horizon["safe_after"] = forged_horizon["dispatch_closed_at"]
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as forged_close:
            await conn.execute(
                """
                UPDATE cloud_ro_effect_intents
                   SET status='closed', horizon=$2::jsonb,
                       dispatch_closed_at=$3, safe_after=$3, closed_at=now()
                 WHERE id=$1::uuid
                """,
                effect_id,
                json.dumps(forged_horizon),
                closed_at,
            )
        assert forged_close.value.constraint_name == (
            "cloud_ro_effect_intents_horizon_authority"
        )
    assert await db.close_cloud_ro_effect_intent(
        effect_id,
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        expected_engage_attempt=_ATTEMPT,
        horizon=horizon,
    )
    assert not await db.finish_ro_mount_revocation_if_matches(
        installed["id"],
        expected_thread_id=_THREAD,
        expected_runtime_generation=_GENERATION,
        plan=plan,
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as bypass:
            await conn.execute(
                "UPDATE cloud_ro_mounts SET status='revoked' WHERE id=$1::uuid",
                installed["id"],
            )
        assert bypass.value.constraint_name == "cloud_ro_mounts_effect_fence"
