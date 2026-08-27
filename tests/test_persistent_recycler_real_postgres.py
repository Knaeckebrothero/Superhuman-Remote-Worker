"""Real-PostgreSQL authority and race proofs for persistent pod recycling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from security import crypto
from orchestrator.services import runtime_actor
from services.cloud.backend_instance_authority import (
    MainCloudBackendInstanceAuthority,
    main_cloud_installation_proof_sha256,
)
from services.cloud.handles import ProjectFolderHandle
from services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)
from services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)
from orchestrator.services.persistent_provisioner import (
    PersistentPodCreateResult,
    PersistentPodCreateStatus,
)
from orchestrator.services.persistent_recycler import (
    PersistentPodObservation,
    PersistentThreadRecycler,
)
from src.database.postgres_db import PostgresDB as AgentPostgresDB
from src.shared.persistent_input_delivery import (
    InputDeliveryAuthorityLost,
    claim_pending_input_deliveries,
    lock_runtime_authority,
    mark_input_delivery_queued,
    persist_input_delivery,
    transition_input_delivery,
)
from src.shared.runtime_actor import RUNTIME_ACTOR_BOOTSTRAP_HEADER

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


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
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied, monkeypatch):
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "false")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "P" * 32)
    crypto.reset_cipher_cache()
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=10,
    )
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute(
            "TRUNCATE cloud_ro_effect_intents, cloud_ro_mounts, thread_mounts, "
            "main_cloud_active_backend, main_cloud_backend_instances, "
            "thread_workspace_provision_intents, "
            "thread_agent_pod_provision_intents, "
            "thread_agent_workspace_claims, "
            "runtime_actor_access_tokens, runtime_actor_grants, "
            "runtime_actor_bootstraps, job_message_routes, session_wake_events, "
            "project_officers, threads, agents, project_members, projects, "
            "users CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()
        crypto.reset_cipher_cache()


class FakeProvisioner:
    expected_build_sha = "new-build"
    image_ref = "example.test/agent:sha-new-build"
    is_available = True

    def __init__(self):
        self.current: dict | None = None
        self.create_calls = 0
        self.created_targets: list[str | None] = []
        self.deleted_uids: list[str] = []
        self.pvc_identities: dict[str, tuple[str, str]] = {}
        self.fail_creates = False

    async def get_pod_status(self, thread_id: str):
        return dict(self.current) if self.current else None

    async def delete_agent_pod_exact(self, thread_id: str, *, expected_pod_uid: str):
        self.deleted_uids.append(expected_pod_uid)
        if self.current and self.current.get("pod_uid") == expected_pod_uid:
            self.current = None
        return True

    async def create_agent_pod(
        self,
        thread_id: str,
        *,
        config_name: str,
        lifecycle_generation: str,
        target_image_ref: str | None = None,
    ):
        self.create_calls += 1
        self.created_targets.append(target_image_ref)
        # Production provisioning uses create-or-reuse for this deterministic
        # PVC and never deletes it during pod recycle. Keep an external UID in
        # the fake so lifecycle tests can prove object identity, not just name.
        self.pvc_identities.setdefault(
            thread_id,
            (f"pvc-persistent-{thread_id[:12]}", f"pvc-{uuid4()}"),
        )
        if self.fail_creates:
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                f"persistent-{thread_id[:12]}",
                failure_class="injected_create_failure",
            )
        build = (
            target_image_ref.rsplit(":sha-", 1)[-1]
            if target_image_ref and ":sha-" in target_image_ref
            else self.expected_build_sha
        )
        uid = f"replacement-{lifecycle_generation[:8]}"
        self.current = _pod_status(
            thread_id,
            uid=uid,
            build=build,
            generation=lifecycle_generation,
            ready=False,
        )
        return PersistentPodCreateResult(
            PersistentPodCreateStatus.CREATED,
            f"persistent-{thread_id[:12]}",
            pod_uid=uid,
            build_sha=build,
        )


def _pod_status(
    thread_id: str,
    *,
    uid: str,
    build: str,
    generation: str | None = None,
    ready: bool = True,
):
    labels = {
        "srw/component": "persistent-agent",
        "srw/thread-id": thread_id,
        "srw/build-sha": build,
    }
    if generation:
        labels["srw/recycle-generation"] = generation
    return {
        "thread_id": thread_id,
        "pod_name": f"persistent-{thread_id[:12]}",
        "pod_uid": uid,
        "build_sha": build,
        "phase": "Running",
        "ready": ready,
        "terminating": False,
        "labels": labels,
    }


async def _seed(
    db: PostgresDB,
    *,
    preexisting_hold: dict | None = None,
    bind_agent: bool = True,
    publish_agent_pod: bool = True,
):
    ids = {key: str(uuid4()) for key in ("user", "project", "thread", "agent")}
    ids["attach_token"] = str(uuid4())
    metadata = {
        "config_override": {
            "officer": {"enabled": True, "hold": preexisting_hold},
            # This authority fixture has no separate workspace Pod. Model it
            # truthfully as the pinned lite tier; sandbox receipts require an
            # exact captured workspace generation/incarnation.
            "workspace": {"backend": "none"},
        },
    }
    if publish_agent_pod:
        metadata["agent_pod"] = {
            "pod_name": f"persistent-{ids['thread'][:12]}",
            "pod_uid": "old-pod",
            "observed_build_sha": "old-build",
        }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, display_name, email) VALUES ($1, 'owner', $2)",
            UUID(ids["user"]),
            f"{ids['user']}@example.test",
        )
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'recycle proof')",
            UUID(ids["project"]),
        )
        await conn.execute(
            "INSERT INTO project_members (project_id,user_id,role) "
            "VALUES ($1,$2,'owner')",
            UUID(ids["project"]),
            UUID(ids["user"]),
        )
        await conn.execute(
            "INSERT INTO threads "
            "(id,user_id,project_id,status,execution_lane,config_name,metadata) "
            "VALUES ($1,$2,$3,'active','pinned','centurion',$4::jsonb)",
            UUID(ids["thread"]),
            UUID(ids["user"]),
            UUID(ids["project"]),
            json.dumps(metadata),
        )
        if bind_agent:
            await conn.execute(
                "INSERT INTO agents "
                "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,last_heartbeat) "
                "VALUES ($1,'centurion',$2,'127.0.0.1','old-pod','session','persistent',now())",
                UUID(ids["agent"]),
                f"persistent-{ids['thread'][:12]}",
            )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE threads SET agent_id=$2, control_admission_agent_id=$2, "
                    "runtime_attach_token=$3 WHERE id=$1",
                    UUID(ids["thread"]),
                    UUID(ids["agent"]),
                    UUID(ids["attach_token"]),
                )
                await conn.execute(
                    "UPDATE agents SET thread_id=$2::uuid "
                    "WHERE id=$1::uuid AND thread_id IS NULL",
                    UUID(ids["agent"]),
                    UUID(ids["thread"]),
                )
        await conn.execute(
            "INSERT INTO project_officers (project_id,thread_id) VALUES ($1,$2)",
            UUID(ids["project"]),
            UUID(ids["thread"]),
        )
    if bind_agent:
        actor = await runtime_actor.mint_thread_runtime_actor(
            db, thread_id=ids["thread"], agent_id=ids["agent"]
        )
        ids["old_access"] = actor.access_credential
    return ids


async def _seed_protected_ro_attempt(
    db: PostgresDB,
    ids: dict[str, str],
    *,
    status: str = "engaging",
) -> tuple[str, ProtectedNextcloudReaderGrantPlan, str, str]:
    """Install one canonical attempt-scoped reader for retirement proofs."""

    backend_instance_id = str(uuid4())
    selected_mount_id = str(uuid4())
    attempt = str(uuid4())
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    authority = MainCloudBackendInstanceAuthority.capture(
        backend_instance_id=backend_instance_id,
        backend_id="nextcloud",
        routing={
            "version": 1,
            "backend_id": "nextcloud",
            "base_url": "https://cloud.internal.invalid",
            "public_url": "https://cloud.invalid",
            "admin_user": "admin",
            "agent_user": "agent-service",
            "protected_effect_url": "http://protected-effect.internal.invalid",
            "protected_effect_config_sha256": "a" * 64,
        },
        installation_proof_sha256=main_cloud_installation_proof_sha256(
            backend_id="nextcloud",
            remote_identity=f"installation-{backend_instance_id}",
        ),
        secret_refs={
            "admin_password": "env:NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
            "protected_effect_hmac_key": ("env:NEXTCLOUD_PROTECTED_EFFECT_HMAC_KEY"),
        },
    )
    installed = await db.install_initial_main_cloud_backend_instance(authority)
    if installed is None:
        async with db.acquire() as conn:
            instances = await conn.fetch(
                "SELECT id,backend_id,routing,routing_sha256,"
                "installation_proof_sha256,secret_refs,secret_revision "
                "FROM main_cloud_backend_instances"
            )
            active = await conn.fetch("SELECT * FROM main_cloud_active_backend")
        pytest.fail(
            "initial protected-reader backend instance did not install: "
            f"expected={authority.binding!r}, "
            f"instances={[dict(row) for row in instances]!r}, "
            f"active={[dict(row) for row in active]!r}"
        )
    handle = ProjectFolderHandle(
        backend="nextcloud",
        native_id="7",
        vendor_meta={"mountpoint": "Retirement Proof"},
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE projects SET main_cloud_backend='nextcloud',"
            "main_cloud_backend_instance_id=$2::uuid,"
            "main_cloud_folder_handle=$3 WHERE id=$1::uuid",
            UUID(ids["project"]),
            UUID(backend_instance_id),
            handle.to_db(),
        )
        await conn.execute(
            "UPDATE threads SET main_cloud_backend='nextcloud',"
            "main_cloud_backend_instance_id=$2::uuid,metadata=$3::jsonb "
            "WHERE id=$1::uuid",
            UUID(ids["thread"]),
            UUID(backend_instance_id),
            json.dumps(
                {
                    "protected_cloud": True,
                    "config_override": {"workspace": {"backend": "sandbox"}},
                }
            ),
        )
        await conn.execute(
            "INSERT INTO thread_mounts "
            "(id,thread_id,mount_kind,target_path,source_kind,source_ref,"
            "backend_id,backend_instance_id,cloud_handle,webdav_url) VALUES "
            "($1::uuid,$2::uuid,'project','cloud','project_folder',$3::uuid,"
            "'nextcloud',$4::uuid,$5,$6)",
            UUID(selected_mount_id),
            UUID(ids["thread"]),
            UUID(ids["project"]),
            UUID(backend_instance_id),
            handle.to_db(),
            "https://cloud.internal.invalid/remote.php/dav/files/reader",
        )
    source = ProtectedMountSourceIdentity(
        backend_instance_id=backend_instance_id,
        source_ref=ids["project"],
        target_path="cloud",
        native_id="7",
        mountpoint="Retirement Proof",
    )
    plan = ProtectedNextcloudReaderGrantPlan(
        engage_attempt=attempt,
        backend_instance_id=backend_instance_id,
        source=source,
    )
    installed = await db.install_ro_mount_engage_intent(
        thread_id=ids["thread"],
        user_id=ids["user"],
        selected_mount_id=selected_mount_id,
        expected_runtime_generation=generation,
        plan=plan,
        credentials="attempt-secret",
        webdav_url="https://cloud.internal.invalid/dav/attempt",
    )
    assert installed is not None
    row_id = str(installed["id"])
    if status == "active":
        assert await db.activate_ro_mount_attempt_with_baseline(
            row_id,
            {},
            thread_id=ids["thread"],
            user_id=ids["user"],
            selected_mount_id=selected_mount_id,
            expected_runtime_generation=generation,
            plan=plan,
        )
    elif status == "revoked":
        assert await db.begin_ro_mount_revocation_if_matches(
            row_id,
            expected_thread_id=ids["thread"],
            expected_runtime_generation=generation,
            plan=plan,
        )
        assert await db.finish_ro_mount_revocation_if_matches(
            row_id,
            expected_thread_id=ids["thread"],
            expected_runtime_generation=generation,
            plan=plan,
        )
    elif status != "engaging":
        raise AssertionError(f"unsupported protected-reader status: {status}")
    return row_id, plan, generation, selected_mount_id


@pytest.mark.asyncio
async def test_pinned_session_binding_reads_one_reciprocal_pool_snapshot(db):
    ids = await _seed(db, publish_agent_pod=False)
    thread = await db.get_thread(ids["thread"])

    binding = await db.get_pinned_session_binding(
        ids["thread"],
        expected_runtime_generation=str(thread["runtime_generation"]),
    )

    assert binding is not None
    assert binding.thread_id == ids["thread"]
    assert binding.runtime_generation == str(thread["runtime_generation"])
    assert binding.agent_id == ids["agent"]
    assert binding.runtime_attach_token == ids["attach_token"]
    assert binding.agent_hostname == f"persistent-{ids['thread'][:12]}"
    assert binding.pod_uid == "old-pod"
    assert binding.pod_ip == "127.0.0.1"
    assert binding.pod_port == 8001
    assert binding.agent_status == "session"

    assert (
        await db.get_pinned_session_binding(
            ids["thread"],
            expected_runtime_generation=str(uuid4()),
        )
        is None
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET pod_uid=NULL WHERE id=$1::uuid",
            ids["agent"],
        )
    assert (
        await db.get_pinned_session_binding(
            ids["thread"],
            expected_runtime_generation=str(thread["runtime_generation"]),
        )
        is None
    )


@pytest.mark.asyncio
async def test_pinned_session_binding_requires_exact_dedicated_pod_attempt(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    thread = await db.get_thread(ids["thread"])
    generation = str(thread["runtime_generation"])
    attempt = str(uuid4())
    pod_name = f"persistent-{ids['thread'][:12]}"

    reserved = await db.reserve_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=attempt,
        pod_name=pod_name,
        provisioner="persistent",
    )
    assert reserved is not None
    assert await db.publish_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=attempt,
        pod_name=pod_name,
        pod_uid="dedicated-pod-uid",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,last_heartbeat) "
            "VALUES ($1,'centurion',$2,'127.0.0.2',$3,'session','persistent',now())",
            UUID(ids["agent"]),
            pod_name,
            "dedicated-pod-uid",
        )
        async with conn.transaction():
            await conn.execute(
                "UPDATE threads SET agent_id=$2,control_admission_agent_id=$2,"
                "runtime_attach_token=$3 WHERE id=$1",
                UUID(ids["thread"]),
                UUID(ids["agent"]),
                UUID(ids["attach_token"]),
            )
            await conn.execute(
                "UPDATE agents SET thread_id=$2 WHERE id=$1",
                UUID(ids["agent"]),
                UUID(ids["thread"]),
            )

    assert (
        await db.get_pinned_session_binding(
            ids["thread"], expected_runtime_generation=generation
        )
        is not None
    )

    # The row trigger deliberately owns name/UID reciprocity; the joined
    # selection additionally refuses a stale/corrupt G or provision-attempt
    # coordinate instead of adopting it as current physical authority.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=jsonb_set(metadata,"
            "'{agent_pod,runtime_generation}',to_jsonb($2::text)) WHERE id=$1",
            UUID(ids["thread"]),
            str(uuid4()),
        )
    assert (
        await db.get_pinned_session_binding(
            ids["thread"], expected_runtime_generation=generation
        )
        is None
    )

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=jsonb_set(jsonb_set(metadata,"
            "'{agent_pod,runtime_generation}',to_jsonb($2::text)),"
            "'{agent_pod,provision_attempt}',to_jsonb($3::text)) WHERE id=$1",
            UUID(ids["thread"]),
            generation,
            str(uuid4()),
        )
    assert (
        await db.get_pinned_session_binding(
            ids["thread"], expected_runtime_generation=generation
        )
        is None
    )


async def _authorize_and_ack(
    db: PostgresDB,
    ids: dict[str, str],
    authority: dict,
    *,
    settle_status: str = "ended",
) -> None:
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status=settle_status,
    )
    context = authority["context"]
    workspace = context.get("workspace_container") or {}
    binding = context.get("workspace_binding") or {}
    backend = str(context.get("workspace_backend") or "")
    protocol = {
        "sandbox": "workspace_process_zero_v1",
        "virtual": "agent_runtime_zero_v1",
        "none": "agent_runtime_zero_v1",
        "vm": "workspace_actuator_zero_v1",
        "remote": "workspace_actuator_zero_v1",
    }[backend]
    vm = context.get("vm") or {}
    expected_generation = (
        binding.get("generation")
        if backend == "sandbox"
        else vm.get("provision_generation")
        if backend in {"vm", "remote"}
        else None
    )
    expected_runtime = (
        workspace.get("_runtime_incarnation")
        if backend == "sandbox"
        else vm.get("vm_uid")
        if backend in {"vm", "remote"}
        else None
    )
    receipt = await db.acknowledge_pinned_thread_local_quiescence(
        ids["thread"],
        expected_runtime_generation=authority["generation"],
        expected_retirement_token=authority["token"],
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
        expected_settle_status=settle_status,
        expected_quiescence_protocol=protocol,
        expected_workspace_generation=expected_generation,
        expected_workspace_runtime_incarnation=expected_runtime,
    )
    assert receipt is not None


async def _recycle_state(db: PostgresDB, thread_id: str):
    row = await db.get_thread(thread_id)
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return metadata["agent_pod"]["recycle"], metadata


async def _runtime_identity(db: PostgresDB, thread_id: str) -> tuple[str, str]:
    thread = await db.get_thread(thread_id)
    assert thread is not None
    generation = str(thread.get("runtime_generation") or "")
    attach_token = str(thread.get("runtime_attach_token") or "")
    assert generation and attach_token
    return generation, attach_token


async def _reconcile_until_phase(
    recycler: PersistentThreadRecycler,
    *,
    thread_id: str,
    reason: str,
    expected_build_sha: str | None,
    expected_project_id: str,
    phases: set[str],
    limit: int = 8,
):
    """Drive the durable recycler through its intentionally split DB phases."""

    result = None
    for _ in range(limit):
        result = await recycler.request_and_reconcile(
            thread_id=thread_id,
            reason=reason,
            expected_build_sha=expected_build_sha,
            expected_project_id=expected_project_id,
        )
        if result.phase in phases:
            return result
    current = await recycler._read_recycle(thread_id)
    thread = await recycler._db.get_thread(thread_id)
    raise AssertionError(
        f"recycler did not reach {sorted(phases)!r}; "
        f"last phase was {getattr(result, 'phase', None)!r}; "
        f"state={current!r}; "
        f"thread_generation={thread.get('runtime_generation') if thread else None!r}; "
        f"thread_attach={thread.get('runtime_attach_token') if thread else None!r}; "
        f"thread_agent={thread.get('agent_id') if thread else None!r}"
    )


def _managed_gitea(*, probe: bool = True) -> MagicMock:
    client = MagicMock()
    client.repository_owner = "srw"
    client.is_initialized = True
    client.clean_repo_url = MagicMock(
        side_effect=lambda name: f"http://gitea:3000/srw/{name}.git"
    )
    client.ensure_repo_deploy_key = AsyncMock(return_value=91)
    client.probe_repo_deploy_key = AsyncMock(return_value=probe)
    return client


async def _bind_replacement_agent(
    db: PostgresDB, *, thread_id: str, pod_uid: str
) -> tuple[str, runtime_actor.RuntimeActorContext]:
    agent_id = str(uuid4())
    attach_token = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,last_heartbeat) "
            "VALUES ($1,'centurion',$2,'127.0.0.2',$3,'session','persistent',now())",
            UUID(agent_id),
            f"persistent-{thread_id[:12]}",
            pod_uid,
        )
        async with conn.transaction():
            old_agent_id = await conn.fetchval(
                "SELECT agent_id FROM threads WHERE id=$1 FOR UPDATE",
                UUID(thread_id),
            )
            if old_agent_id is not None and str(old_agent_id) != agent_id:
                await conn.execute(
                    "UPDATE agents SET thread_id=NULL,status='offline' WHERE id=$1",
                    old_agent_id,
                )
            await conn.execute(
                "UPDATE threads SET agent_id=$2,control_admission_agent_id=$2,"
                "runtime_attach_token=$3,status='active',"
                "metadata=jsonb_set(jsonb_set(metadata,"
                "'{agent_pod,pod_name}',to_jsonb($4::text),true),"
                "'{agent_pod,pod_uid}',to_jsonb($5::text),true) WHERE id=$1",
                UUID(thread_id),
                UUID(agent_id),
                UUID(attach_token),
                f"persistent-{thread_id[:12]}",
                pod_uid,
            )
            await conn.execute(
                "UPDATE agents SET thread_id=$2 WHERE id=$1 AND thread_id IS NULL",
                UUID(agent_id),
                UUID(thread_id),
            )
    actor = await runtime_actor.mint_thread_runtime_actor(
        db, thread_id=thread_id, agent_id=agent_id
    )
    return agent_id, actor


@pytest.mark.asyncio
async def test_ended_transition_trigger_blocks_old_agent_but_allows_resume(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    before_generation = (await db.get_thread(ids["thread"]))["runtime_generation"]
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='ended', agent_id=NULL WHERE id=$1",
            UUID(ids["thread"]),
        )
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute(
                "UPDATE threads SET status='active' WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert refused.value.constraint_name == "threads_ended_transition_fence"
        assert (
            await conn.fetchval(
                "SELECT status::text FROM threads WHERE id=$1", UUID(ids["thread"])
            )
            == "ended"
        )

    assert await db.resume_thread(ids["thread"]) is True
    resumed = await db.get_thread(ids["thread"])
    assert resumed["status"] == "created"
    assert resumed["agent_id"] is None
    assert resumed["runtime_generation"] != before_generation


@pytest.mark.asyncio
async def test_pinned_retirement_closes_admission_until_exact_abort(db):
    ids = await _seed(db)
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)

    assert authority["state"] == "pending"
    assert authority["reused"] is False
    assert authority["context"]["agent_id"] == ids["agent"]
    assert authority["context"]["agent"]["pod_uid"] == "old-pod"
    assert authority["context"]["route"]["owner_pod_uid"] == "old-pod"
    thread = await db.get_thread(ids["thread"])
    assert thread["status"] == "active"
    assert thread["runtime_retirement_token"] == UUID(authority["token"])
    # The marker itself closes admission; ownership stays byte-for-byte
    # unchanged so a non-force preflight abort can reopen the same runtime.
    assert str(thread["control_admission_agent_id"]) == ids["agent"]
    assert not await db.pinned_runtime_generation_is_open(
        ids["thread"], authority["generation"]
    )
    assert await db.resume_thread(ids["thread"]) is False

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute(
                "UPDATE threads SET status='awaiting_user' WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert refused.value.constraint_name == "threads_runtime_retirement_pending"
        with pytest.raises(asyncpg.CheckViolationError) as replaced:
            await conn.execute(
                "UPDATE threads SET runtime_retirement_token=$2, "
                "runtime_retirement_context=jsonb_set("
                "runtime_retirement_context,'{forged}','true'::jsonb) "
                "WHERE id=$1",
                UUID(ids["thread"]),
                uuid4(),
            )
        assert replaced.value.constraint_name == "threads_runtime_retirement_immutable"
        with pytest.raises(asyncpg.CheckViolationError) as forged_abort:
            await conn.execute(
                "UPDATE threads SET runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_context=NULL, agent_id=NULL, "
                "control_admission_agent_id=NULL, runtime_attach_token=NULL "
                "WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert (
            forged_abort.value.constraint_name == "threads_runtime_retirement_ownership"
        )

    assert not await db.abort_pinned_thread_retirement(
        ids["thread"], token=str(uuid4()), generation=authority["generation"]
    )
    assert await db.abort_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
    )
    assert await db.pinned_runtime_generation_is_open(
        ids["thread"], authority["generation"]
    )
    reopened = await db.get_thread(ids["thread"])
    assert str(reopened["agent_id"]) == ids["agent"]
    assert str(reopened["control_admission_agent_id"]) == ids["agent"]
    assert str(reopened["runtime_attach_token"]) == ids["attach_token"]


@pytest.mark.asyncio
async def test_authorized_retirement_is_irrevocable(db):
    ids = await _seed(db)
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )
    assert not await db.abort_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute(
                "UPDATE threads SET runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_authorized_at=NULL, "
                "runtime_retirement_context=NULL, "
                "runtime_retirement_stage_receipt=NULL, "
                "runtime_retirement_local_quiescence=NULL WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert (
            refused.value.constraint_name
            == "threads_runtime_retirement_stage_receipt_pending"
        )
    assert (await db.get_thread(ids["thread"]))["runtime_retirement_token"] == UUID(
        authority["token"]
    )


@pytest.mark.asyncio
async def test_permanent_retirement_is_abortable_only_before_authorization(db):
    ids = await _seed(db)
    hidden = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.abort_pinned_thread_retirement(
        ids["thread"],
        token=hidden["token"],
        generation=hidden["generation"],
    )
    reopened = await db.get_thread(ids["thread"])
    assert reopened["runtime_retirement_token"] is None
    assert str(reopened["agent_id"]) == ids["agent"]

    authorized = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authorized["token"],
        generation=authorized["generation"],
        settle_status="ended",
    )
    assert not await db.abort_pinned_thread_retirement(
        ids["thread"],
        token=authorized["token"],
        generation=authorized["generation"],
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as direct_abort:
            await conn.execute(
                "UPDATE threads SET runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_authorized_at=NULL, "
                "runtime_retirement_context=NULL WHERE id=$1",
                UUID(ids["thread"]),
            )
    assert (
        direct_abort.value.constraint_name
        == "threads_runtime_retirement_stage_receipt_pending"
    )


@pytest.mark.asyncio
async def test_permanent_delete_requires_exact_physical_quiescence(db):
    """DELETE cannot bypass the soft-settlement UPDATE trigger."""

    ids = await _seed(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as no_begin:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert no_begin.value.constraint_name == "threads_pinned_delete_authority"

    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )
    with pytest.raises(RuntimeError, match="lacks physical quiescence"):
        await db.delete_thread(
            ids["thread"],
            expected_runtime_retirement_token=authority["token"],
            expected_runtime_generation=authority["generation"],
        )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as no_receipt:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert no_receipt.value.constraint_name == "threads_pinned_delete_authority"
        assert await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM threads WHERE id=$1)",
            UUID(ids["thread"]),
        )
        assert not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM thread_runtime_retirement_outcomes "
            "WHERE thread_id=$1)",
            UUID(ids["thread"]),
        )

    await _authorize_and_ack(db, ids, authority)
    assert await db.clear_pinned_retirement_physical_runtime_endpoint(
        ids["thread"],
        runtime_generation=authority["generation"],
        retirement_token=authority["token"],
    )
    await db.delete_thread(
        ids["thread"],
        expected_runtime_retirement_token=authority["token"],
        expected_runtime_generation=authority["generation"],
    )
    assert await db.get_thread(ids["thread"]) is None
    outcome = await db.get_pinned_thread_retirement_outcome(
        ids["thread"],
        runtime_generation=authority["generation"],
        retirement_token=authority["token"],
        agent_id=ids["agent"],
        runtime_attach_token=ids["attach_token"],
        disposition="ended",
        permanent=True,
    )
    assert outcome is not None
    assert outcome["outcome"] == "deleted"


@pytest.mark.asyncio
async def test_soft_ended_generation_is_durable_quiescence_for_later_delete(db):
    """A same-G soft settlement permits later destruction without a live ACK."""

    ids = await _seed(db)
    workspace_generation = str(uuid4())
    workspace_runtime = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=metadata || jsonb_build_object("
            "'workspace_container',jsonb_build_object("
            "'status','ready','provisioner','k8s','namespace','default',"
            "'pod_name','workspace-soft-settlement',"
            "'_runtime_incarnation',$2::text,"
            "'_canvas_workspace_generation',$3::text),"
            "'_workspace_binding',jsonb_build_object("
            "'generation',$3::text,'kind','remote','backing_id',"
            "'k8s-pod:default:' || $2::text,"
            "'ssh_host_key_fingerprint','SHA256:test'),"
            "'config_override',jsonb_build_object('officer',jsonb_build_object("
            "'enabled',true),'workspace',jsonb_build_object('backend','sandbox'))) "
            "WHERE id=$1",
            UUID(ids["thread"]),
            workspace_runtime,
            workspace_generation,
        )
    soft = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    await _authorize_and_ack(db, ids, soft)
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=soft["token"],
        generation=soft["generation"],
        final_status="ended",
    )
    ended = await db.get_thread(ids["thread"])
    assert ended["status"] == "ended"
    assert ended["runtime_authority_exposed"] is True
    assert ended["runtime_retirement_local_quiescence"] is None
    assert ended["agent_id"] is None
    assert ended["runtime_attach_token"] is None
    ended_metadata = _json(ended["metadata"])
    assert (
        ended_metadata["workspace_container"]["_runtime_incarnation"]
        == workspace_runtime
    )

    permanent = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=permanent["token"],
        generation=permanent["generation"],
        settle_status="ended",
    )
    assert await db.pinned_thread_has_prior_soft_settlement(
        ids["thread"],
        runtime_generation=permanent["generation"],
        retirement_token=permanent["token"],
    )
    # A prior settlement proves process zero only.  The retained Pod/PVC
    # identity still blocks both the helper and a literal DELETE until the
    # permanent actuator's exact DB half clears it.
    with pytest.raises(RuntimeError, match="lacks physical quiescence"):
        await db.delete_thread(
            ids["thread"],
            expected_runtime_retirement_token=permanent["token"],
            expected_runtime_generation=permanent["generation"],
        )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as retained:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert retained.value.constraint_name == "threads_pinned_delete_authority"
    assert await db.clear_pinned_retirement_physical_runtime_endpoint(
        ids["thread"],
        runtime_generation=permanent["generation"],
        retirement_token=permanent["token"],
        completed_external_cleanup_protocol="sandbox_actuator_zero_v1",
    )
    cleared = _json((await db.get_thread(ids["thread"]))["metadata"])
    assert "_workspace_binding" not in cleared
    assert cleared["workspace_container"]["_runtime_incarnation"] is None
    await db.delete_thread(
        ids["thread"],
        expected_runtime_retirement_token=permanent["token"],
        expected_runtime_generation=permanent["generation"],
    )
    assert await db.get_thread(ids["thread"]) is None
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM thread_runtime_retirement_outcomes "
                "WHERE thread_id=$1",
                UUID(ids["thread"]),
            )
            == 2
        )


@pytest.mark.asyncio
async def test_permanent_sandbox_absence_accepts_orchestrator_zero_receipt(db):
    """Exact Pod absence may receipt a permanent bound sandbox retirement."""

    ids = await _seed(db)
    workspace_generation = str(uuid4())
    workspace_runtime = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=metadata || jsonb_build_object("
            "'workspace_container',jsonb_build_object("
            "'status','ready','provisioner','k8s','namespace','default',"
            "'pod_name','workspace-absent-retirement',"
            "'_runtime_incarnation',$2::text,"
            "'_canvas_workspace_generation',$3::text),"
            "'_workspace_binding',jsonb_build_object("
            "'generation',$3::text,'kind','remote','backing_id',"
            "'k8s-pod:default:' || $2::text,"
            "'ssh_host_key_fingerprint','SHA256:test'),"
            "'config_override',jsonb_build_object('officer',jsonb_build_object("
            "'enabled',true),'workspace',jsonb_build_object('backend','sandbox'))) "
            "WHERE id=$1",
            UUID(ids["thread"]),
            workspace_runtime,
            workspace_generation,
        )
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )
    assert (
        await db.acknowledge_pinned_thread_local_quiescence(
            ids["thread"],
            expected_runtime_generation=authority["generation"],
            expected_retirement_token=authority["token"],
            expected_agent_id=ids["agent"],
            expected_attach_token=ids["attach_token"],
            expected_settle_status="ended",
            expected_quiescence_protocol="sandbox_actuator_zero_v1",
            expected_workspace_generation=workspace_generation,
            expected_workspace_runtime_incarnation=workspace_runtime,
            quiescence_actor="agent",
        )
        is None
    )
    receipt = await db.acknowledge_pinned_thread_local_quiescence(
        ids["thread"],
        expected_runtime_generation=authority["generation"],
        expected_retirement_token=authority["token"],
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
        expected_settle_status="ended",
        expected_quiescence_protocol="sandbox_actuator_zero_v1",
        expected_workspace_generation=workspace_generation,
        expected_workspace_runtime_incarnation=workspace_runtime,
        quiescence_actor="orchestrator",
    )
    assert receipt is not None
    assert receipt["quiescence_protocol"] == "sandbox_actuator_zero_v1"
    assert receipt["quiescence_actor"] == "orchestrator"


@pytest.mark.asyncio
async def test_unexposed_permanent_delete_waits_for_external_runtime_cleanup(db):
    ids = await _seed(db, bind_agent=False)
    runtime_uid = str(uuid4())
    binding_generation = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=jsonb_build_object("
            "'config_override',jsonb_build_object('workspace',jsonb_build_object("
            "'backend','sandbox')),'workspace_container',jsonb_build_object("
            "'status','ready','provisioner','k8s','namespace','default',"
            "'pod_name','workspace-unexposed-cleanup',"
            "'_runtime_incarnation',$2::text,"
            "'_canvas_workspace_generation',$3::text),"
            "'_workspace_binding',jsonb_build_object("
            "'generation',$3::text,'kind','remote','backing_id',"
            "'k8s-pod:default:' || $2::text,"
            "'ssh_host_key_fingerprint','SHA256:test')) WHERE id=$1",
            UUID(ids["thread"]),
            runtime_uid,
            binding_generation,
        )
    before = await db.get_thread(ids["thread"])
    assert before["runtime_authority_exposed"] is False
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )
    with pytest.raises(RuntimeError, match="lacks physical quiescence"):
        await db.delete_thread(
            ids["thread"],
            expected_runtime_retirement_token=authority["token"],
            expected_runtime_generation=authority["generation"],
        )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert refused.value.constraint_name == "threads_pinned_delete_authority"
    assert await db.clear_pinned_retirement_physical_runtime_endpoint(
        ids["thread"],
        runtime_generation=authority["generation"],
        retirement_token=authority["token"],
        completed_external_cleanup_protocol="sandbox_actuator_zero_v1",
    )
    cleared = _json((await db.get_thread(ids["thread"]))["metadata"])
    assert "_workspace_binding" not in cleared
    assert cleared["workspace_container"]["status"] == "deleted"
    await db.delete_thread(
        ids["thread"],
        expected_runtime_retirement_token=authority["token"],
        expected_runtime_generation=authority["generation"],
    )
    assert await db.get_thread(ids["thread"]) is None


@pytest.mark.asyncio
async def test_physical_cleanup_accepts_only_captured_suspend_completion(db):
    """The fenced suspending generation may finish; a ready capture may not."""

    for captured_status, expected in (("suspending", True), ("ready", False)):
        ids = await _seed(db, bind_agent=False)
        runtime_uid = str(uuid4())
        binding_generation = str(uuid4())
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object("
                "'config_override',jsonb_build_object('workspace',jsonb_build_object("
                "'backend','sandbox')),'workspace_container',jsonb_build_object("
                "'status',$2::text,'provisioner','k8s','namespace','default',"
                "'pod_name','workspace-suspend-completion',"
                "'_runtime_incarnation',$3::text,"
                "'_canvas_workspace_generation',$4::text),"
                "'_workspace_binding',jsonb_build_object("
                "'generation',$4::text,'kind','remote','backing_id',"
                "'k8s-pod:default:' || $3::text,"
                "'ssh_host_key_fingerprint','SHA256:test')) WHERE id=$1",
                UUID(ids["thread"]),
                captured_status,
                runtime_uid,
                binding_generation,
            )
        authority = await db.begin_pinned_thread_retirement(
            ids["thread"], permanent=True
        )
        assert await db.authorize_pinned_thread_retirement(
            ids["thread"],
            token=authority["token"],
            generation=authority["generation"],
            settle_status="ended",
        )
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_set("
                "metadata,'{workspace_container,status}','\"suspended\"'::jsonb) "
                "WHERE id=$1::uuid",
                UUID(ids["thread"]),
            )
        assert (
            await db.clear_pinned_retirement_physical_runtime_endpoint(
                ids["thread"],
                runtime_generation=authority["generation"],
                retirement_token=authority["token"],
                completed_external_cleanup_protocol="sandbox_actuator_zero_v1",
            )
            is expected
        )


@pytest.mark.asyncio
async def test_direct_metadata_clear_cannot_forge_external_cleanup_receipt(db):
    """Captured physical authority still needs the DB-owned receipt edge."""

    ids = await _seed(db, bind_agent=False)
    runtime_uid = str(uuid4())
    binding_generation = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=jsonb_build_object("
            "'config_override',jsonb_build_object('workspace',jsonb_build_object("
            "'backend','sandbox')),'workspace_container',jsonb_build_object("
            "'status','ready','provisioner','k8s','namespace','default',"
            "'pod_name','workspace-direct-clear',"
            "'_runtime_incarnation',$2::text,"
            "'_canvas_workspace_generation',$3::text),"
            "'_workspace_binding',jsonb_build_object("
            "'generation',$3::text,'kind','remote','backing_id',"
            "'k8s-pod:default:' || $2::text,"
            "'ssh_host_key_fingerprint','SHA256:test')) WHERE id=$1",
            UUID(ids["thread"]),
            runtime_uid,
            binding_generation,
        )
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )

    # An unaware same-role writer can make the mutable mirror look absent, but
    # it cannot mint the append-once transition that the literal DELETE belt
    # requires from the trusted actuator path.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=metadata "
            "- 'workspace_container' - '_workspace_binding' WHERE id=$1",
            UUID(ids["thread"]),
        )
        assert await conn.fetchval(
            "SELECT runtime_retirement_external_cleanup IS NULL "
            "FROM threads WHERE id=$1",
            UUID(ids["thread"]),
        )
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert refused.value.constraint_name == "threads_pinned_delete_authority"


@pytest.mark.asyncio
async def test_modern_soft_outcome_cannot_be_relabelled_as_legacy_tombstone(db):
    """Erasing retained mirrors never reopens the markerless DELETE lane."""

    ids = await _seed(db, bind_agent=False)
    runtime_uid = str(uuid4())
    binding_generation = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=jsonb_build_object("
            "'config_override',jsonb_build_object('workspace',jsonb_build_object("
            "'backend','sandbox')),'workspace_container',jsonb_build_object("
            "'status','ready','provisioner','k8s','namespace','default',"
            "'pod_name','workspace-soft-outcome',"
            "'_runtime_incarnation',$2::text,"
            "'_canvas_workspace_generation',$3::text),"
            "'_workspace_binding',jsonb_build_object("
            "'generation',$3::text,'kind','remote','backing_id',"
            "'k8s-pod:default:' || $2::text,"
            "'ssh_host_key_fingerprint','SHA256:test')) WHERE id=$1",
            UUID(ids["thread"]),
            runtime_uid,
            binding_generation,
        )
    soft = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=soft["token"],
        generation=soft["generation"],
        settle_status="ended",
    )
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=soft["token"],
        generation=soft["generation"],
        final_status="ended",
    )

    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM thread_runtime_retirement_outcomes "
            "WHERE thread_id=$1 AND permanent=false AND outcome='settled')",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "UPDATE threads SET metadata='{}'::jsonb WHERE id=$1",
            UUID(ids["thread"]),
        )
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert refused.value.constraint_name == "threads_pinned_delete_authority"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome_shape", ["wrong_generation", "wrong_disposition", "wrong_outcome"]
)
async def test_nonmatching_prior_soft_outcome_cannot_be_forged(db, outcome_shape):
    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='ended',runtime_authority_exposed=true "
            "WHERE id=$1",
            UUID(ids["thread"]),
        )
        generation = await conn.fetchval(
            "SELECT runtime_generation FROM threads WHERE id=$1", UUID(ids["thread"])
        )
        outcome_generation = (
            uuid4() if outcome_shape == "wrong_generation" else generation
        )
        disposition = "suspended" if outcome_shape == "wrong_disposition" else "ended"
        outcome = "deleted" if outcome_shape == "wrong_outcome" else "settled"
        with pytest.raises(asyncpg.CheckViolationError) as forged:
            await conn.execute(
                "INSERT INTO thread_runtime_retirement_outcomes "
                "(thread_id,runtime_generation,retirement_token,disposition,"
                "permanent,outcome) VALUES ($1,$2,$3,$4,false,$5)",
                UUID(ids["thread"]),
                outcome_generation,
                uuid4(),
                disposition,
                outcome,
            )
        assert forged.value.constraint_name == "thread_runtime_outcome_insert_authority"


@pytest.mark.asyncio
async def test_prior_soft_outcome_cannot_authorize_resumed_exposed_generation(db):
    ids = await _seed(db)
    soft = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    await _authorize_and_ack(db, ids, soft)
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=soft["token"],
        generation=soft["generation"],
        final_status="ended",
    )
    assert await db.resume_thread(ids["thread"])
    successor, _actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid="successor-pod"
    )
    permanent = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert permanent["generation"] != soft["generation"]
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=permanent["token"],
        generation=permanent["generation"],
        settle_status="ended",
    )
    assert not await db.pinned_thread_has_prior_soft_settlement(
        ids["thread"],
        runtime_generation=permanent["generation"],
        retirement_token=permanent["token"],
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert refused.value.constraint_name == "threads_pinned_delete_authority"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "physical_evidence",
    [
        "sandbox",
        "binding",
        "endpoint",
        "provisioning",
        "agent_pod",
        "vm",
        "reader",
        "malformed_workspace",
        "malformed_binding",
        "malformed_vm",
    ],
)
async def test_legacy_unexposed_delete_requires_physical_runtime_absence(
    db, physical_evidence
):
    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='ended', metadata='{}'::jsonb WHERE id=$1",
            UUID(ids["thread"]),
        )
        if physical_evidence == "sandbox":
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object("
                "'workspace_container',jsonb_build_object("
                "'status','ready','_runtime_incarnation',$2::text),"
                "'_workspace_binding',jsonb_build_object("
                "'generation',$3::text)) WHERE id=$1",
                UUID(ids["thread"]),
                str(uuid4()),
                str(uuid4()),
            )
        elif physical_evidence == "binding":
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object("
                "'_workspace_binding',jsonb_build_object('generation',$2::text)) "
                "WHERE id=$1",
                UUID(ids["thread"]),
                str(uuid4()),
            )
        elif physical_evidence == "endpoint":
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object("
                "'workspace_container',jsonb_build_object('status','deleted',"
                "'pod_ip','10.42.0.9')) WHERE id=$1",
                UUID(ids["thread"]),
            )
        elif physical_evidence == "provisioning":
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object("
                "'workspace_container',jsonb_build_object('status','provisioning')) "
                "WHERE id=$1",
                UUID(ids["thread"]),
            )
        elif physical_evidence == "agent_pod":
            with pytest.raises(asyncpg.CheckViolationError) as refused:
                await conn.execute(
                    "UPDATE threads SET metadata=jsonb_build_object("
                    "'agent_pod',jsonb_build_object('pod_name','persistent-old',"
                    "'pod_uid','pod-u1')) WHERE id=$1",
                    UUID(ids["thread"]),
                )
            assert refused.value.constraint_name == "threads_ended_runtime_authority"
            return
        elif physical_evidence == "vm":
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object("
                "'vm',jsonb_build_object('status','ready','vm_uid','vm-u1',"
                "'provision_generation',$2::text)) WHERE id=$1",
                UUID(ids["thread"]),
                str(uuid4()),
            )
        elif physical_evidence == "reader":
            with pytest.raises(asyncpg.CheckViolationError) as refused:
                await conn.execute(
                    "INSERT INTO cloud_ro_mounts "
                    "(thread_id,user_id,backend,status,reader_id,grant_handle,"
                    "credentials,webdav_url,auth_kind) VALUES "
                    "($1,$2,'nextcloud','engaging','reader','grant','credential',"
                    "'https://cloud','basic')",
                    UUID(ids["thread"]),
                    UUID(ids["user"]),
                )
            assert refused.value.constraint_name == "cloud_ro_mounts_authority_shape"
            return
        else:
            nested_key = physical_evidence.removeprefix("malformed_")
            key = {
                "workspace": "workspace_container",
                "binding": "_workspace_binding",
            }.get(nested_key, nested_key)
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_build_object($2::text,'malformed') "
                "WHERE id=$1",
                UUID(ids["thread"]),
                key,
            )
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert refused.value.constraint_name == "threads_pinned_delete_authority"


@pytest.mark.asyncio
async def test_legacy_unexposed_ownerless_ended_row_remains_deletable(db):
    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='ended', metadata='{}'::jsonb WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute("DELETE FROM threads WHERE id=$1", UUID(ids["thread"]))
        assert not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM threads WHERE id=$1)",
            UUID(ids["thread"]),
        )


@pytest.mark.asyncio
async def test_pinned_delete_trigger_does_not_change_stateless_authority(db):
    thread_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads (id,status,execution_lane,metadata) "
            "VALUES ($1,'ended','stateless','{}'::jsonb)",
            thread_id,
        )
        await conn.execute("DELETE FROM threads WHERE id=$1", thread_id)
        assert not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM threads WHERE id=$1)", thread_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    [
        "thread_runtime_retirement_outcomes",
        "thread_runtime_attach_abort_outcomes",
    ],
)
async def test_runtime_authority_outcomes_are_database_append_only(db, table):
    ids = await _seed(db)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    async with db.acquire() as conn:
        if table == "thread_runtime_retirement_outcomes":
            authority = await db.begin_pinned_thread_retirement(
                ids["thread"], permanent=False
            )
            await _authorize_and_ack(db, ids, authority)
            assert await db.settle_pinned_thread_retirement(
                ids["thread"],
                token=authority["token"],
                generation=authority["generation"],
            )
            token = UUID(authority["token"])
            key_column = "retirement_token"
        else:
            import main as orch_main

            token = UUID(ids["attach_token"])
            async with db.acquire() as setup_conn:
                await setup_conn.execute(
                    "UPDATE threads SET status='created' WHERE id=$1",
                    UUID(ids["thread"]),
                )
            with patch.object(orch_main, "postgres_db", db):
                assert (
                    await orch_main._release_session_attach_binding(
                        ids["agent"],
                        ids["thread"],
                        expected_runtime_generation=generation,
                        expected_attach_token=ids["attach_token"],
                        pre_delivery=True,
                    )
                    == "released"
                )
            key_column = "runtime_attach_token"
        for statement in (
            f"UPDATE {table} SET {key_column}={key_column} WHERE {key_column}=$1",
            f"DELETE FROM {table} WHERE {key_column}=$1",
        ):
            with pytest.raises(asyncpg.CheckViolationError) as immutable:
                await conn.execute(statement, token)
            assert (
                immutable.value.constraint_name == "thread_runtime_outcomes_append_only"
            )


@pytest.mark.asyncio
async def test_forged_conflicting_retirement_outcome_is_rejected(db):
    ids = await _seed(db)
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    await _authorize_and_ack(db, ids, authority)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as forged:
            await conn.execute(
                "INSERT INTO thread_runtime_retirement_outcomes "
                "(thread_id,runtime_generation,retirement_token,agent_id,"
                "runtime_attach_token,disposition,permanent,outcome) "
                "VALUES ($1,$2,$3,$4,$5,'ended',true,'deleted')",
                UUID(ids["thread"]),
                UUID(authority["generation"]),
                UUID(authority["token"]),
                UUID(ids["agent"]),
                UUID(ids["attach_token"]),
            )
        assert forged.value.constraint_name == "thread_runtime_outcome_insert_authority"
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        final_status="ended",
    )


@pytest.mark.asyncio
async def test_abandoned_hidden_preflight_expires_without_runtime_mutation(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET control_admission_agent_id=NULL WHERE id=$1",
            UUID(ids["thread"]),
        )
    authority = await db.begin_pinned_thread_retirement(
        ids["thread"],
        permanent=True,
        initiator="agent",
        expected_runtime_generation=str(
            (await db.get_thread(ids["thread"]))["runtime_generation"]
        ),
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
    )
    assert authority["context"]["control_admission_reopen_agent_id"] == ids["agent"]
    await asyncio.sleep(1.05)
    expired = await db.abort_stale_pinned_retirement_preflights(grace_seconds=1)
    assert [str(row["id"]) for row in expired] == [ids["thread"]]
    assert str(expired[0]["runtime_retirement_token"]) == authority["token"]
    reopened = await db.get_thread(ids["thread"])
    assert reopened["runtime_retirement_token"] is None
    assert str(reopened["runtime_generation"]) == authority["generation"]
    assert str(reopened["agent_id"]) == ids["agent"]
    assert str(reopened["runtime_attach_token"]) == ids["attach_token"]
    assert str(reopened["control_admission_agent_id"]) == ids["agent"]


@pytest.mark.asyncio
async def test_exact_agent_begin_authorizes_atomically_without_control_reopen(db):
    """Terminal agent Begin has no committed hidden/preflight window.

    The loop has already closed its control inbox. The same transaction that
    installs T authorizes it, so a handler crash after the DB call cannot let
    the stale-preflight reaper reopen a consumerless runtime.
    """

    from orchestrator.services.thread_control_inbox import (
        ControlAdmissionError,
        admit_thread_control,
    )

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET control_admission_agent_id=NULL WHERE id=$1",
            UUID(ids["thread"]),
        )
    before = await db.get_thread(ids["thread"])
    authority = await db.begin_pinned_thread_retirement(
        ids["thread"],
        permanent=False,
        settle_status="ended",
        initiator="agent",
        expected_runtime_generation=str(before["runtime_generation"]),
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
        authorize_immediately=True,
    )
    assert authority["state"] == "pending"
    assert authority["authorized_at"] is not None
    assert authority["context"]["control_admission_reopen_agent_id"] is None

    # Even after the hidden-preflight TTL, the authorized attempt is neither
    # abortable nor eligible for the stale marker reaper.
    await asyncio.sleep(1.05)
    assert await db.abort_stale_pinned_retirement_preflights(grace_seconds=1) == []
    assert not await db.abort_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
    )
    current = await db.get_thread(ids["thread"])
    assert str(current["runtime_retirement_token"]) == authority["token"]
    assert current["runtime_retirement_authorized_at"] is not None
    assert current["control_admission_agent_id"] is None

    # Neither a browser control nor an exact queued input can cross the
    # authorized T, even though thread/agent/G/attach ownership is unchanged.
    with pytest.raises(ControlAdmissionError, match="retirement"):
        await admit_thread_control(
            db,
            thread_id=ids["thread"],
            owner_user_id=ids["user"],
            client_request_id=uuid4(),
            verb="mode.set",
            payload={"mode": "manual"},
            requested_by="test",
            expected_runtime_generation=authority["generation"],
            require_pinned_runtime_generation=True,
        )
    async with db.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(InputDeliveryAuthorityLost):
                await lock_runtime_authority(
                    conn,
                    thread_id=ids["thread"],
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=authority["generation"],
                    runtime_attach_token=ids["attach_token"],
                )


@pytest.mark.asyncio
async def test_authorize_vs_hidden_preflight_expiry_has_one_winner(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET control_admission_agent_id=NULL WHERE id=$1",
            UUID(ids["thread"]),
        )
    row = await db.get_thread(ids["thread"])
    authority = await db.begin_pinned_thread_retirement(
        ids["thread"],
        permanent=False,
        initiator="agent",
        expected_runtime_generation=str(row["runtime_generation"]),
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
    )
    await asyncio.sleep(1.05)
    authorized, expired = await asyncio.gather(
        db.authorize_pinned_thread_retirement(
            ids["thread"],
            token=authority["token"],
            generation=authority["generation"],
            settle_status="ended",
        ),
        db.abort_stale_pinned_retirement_preflights(grace_seconds=1),
    )
    assert bool(authorized) is not bool(expired)
    current = await db.get_thread(ids["thread"])
    if authorized:
        assert str(current["runtime_retirement_token"]) == authority["token"]
        assert current["runtime_retirement_authorized_at"] is not None
        assert current["control_admission_agent_id"] is None
    else:
        assert current["runtime_retirement_token"] is None
        assert str(current["agent_id"]) == ids["agent"]
        assert str(current["control_admission_agent_id"]) == ids["agent"]


@pytest.mark.asyncio
async def test_owner_preflight_cannot_forge_control_admission_reopen(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET control_admission_agent_id=NULL WHERE id=$1",
            UUID(ids["thread"]),
        )
    authority = await db.begin_pinned_thread_retirement(
        ids["thread"], permanent=False, initiator="owner"
    )
    assert authority["context"]["control_admission_reopen_agent_id"] is None
    assert await db.abort_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
    )
    current = await db.get_thread(ids["thread"])
    assert current["runtime_retirement_token"] is None
    assert current["control_admission_agent_id"] is None


@pytest.mark.asyncio
async def test_stale_retirement_ack_cannot_certify_new_attempt(db):
    ids = await _seed(db)
    first = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert await db.abort_pinned_thread_retirement(
        ids["thread"], token=first["token"], generation=first["generation"]
    )
    second = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert second["token"] != first["token"]
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=second["token"],
        generation=second["generation"],
        settle_status="ended",
    )
    kwargs = {
        "expected_runtime_generation": second["generation"],
        "expected_agent_id": ids["agent"],
        "expected_attach_token": ids["attach_token"],
        "expected_settle_status": "ended",
        "expected_quiescence_protocol": "agent_runtime_zero_v1",
        "expected_workspace_generation": None,
        "expected_workspace_runtime_incarnation": None,
    }
    assert (
        await db.acknowledge_pinned_thread_local_quiescence(
            ids["thread"],
            expected_retirement_token=first["token"],
            **kwargs,
        )
        is None
    )
    assert (
        await db.acknowledge_pinned_thread_local_quiescence(
            ids["thread"],
            expected_retirement_token=second["token"],
            **kwargs,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_runtime_exposure_is_monotonic_within_generation(db):
    ids = await _seed(db)
    original = await db.get_thread(ids["thread"])
    assert original["runtime_authority_exposed"] is True
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE agents SET thread_id=NULL WHERE id=$1", UUID(ids["agent"])
            )
            await conn.execute(
                "UPDATE threads SET agent_id=NULL, "
                "control_admission_agent_id=NULL, runtime_attach_token=NULL "
                "WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert await conn.fetchval(
            "SELECT runtime_authority_exposed FROM threads WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "UPDATE threads SET runtime_authority_exposed=false WHERE id=$1",
            UUID(ids["thread"]),
        )
        # The complete retained Pod tuple is itself exposure evidence, so the
        # trigger latches the bit back to true rather than trusting the writer.
        assert await conn.fetchval(
            "SELECT runtime_authority_exposed FROM threads WHERE id=$1",
            UUID(ids["thread"]),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_pre_registration_agent_pod_is_exact_stopped_before_retirement(
    db, permanent
):
    import main as orch_main

    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(thread["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        await conn.execute(
            "UPDATE threads SET status='created', metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    entry = await db.get_thread(ids["thread"])
    assert entry is not None
    assert entry["runtime_authority_exposed"] is True
    assert entry["agent_id"] is None
    assert entry["runtime_attach_token"] is None

    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.delete_agent_pod_exact = AsyncMock(return_value=True)
    provisioner.agent_pod_authority = AsyncMock(return_value="exact_absent")
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "agent_provisioner", provisioner),
        patch.object(
            orch_main.session_router,
            "teardown_route",
            AsyncMock(return_value=True),
        ),
        patch.object(
            orch_main, "_conclude_conference_if_any", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main, "_thread_turn_in_flight", AsyncMock(return_value=False)
        ),
    ):
        result = await orch_main._end_thread_flow(
            ids["thread"], entry, permanent=permanent, force=True
        )

    assert result == {"status": "deleted" if permanent else "ended"}
    provisioner.delete_agent_pod_exact.assert_awaited_once_with(
        f"persistent-{ids['thread'][:12]}", expected_pod_uid="old-pod"
    )
    if permanent:
        assert await db.get_thread(ids["thread"]) is None
    else:
        settled = await db.get_thread(ids["thread"])
        assert settled is not None and settled["status"] == "ended"
        assert "agent_pod" not in _json(settled["metadata"])


@pytest.mark.asyncio
async def test_pre_registration_pod_recovery_refuses_a_late_agent_owner(db):
    """A direct/legacy registration race cannot be mistaken for an orphan Pod."""
    import main as orch_main

    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(thread["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        await conn.execute(
            "UPDATE threads SET status='created', metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert retirement is not None
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=retirement["generation"],
        settle_status="ended",
    )
    # Simulate an unaware writer publishing the physical actor identity after
    # Begin.  It cannot bind through the fenced thread, but its matching row is
    # enough to make Pod ownership ambiguous and must prevent any delete.
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id,config_name,hostname,pod_uid,status,"
            "agent_mode,last_heartbeat) VALUES "
            "($1::uuid,'centurion',$2,$3,'ready','persistent',now())",
            UUID(ids["agent"]),
            f"persistent-{ids['thread'][:12]}",
            "old-pod",
        )

    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.delete_agent_pod_exact = AsyncMock(return_value=True)
    provisioner.agent_pod_authority = AsyncMock(return_value="exact_absent")
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "agent_provisioner", provisioner),
    ):
        current = await db.get_thread(ids["thread"])
        assert current is not None
        assert not await orch_main._recover_pre_registration_agent_pod_zero(
            retirement, current
        )

    provisioner.delete_agent_pod_exact.assert_not_awaited()
    current = await db.get_thread(ids["thread"])
    assert current is not None
    assert "agent_pod" in _json(current["metadata"])
    assert current["runtime_retirement_local_quiescence"] is None


@pytest.mark.asyncio
async def test_pre_registration_pod_recovery_reaps_exact_offline_orphan(db):
    """A failed registration row cannot wedge exact permanent retirement."""
    import main as orch_main

    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(thread["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        await conn.execute(
            "UPDATE threads SET status='created', metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert retirement is not None
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=retirement["generation"],
        settle_status="ended",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id,config_name,hostname,pod_uid,status,"
            "agent_mode,last_heartbeat) VALUES "
            "($1::uuid,'centurion',$2,$3,'offline','persistent',now())",
            UUID(ids["agent"]),
            f"persistent-{ids['thread'][:12]}",
            "old-pod",
        )

    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.delete_agent_pod_exact = AsyncMock(return_value=True)
    provisioner.agent_pod_authority = AsyncMock(return_value="exact_absent")
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "agent_provisioner", provisioner),
    ):
        current = await db.get_thread(ids["thread"])
        assert current is not None
        assert await orch_main._recover_pre_registration_agent_pod_zero(
            retirement, current
        )

    provisioner.delete_agent_pod_exact.assert_awaited_once_with(
        f"persistent-{ids['thread'][:12]}", expected_pod_uid="old-pod"
    )
    assert await db.get_agent(ids["agent"]) is None
    current = await db.get_thread(ids["thread"])
    assert current is not None
    assert "agent_pod" not in _json(current["metadata"])
    receipt = _json(current["runtime_retirement_local_quiescence"])
    assert receipt["quiescence_protocol"] == "agent_runtime_zero_v1"
    assert receipt["agent_pod_uid"] == "old-pod"


@pytest.mark.asyncio
async def test_pre_registration_recovery_proves_physical_workspace_zero(db):
    """Agent-orphan zero alone cannot authorize a captured sandbox cleanup."""
    import main as orch_main

    ids = await _seed(db, bind_agent=False)
    workspace_generation = str(uuid4())
    workspace_runtime = str(uuid4())
    async with db.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(thread["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        metadata["workspace_container"] = {
            "status": "ready",
            "provisioner": "k8s",
            "namespace": "default",
            "pod_name": f"ws-thread-{ids['thread'][:12]}",
            "_runtime_incarnation": workspace_runtime,
            "_canvas_workspace_generation": workspace_generation,
        }
        metadata["_workspace_binding"] = {
            "kind": "remote",
            "generation": workspace_generation,
            "backing_id": f"k8s-pod:default:{workspace_runtime}",
            "ssh_host_key_fingerprint": "SHA256:test",
        }
        await conn.execute(
            "UPDATE threads SET status='created', metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert retirement is not None
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=retirement["generation"],
        settle_status="ended",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id,config_name,hostname,pod_uid,status,"
            "agent_mode,last_heartbeat) VALUES "
            "($1::uuid,'centurion',$2,$3,'offline','persistent',now())",
            UUID(ids["agent"]),
            f"persistent-{ids['thread'][:12]}",
            "old-pod",
        )

    agent_provisioner = MagicMock(is_available=True)
    agent_provisioner.delete_agent_pod_exact = AsyncMock(return_value=True)
    agent_provisioner.agent_pod_authority = AsyncMock(return_value="exact_absent")
    container_provisioner = MagicMock(is_available=True)
    container_provisioner.workspace_pod_authority = AsyncMock(return_value="exact_live")
    container_provisioner.delete_workspace = AsyncMock(return_value=True)
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "agent_provisioner", agent_provisioner),
        patch.object(orch_main, "container_provisioner", container_provisioner),
    ):
        current = await db.get_thread(ids["thread"])
        assert current is not None
        assert await orch_main._recover_pre_registration_agent_pod_zero(
            retirement, current
        )

    container_provisioner.delete_workspace.assert_awaited_once_with(
        orch_main.WorkspaceOwner.session(ids["thread"]),
        expected_runtime_incarnation=workspace_runtime,
        wait_for_exact_absence=True,
        exact_absence_timeout_seconds=120.0,
        defer_context_clear=True,
    )
    assert await db.get_agent(ids["agent"]) is None
    current = await db.get_thread(ids["thread"])
    assert current is not None
    assert "agent_pod" not in _json(current["metadata"])
    receipt = _json(current["runtime_retirement_local_quiescence"])
    assert receipt["quiescence_protocol"] == "sandbox_actuator_zero_v1"
    assert receipt["workspace_generation"] == workspace_generation
    assert receipt["workspace_runtime_incarnation"] == workspace_runtime
    assert receipt["agent_pod_uid"] == "old-pod"
    assert await db.clear_pinned_retirement_physical_runtime_endpoint(
        ids["thread"],
        runtime_generation=retirement["generation"],
        retirement_token=retirement["token"],
        completed_quiescence_protocol="sandbox_actuator_zero_v1",
        completed_external_cleanup_protocol="sandbox_actuator_zero_v1",
    )
    current = await db.get_thread(ids["thread"])
    assert current is not None
    metadata = _json(current["metadata"])
    assert metadata["workspace_container"]["status"] == "deleted"
    assert "_workspace_binding" not in metadata
    assert await db.pinned_retirement_external_cleanup_complete(
        ids["thread"],
        runtime_generation=retirement["generation"],
        retirement_token=retirement["token"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_response_lost_agent_create_uses_retained_pod_and_pvc_fences(
    db, permanent
):
    """A 404 is never treated as zero; exact same-name fences own settlement."""

    import main as orch_main

    ids = await _seed(
        db,
        bind_agent=False,
        publish_agent_pod=False,
    )
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(row["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        await conn.execute(
            "UPDATE threads SET status='created',metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    entry = await db.get_thread(ids["thread"])
    generation = str(entry["runtime_generation"])
    attempt_id = str(uuid4())
    claim_name = f"pvc-agent-s-{ids['thread'][:12]}"
    intent = await db.reserve_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=attempt_id,
        pod_name=f"srw-agent-s-{attempt_id[:8]}",
        provisioner="agent",
        pvc_name=claim_name,
    )
    assert intent is not None
    claim_id = str(intent["workspace_claim"]["claim_id"])
    entry = await db.get_thread(ids["thread"])

    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.fence_agent_pod_provision_intent = AsyncMock(
        return_value={"state": "exact_fence", "pod_uid": "pod-fence-uid"}
    )
    provisioner.fence_agent_workspace_claim = AsyncMock(
        return_value={"state": "exact_fence", "pvc_uid": "pvc-fence-uid"}
    )
    provisioner.ensure_agent_workspace_claim = AsyncMock(
        return_value="retained-pvc-uid"
    )
    provisioner.agent_pod_provision_intent_authority = AsyncMock()
    provisioner.agent_workspace_claim_authority = AsyncMock()
    provisioner.delete_agent_workspace_claim_exact = AsyncMock(return_value=True)
    provisioner.delete_agent_pod_exact = AsyncMock(return_value=True)
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "agent_provisioner", provisioner),
        patch.object(
            orch_main.session_router,
            "teardown_route",
            AsyncMock(return_value=True),
        ),
        patch.object(
            orch_main, "_conclude_conference_if_any", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main, "_thread_turn_in_flight", AsyncMock(return_value=False)
        ),
    ):
        result = await orch_main._end_thread_flow(
            ids["thread"], entry, permanent=permanent, force=True
        )

    assert result == {"status": "deleted" if permanent else "ended"}
    async with db.acquire() as conn:
        pod_fence = await conn.fetchrow(
            "SELECT status,pod_uid,gc_after FROM "
            "thread_agent_pod_provision_intents WHERE attempt_id=$1::uuid",
            UUID(attempt_id),
        )
        claim = await conn.fetchrow(
            "SELECT status,pvc_uid,gc_after FROM thread_agent_workspace_claims "
            "WHERE claim_id=$1::uuid",
            UUID(claim_id),
        )
    assert pod_fence["status"] == "fenced"
    assert pod_fence["pod_uid"] == "pod-fence-uid"
    assert pod_fence["gc_after"] is not None
    assert not await db.complete_pinned_k8s_create_fence_gc(
        resource_kind="pod",
        authority_id=attempt_id,
        expected_resource_uid="pod-fence-uid",
    )
    if permanent:
        assert await db.get_thread(ids["thread"]) is None
        assert claim["status"] == "fenced"
        assert claim["pvc_uid"] == "pvc-fence-uid"
        assert claim["gc_after"] is not None
        assert not await db.complete_pinned_k8s_create_fence_gc(
            resource_kind="pvc",
            authority_id=claim_id,
            expected_resource_uid="pvc-fence-uid",
        )
    else:
        settled = await db.get_thread(ids["thread"])
        assert settled is not None and settled["status"] == "ended"
        assert claim["status"] == "ready"
        assert claim["pvc_uid"] == "retained-pvc-uid"
        async with db.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError) as blocked:
                await conn.execute(
                    "UPDATE threads SET status='created' WHERE id=$1::uuid",
                    UUID(ids["thread"]),
                )
        assert blocked.value.constraint_name == "threads_resume_create_fence_authority"


@pytest.mark.asyncio
async def test_reclaimed_agent_workspace_claim_is_idempotent_retirement_replay(db):
    """Exact post-horizon fence GC remains complete through physical CAS."""

    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(row["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        await conn.execute(
            "UPDATE threads SET status='created',metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    entry = await db.get_thread(ids["thread"])
    generation = str(entry["runtime_generation"])
    attempt_id = str(uuid4())
    claim_name = f"pvc-agent-s-{ids['thread'][:12]}"
    intent = await db.reserve_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=attempt_id,
        pod_name=f"srw-agent-s-{attempt_id[:8]}",
        provisioner="agent",
        pvc_name=claim_name,
    )
    assert intent is not None
    claim_id = str(intent["workspace_claim"]["claim_id"])
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=retirement["generation"],
        settle_status="ended",
    )
    revoke = {
        "expected_runtime_generation": retirement["generation"],
        "expected_retirement_token": retirement["token"],
        "expected_claim_id": claim_id,
        "expected_pvc_name": claim_name,
    }
    assert await db.revoke_pinned_agent_workspace_claim(ids["thread"], **revoke)
    assert await db.fence_pinned_agent_workspace_claim(
        ids["thread"],
        **revoke,
        fence_pvc_uid="fence-pvc-uid",
    )
    pod_revoke = {
        "expected_runtime_generation": retirement["generation"],
        "expected_retirement_token": retirement["token"],
        "expected_attempt_id": attempt_id,
        "expected_pod_name": f"srw-agent-s-{attempt_id[:8]}",
    }
    assert await db.revoke_pinned_agent_pod_provision_intent(
        ids["thread"], **pod_revoke
    )
    assert await db.fence_pinned_agent_pod_provision_intent(
        ids["thread"],
        **pod_revoke,
        fence_pod_uid="pod-fence-uid",
    )
    assert await db.acknowledge_pinned_agent_pod_provision_intent_zero(
        ids["thread"],
        **pod_revoke,
        observed_pod_uid="pod-fence-uid",
    )
    async with db.acquire() as conn:
        # Build the post-GC fixture without spending the production ten-minute
        # request horizon. Other tests exercise the real fenced -> reclaimed
        # transition; this one isolates replay of its terminal row.
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role='replica'")
            await conn.execute(
                "UPDATE thread_agent_workspace_claims "
                "SET status='reclaimed',resolved_at=now() "
                "WHERE claim_id=$1::uuid",
                UUID(claim_id),
            )
    assert await db.revoke_pinned_agent_workspace_claim(ids["thread"], **revoke)
    assert await db.clear_pinned_retirement_physical_runtime_endpoint(
        ids["thread"],
        runtime_generation=retirement["generation"],
        retirement_token=retirement["token"],
    )


@pytest.mark.asyncio
async def test_k8s_create_fence_rows_cannot_be_forged_terminal_at_insert(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as claim_refused:
            await conn.execute(
                "INSERT INTO thread_agent_workspace_claims ("
                "claim_id,thread_id,created_runtime_generation,create_attempt,"
                "provisioner,pvc_name,status,pvc_uid,fenced_at,gc_after) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,'agent',$5,"
                "'fenced','fake-pvc-uid',now(),now()+interval '10 minutes')",
                uuid4(),
                UUID(ids["thread"]),
                UUID(generation),
                uuid4(),
                f"pvc-agent-s-{ids['thread'][:12]}",
            )
        assert (
            claim_refused.value.constraint_name
            == "thread_agent_workspace_claim_authority"
        )
        with pytest.raises(asyncpg.CheckViolationError) as pod_refused:
            await conn.execute(
                "INSERT INTO thread_agent_pod_provision_intents ("
                "attempt_id,thread_id,runtime_generation,provisioner,pod_name,"
                "status,pod_uid,fenced_at,gc_after) VALUES ("
                "$1::uuid,$2::uuid,$3::uuid,'agent',$4,'fenced',"
                "'fake-pod-uid',now(),now()+interval '10 minutes')",
                uuid4(),
                UUID(ids["thread"]),
                UUID(generation),
                f"srw-agent-s-{ids['thread'][:12]}",
            )
        assert (
            pod_refused.value.constraint_name
            == "thread_agent_pod_provision_intent_authority"
        )


@pytest.mark.asyncio
async def test_published_agent_create_intent_is_idempotently_adopted(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    original_attempt = str(uuid4())
    pod_name = f"srw-agent-s-{original_attempt[:8]}"
    pvc_name = f"pvc-agent-s-{ids['thread'][:12]}"
    reserved = await db.reserve_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=original_attempt,
        pod_name=pod_name,
        provisioner="agent",
        pvc_name=pvc_name,
    )
    assert reserved is not None
    claim = reserved["workspace_claim"]
    assert await db.publish_pinned_agent_workspace_claim(
        ids["thread"],
        expected_runtime_generation=generation,
        claim_id=str(claim["claim_id"]),
        pvc_name=pvc_name,
        pvc_uid="pvc-uid",
    )
    assert await db.publish_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=original_attempt,
        pod_name=pod_name,
        pod_uid="pod-uid",
        namespace="test",
    )

    adopted = await db.reserve_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=str(uuid4()),
        pod_name="srw-agent-s-unused",
        provisioner="agent",
        pvc_name=pvc_name,
    )
    assert adopted is not None
    assert str(adopted["attempt_id"]) == original_attempt
    assert str(adopted["pod_name"]) == pod_name
    assert str(adopted["workspace_claim"]["claim_id"]) == str(claim["claim_id"])
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM thread_agent_pod_provision_intents "
                "WHERE thread_id=$1::uuid",
                UUID(ids["thread"]),
            )
            == 1
        )


@pytest.mark.asyncio
async def test_planned_agent_create_intent_cannot_be_reissued_after_end_begins(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    attempt = str(uuid4())
    pod_name = f"srw-agent-s-{attempt[:8]}"
    assert await db.reserve_pinned_agent_pod_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        attempt_id=attempt,
        pod_name=pod_name,
        provisioner="agent",
    )
    open_rows = await db.list_pinned_agent_create_intents_for_reconcile()
    assert len(open_rows) == 1
    assert str(open_rows[0]["attempt_id"]) == attempt
    assert open_rows[0]["workspace_claim"] is None
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert retirement["state"] == "pending"
    assert (
        await db.reserve_pinned_agent_pod_provision_intent(
            ids["thread"],
            expected_runtime_generation=generation,
            attempt_id=attempt,
            pod_name=pod_name,
            provisioner="agent",
        )
        is None
    )
    assert await db.list_pinned_agent_create_intents_for_reconcile() == []


@pytest.mark.asyncio
async def test_planned_workspace_create_is_fenced_before_soft_settlement(db):
    """An admitted multi-object create is external authority, not fake process exposure."""

    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    before = await db.get_thread(ids["thread"])
    generation = str(before["runtime_generation"])
    attempt = str(uuid4())
    pod_name = f"ws-thread-{ids['thread'][:12]}"
    intent = await db.reserve_pinned_thread_workspace_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        expected_agent_id=None,
        expected_attach_token=None,
        expected_workspace_context=None,
        expected_binding_context=None,
        attempt_id=attempt,
        namespace="superhuman-remote-worker",
        pod_name=pod_name,
        pvc_name=None,
        seed_configmap_name=None,
        service_name=None,
        retained_service_uid=None,
        network_tier="internet-only",
        manifest_fingerprint="a" * 64,
    )
    assert intent is not None and str(intent["attempt_id"]) == attempt
    pending = await db.get_thread(ids["thread"])
    assert pending["runtime_authority_exposed"] is False
    assert _json(pending["metadata"])["workspace_container"] == {
        "status": "pending",
        "provisioner": "k8s",
        "_workspace_provision_attempt": attempt,
        "_workspace_provision_generation": generation,
        "_runtime_incarnation": None,
        "_canvas_workspace_generation": None,
        "pod_ip": None,
        "pod_name": None,
        "host": None,
        "port": None,
        "ide_host": None,
        "ide_port": None,
    }

    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert retirement["state"] == "pending"
    assert retirement["context"]["workspace_provision_intent"]["attempt_id"] == attempt
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        settle_status="ended",
    )
    revoked = await db.revoke_pinned_thread_workspace_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        expected_retirement_token=retirement["token"],
        expected_attempt_id=attempt,
    )
    assert revoked is not None and revoked["status"] == "revoking"
    fence_uid = str(uuid4())
    assert await db.fence_pinned_thread_workspace_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        expected_retirement_token=retirement["token"],
        expected_attempt_id=attempt,
        fence_pod_uid=fence_uid,
        fence_pvc_uid=None,
        fence_configmap_uid=None,
        fence_service_uid=None,
        permanent=False,
    )
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        final_status="ended",
    )

    settled = await db.get_thread(ids["thread"])
    assert settled["status"] == "ended"
    workspace = _json(settled["metadata"])["workspace_container"]
    assert workspace["status"] == "deleted"
    assert "_workspace_provision_attempt" not in workspace
    async with db.acquire() as conn:
        fence = await conn.fetchrow(
            "SELECT status,fence_pod_uid,gc_after FROM "
            "thread_workspace_provision_intents WHERE attempt_id=$1::uuid",
            UUID(attempt),
        )
    assert fence["status"] == "fenced"
    assert fence["fence_pod_uid"] == fence_uid
    assert fence["gc_after"] is not None
    assert not await db.retire_pinned_thread_workspace_provision_fence(
        attempt,
        expected_fence_pod_uid=fence_uid,
        expected_fence_pvc_uid=None,
        expected_fence_configmap_uid=None,
        expected_fence_service_uid=None,
    )


@pytest.mark.asyncio
async def test_permanent_workspace_create_fence_survives_thread_delete(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    before = await db.get_thread(ids["thread"])
    generation = str(before["runtime_generation"])
    attempt = str(uuid4())
    assert await db.reserve_pinned_thread_workspace_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        expected_agent_id=None,
        expected_attach_token=None,
        expected_workspace_context=None,
        expected_binding_context=None,
        attempt_id=attempt,
        namespace="superhuman-remote-worker",
        pod_name=f"ws-thread-{ids['thread'][:12]}",
        pvc_name=None,
        seed_configmap_name=None,
        service_name=None,
        retained_service_uid=None,
        network_tier="internet-only",
        manifest_fingerprint="b" * 64,
    )
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert retirement["state"] == "pending"
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        settle_status="ended",
    )
    assert await db.revoke_pinned_thread_workspace_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        expected_retirement_token=retirement["token"],
        expected_attempt_id=attempt,
    )
    fence_uid = str(uuid4())
    assert await db.fence_pinned_thread_workspace_provision_intent(
        ids["thread"],
        expected_runtime_generation=generation,
        expected_retirement_token=retirement["token"],
        expected_attempt_id=attempt,
        fence_pod_uid=fence_uid,
        fence_pvc_uid=None,
        fence_configmap_uid=None,
        fence_service_uid=None,
        permanent=True,
    )
    assert await db.clear_pinned_retirement_physical_runtime_endpoint(
        ids["thread"],
        runtime_generation=generation,
        retirement_token=retirement["token"],
        completed_external_cleanup_protocol="workspace_provision_fence_v1",
    )
    await db.delete_thread(
        ids["thread"],
        expected_runtime_retirement_token=retirement["token"],
        expected_runtime_generation=generation,
    )
    assert await db.get_thread(ids["thread"]) is None
    async with db.acquire() as conn:
        retained = await conn.fetchrow(
            "SELECT thread_id,status,fence_pod_uid FROM "
            "thread_workspace_provision_intents WHERE attempt_id=$1::uuid",
            UUID(attempt),
        )
    assert str(retained["thread_id"]) == ids["thread"]
    assert retained["status"] == "fenced"
    assert retained["fence_pod_uid"] == fence_uid


@pytest.mark.asyncio
async def test_pinned_vm_provision_generation_is_installed_under_exact_actor(db):
    from orchestrator.services.vm_provisioner import VMProvisioner

    ids = await _seed(db)
    before = await db.get_thread(ids["thread"])
    context = VMProvisioner._fresh_provision_ctx()
    context["status"] = "provisioning"

    assert await db.begin_pinned_thread_vm_provisioning(
        ids["thread"],
        expected_runtime_generation=str(before["runtime_generation"]),
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
        expected_vm_context=None,
        provision_context=context,
    )
    stored = _json((await db.get_thread(ids["thread"]))["metadata"])["vm"]
    assert stored["status"] == "provisioning"
    assert stored["provision_generation"] == context["provision_generation"]
    assert stored["identity_authenticated"] is False
    assert stored["vm_uid"] is None
    assert stored["_runtime_incarnation"] is None

    replacement = VMProvisioner._fresh_provision_ctx()
    replacement["status"] = "provisioning"
    assert not await db.begin_pinned_thread_vm_provisioning(
        ids["thread"],
        expected_runtime_generation=str(before["runtime_generation"]),
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
        expected_vm_context=None,
        provision_context=replacement,
    )
    unchanged = _json((await db.get_thread(ids["thread"]))["metadata"])["vm"]
    assert unchanged["provision_generation"] == context["provision_generation"]


@pytest.mark.asyncio
async def test_pinned_vm_provision_cas_loses_after_retirement_begin(db):
    from orchestrator.services.vm_provisioner import VMProvisioner

    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    before = await db.get_thread(ids["thread"])
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=True)
    assert retirement["state"] == "pending"
    context = VMProvisioner._fresh_provision_ctx()
    context["status"] = "provisioning"

    assert not await db.begin_pinned_thread_vm_provisioning(
        ids["thread"],
        expected_runtime_generation=str(before["runtime_generation"]),
        expected_agent_id=None,
        expected_attach_token=None,
        expected_vm_context=None,
        provision_context=context,
    )
    assert "vm" not in _json((await db.get_thread(ids["thread"]))["metadata"])


@pytest.mark.asyncio
async def test_failed_attach_abort_rotates_generation_and_stale_retry_preserves_b(db):
    """A proved pre-delivery A abort cannot clear the same agent rebound as B."""
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    old_generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])

    with patch.object(orch_main, "postgres_db", db):
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=old_generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "released"
        )
        rotated = await db.get_thread(ids["thread"])
        successor_generation = str(rotated["runtime_generation"])
        assert successor_generation != old_generation
        assert rotated["runtime_authority_exposed"] is False
        assert rotated["agent_id"] is None
        assert rotated["runtime_attach_token"] is None
        assert "agent_pod" not in _json(rotated["metadata"])
        async with db.acquire() as conn:
            released_agent = await conn.fetchrow(
                "SELECT thread_id,status::text FROM agents WHERE id=$1",
                UUID(ids["agent"]),
            )
        assert released_agent["thread_id"] is None
        assert released_agent["status"] == "ready"

        b_token = await orch_main._reserve_session_attach_binding(
            ids["agent"],
            ids["thread"],
            expected_runtime_generation=successor_generation,
        )
        assert b_token is not None

        # Lost A response/retry reads only A's append-only exact outcome. It
        # does not infer success from current mismatch and cannot clear B.
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=old_generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "already_detached"
        )

    current = await db.get_thread(ids["thread"])
    assert str(current["runtime_generation"]) == successor_generation
    assert str(current["runtime_attach_token"]) == b_token
    assert str(current["agent_id"]) == ids["agent"]
    assert current["runtime_authority_exposed"] is True
    assert "agent_pod" not in _json(current["metadata"])


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_attach_abort_successor_can_end_before_reconcile(db, permanent):
    """G2 owns no stale G1 Pod marker even when End beats successor prepare."""
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1::uuid FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(thread["metadata"])
        metadata["config_override"]["officer"]["enabled"] = False
        await conn.execute(
            "UPDATE threads SET status='created', metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )
    old_generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])

    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.delete_agent_pod_exact = AsyncMock(return_value=True)
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "agent_provisioner", provisioner),
        patch.object(
            orch_main.session_router,
            "teardown_route",
            AsyncMock(return_value=True),
        ),
        patch.object(
            orch_main, "_conclude_conference_if_any", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main, "_thread_turn_in_flight", AsyncMock(return_value=False)
        ),
    ):
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=old_generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "released"
        )
        successor = await db.get_thread(ids["thread"])
        assert successor is not None
        assert "agent_pod" not in _json(successor["metadata"])
        result = await orch_main._end_thread_flow(
            ids["thread"], successor, permanent=permanent, force=True
        )

    assert result == {"status": "deleted" if permanent else "ended"}
    # Abort released the exact process to the pool; G2 End must not UID-delete
    # it merely because G1 once carried the marker.
    provisioner.delete_agent_pod_exact.assert_not_awaited()
    if permanent:
        assert await db.get_thread(ids["thread"]) is None
    else:
        settled = await db.get_thread(ids["thread"])
        assert settled is not None and settled["status"] == "ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_target", ["agent_update", "outcome_insert"])
async def test_failed_attach_abort_fault_rolls_back_both_authority_rows(
    db, failure_target
):
    """Faults after either mutation cannot leave a half-rotated authority."""
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    original = await db.get_thread(ids["thread"])
    original_generation = str(original["runtime_generation"])
    trigger_name = "test_attach_abort_failure"
    target = (
        "public.agents"
        if failure_target == "agent_update"
        else "public.thread_runtime_attach_abort_outcomes"
    )
    operation = "UPDATE" if failure_target == "agent_update" else "INSERT"
    async with db.acquire() as conn:
        await conn.execute(
            f"""
            CREATE OR REPLACE FUNCTION public.{trigger_name}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected attach-abort failure';
            END;
            $$;
            DROP TRIGGER IF EXISTS {trigger_name} ON {target};
            CREATE TRIGGER {trigger_name}
            BEFORE {operation} ON {target}
            FOR EACH ROW EXECUTE FUNCTION public.{trigger_name}();
            """
        )
    try:
        with patch.object(orch_main, "postgres_db", db):
            with pytest.raises(asyncpg.RaiseError):
                await orch_main._release_session_attach_binding(
                    ids["agent"],
                    ids["thread"],
                    expected_runtime_generation=original_generation,
                    expected_attach_token=ids["attach_token"],
                    pre_delivery=True,
                )
    finally:
        async with db.acquire() as conn:
            await conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {target}")
            await conn.execute(f"DROP FUNCTION IF EXISTS public.{trigger_name}()")

    current = await db.get_thread(ids["thread"])
    assert str(current["runtime_generation"]) == original_generation
    assert str(current["agent_id"]) == ids["agent"]
    assert str(current["runtime_attach_token"]) == ids["attach_token"]
    assert current["runtime_authority_exposed"] is True
    assert _json(current["metadata"])["agent_pod"]["pod_uid"] == "old-pod"
    async with db.acquire() as conn:
        agent = await conn.fetchrow(
            "SELECT thread_id,status::text FROM agents WHERE id=$1",
            UUID(ids["agent"]),
        )
        outcome_count = await conn.fetchval(
            "SELECT count(*) FROM thread_runtime_attach_abort_outcomes "
            "WHERE thread_id=$1 AND runtime_generation=$2",
            UUID(ids["thread"]),
            UUID(original_generation),
        )
    assert str(agent["thread_id"]) == ids["thread"]
    assert agent["status"] == "session"
    assert outcome_count == 0


@pytest.mark.asyncio
async def test_failed_attach_abort_exact_outcome_survives_thread_deletion(db):
    """A dropped 200 can be proved after a concurrent permanent row delete."""
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    with patch.object(orch_main, "postgres_db", db):
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "released"
        )
        current = await db.get_thread(ids["thread"])
        retirement = await db.begin_pinned_thread_retirement(
            ids["thread"], permanent=True
        )
        assert retirement["state"] == "pending"
        assert retirement["generation"] == str(current["runtime_generation"])
        assert await db.authorize_pinned_thread_retirement(
            ids["thread"],
            token=retirement["token"],
            generation=retirement["generation"],
            settle_status="ended",
        )
        assert await db.clear_pinned_retirement_physical_runtime_endpoint(
            ids["thread"],
            runtime_generation=retirement["generation"],
            retirement_token=retirement["token"],
        )
        await db.delete_thread(
            ids["thread"],
            expected_runtime_retirement_token=retirement["token"],
            expected_runtime_generation=retirement["generation"],
        )
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "already_detached"
        )


@pytest.mark.asyncio
async def test_failed_attach_abort_outcome_is_restart_safe_successor_work(db):
    """A process restart can rediscover only the exact unbound G2."""
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    retired_generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    with patch.object(orch_main, "postgres_db", db):
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=retired_generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "released"
        )

    current = await db.get_thread(ids["thread"])
    successor_generation = str(current["runtime_generation"])
    candidates = await db.list_retryable_thread_attach_abort_successors()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert str(candidate["thread_id"]) == ids["thread"]
    assert str(candidate["retired_runtime_generation"]) == retired_generation
    assert str(candidate["successor_generation"]) == successor_generation
    assert candidate["quiescence_protocol"] == "pre_delivery_no_payload_v1"
    assert candidate["workspace_generation"] is None
    assert candidate["workspace_runtime_incarnation"] is None

    # Once another exact owner binds, the old append-only outcome remains for
    # readback but cannot keep scheduling work into that live generation.
    with patch.object(orch_main, "postgres_db", db):
        token = await orch_main._reserve_session_attach_binding(
            ids["agent"],
            ids["thread"],
            expected_runtime_generation=successor_generation,
        )
    assert token is not None
    assert await db.list_retryable_thread_attach_abort_successors() == []


@pytest.mark.asyncio
async def test_workspace_zero_abort_clears_only_exact_g2_captured_endpoint(db):
    """The K8s delete's DB half preserves backing and rejects stale U1/G1."""

    import main as orch_main

    ids = await _seed(db)
    workspace_generation = str(uuid4())
    old_workspace_runtime = str(uuid4())
    new_workspace_runtime = str(uuid4())
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM threads WHERE id=$1 FOR UPDATE",
            UUID(ids["thread"]),
        )
        metadata = _json(row["metadata"])
        metadata["config_override"]["workspace"] = {"backend": "sandbox"}
        metadata["workspace_container"] = {
            "status": "ready",
            "provisioner": "k8s",
            "pod_ip": "10.42.0.25",
            "host": "ws-thread.example.svc",
            "port": 30022,
            "_canvas_workspace_generation": workspace_generation,
            "_runtime_incarnation": old_workspace_runtime,
        }
        metadata["_workspace_binding"] = {
            "generation": workspace_generation,
            "kind": "remote",
            "backing_id": "k8s-pvc:workspace-pvc-a",
            "ssh_host_key_fingerprint": "SHA256:" + ("A" * 43),
        }
        await conn.execute(
            "UPDATE threads SET status='created',metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(metadata),
        )

    retired_generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    with patch.object(orch_main, "postgres_db", db):
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=retired_generation,
                expected_attach_token=ids["attach_token"],
                expected_agent_pod_uid="old-pod",
                local_runtime_quiesced=True,
                local_quiescence_protocol="workspace_process_zero_v1",
                workspace_generation=workspace_generation,
                workspace_runtime_incarnation=old_workspace_runtime,
            )
            == "released"
        )

    successor = await db.get_thread(ids["thread"])
    successor_generation = str(successor["runtime_generation"])
    assert not await db.clear_pinned_attach_abort_workspace_endpoint(
        ids["thread"],
        retired_runtime_generation=retired_generation,
        retired_attach_token=ids["attach_token"],
        retired_agent_id=ids["agent"],
        successor_generation=successor_generation,
        workspace_generation=workspace_generation,
        workspace_runtime_incarnation=new_workspace_runtime,
    )
    unchanged = _json((await db.get_thread(ids["thread"]))["metadata"])
    assert (
        unchanged["workspace_container"]["_runtime_incarnation"]
        == old_workspace_runtime
    )

    assert await db.clear_pinned_attach_abort_workspace_endpoint(
        ids["thread"],
        retired_runtime_generation=retired_generation,
        retired_attach_token=ids["attach_token"],
        retired_agent_id=ids["agent"],
        successor_generation=successor_generation,
        workspace_generation=workspace_generation,
        workspace_runtime_incarnation=old_workspace_runtime,
    )
    cleared = _json((await db.get_thread(ids["thread"]))["metadata"])
    assert cleared["workspace_container"]["status"] == "deleted"
    assert cleared["workspace_container"]["_runtime_incarnation"] is None
    assert cleared["workspace_container"]["pod_ip"] is None
    assert cleared["workspace_container"]["host"] is None
    assert cleared["_workspace_binding"]["generation"] == workspace_generation

    # A later U2 publication cannot be cleared by replaying U1's outcome.
    assert await db.merge_thread_workspace_context(
        ids["thread"],
        {
            "status": "ready",
            "provisioner": "k8s",
            "pod_ip": "10.42.0.26",
            "host": "ws-thread.example.svc",
            "port": 30022,
            "_canvas_workspace_generation": workspace_generation,
            "_runtime_incarnation": new_workspace_runtime,
        },
    )
    assert not await db.clear_pinned_attach_abort_workspace_endpoint(
        ids["thread"],
        retired_runtime_generation=retired_generation,
        retired_attach_token=ids["attach_token"],
        retired_agent_id=ids["agent"],
        successor_generation=successor_generation,
        workspace_generation=workspace_generation,
        workspace_runtime_incarnation=old_workspace_runtime,
    )
    replacement = _json((await db.get_thread(ids["thread"]))["metadata"])
    assert (
        replacement["workspace_container"]["_runtime_incarnation"]
        == new_workspace_runtime
    )


@pytest.mark.asyncio
async def test_failed_attach_abort_refuses_status_pod_and_protocol_mismatch(db):
    import main as orch_main

    ids = await _seed(db)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    with patch.object(orch_main, "postgres_db", db):
        # A runtime that reached active may already have admitted provider or
        # Officer boot work; it cannot be rewritten into a never-exposed life.
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "unsafe"
        )
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET status='created' WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=generation,
                expected_attach_token=ids["attach_token"],
                expected_agent_pod_uid="replacement-pod",
                local_runtime_quiesced=True,
                local_quiescence_protocol="agent_runtime_zero_v1",
            )
            == "unsafe"
        )
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=generation,
                expected_attach_token=ids["attach_token"],
                expected_agent_pod_uid="old-pod",
                local_runtime_quiesced=True,
                local_quiescence_protocol="workspace_process_zero_v1",
            )
            == "unsafe"
        )
    unchanged = await db.get_thread(ids["thread"])
    assert str(unchanged["runtime_generation"]) == generation
    assert str(unchanged["agent_id"]) == ids["agent"]


@pytest.mark.asyncio
async def test_failed_attach_proof_joins_an_authorized_owner_retirement(db):
    """End beating attach cleanup receipts G1 and never creates successor G2."""

    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    retirement = await db.begin_pinned_thread_retirement(
        ids["thread"],
        permanent=True,
        expected_runtime_generation=generation,
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
    )
    assert retirement["state"] == "pending"
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        settle_status="ended",
    )

    with patch.object(orch_main, "postgres_db", db):
        assert not await orch_main._acknowledge_retiring_failed_attach(
            ids["agent"],
            ids["thread"],
            expected_runtime_generation=generation,
            expected_attach_token=ids["attach_token"],
            expected_agent_pod_uid="replacement-pod",
            local_quiescence_protocol="agent_runtime_zero_v1",
            workspace_generation=None,
            workspace_runtime_incarnation=None,
        )
        assert await orch_main._acknowledge_retiring_failed_attach(
            ids["agent"],
            ids["thread"],
            expected_runtime_generation=generation,
            expected_attach_token=ids["attach_token"],
            expected_agent_pod_uid="old-pod",
            local_quiescence_protocol="agent_runtime_zero_v1",
            workspace_generation=None,
            workspace_runtime_incarnation=None,
        )

    current = await db.get_thread(ids["thread"])
    receipt = _json(current["runtime_retirement_local_quiescence"])
    assert receipt["retirement_token"] == retirement["token"]
    assert receipt["runtime_generation"] == generation
    assert receipt["quiescence_protocol"] == "agent_runtime_zero_v1"
    assert str(current["runtime_generation"]) == generation
    assert str(current["agent_id"]) == ids["agent"]
    assert str(current["runtime_attach_token"]) == ids["attach_token"]
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM thread_runtime_attach_abort_outcomes "
                "WHERE thread_id=$1",
                UUID(ids["thread"]),
            )
            == 0
        )


@pytest.mark.asyncio
async def test_retiring_failed_attach_lost_ack_has_exact_outcome_readback(db):
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    retirement = await db.begin_pinned_thread_retirement(
        ids["thread"],
        permanent=False,
        expected_runtime_generation=generation,
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
    )
    assert retirement["state"] == "pending"
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        settle_status="ended",
    )
    with patch.object(orch_main, "postgres_db", db):
        assert await orch_main._acknowledge_retiring_failed_attach(
            ids["agent"],
            ids["thread"],
            expected_runtime_generation=generation,
            expected_attach_token=ids["attach_token"],
            expected_agent_pod_uid="old-pod",
            local_quiescence_protocol="agent_runtime_zero_v1",
            workspace_generation=None,
            workspace_runtime_incarnation=None,
        )
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        final_status="ended",
    )
    assert await db.has_exact_pinned_runtime_retirement_outcome(
        ids["thread"],
        runtime_generation=generation,
        agent_id=ids["agent"],
        runtime_attach_token=ids["attach_token"],
    )
    assert not await db.has_exact_pinned_runtime_retirement_outcome(
        ids["thread"],
        runtime_generation=str(uuid4()),
        agent_id=ids["agent"],
        runtime_attach_token=ids["attach_token"],
    )


@pytest.mark.asyncio
async def test_retiring_failed_attach_refuses_committed_input_admission(db):
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    async with db.acquire() as conn:
        message_id = await conn.fetchval(
            "INSERT INTO thread_messages (thread_id,role,content,turn_number) "
            "VALUES ($1,'user','admitted',1) RETURNING id",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "INSERT INTO thread_input_deliveries ("
            "delivery_id,thread_id,message_id,source,state,claim_generation,"
            "owner_agent_id,owner_pod_uid,owner_runtime_generation,"
            "admitted_turn_number,admitted_at) VALUES ("
            "$1,$2,$3,'direct_human','admitted',1,$4,'old-pod',$5,1,now())",
            uuid4(),
            UUID(ids["thread"]),
            message_id,
            UUID(ids["agent"]),
            UUID(generation),
        )
    retirement = await db.begin_pinned_thread_retirement(
        ids["thread"],
        permanent=True,
        expected_runtime_generation=generation,
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
    )
    assert retirement["state"] == "pending"
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=generation,
        settle_status="ended",
    )

    with patch.object(orch_main, "postgres_db", db):
        assert not await orch_main._acknowledge_retiring_failed_attach(
            ids["agent"],
            ids["thread"],
            expected_runtime_generation=generation,
            expected_attach_token=ids["attach_token"],
            expected_agent_pod_uid="old-pod",
            local_quiescence_protocol="agent_runtime_zero_v1",
            workspace_generation=None,
            workspace_runtime_incarnation=None,
        )
    assert (await db.get_thread(ids["thread"]))[
        "runtime_retirement_local_quiescence"
    ] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("admission_kind", ["input", "control"])
async def test_failed_attach_abort_refuses_any_committed_admission(db, admission_kind):
    import main as orch_main

    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='created' WHERE id=$1",
            UUID(ids["thread"]),
        )
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    async with db.acquire() as conn:
        if admission_kind == "input":
            message_id = await conn.fetchval(
                "INSERT INTO thread_messages (thread_id,role,content,turn_number) "
                "VALUES ($1,'user','admitted',1) RETURNING id",
                UUID(ids["thread"]),
            )
            await conn.execute(
                "INSERT INTO thread_input_deliveries ("
                "delivery_id,thread_id,message_id,source,state,claim_generation,"
                "owner_agent_id,owner_pod_uid,owner_runtime_generation,"
                "admitted_turn_number,admitted_at) VALUES ("
                "$1,$2,$3,'direct_human','admitted',1,$4,'old-pod',$5,1,now())",
                uuid4(),
                UUID(ids["thread"]),
                message_id,
                UUID(ids["agent"]),
                UUID(generation),
            )
        else:
            await conn.execute(
                "INSERT INTO thread_control_requests ("
                "thread_id,request_seq,client_request_id,verb,payload,"
                "requested_by,runtime_generation) VALUES ("
                "$1,1,$2,'interrupt','{}'::jsonb,'owner',$3)",
                UUID(ids["thread"]),
                uuid4(),
                UUID(generation),
            )
    with patch.object(orch_main, "postgres_db", db):
        assert (
            await orch_main._release_session_attach_binding(
                ids["agent"],
                ids["thread"],
                expected_runtime_generation=generation,
                expected_attach_token=ids["attach_token"],
                pre_delivery=True,
            )
            == "unsafe"
        )
    current = await db.get_thread(ids["thread"])
    assert str(current["runtime_generation"]) == generation
    assert str(current["agent_id"]) == ids["agent"]


def _pinned_event_writer(db: PostgresDB, ids: dict[str, str], generation: str):
    import src.api.persistent_app as agent_app

    return agent_app._OrderedPersistentEventWriter(
        postgres_conn=db._pool,
        thread_id=ids["thread"],
        epoch=0,
        on_terminal_failure=lambda _events, _reason: None,
        pinned_agent_id=ids["agent"],
        pinned_runtime_generation=generation,
        pinned_runtime_attach_token=ids["attach_token"],
    )


@pytest.mark.asyncio
async def test_pinned_event_flush_after_settlement_cannot_append_after_terminal(db):
    """A delayed G1 flush observes terminal authority and appends nothing."""
    import src.api.persistent_app as agent_app

    ids = await _seed(db)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    writer = _pinned_event_writer(db, ids, generation)
    late = agent_app._QueuedPersistentEvent(
        0, 1, "token", {"content": "must not follow terminal"}
    )

    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    await _authorize_and_ack(db, ids, retirement)

    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=retirement["generation"],
    )
    assert await writer._write_batch([late]) == 0
    async with db.acquire() as conn:
        frames = await conn.fetch(
            "SELECT kind,payload FROM thread_events WHERE thread_id=$1 ORDER BY seq",
            UUID(ids["thread"]),
        )
        hwm = await conn.fetchval(
            "SELECT events_seq_hwm FROM threads WHERE id=$1", UUID(ids["thread"])
        )
    assert [frame["kind"] for frame in frames] == ["session.ended"]
    assert hwm == 1


@pytest.mark.asyncio
async def test_pinned_event_flush_winning_before_begin_serializes_before_terminal(db):
    """The converse ordering preserves a valid pre-Begin frame exactly once."""
    import src.api.persistent_app as agent_app

    ids = await _seed(db)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    writer = _pinned_event_writer(db, ids, generation)
    assert (
        await writer._write_batch(
            [agent_app._QueuedPersistentEvent(0, 1, "token", {"content": "before"})]
        )
        == 1
    )
    retirement = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    await _authorize_and_ack(db, ids, retirement)
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=retirement["token"],
        generation=retirement["generation"],
    )
    async with db.acquire() as conn:
        frames = await conn.fetch(
            "SELECT kind,payload FROM thread_events WHERE thread_id=$1 ORDER BY seq",
            UUID(ids["thread"]),
        )
    assert [frame["kind"] for frame in frames] == ["token", "session.ended"]
    assert _json(frames[0]["payload"])["content"] == "before"


@pytest.mark.asyncio
async def test_pinned_retirement_is_single_owner_retryable_and_resume_rotates(db):
    ids = await _seed(db)
    first, second = await asyncio.gather(
        db.begin_pinned_thread_retirement(ids["thread"], permanent=False),
        db.begin_pinned_thread_retirement(ids["thread"], permanent=False),
    )
    assert first["state"] == second["state"] == "pending"
    assert first["token"] == second["token"]
    assert first["generation"] == second["generation"]
    assert {first["reused"], second["reused"]} == {False, True}
    await _authorize_and_ack(db, ids, first)

    assert not await db.settle_pinned_thread_retirement(
        ids["thread"], token=str(uuid4()), generation=first["generation"]
    )
    assert await db.settle_pinned_thread_retirement(
        ids["thread"], token=first["token"], generation=first["generation"]
    )
    ended = await db.get_thread(ids["thread"])
    assert ended["status"] == "ended"
    assert ended["runtime_retirement_token"] is None
    assert ended["runtime_generation"] == UUID(first["generation"])

    assert await db.resume_thread(ids["thread"])
    resumed = await db.get_thread(ids["thread"])
    assert resumed["status"] == "created"
    assert resumed["runtime_generation"] != UUID(first["generation"])

    # A delayed G1 settlement can neither end nor mutate the reopened G2.
    assert not await db.settle_pinned_thread_retirement(
        ids["thread"], token=first["token"], generation=first["generation"]
    )
    assert (await db.get_thread(ids["thread"]))["status"] == "created"


@pytest.mark.asyncio
async def test_pinned_retirement_disposition_is_immutable_and_enforced(db):
    ids = await _seed(db)
    authority = await db.begin_pinned_thread_retirement(
        ids["thread"], permanent=False, settle_status="suspended"
    )
    assert authority["context"]["settle_status"] == "suspended"

    changed = await db.begin_pinned_thread_retirement(
        ids["thread"], permanent=False, settle_status="ended"
    )
    assert changed == {
        "state": "conflict",
        "reason": "retirement_disposition_changed",
        "token": authority["token"],
        "generation": authority["generation"],
    }
    reused = await db.begin_pinned_thread_retirement(
        ids["thread"], permanent=False, settle_status="suspended"
    )
    assert reused["state"] == "pending"
    assert reused["reused"] is True
    assert reused["token"] == authority["token"]
    await _authorize_and_ack(db, ids, authority, settle_status="suspended")

    assert not await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        final_status="ended",
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as wrong_disposition:
            await conn.execute(
                "UPDATE threads SET status='ended', ended_at=now(), "
                "runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_authorized_at=NULL, "
                "runtime_retirement_context=NULL, "
                "runtime_retirement_stage_receipt=NULL, "
                "runtime_retirement_local_quiescence=NULL, "
                "agent_id=NULL, control_admission_agent_id=NULL, "
                "runtime_attach_token=NULL WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert (
            wrong_disposition.value.constraint_name
            == "threads_runtime_retirement_disposition"
        )

    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        final_status="suspended",
    )
    settled = await db.get_thread(ids["thread"])
    assert settled["status"] == "suspended"
    assert str(settled["runtime_generation"]) != authority["generation"]


@pytest.mark.asyncio
async def test_direct_soft_suspension_forces_generation_and_clears_ownership(db):
    ids = await _seed(db)
    authority = await db.begin_pinned_thread_retirement(
        ids["thread"], permanent=False, settle_status="suspended"
    )
    before = UUID(authority["generation"])
    await _authorize_and_ack(db, ids, authority, settle_status="suspended")

    async with db.acquire() as conn:
        # A marker-shaped status transition may not retain stale ownership.
        with pytest.raises(asyncpg.CheckViolationError) as partial:
            await conn.execute(
                "UPDATE threads SET status='suspended', ended_at=NULL, "
                "runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_authorized_at=NULL, "
                "runtime_retirement_context=NULL, "
                "runtime_retirement_stage_receipt=NULL WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert partial.value.constraint_name == "threads_runtime_retirement_pending"

        async with conn.transaction():
            await conn.execute(
                "UPDATE agents SET thread_id=NULL,status='offline' "
                "WHERE id=$1 AND thread_id=$2",
                UUID(ids["agent"]),
                UUID(ids["thread"]),
            )
            await conn.execute(
                "UPDATE threads SET status='suspended', ended_at=NULL, "
                "runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_authorized_at=NULL, "
                "runtime_retirement_context=NULL, "
                "runtime_retirement_stage_receipt=NULL, "
                "runtime_retirement_local_quiescence=NULL, "
                "agent_id=NULL, control_admission_agent_id=NULL, "
                "runtime_attach_token=NULL WHERE id=$1",
                UUID(ids["thread"]),
            )
        settled = await conn.fetchrow(
            "SELECT status::text, runtime_generation, agent_id, "
            "control_admission_agent_id, runtime_attach_token "
            "FROM threads WHERE id=$1",
            UUID(ids["thread"]),
        )
    assert settled["status"] == "suspended"
    assert settled["runtime_generation"] != before
    assert settled["agent_id"] is None
    assert settled["control_admission_agent_id"] is None
    assert settled["runtime_attach_token"] is None


@pytest.mark.asyncio
async def test_never_engaged_protected_retirement_requires_receipt_and_journals(db):
    ids = await _seed(db, bind_agent=False)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET agent_id=NULL, control_admission_agent_id=NULL, "
            "runtime_attach_token=NULL, "
            "metadata=$2::jsonb WHERE id=$1",
            UUID(ids["thread"]),
            json.dumps(
                {
                    "protected_cloud": True,
                    "config_override": {"workspace": {"backend": "sandbox"}},
                }
            ),
        )

    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert authority["context"]["protected_cloud"] is True
    assert authority["context"]["protected_ro"] is None
    assert authority["context"]["agent_id"] is None
    assert authority["context"]["runtime_authority_exposed"] is False
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )

    event = {
        "thread_id": ids["thread"],
        "session_runtime_generation": authority["generation"],
        "staged_epoch": 0,
        "file_count": 0,
        "counts": {"added": 0, "modified": 0, "deleted": 0},
        "mount_id": None,
    }
    # Application settlement and direct SQL are both fail-closed before the
    # exact never-engaged receipt exists.
    assert not await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        staged_event=event,
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError) as unreceipted:
            await conn.execute(
                "UPDATE threads SET status='ended', ended_at=now(), "
                "runtime_retirement_token=NULL, "
                "runtime_retirement_permanent=NULL, "
                "runtime_retirement_started_at=NULL, "
                "runtime_retirement_authorized_at=NULL, "
                "runtime_retirement_context=NULL, "
                "runtime_retirement_stage_receipt=NULL, "
                "agent_id=NULL, control_admission_agent_id=NULL, "
                "runtime_attach_token=NULL WHERE id=$1",
                UUID(ids["thread"]),
            )
        assert (
            unreceipted.value.constraint_name
            == "threads_runtime_retirement_stage_receipt_pending"
        )

    receipt = await db.publish_never_engaged_retirement_stage_receipt(
        ids["thread"],
        expected_runtime_generation=authority["generation"],
        expected_retirement_token=authority["token"],
    )
    assert receipt is not None
    assert receipt["kind"] == "never_engaged"
    assert await db.settle_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        staged_event=event,
    )
    async with db.acquire() as conn:
        frames = await conn.fetch(
            "SELECT kind, payload FROM thread_events WHERE thread_id=$1 ORDER BY seq",
            UUID(ids["thread"]),
        )
    assert [frame["kind"] for frame in frames] == [
        "cloud.diff_staged",
        "session.ended",
    ]
    assert _json(frames[0]["payload"])["file_count"] == 0
    assert (
        _json(frames[1]["payload"])["session_runtime_generation"]
        == authority["generation"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("captured_status", ["engaging", "active", "revoked"])
async def test_never_delivered_reader_can_publish_never_engaged_receipt(
    db, captured_status
):
    ids = await _seed(db, bind_agent=False)
    row_id, plan, generation, _mount_id = await _seed_protected_ro_attempt(
        db,
        ids,
        status=captured_status,
    )

    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )
    assert authority["context"]["protected_ro"]["status"] == captured_status
    assert authority["context"]["protected_ro"]["etag_baseline"] == (
        {} if captured_status == "active" else None
    )
    # End winning while baseline capture is blocked leaves the exact attempt
    # engaging in Begin's context; engage rollback then makes the current row
    # revoked before the zero-proof is allowed.
    if captured_status in {"engaging", "active"}:
        assert await db.begin_ro_mount_revocation_if_matches(
            row_id,
            expected_thread_id=ids["thread"],
            expected_runtime_generation=generation,
            plan=plan,
        )
        assert await db.finish_ro_mount_revocation_if_matches(
            row_id,
            expected_thread_id=ids["thread"],
            expected_runtime_generation=generation,
            plan=plan,
        )

    receipt = await db.publish_never_engaged_retirement_stage_receipt(
        ids["thread"],
        expected_runtime_generation=authority["generation"],
        expected_retirement_token=authority["token"],
    )
    assert receipt is not None
    assert receipt["kind"] == "never_engaged"
    assert receipt["mount_id"] == row_id
    assert receipt["engage_attempt"] == plan.engage_attempt


@pytest.mark.asyncio
async def test_never_engaged_receipt_rejects_preexisting_staged_overlay(db):
    ids = await _seed(db, bind_agent=False)
    row_id, plan, generation, _mount_id = await _seed_protected_ro_attempt(
        db,
        ids,
        status="active",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE cloud_ro_mounts SET staged_epoch=1, "
            "staged_summary='{\"file_count\":1}'::jsonb WHERE id=$1::uuid",
            UUID(row_id),
        )
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert await db.authorize_pinned_thread_retirement(
        ids["thread"],
        token=authority["token"],
        generation=authority["generation"],
        settle_status="ended",
    )
    assert authority["context"]["protected_ro"]["status"] == "active"
    assert await db.begin_ro_mount_revocation_if_matches(
        row_id,
        expected_thread_id=ids["thread"],
        expected_runtime_generation=generation,
        plan=plan,
    )
    assert await db.finish_ro_mount_revocation_if_matches(
        row_id,
        expected_thread_id=ids["thread"],
        expected_runtime_generation=generation,
        plan=plan,
    )
    assert (
        await db.publish_never_engaged_retirement_stage_receipt(
            ids["thread"],
            expected_runtime_generation=authority["generation"],
            expected_retirement_token=authority["token"],
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_status", ["engaging", "active"])
async def test_soft_end_revokes_never_delivered_reader_before_zero_stage(
    db, reader_status
):
    import main as orch_main

    ids = await _seed(db, bind_agent=False)
    row_id, plan, _generation, _mount_id = await _seed_protected_ro_attempt(
        db,
        ids,
        status=reader_status,
    )

    backend = MagicMock()
    backend.revoke_protected_reader_attempt = AsyncMock()
    cloud_router = MagicMock()
    cloud_router.for_backend_instance.return_value = backend
    thread = await db.get_thread(ids["thread"])
    assert thread is not None
    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "main_cloud_router", cloud_router),
        patch.object(
            orch_main.session_router,
            "teardown_route",
            AsyncMock(return_value=True),
        ),
        patch.object(
            orch_main, "_conclude_conference_if_any", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main, "_thread_turn_in_flight", AsyncMock(return_value=False)
        ),
    ):
        result = await orch_main._end_thread_flow(
            ids["thread"], thread, permanent=False, force=True
        )

    assert result == {"status": "ended"}
    settled = await db.get_thread(ids["thread"])
    assert settled is not None
    assert settled["status"] == "ended"
    receipt = _json(settled["runtime_retirement_stage_receipt"])
    assert receipt is None  # soft settlement clears the live marker atomically
    ro_row = await db.get_ro_mount_by_thread(ids["thread"])
    assert ro_row is not None and ro_row["status"] == "revoked"
    backend.revoke_protected_reader_attempt.assert_awaited_once_with(plan)
    async with db.acquire() as conn:
        staged = await conn.fetchrow(
            "SELECT payload FROM thread_events WHERE thread_id=$1::uuid "
            "AND kind='cloud.diff_staged'",
            UUID(ids["thread"]),
        )
    assert staged is not None
    assert _json(staged["payload"])["file_count"] == 0


@pytest.mark.asyncio
async def test_malformed_protected_marker_cannot_soft_retire_as_unprotected(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata=jsonb_set(metadata, "
            "'{protected_cloud}', '\"true\"'::jsonb) WHERE id=$1",
            UUID(ids["thread"]),
        )
    refused = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    assert refused == {
        "state": "malformed",
        "reason": "protected_cloud_malformed",
    }
    assert (await db.get_thread(ids["thread"]))["runtime_retirement_token"] is None


@pytest.mark.asyncio
async def test_pending_pinned_retirement_blocks_legacy_resume_sql(db):
    ids = await _seed(db)
    authority = await db.begin_pinned_thread_retirement(ids["thread"], permanent=False)
    async with db.acquire() as conn:
        # Model a partially completed End whose old writer has already set the
        # terminal status but retained the durable cleanup token.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE threads SET status='ended' WHERE id=$1", UUID(ids["thread"])
            )
        # Marker remains authoritative and no old Resume SQL can rotate it.
        assert await conn.fetchval(
            "SELECT runtime_retirement_token FROM threads WHERE id=$1",
            UUID(ids["thread"]),
        ) == UUID(authority["token"])


@pytest.mark.asyncio
async def test_stale_connection_self_heal_cannot_clear_successor_binding(db):
    """A /connection snapshot of offline A cannot detach concurrently bound B."""

    ids = await _seed(db)
    generation = str((await db.get_thread(ids["thread"]))["runtime_generation"])
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET status='offline' WHERE id=$1",
            UUID(ids["agent"]),
        )
    successor, _actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid="successor-pod"
    )

    # This is the stale continuation from the earlier A snapshot.  A broad
    # update_thread_agent(..., None) would erase B here; the exact CAS loses.
    assert (
        await db.clear_stale_thread_agent_if_matches(
            ids["thread"],
            ids["agent"],
            expected_runtime_generation=generation,
        )
        is False
    )
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.agent_id, a.thread_id, a.status "
            "FROM threads t JOIN agents a ON a.id=t.agent_id WHERE t.id=$1",
            UUID(ids["thread"]),
        )
    assert str(row["agent_id"]) == successor
    assert str(row["thread_id"]) == ids["thread"]
    assert row["status"] == "session"


@pytest.mark.asyncio
async def test_turn_boundary_recycle_preserves_thread_and_replaces_authority(db):
    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="legate",
        dedup_key="recycle-continuity",
        payload={"message": "survives pod replacement"},
        project_id=ids["project"],
    )
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    old = PersistentPodObservation.from_status(ids["thread"], provisioner.current)

    requested = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=old,
        expected_project_id=ids["project"],
    )
    assert requested.phase == "awaiting_old_pod_exit"
    state, metadata = await _recycle_state(db, ids["thread"])
    hold = metadata["config_override"]["officer"]["hold"]
    assert hold["kind"] == "maintenance"
    assert "thread_id" not in hold
    assert state["hold_owned"] is True
    acknowledgement = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert acknowledgement.acknowledged is True

    # The exact old object has disappeared after the runtime's clean exit.
    provisioner.current = None
    await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"awaiting_replacement"},
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["phase"] == "awaiting_replacement"
    assert provisioner.create_calls == 1

    new_uid = provisioner.current["pod_uid"]
    successor, successor_actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid=new_uid
    )
    generation = state["generation"]
    provisioner.current = _pod_status(
        ids["thread"],
        uid=new_uid,
        build="new-build",
        generation=generation,
        ready=True,
    )
    completed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert completed.phase == "complete"
    row = await db.get_thread(ids["thread"])
    assert str(row["agent_id"]) == successor
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"] is None
    assert metadata["agent_pod"]["pod_uid"] == new_uid

    with pytest.raises(Exception):
        await runtime_actor._actor_for_access(db, ids["old_access"])
    current = await runtime_actor._actor_for_access(
        db, successor_actor.access_credential
    )
    assert current.thread_id == ids["thread"]
    async with db.acquire() as conn:
        live_grants = await conn.fetchval(
            "SELECT count(*) FROM runtime_actor_grants "
            "WHERE thread_id=$1 AND agent_id=$2 AND revoked_at IS NULL",
            UUID(ids["thread"]),
            UUID(successor),
        )
        post_thread = await conn.fetchval(
            "SELECT thread_id FROM project_officers WHERE project_id=$1",
            UUID(ids["project"]),
        )
        pending_wakes = await conn.fetchval(
            "SELECT count(*) FROM session_wake_events "
            "WHERE thread_id=$1 AND dedup_key='recycle-continuity'",
            UUID(ids["thread"]),
        )
    assert live_grants == 1
    assert str(post_thread) == ids["thread"]
    assert pending_wakes == 1


@pytest.mark.asyncio
async def test_recycler_legacy_thread_recovers_through_registration_route(db, caplog):
    """0177 recovery uses the same adoption/bind/grant path as a real pod."""

    import main as orch_main

    ids = await _seed(db)
    message_id = await db.save_thread_message(
        ids["thread"], "human", "continuity marker", turn_number=7
    )
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="test",
        dedup_key="managed-authority-recycle",
        payload={"kind": "continuity"},
        project_id=ids["project"],
    )
    repo_name = f"thread-{ids['thread'][:8]}"
    legacy_url = f"http://admin:shared-secret@gitea:3000/srw/{repo_name}.git"
    async with db.acquire() as conn:
        # Reproduce only state the immediately previous release emitted: the
        # already-bound commissioned thread carried its deterministic primary
        # remote in ordinary workspace metadata and had no 0176 authority row.
        await conn.execute(
            "ALTER TABLE threads DISABLE TRIGGER "
            "trg_managed_thread_repository_url_authority"
        )
        try:
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_set(metadata, "
                "'{workspace_container}', jsonb_build_object("
                "'repo_name', $2::text, 'git_remote_url', $3::text), true) "
                "WHERE id=$1",
                UUID(ids["thread"]),
                repo_name,
                legacy_url,
            )
        finally:
            await conn.execute(
                "ALTER TABLE threads ENABLE TRIGGER "
                "trg_managed_thread_repository_url_authority"
            )
        post_before = dict(
            await conn.fetchrow(
                "SELECT project_id, thread_id, config_override, "
                "communication_policy, state, incarnations, created_at "
                "FROM project_officers WHERE project_id=$1",
                UUID(ids["project"]),
            )
        )
        thread_before = dict(
            await conn.fetchrow(
                "SELECT id, user_id, project_id, execution_lane, config_name, "
                "created_at FROM threads WHERE id=$1",
                UUID(ids["thread"]),
            )
        )
        old_incarnation = await conn.fetchval(
            "SELECT officer_incarnation FROM runtime_actor_grants "
            "WHERE thread_id=$1 AND revoked_at IS NULL",
            UUID(ids["thread"]),
        )
        wake_before = dict(
            await conn.fetchrow(
                "SELECT id, thread_id, project_id, source, dedup_key, payload, "
                "state, created_at FROM session_wake_events "
                "WHERE thread_id=$1 AND dedup_key='managed-authority-recycle'",
                UUID(ids["thread"]),
            )
        )

    provisioner = FakeProvisioner()
    pvc_identity = (
        f"pvc-persistent-{ids['thread'][:12]}",
        f"pvc-fixture-{uuid4()}",
    )
    provisioner.pvc_identities[ids["thread"]] = pvc_identity
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    observation = PersistentPodObservation.from_status(
        ids["thread"], provisioner.current
    )

    requested = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=observation,
        expected_project_id=ids["project"],
    )
    assert requested.phase == "awaiting_old_pod_exit"
    acknowledged = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert acknowledged.acknowledged is True

    provisioner.current = None
    advanced = await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"awaiting_replacement"},
    )
    assert advanced.phase == "awaiting_replacement"
    assert provisioner.create_calls == 1
    assert provisioner.pvc_identities[ids["thread"]] == pvc_identity
    detached = await db.get_thread(ids["thread"])
    assert detached["agent_id"] is None
    detached_metadata = _json(detached["metadata"])
    assert detached_metadata["workspace_container"]["git_remote_url"] == legacy_url
    assert detached_metadata["config_override"]["officer"]["hold"] is not None
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["phase"] == "awaiting_replacement"
    assert state["generation"] == advanced.generation

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agents WHERE id=$1", UUID(ids["agent"])
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM runtime_actor_grants "
                "WHERE thread_id=$1 AND revoked_at IS NULL",
                UUID(ids["thread"]),
            )
            == 0
        )
        # A direct replacement bind remains database-fenced before proof.
        unauthorized_agent = uuid4()
        await conn.execute(
            "INSERT INTO agents "
            "(id, config_name, hostname, status, agent_mode) "
            "VALUES ($1, 'centurion', 'unproven-replacement', 'booting', "
            "'persistent')",
            unauthorized_agent,
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as unproven:
            await conn.execute(
                "UPDATE agents SET thread_id=$2 WHERE id=$1",
                unauthorized_agent,
                UUID(ids["thread"]),
            )
        assert unproven.value.constraint_name == "agents_thread_authority"
        await conn.execute("DELETE FROM agents WHERE id=$1", unauthorized_agent)

    replacement_uid = provisioner.current["pod_uid"]
    bootstrap = await runtime_actor.issue_runtime_actor_bootstrap(db, ids["thread"])
    request = MagicMock()
    request.headers = {RUNTIME_ACTOR_BOOTSTRAP_HEADER: bootstrap}
    registration = orch_main.AgentRegistration(
        config_name="centurion",
        pod_ip="127.0.0.2",
        hostname=provisioner.current["pod_name"],
        agent_mode="persistent",
        thread_id=ids["thread"],
        session_runtime_generation=UUID(
            str((await db.get_thread(ids["thread"]))["runtime_generation"])
        ),
        build_sha="new-build",
        pod_uid=replacement_uid,
    )
    gitea = _managed_gitea(probe=False)
    with (
        patch.object(orch_main, "require_internal", AsyncMock()),
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "gitea_client", gitea),
        pytest.raises(orch_main.HTTPException) as unavailable,
    ):
        await orch_main.register_agent(request, registration)
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail == "Workspace repository authority is unavailable"
    assert "shared-secret" not in str(unavailable.value.detail)
    assert "shared-secret" not in caplog.text

    failed = await db.get_thread(ids["thread"])
    failed_metadata = _json(failed["metadata"])
    assert failed["agent_id"] is None
    assert failed_metadata["workspace_container"]["git_remote_url"] == legacy_url
    assert failed_metadata["config_override"]["officer"]["hold"] is not None
    failed_state, _ = await _recycle_state(db, ids["thread"])
    assert failed_state["generation"] == state["generation"]
    assert failed_state["phase"] == "awaiting_replacement"
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agents WHERE thread_id=$1",
                UUID(ids["thread"]),
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM runtime_actor_grants "
                "WHERE thread_id=$1 AND revoked_at IS NULL",
                UUID(ids["thread"]),
            )
            == 0
        )
        failed_authority = dict(
            await conn.fetchrow(
                "SELECT id, generation, status, access_mode, authority_kind, "
                "authority_id, project_id, failure_class "
                "FROM managed_repository_authorities WHERE repo_name=$1",
                repo_name,
            )
        )
    assert failed_authority["status"] == "provisioning"
    assert failed_authority["failure_class"] == "deploy_key_probe"

    # Retry the exact same production registration. The existing reservation
    # is proven and activated, the observed URL is CAS-scrubbed, and only then
    # may the route insert/bind the agent and mint its Officer runtime actor.
    gitea.probe_repo_deploy_key.return_value = True
    with (
        patch.object(orch_main, "require_internal", AsyncMock()),
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "gitea_client", gitea),
    ):
        response = await orch_main.register_agent(request, registration)
    assert response.runtime_actor is not None
    successor = response.agent_id

    bound = await db.get_thread(ids["thread"])
    bound_metadata = _json(bound["metadata"])
    clean_url = bound_metadata["workspace_container"]["git_remote_url"]
    assert str(bound["agent_id"]) == successor
    assert clean_url == f"http://gitea:3000/srw/{repo_name}.git"
    assert "shared-secret" not in clean_url
    # Registration alone is not readiness and must not release the hold.
    assert bound_metadata["config_override"]["officer"]["hold"] is not None
    async with db.acquire() as conn:
        authority = dict(
            await conn.fetchrow(
                "SELECT id, generation, status, access_mode, authority_kind, "
                "authority_id, project_id, "
                "private_key_ciphertext IS NOT NULL AS encrypted "
                "FROM managed_repository_authorities WHERE repo_name=$1",
                repo_name,
            )
        )
        grant = dict(
            await conn.fetchrow(
                "SELECT id, caller_kind, project_id, thread_id, agent_id, "
                "officer_incarnation, revoked_at, refresh_expires_at > now() "
                "AS refresh_valid FROM runtime_actor_grants "
                "WHERE thread_id=$1 AND agent_id=$2 AND revoked_at IS NULL",
                UUID(ids["thread"]),
                UUID(successor),
            )
        )
    assert authority == {
        "id": failed_authority["id"],
        "generation": 1,
        "status": "active",
        "access_mode": "write",
        "authority_kind": "thread",
        "authority_id": UUID(ids["thread"]),
        "project_id": UUID(ids["project"]),
        "encrypted": True,
    }
    assert grant["caller_kind"] == "officer"
    assert str(grant["project_id"]) == ids["project"]
    assert str(grant["thread_id"]) == ids["thread"]
    assert str(grant["agent_id"]) == successor
    assert grant["officer_incarnation"] == old_incarnation
    assert grant["revoked_at"] is None
    assert grant["refresh_valid"] is True

    await db.heartbeat(successor, status="session")
    provisioner.current = _pod_status(
        ids["thread"],
        uid=replacement_uid,
        build="new-build",
        generation=state["generation"],
        ready=True,
    )
    completed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert completed.phase == "complete"

    final_thread = await db.get_thread(ids["thread"])
    final_metadata = _json(final_thread["metadata"])
    assert str(final_thread["agent_id"]) == successor
    assert final_metadata["config_override"]["officer"]["hold"] is None
    assert final_metadata["agent_pod"]["recycle"]["phase"] == "complete"
    assert provisioner.pvc_identities[ids["thread"]] == pvc_identity
    async with db.acquire() as conn:
        post_after = dict(
            await conn.fetchrow(
                "SELECT project_id, thread_id, config_override, "
                "communication_policy, state, incarnations, created_at "
                "FROM project_officers WHERE project_id=$1",
                UUID(ids["project"]),
            )
        )
        thread_after = dict(
            await conn.fetchrow(
                "SELECT id, user_id, project_id, execution_lane, config_name, "
                "created_at FROM threads WHERE id=$1",
                UUID(ids["thread"]),
            )
        )
        messages = await conn.fetch(
            "SELECT id, content, turn_number FROM thread_messages WHERE thread_id=$1",
            UUID(ids["thread"]),
        )
        wake_after = dict(
            await conn.fetchrow(
                "SELECT id, thread_id, project_id, source, dedup_key, payload, "
                "state, created_at FROM session_wake_events "
                "WHERE thread_id=$1 AND dedup_key='managed-authority-recycle'",
                UUID(ids["thread"]),
            )
        )
        live_grants = await conn.fetchval(
            "SELECT count(*) FROM runtime_actor_grants "
            "WHERE thread_id=$1 AND revoked_at IS NULL",
            UUID(ids["thread"]),
        )
    assert post_after == post_before
    assert thread_after == thread_before
    assert [
        (str(row["id"]), row["content"], row["turn_number"]) for row in messages
    ] == [(message_id, "continuity marker", 7)]
    assert wake_after == wake_before
    assert live_grants == 1


@pytest.mark.asyncio
async def test_concurrent_missing_pod_recovery_uses_one_generation_and_create(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)

    results = await asyncio.gather(
        *(
            recycler.request_and_reconcile(
                thread_id=ids["thread"],
                reason="missing_pod",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(4)
        )
    )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert len({r.generation for r in results if r.generation}) == 1
    assert state["phase"] == "awaiting_replacement"
    assert provisioner.create_calls == 1


@pytest.mark.asyncio
async def test_raw_delete_wake_rejection_survives_hold_and_replacement(db):
    """The observed ordering: wake claim precedes the 60s lifecycle tick.

    The terminating runtime refuses that first delivery, so the outbox row is
    released rather than stamped sent.  Missing-pod reconciliation then owns a
    maintenance hold; after exact replacement authority is healthy, the same
    durable delivery id is claimed and can be settled once.
    """

    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="timer",
        dedup_key="timer",
        payload={"minutes": 30, "reason": "raw deletion race"},
        project_id=ids["project"],
    )

    claimed = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assert len(claimed) == 1
    first = await db.assign_session_wake_delivery_groups([int(claimed[0]["id"])])
    assert len(first) == 1
    delivery_id = first[0]["delivery_id"]
    await db.release_session_wake_events([int(first[0]["id"])])

    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    missing = await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"awaiting_replacement"},
    )
    assert missing.phase == "awaiting_replacement"
    state, metadata = await _recycle_state(db, ids["thread"])
    hold = metadata["config_override"]["officer"]["hold"]
    assert hold["kind"] == "maintenance"
    assert "thread_id" not in hold
    assert (
        await db.claim_pending_session_wake_events(
            debounce_seconds_by_source={"timer": 0}
        )
        == []
    )

    new_uid = provisioner.current["pod_uid"]
    successor, actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid=new_uid
    )
    provisioner.current = _pod_status(
        ids["thread"],
        uid=new_uid,
        build="new-build",
        generation=state["generation"],
        ready=True,
    )
    completed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert completed.phase == "complete"
    assert str((await db.get_thread(ids["thread"]))["agent_id"]) == successor
    recovered_actor = await runtime_actor._actor_for_access(db, actor.access_credential)
    assert recovered_actor.thread_id == ids["thread"]
    assert recovered_actor.caller_kind == "officer"

    retried = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assert len(retried) == 1
    second = await db.assign_session_wake_delivery_groups([int(retried[0]["id"])])
    assert [row["delivery_id"] for row in second] == [delivery_id]
    runtime_generation, runtime_attach_token = await _runtime_identity(
        db, ids["thread"]
    )
    async with db.acquire() as conn:
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="replacement executes the retained wake",
                source="officer_wake",
                turn_number=1,
                agent_id=successor,
                pod_uid=new_uid,
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid=new_uid,
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(delivery["claim_generation"]),
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid=new_uid,
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(delivery["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    await db.finish_session_wake_events([int(second[0]["id"])])
    assert (
        await db.claim_pending_session_wake_events(
            debounce_seconds_by_source={"timer": 0}
        )
        == []
    )


@pytest.mark.asyncio
async def test_concurrent_delivery_identity_and_transcript_accept_are_once(db):
    ids = await _seed(db)
    for key in ("first", "second"):
        assert await db.enqueue_session_wake_event(
            ids["thread"],
            source="test",
            dedup_key=key,
            payload={"summary": key},
            project_id=ids["project"],
        )

    left, right = await asyncio.gather(
        db.claim_pending_session_wake_events(),
        db.claim_pending_session_wake_events(),
    )
    claimed = [*left, *right]
    assert len(claimed) == 2
    claimed_ids = [int(row["id"]) for row in claimed]

    assigned_a, assigned_b = await asyncio.gather(
        db.assign_session_wake_delivery_groups(claimed_ids),
        db.assign_session_wake_delivery_groups(claimed_ids),
    )
    delivery_ids_a = {row["delivery_id"] for row in assigned_a}
    delivery_ids_b = {row["delivery_id"] for row in assigned_b}
    assert len(delivery_ids_a) == 1
    assert delivery_ids_a == delivery_ids_b
    delivery_id = next(iter(delivery_ids_a))
    assert (
        len(await db.get_session_wake_delivery_group(ids["thread"], delivery_id)) == 2
    )

    runtime_generation, runtime_attach_token = await _runtime_identity(
        db, ids["thread"]
    )

    async def persist_once():
        async with db.acquire() as conn:
            async with conn.transaction():
                return await persist_input_delivery(
                    conn,
                    thread_id=ids["thread"],
                    delivery_id=delivery_id,
                    role="event",
                    content="same accepted wake",
                    source="officer_wake",
                    turn_number=1,
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                    runtime_attach_token=runtime_attach_token,
                )

    first, retry = await asyncio.gather(persist_once(), persist_once())
    assert sorted((first["transcript_inserted"], retry["transcript_inserted"])) == [
        False,
        True,
    ]
    assert first["claim_generation"] == retry["claim_generation"] == 1
    async with db.acquire() as conn:
        async with conn.transaction():
            rerendered = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="newer sitrep text must not replace accepted input",
                source="officer_wake",
                turn_number=2,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )
    assert rerendered["content"] == "same accepted wake"
    async with db.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM thread_messages "
            "WHERE thread_id=$1 AND role='event' "
            "AND content='same accepted wake'",
            UUID(ids["thread"]),
        )
    assert count == 1


@pytest.mark.asyncio
async def test_input_persist_crash_successor_reclaim_and_stale_owner_fence(db):
    """INSERT-without-queue is reclaimable; the predecessor cannot settle it."""

    ids = await _seed(db)
    delivery_id = uuid4()
    old_runtime, runtime_attach_token = await _runtime_identity(db, ids["thread"])
    async with db.acquire() as conn:
        async with conn.transaction():
            first = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="durable before process death",
                source="officer_wake",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=old_runtime,
                runtime_attach_token=runtime_attach_token,
            )
    assert first["state"] == "owned"
    assert first["transcript_inserted"] is True

    successor, _actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid="successor-pod"
    )
    new_runtime, new_attach_token = await _runtime_identity(db, ids["thread"])

    async with db.acquire() as conn:
        async with conn.transaction():
            reclaimed = await claim_pending_input_deliveries(
                conn,
                thread_id=ids["thread"],
                agent_id=successor,
                pod_uid="successor-pod",
                runtime_generation=new_runtime,
                runtime_attach_token=new_attach_token,
            )
    assert len(reclaimed) == 1
    assert int(reclaimed[0]["claim_generation"]) == 2

    async with db.acquire() as conn:
        async with conn.transaction():
            assert not await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=old_runtime,
                runtime_attach_token=runtime_attach_token,
                claim_generation=1,
                transition="settled",
            )
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid="successor-pod",
                runtime_generation=new_runtime,
                runtime_attach_token=new_attach_token,
                claim_generation=2,
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid="successor-pod",
                runtime_generation=new_runtime,
                runtime_attach_token=new_attach_token,
                claim_generation=2,
                transition="admitted",
                turn_number=1,
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid="successor-pod",
                runtime_generation=new_runtime,
                runtime_attach_token=new_attach_token,
                claim_generation=2,
                transition="settled",
            )

    async with db.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE state='settled') "
            "AS settled FROM thread_input_deliveries WHERE delivery_id=$1",
            delivery_id,
        )
        transcript = await conn.fetchval(
            "SELECT count(*) FROM thread_messages message JOIN "
            "thread_input_deliveries delivery ON delivery.message_id=message.id "
            "WHERE delivery.delivery_id=$1",
            delivery_id,
        )
    assert dict(counts) == {"total": 1, "settled": 1}
    assert transcript == 1


@pytest.mark.asyncio
async def test_direct_human_cancellation_is_source_and_generation_fenced(db):
    ids = await _seed(db)
    direct_id = uuid4()
    event_id = uuid4()
    runtime_generation, runtime_attach_token = await _runtime_identity(
        db, ids["thread"]
    )
    async with db.acquire() as conn:
        async with conn.transaction():
            direct = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=direct_id,
                role="human",
                content="visible stopped intent",
                source="direct_human",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )
            event = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=event_id,
                role="event",
                content="durable wake debt",
                source="officer_wake",
                turn_number=2,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )
            for delivery_id, delivery in ((direct_id, direct), (event_id, event)):
                assert await mark_input_delivery_queued(
                    conn,
                    delivery_id=delivery_id,
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                    runtime_attach_token=runtime_attach_token,
                    claim_generation=int(delivery["claim_generation"]),
                )
            assert not await transition_input_delivery(
                conn,
                delivery_id=direct_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(direct["claim_generation"]) + 1,
                transition="cancelled",
                turn_number=1,
                reason="human_stop_before_provider",
            )
            assert not await transition_input_delivery(
                conn,
                delivery_id=event_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(event["claim_generation"]),
                transition="cancelled",
                turn_number=2,
                reason="human_stop_before_provider",
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=direct_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(direct["claim_generation"]),
                transition="cancelled",
                turn_number=1,
                reason="human_stop_before_provider",
            )

    async with db.acquire() as conn:
        cancelled = await conn.fetchrow(
            "SELECT state, source, cancelled_at, cancelled_turn_number, "
            "cancelled_reason FROM thread_input_deliveries WHERE delivery_id=$1",
            direct_id,
        )
        assert dict(cancelled) == {
            "state": "cancelled",
            "source": "direct_human",
            "cancelled_at": cancelled["cancelled_at"],
            "cancelled_turn_number": 1,
            "cancelled_reason": "human_stop_before_provider",
        }
        assert cancelled["cancelled_at"] is not None
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM thread_messages WHERE thread_id=$1 "
                "AND content='visible stopped intent'",
                UUID(ids["thread"]),
            )
            == 1
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE thread_input_deliveries SET state='cancelled', "
                "cancelled_at=statement_timestamp(), cancelled_turn_number=2, "
                "cancelled_reason='forged' WHERE delivery_id=$1",
                event_id,
            )

    async with db.acquire() as conn:
        async with conn.transaction():
            reclaimed = await claim_pending_input_deliveries(
                conn,
                thread_id=ids["thread"],
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )
    assert {str(row["delivery_id"]) for row in reclaimed} == {str(event_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("first_transition", ["cancelled", "admitted"])
async def test_direct_human_cancel_races_provider_admission_exactly_once(
    db,
    pg_dsn,
    first_transition,
):
    """Two physical connections race the same owner generation.

    A third transaction holds the delivery row while both contenders reach
    PostgreSQL's lock wait. Releasing that row makes the deliberately first
    waiter win and proves both terminal branches without scheduler sleeps.
    """

    ids = await _seed(db)
    delivery_id = uuid4()
    runtime_generation, runtime_attach_token = await _runtime_identity(
        db, ids["thread"]
    )
    async with db.acquire() as conn:
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="human",
                content=f"race winner {first_transition}",
                source="direct_human",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )
            generation = int(delivery["claim_generation"])
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=generation,
            )

    async def _wait_until_lock_blocked(observer, backend_pid):
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            lock_wait = await observer.fetchval(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1",
                backend_pid,
            )
            if lock_wait:
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"backend {backend_pid} never reached the lock barrier")

    async def _transition(conn, transition):
        return await transition_input_delivery(
            conn,
            delivery_id=delivery_id,
            agent_id=ids["agent"],
            pod_uid="old-pod",
            runtime_generation=runtime_generation,
            runtime_attach_token=runtime_attach_token,
            claim_generation=generation,
            transition=transition,
            turn_number=1,
            reason=(
                "human_stop_before_provider" if transition == "cancelled" else None
            ),
        )

    other_transition = "admitted" if first_transition == "cancelled" else "cancelled"
    tasks = []
    row_lock_open = False
    async with (
        db.acquire() as lock_conn,
        db.acquire() as first_conn,
        db.acquire() as second_conn,
    ):
        row_lock = lock_conn.transaction()
        await row_lock.start()
        row_lock_open = True
        try:
            await lock_conn.fetchval(
                "SELECT delivery_id FROM thread_input_deliveries "
                "WHERE delivery_id=$1 FOR UPDATE",
                delivery_id,
            )
            first_pid = await first_conn.fetchval("SELECT pg_backend_pid()")
            second_pid = await second_conn.fetchval("SELECT pg_backend_pid()")
            first_task = asyncio.create_task(_transition(first_conn, first_transition))
            tasks.append(first_task)
            await _wait_until_lock_blocked(lock_conn, first_pid)
            second_task = asyncio.create_task(
                _transition(second_conn, other_transition)
            )
            tasks.append(second_task)
            await _wait_until_lock_blocked(lock_conn, second_pid)
            await row_lock.commit()
            row_lock_open = False
            first_won, second_won = await asyncio.wait_for(
                asyncio.gather(first_task, second_task),
                timeout=2,
            )
        finally:
            if row_lock_open:
                await row_lock.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    assert (first_won, second_won) == (True, False)
    async with db.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM thread_input_deliveries WHERE delivery_id=$1",
            delivery_id,
        )

    if first_transition == "cancelled":
        assert state == "cancelled"
        async with db.acquire() as conn:
            async with conn.transaction():
                reclaimed = await claim_pending_input_deliveries(
                    conn,
                    thread_id=ids["thread"],
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                    runtime_attach_token=runtime_attach_token,
                )
        assert reclaimed == []
    else:
        assert state == "admitted"
        async with db.acquire() as conn:
            async with conn.transaction():
                assert not await transition_input_delivery(
                    conn,
                    delivery_id=delivery_id,
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                    runtime_attach_token=runtime_attach_token,
                    claim_generation=generation,
                    transition="cancelled",
                    turn_number=1,
                    reason="human_stop_before_provider",
                )
                assert await transition_input_delivery(
                    conn,
                    delivery_id=delivery_id,
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                    runtime_attach_token=runtime_attach_token,
                    claim_generation=generation,
                    transition="settled",
                )
                reclaimed = await claim_pending_input_deliveries(
                    conn,
                    thread_id=ids["thread"],
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                    runtime_attach_token=runtime_attach_token,
                )
        assert reclaimed == []

    reader = AgentPostgresDB(pg_dsn, min_connections=1, max_connections=1)
    await reader.connect()
    try:
        history = await reader.get_thread_messages_history(ids["thread"], limit=None)
    finally:
        await reader.close()
    matching = [
        row for row in history if row["content"] == f"race winner {first_transition}"
    ]
    assert bool(matching) is (first_transition == "admitted")


@pytest.mark.asyncio
async def test_wake_outbox_refuses_transcript_only_then_accepts_admission(db):
    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="timer",
        dedup_key="execution-boundary",
        payload={"minutes": 30},
        project_id=ids["project"],
    )
    claimed = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assigned = await db.assign_session_wake_delivery_groups([int(claimed[0]["id"])])
    delivery_id = assigned[0]["delivery_id"]
    runtime_generation, runtime_attach_token = await _runtime_identity(
        db, ids["thread"]
    )
    async with db.acquire() as conn:
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="timer wake",
                source="officer_wake",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
            )

    with pytest.raises(asyncpg.CheckViolationError):
        await db.finish_session_wake_events([int(assigned[0]["id"])])

    async with db.acquire() as conn:
        async with conn.transaction():
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(delivery["claim_generation"]),
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=runtime_attach_token,
                claim_generation=int(delivery["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    await db.finish_session_wake_events([int(assigned[0]["id"])])
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM session_wake_events WHERE id=$1",
                int(assigned[0]["id"]),
            )
            == "sent"
        )


@pytest.mark.asyncio
async def test_job_wake_outbox_requires_durable_provider_admission(db):
    ids = await _seed(db)
    thread_id, agent_id, job_id = uuid4(), uuid4(), uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads "
            "(id,user_id,project_id,status,execution_lane,config_name,metadata) "
            "VALUES ($1,$2,$3,'active','pinned','base','{}'::jsonb)",
            thread_id,
            UUID(ids["user"]),
            UUID(ids["project"]),
        )
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode) "
            "VALUES ($1,'base',$2,'127.0.0.3','plain-pod','session',"
            "'persistent')",
            agent_id,
            f"persistent-{str(thread_id)[:12]}",
        )
        attach_token = uuid4()
        async with conn.transaction():
            await conn.execute(
                "UPDATE threads SET agent_id=$2,control_admission_agent_id=$2,"
                "runtime_attach_token=$3 WHERE id=$1",
                thread_id,
                agent_id,
                attach_token,
            )
            await conn.execute(
                "UPDATE agents SET thread_id=$2 WHERE id=$1", agent_id, thread_id
            )
        await conn.execute(
            "INSERT INTO jobs "
            "(id,description,status,user_id,project_id,created_by_thread_id,"
            "wake_on_complete,wake_state) "
            "VALUES ($1,'execution boundary','completed',$2,$3,$4,true,'pending')",
            job_id,
            UUID(ids["user"]),
            UUID(ids["project"]),
            thread_id,
        )

    claimed = [row for row in await db.claim_pending_job_wakes() if row["id"] == job_id]
    assert len(claimed) == 1
    with pytest.raises(asyncpg.CheckViolationError):
        await db.finish_job_wake(str(job_id), "completed")

    async with db.acquire() as conn:
        delivery_id = await conn.fetchval(
            "SELECT wake_delivery_id FROM jobs WHERE id=$1", job_id
        )
        runtime_generation = await conn.fetchval(
            "SELECT runtime_generation FROM threads WHERE id=$1", thread_id
        )
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=thread_id,
                delivery_id=delivery_id,
                role="event",
                content="execute this wake once",
                source="officer_wake",
                turn_number=1,
                agent_id=agent_id,
                pod_uid="plain-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=attach_token,
            )
            runtime_generation = delivery["owner_runtime_generation"]
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=agent_id,
                pod_uid="plain-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=attach_token,
                claim_generation=int(delivery["claim_generation"]),
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=agent_id,
                pod_uid="plain-pod",
                runtime_generation=runtime_generation,
                runtime_attach_token=attach_token,
                claim_generation=int(delivery["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    assert await db.finish_job_wake(str(job_id), "completed") is True


@pytest.mark.asyncio
async def test_pre_0174_claimers_fail_before_session_or_job_network_delivery(db):
    """The mixed-version fence is tied to each claim attempt, not old state."""

    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="timer",
        dedup_key="rolling-fence",
        payload={"minutes": 30},
        project_id=ids["project"],
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE session_wake_events SET state='sending', "
                "claimed_at=now(), attempts=attempts+1 "
                "WHERE thread_id=$1 AND dedup_key='rolling-fence'",
                UUID(ids["thread"]),
            )
        event_state = await conn.fetchrow(
            "SELECT state, attempts FROM session_wake_events "
            "WHERE thread_id=$1 AND dedup_key='rolling-fence'",
            UUID(ids["thread"]),
        )
    assert dict(event_state) == {"state": "pending", "attempts": 0}

    claimed_events = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assert len(claimed_events) == 1
    claimed_payload = claimed_events[0]["payload"]
    if isinstance(claimed_payload, str):
        claimed_payload = json.loads(claimed_payload)
    assert claimed_payload["_delivery_claim_attempt"] == 1
    UUID(claimed_payload["_delivery_id"])

    job_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs "
            "(id,description,status,user_id,project_id,created_by_thread_id,"
            "wake_on_complete,wake_state) "
            "VALUES ($1,'rolling claim','completed',$2,$3,$4,true,'pending')",
            job_id,
            UUID(ids["user"]),
            UUID(ids["project"]),
            UUID(ids["thread"]),
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE jobs SET wake_state='sending', wake_claimed_at=now(), "
                "wake_attempts=wake_attempts+1 WHERE id=$1",
                job_id,
            )
        job_state = await conn.fetchrow(
            "SELECT wake_state, wake_attempts FROM jobs WHERE id=$1", job_id
        )
    assert dict(job_state) == {"wake_state": "pending", "wake_attempts": 0}

    claimed_jobs = [
        row for row in await db.claim_pending_job_wakes() if row["id"] == job_id
    ]
    assert len(claimed_jobs) == 1
    async with db.acquire() as conn:
        job_claim = await conn.fetchrow(
            "SELECT wake_delivery_id, wake_delivery_claim_attempt, wake_attempts "
            "FROM jobs WHERE id=$1",
            job_id,
        )
    assert job_claim["wake_delivery_id"] is not None
    assert job_claim["wake_delivery_claim_attempt"] == job_claim["wake_attempts"] == 1


@pytest.mark.asyncio
async def test_replacement_binding_steals_once_and_old_agent_cannot_mutate(db):
    ids = await _seed(db)
    delivery_id = uuid4()
    old_runtime, old_attach_token = await _runtime_identity(db, ids["thread"])
    async with db.acquire() as conn:
        async with conn.transaction():
            await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="human",
                content="retain direct input",
                source="direct_human",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=old_runtime,
                runtime_attach_token=old_attach_token,
            )

    successor, _actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid="new-pod"
    )
    successor_runtime, successor_attach = await _runtime_identity(db, ids["thread"])
    async with db.acquire() as conn:
        async with conn.transaction():
            rows = await claim_pending_input_deliveries(
                conn,
                thread_id=ids["thread"],
                agent_id=successor,
                pod_uid="new-pod",
                runtime_generation=successor_runtime,
                runtime_attach_token=successor_attach,
            )
    assert len(rows) == 1
    assert int(rows[0]["claim_generation"]) == 2

    async with db.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(InputDeliveryAuthorityLost):
                await lock_runtime_authority(
                    conn,
                    thread_id=ids["thread"],
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=old_runtime,
                    runtime_attach_token=old_attach_token,
                )


@pytest.mark.asyncio
async def test_generic_session_pod_is_not_misclassified_as_missing_legacy_pod(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE agents SET hostname='srw-agent-s-generic' WHERE id=$1",
                UUID(ids["agent"]),
            )
            await conn.execute(
                "UPDATE threads SET metadata=jsonb_set(metadata,"
                "'{agent_pod,pod_name}',to_jsonb('srw-agent-s-generic'::text),true) "
                "WHERE id=$1",
                UUID(ids["thread"]),
            )
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)

    result = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="operator_requested",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )

    assert result.state == "blocked"
    assert result.failure_class == "unsupported_pod_authority"
    assert provisioner.create_calls == 0
    assert str((await db.get_thread(ids["thread"]))["agent_id"]) == ids["agent"]


@pytest.mark.asyncio
async def test_preexisting_maintenance_hold_is_never_claimed_or_cleared(db):
    original = {
        "kind": "maintenance",
        "since": "2026-08-20T00:00:00+00:00",
        "note": "operator maintenance",
    }
    ids = await _seed(db, preexisting_hold=original)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, metadata = await _recycle_state(db, ids["thread"])
    assert state["hold_owned"] is False
    assert state["preexisting_hold"] is True
    assert metadata["config_override"]["officer"]["hold"] == original


@pytest.mark.asyncio
async def test_conference_hold_blocks_recycle_without_mutating_its_authority(db):
    conference_thread = str(uuid4())
    original = {
        "kind": "conference",
        "thread_id": conference_thread,
        "since": "2026-08-20T00:00:00+00:00",
    }
    ids = await _seed(db, preexisting_hold=original)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)

    result = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )

    assert result.state == "blocked"
    assert result.failure_class == "conference_hold"
    row = await db.get_thread(ids["thread"])
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["config_override"]["officer"]["hold"] == original
    assert "recycle" not in metadata["agent_pod"]
    assert provisioner.create_calls == 0


@pytest.mark.asyncio
async def test_retryable_failure_keeps_hold_and_pages_once_before_convergence(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.fail_creates = True
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        return True

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    failed = await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"failed_retryable"},
    )
    # Failure notification is a separately claimed durable side effect and is
    # dispatched on the following reconciliation tick.
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert failed.phase == "failed_retryable"
    assert pages == [(ids["project"], ids["thread"], "injected_create_failure")]
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"]["kind"] == "maintenance"

    # Immediate replicas respect backoff and do not page or create again.
    await asyncio.gather(
        *(
            recycler.request_and_reconcile(
                thread_id=ids["thread"],
                reason="missing_pod",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(3)
        )
    )
    assert provisioner.create_calls == 1
    assert len(pages) == 1

    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,next_retry_at}',
                   to_jsonb((now() - interval '1 minute')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )
    provisioner.fail_creates = False
    retried = await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"awaiting_replacement"},
    )
    assert retried.phase == "awaiting_replacement"
    assert provisioner.create_calls == 2
    assert len(pages) == 1


@pytest.mark.asyncio
async def test_unsettled_old_runtime_times_out_without_forced_deletion(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        return True

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,drain_wait_started_at}',
                   to_jsonb((now() - interval '6 minutes')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )
    failed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert failed.phase == "failed_retryable"
    assert failed.failure_class == "drain_boundary_timeout"
    assert provisioner.deleted_uids == []
    assert pages == [(ids["project"], ids["thread"], "drain_boundary_timeout")]
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"]["kind"] == "maintenance"


@pytest.mark.asyncio
async def test_reciprocal_uid_mismatch_holds_and_pages_without_mutation(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(
        ids["thread"], uid="foreign-pod", build="new-build"
    )
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        return True

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    results = await asyncio.gather(
        *(
            recycler.request_and_reconcile(
                thread_id=ids["thread"],
                reason="authority_mismatch",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(3)
        )
    )
    assert {result.phase for result in results} == {"blocked"}
    assert pages == [(ids["project"], ids["thread"], "reciprocal_binding_mismatch")]
    row = await db.get_thread(ids["thread"])
    assert str(row["agent_id"]) == ids["agent"]
    state, metadata = await _recycle_state(db, ids["thread"])
    assert state["last_failure"]["class"] == "reciprocal_binding_mismatch"
    assert metadata["config_override"]["officer"]["hold"]["kind"] == "maintenance"
    assert provisioner.create_calls == 0
    assert provisioner.deleted_uids == []


@pytest.mark.asyncio
async def test_decommission_or_new_incarnation_cannot_be_revived(db):
    ids = await _seed(db, bind_agent=False, publish_agent_pod=False)
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='ended' WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "UPDATE project_officers SET thread_id=NULL WHERE project_id=$1",
            UUID(ids["project"]),
        )
    ended = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert ended.state == "cancelled"
    assert provisioner.create_calls == 0

    successor_thread = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads (id,project_id,status,execution_lane,metadata) "
            "VALUES ($1,$2,'active','pinned',"
            '\'{"config_override":{"officer":{"enabled":true}}}\'::jsonb)',
            successor_thread,
            UUID(ids["project"]),
        )
        await conn.execute(
            "UPDATE project_officers SET thread_id=$2 WHERE project_id=$1",
            UUID(ids["project"]),
            successor_thread,
        )
    stale = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert stale.state == "cancelled"
    assert provisioner.create_calls == 0


@pytest.mark.asyncio
async def test_headerless_and_asserted_parked_boundary_use_locked_generation(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=PersistentPodObservation.from_status(
            ids["thread"], provisioner.current
        ),
        expected_project_id=ids["project"],
    )

    spoofed = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=str(uuid4())
    )
    assert spoofed.active_generation is True
    assert spoofed.acknowledged is False
    assert (await db.get_thread(ids["thread"]))["status"] == "active"

    headerless = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert headerless.acknowledged is True
    # Recycle parking is a physical-agent handoff, not a lifecycle suspend.
    assert (await db.get_thread(ids["thread"]))["status"] == "active"


@pytest.mark.asyncio
async def test_headerless_ordinary_persistent_generation_needs_no_workspace(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata = jsonb_set("
            "metadata, '{config_override,officer,enabled}', 'false'::jsonb) "
            "WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "DELETE FROM project_officers WHERE project_id=$1",
            UUID(ids["project"]),
        )
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="operator_test",
        expected_build_sha="new-build",
        observation=PersistentPodObservation.from_status(
            ids["thread"], provisioner.current
        ),
        expected_project_id=ids["project"],
    )

    acknowledgement = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert acknowledgement.active_generation is True
    assert acknowledgement.acknowledged is True


@pytest.mark.asyncio
async def test_notification_claim_crash_reclaims_once_and_success_is_terminal(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.fail_creates = True
    unpaged = PersistentThreadRecycler(db=db, provisioner=provisioner)
    failed = await _reconcile_until_phase(
        unpaged,
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"failed_retryable"},
    )
    assert failed.phase == "failed_retryable"
    state, _ = await _recycle_state(db, ids["thread"])
    generation = state["generation"]

    # Simulate process death after durable claim and before provider settlement.
    assert await unpaged._claim_notification(ids["thread"], generation, "dead-owner")
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,notification,claim_expires_at}',
                   to_jsonb((now() - interval '1 minute')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        entered.set()
        await release.wait()
        return True

    first = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    second = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    first_task = asyncio.create_task(
        first.request_and_reconcile(
            thread_id=ids["thread"],
            reason="missing_pod",
            expected_build_sha="new-build",
            expected_project_id=ids["project"],
        )
    )
    await entered.wait()
    await second.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert len(pages) == 1
    release.set()
    await first_task

    await asyncio.gather(
        *(
            second.request_and_reconcile(
                thread_id=ids["thread"],
                reason="missing_pod",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(3)
        )
    )
    assert len(pages) == 1
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["notification"]["state"] == "delivered"


@pytest.mark.asyncio
async def test_failed_notification_retries_after_bounded_backoff(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.fail_creates = True
    deliveries = [False, True]
    attempts = 0

    async def notify(_project_id: str, _thread_id: str, _failure_class: str):
        nonlocal attempts
        attempts += 1
        return deliveries.pop(0)

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"failed_retryable"},
    )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["notification"]["state"] == "failed"
    assert state["notification"]["next_retry_at"]
    assert attempts == 1

    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert attempts == 1
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,notification,next_retry_at}',
                   to_jsonb((now() - interval '1 minute')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert attempts == 2
    assert state["notification"]["state"] == "delivered"


@pytest.mark.asyncio
async def test_officer_replacement_requires_exact_current_grant(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await _reconcile_until_phase(
        recycler,
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
        phases={"awaiting_replacement"},
    )
    state, _ = await _recycle_state(db, ids["thread"])
    uid = provisioner.current["pod_uid"]
    agent_id, _ = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid=uid
    )
    provisioner.current = _pod_status(
        ids["thread"],
        uid=uid,
        build="new-build",
        generation=state["generation"],
        ready=True,
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET caller_kind='worker' "
            "WHERE agent_id=$1 AND revoked_at IS NULL",
            UUID(agent_id),
        )
    refused = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert refused.phase == "awaiting_replacement"
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"] is not None

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET caller_kind='officer' "
            "WHERE agent_id=$1 AND revoked_at IS NULL",
            UUID(agent_id),
        )
    accepted = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert accepted.phase == "complete"


@pytest.mark.asyncio
async def test_two_desired_image_changes_chain_without_releasing_hold(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=PersistentPodObservation.from_status(
            ids["thread"], provisioner.current
        ),
        expected_project_id=ids["project"],
    )

    async def drain_and_provision() -> tuple[dict, str]:
        acknowledgement = await recycler.acknowledge_parked_boundary(
            thread_id=ids["thread"], agent_id=None
        )
        assert acknowledgement.acknowledged
        provisioner.current = None
        await _reconcile_until_phase(
            recycler,
            thread_id=ids["thread"],
            reason="image_drift",
            expected_build_sha=provisioner.expected_build_sha,
            expected_project_id=ids["project"],
            phases={"awaiting_replacement"},
        )
        state, metadata = await _recycle_state(db, ids["thread"])
        assert metadata["config_override"]["officer"]["hold"] is not None
        assert state["phase"] == "awaiting_replacement"
        return state, provisioner.current["pod_uid"]

    first, first_uid = await drain_and_provision()
    provisioner.expected_build_sha = "build-two"
    provisioner.image_ref = "example.test/agent:sha-build-two"
    await _bind_replacement_agent(db, thread_id=ids["thread"], pod_uid=first_uid)
    provisioner.current = _pod_status(
        ids["thread"],
        uid=first_uid,
        build="new-build",
        generation=first["generation"],
        ready=True,
    )
    chained_two = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="build-two",
        expected_project_id=ids["project"],
    )
    assert chained_two.phase == "awaiting_old_pod_exit"
    state_two, metadata = await _recycle_state(db, ids["thread"])
    assert state_two["expected_build_sha"] == "build-two"
    assert metadata["config_override"]["officer"]["hold"] is not None

    provisioner.expected_build_sha = "build-three"
    provisioner.image_ref = "example.test/agent:sha-build-three"
    second, second_uid = await drain_and_provision()
    assert second["expected_build_sha"] == "build-two"
    await _bind_replacement_agent(db, thread_id=ids["thread"], pod_uid=second_uid)
    provisioner.current = _pod_status(
        ids["thread"],
        uid=second_uid,
        build="build-two",
        generation=second["generation"],
        ready=True,
    )
    chained_three = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="build-three",
        expected_project_id=ids["project"],
    )
    assert chained_three.phase == "awaiting_old_pod_exit"
    state_three, metadata = await _recycle_state(db, ids["thread"])
    assert state_three["expected_build_sha"] == "build-three"
    assert metadata["config_override"]["officer"]["hold"] is not None

    third, third_uid = await drain_and_provision()
    await _bind_replacement_agent(db, thread_id=ids["thread"], pod_uid=third_uid)
    provisioner.current = _pod_status(
        ids["thread"],
        uid=third_uid,
        build="build-three",
        generation=third["generation"],
        ready=True,
    )
    complete = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="build-three",
        expected_project_id=ids["project"],
    )
    assert complete.phase == "complete"
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"] is None
    assert provisioner.created_targets == [
        "example.test/agent:sha-new-build",
        "example.test/agent:sha-build-two",
        "example.test/agent:sha-build-three",
    ]
