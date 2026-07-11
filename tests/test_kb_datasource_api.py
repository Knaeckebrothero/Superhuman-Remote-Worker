"""Control-plane contract for OKF Knowledge Base datasources (Slice 4)."""

import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from uuid import UUID
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from main import (
    DatasourceCreate,
    DatasourceUpdate,
    ThreadCreateRequest,
    _authorize_thread_datasource_ids,
    _authorize_thread_project_ids,
    _build_datasources_payload,
    _normalize_kb_config,
    _revalidate_thread_datasource_ids,
    _revalidate_thread_project_ids,
    _thread_has_knowledge_scope,
    _validate_kb_repository_url,
    create_datasource,
    create_thread,
    delete_datasource,
    get_datasource_index_status,
    reindex_datasource_knowledge,
    resume_thread,
    update_datasource,
)
from orchestrator.services.kb_datasources import (
    index_status_payload,
    reindex_kb_datasource,
    test_kb_datasource as probe_kb_datasource,
)
from src.services.knowledge_store import KbWatermark


@pytest.fixture(autouse=True)
def _trust_test_git_hosts(monkeypatch):
    monkeypatch.setenv(
        "KB_GIT_ALLOWED_HOSTS",
        "example.test,git.example.test,host",
    )


class TestNormalizeKbConfig:
    def test_defaults_to_repository_root(self):
        assert _normalize_kb_config(None) == {"root_path": ""}
        assert _normalize_kb_config({}) == {"root_path": ""}

    def test_normalizes_relative_posix_path(self):
        assert _normalize_kb_config({"root_path": r"./docs\\knowledge//notes"}) == {
            "root_path": "docs/knowledge/notes"
        }

    @pytest.mark.parametrize(
        "root",
        ["/absolute", "../escape", "docs/../escape", "https://host/vault", "a\x00b"],
    )
    def test_rejects_unsafe_root(self, root):
        with pytest.raises(HTTPException) as exc:
            _normalize_kb_config({"root_path": root})
        assert exc.value.status_code == 400

    def test_rejects_non_string_and_unknown_keys(self):
        with pytest.raises(HTTPException):
            _normalize_kb_config({"root_path": 42})
        with pytest.raises(HTTPException) as exc:
            _normalize_kb_config({"path_prefix": "knowledge"})
        assert "path_prefix" in str(exc.value.detail)


class TestKbRepositoryUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/acme/wiki.git",
            "http://git.internal/acme/wiki.git",
            "ssh://git@github.com/acme/wiki.git",
            "git@github.com:acme/wiki.git",
        ],
    )
    def test_accepts_normal_git_urls(self, url):
        assert _validate_kb_repository_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "",
            None,
            "https://token@github.com/acme/wiki.git",
            "https://u:p@host/repo",
            "https://host/repo.git?token=secret",
            "https://host/repo.git#branch",
            "user:password@host:repo.git",
            "git://host/repo.git",
            "file:///tmp/repo.git",
            "/tmp/repo.git",
            "ext::sh -c id",
            "--upload-pack=evil",
        ],
    )
    def test_rejects_missing_or_embedded_credentials(self, url):
        with pytest.raises(HTTPException) as exc:
            _validate_kb_repository_url(url)
        assert exc.value.status_code == 400


def test_datasource_models_carry_non_secret_config():
    create = DatasourceCreate(
        name="Engineering Knowledge",
        type="kb",
        connection_url="https://github.com/acme/wiki.git",
        config={"root_path": "knowledge"},
    )
    assert create.config == {"root_path": "knowledge"}
    assert DatasourceUpdate(config={"root_path": "docs"}).config == {
        "root_path": "docs"
    }


def test_kb_dispatch_payload_is_source_qualified_and_credential_free():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    sentinel_token = "TOKEN_MUST_NOT_REACH_AGENT"
    sentinel_key = "KEY_MUST_NOT_REACH_AGENT"
    result = _build_datasources_payload(
        [
            {
                "id": datasource_id,
                "type": "kb",
                "name": "Engineering Knowledge",
                "description": "Shared architecture notes",
                "connection_url": "https://github.com/acme/wiki.git",
                "credentials": {
                    "auth_method": "token",
                    "token": sentinel_token,
                    "ssh_key": sentinel_key,
                },
                "default_branch": "main",
                "config": {"root_path": "docs/knowledge"},
                "project_read_only": False,
            }
        ]
    )

    assert result == [
        {
            "type": "kb",
            "name": "Engineering Knowledge",
            "description": "Shared architecture notes",
            "connection_url": None,
            "credentials": {},
            "project_read_only": True,
            "datasource_id": str(datasource_id),
            "config": {"root_path": "docs/knowledge"},
            "default_branch": "main",
        }
    ]
    assert sentinel_token not in repr(result)
    assert sentinel_key not in repr(result)


def test_index_status_payload_is_credential_free_and_serializable():
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    watermark = KbWatermark(
        kb_id=UUID("11111111-2222-3333-4444-555555555555"),
        repo_name="datasource:11111111-2222-3333-4444-555555555555",
        branch="main",
        indexed_commit="a" * 40,
        source_head="b" * 40,
        pipeline_version="embed:parser:root",
        status="partial",
        last_attempt_at=now,
        last_success_at=now,
        last_error="one note failed",
    )

    result = index_status_payload(str(watermark.kb_id), watermark)

    assert result["status"] == "partial"
    assert result["indexed_commit"] == "a" * 40
    assert result["source_head"] == "b" * 40
    assert result["last_success_at"] == now.isoformat()
    assert "repo_name" not in result
    assert "credentials" not in result


@pytest.mark.asyncio
async def test_status_endpoint_uses_normal_visibility_gate():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    request = object()
    gate = AsyncMock(return_value=({}, {"id": datasource_id, "type": "kb"}))
    get_watermark = AsyncMock(return_value=None)

    with (
        patch("main.require_datasource_access", gate),
        patch(
            "src.services.knowledge_store.KnowledgeStore.get_watermark",
            get_watermark,
        ),
    ):
        result = await get_datasource_index_status(request, datasource_id)

    assert result["status"] == "pending"
    gate.assert_awaited_once()
    get_watermark.assert_awaited_once_with(UUID(datasource_id))


@pytest.mark.asyncio
async def test_manual_reindex_is_owner_gated_and_uses_stored_datasource():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    datasource = {"id": datasource_id, "type": "kb"}
    gate = AsyncMock(return_value=({}, datasource))
    run = AsyncMock(return_value={"status": "completed", "upserted": 2})

    with (
        patch("main.require_datasource_owner", gate),
        patch("main._reindex_kb_datasource_now", run),
    ):
        result = await reindex_datasource_knowledge(object(), datasource_id, full=True)

    assert result["status"] == "completed"
    run.assert_awaited_once_with(datasource, force_full=True)


@pytest.mark.asyncio
async def test_create_marks_pending_and_schedules_initial_full_index():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    db = MagicMock()
    db.create_datasource = AsyncMock(
        return_value={
            "id": datasource_id,
            "name": "Engineering Knowledge",
            "type": "kb",
            "credentials": {"token": "secret"},
            "config": {"root_path": "vault"},
        }
    )
    pending = AsyncMock()
    schedule = MagicMock()
    body = DatasourceCreate(
        name="Engineering Knowledge",
        type="kb",
        connection_url="https://example.test/knowledge.git",
        credentials={"token": "secret"},
        config={"root_path": "vault"},
    )

    with (
        patch(
            "main.require_approved_user", AsyncMock(return_value={"id": UUID(int=1)})
        ),
        patch("main.postgres_db", db),
        patch("main._mark_kb_datasource_pending", pending),
        patch("main._schedule_kb_datasource_reindex", schedule),
    ):
        result = await create_datasource(body, object())

    assert "credentials" not in result
    pending.assert_awaited_once_with(str(datasource_id))
    schedule.assert_called_once_with(str(datasource_id), force_full=True)


@pytest.mark.asyncio
async def test_kb_create_rejects_legacy_job_id_auto_attachment():
    victim_job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    db = MagicMock()
    db.create_datasource = AsyncMock()
    body = DatasourceCreate(
        name="Malicious Knowledge",
        type="kb",
        connection_url="https://example.test/knowledge.git",
        job_id=victim_job_id,
    )

    with (
        patch(
            "main.require_approved_user",
            AsyncMock(side_effect=AssertionError("auth ran past shape validation")),
        ),
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await create_datasource(body, object())

    assert exc.value.status_code == 400
    assert "explicit datasource selection" in str(exc.value.detail)
    assert victim_job_id not in str(exc.value.detail)
    db.create_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_kb_create_rejects_non_secret_config_surface():
    """V1 exposes only the typed KB root config; arbitrary datasource config
    must not become an unredacted second credentials bag."""
    db = MagicMock()
    db.create_datasource = AsyncMock()
    body = DatasourceCreate(
        name="Generic",
        type="generic",
        config={"password": "must-not-be-persisted"},
    )

    with (
        patch(
            "main.require_approved_user", AsyncMock(return_value={"id": UUID(int=1)})
        ),
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await create_datasource(body, object())

    assert exc.value.status_code == 400
    assert "only supported" in str(exc.value.detail)
    db.create_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rejects_token_over_plain_http_before_persistence():
    db = MagicMock()
    db.create_datasource = AsyncMock()
    body = DatasourceCreate(
        name="Unsafe Knowledge",
        type="kb",
        connection_url="http://git.example.test/knowledge.git",
        credentials={"auth_method": "token", "token": "secret"},
    )

    with (
        patch(
            "main.require_approved_user", AsyncMock(return_value={"id": UUID(int=1)})
        ),
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await create_datasource(body, object())

    assert exc.value.status_code == 400
    assert "HTTPS" in str(exc.value.detail)
    db.create_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rejects_untrusted_git_host_before_persistence():
    db = MagicMock()
    db.create_datasource = AsyncMock()
    body = DatasourceCreate(
        name="Untrusted Knowledge",
        type="kb",
        connection_url="https://arbitrary.example/knowledge.git",
    )

    with (
        patch(
            "main.require_approved_user", AsyncMock(return_value={"id": UUID(int=1)})
        ),
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await create_datasource(body, object())

    assert exc.value.status_code == 400
    assert "not trusted" in str(exc.value.detail)
    db.create_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_kb_update_rejects_non_secret_config_surface():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    existing = {"id": datasource_id, "type": "generic", "credentials": {}}
    db = MagicMock()
    db.update_datasource = AsyncMock()

    with (
        patch("main.require_datasource_owner", AsyncMock(return_value=({}, existing))),
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await update_datasource(
            object(), datasource_id, DatasourceUpdate(config={"token": "secret"})
        )

    assert exc.value.status_code == 400
    assert "only supported" in str(exc.value.detail)
    db.update_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_validates_preserved_token_against_changed_transport():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    existing = {
        "id": UUID(datasource_id),
        "type": "kb",
        "name": "Knowledge",
        "connection_url": "https://git.example.test/knowledge.git",
        "credentials": {"auth_method": "token", "token": "secret"},
        "config": {"root_path": ""},
    }
    db = MagicMock()
    db.update_datasource = AsyncMock()

    with (
        patch("main.require_datasource_owner", AsyncMock(return_value=({}, existing))),
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await update_datasource(
            object(),
            datasource_id,
            DatasourceUpdate(connection_url="http://git.example.test/knowledge.git"),
        )

    assert exc.value.status_code == 400
    assert "HTTPS" in str(exc.value.detail)
    db.update_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_uses_coordinated_kb_index_and_app_row_cleanup():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    db = MagicMock()
    db.list_datasource_projects = AsyncMock(return_value=[])
    db.delete_datasource = AsyncMock(return_value=True)
    cleanup = AsyncMock(return_value=True)
    gate = AsyncMock(return_value=({}, {"id": datasource_id, "type": "kb"}))

    with (
        patch("main.require_datasource_owner", gate),
        patch("main.postgres_db", db),
        patch("main._delete_kb_datasource_with_index", cleanup),
    ):
        result = await delete_datasource(object(), datasource_id)

    assert result == {"status": "deleted"}
    cleanup.assert_awaited_once_with(datasource_id)
    db.delete_datasource.assert_not_awaited()


@pytest.mark.asyncio
async def test_connectivity_probe_counts_only_indexable_markdown_notes():
    snapshot = MagicMock()
    snapshot.list_tree = AsyncMock(
        return_value=[
            {"path": "vault/index.md", "type": "blob", "sha": "a"},
            {"path": "vault/real.md", "type": "blob", "sha": "b"},
        ]
    )
    source = MagicMock()
    source.get_head = AsyncMock(return_value="c" * 40)

    @asynccontextmanager
    async def open_snapshot(_ref):
        yield snapshot

    source.snapshot = open_snapshot
    datasource = {
        "id": UUID("11111111-2222-3333-4444-555555555555"),
        "type": "kb",
        "connection_url": "https://example.test/knowledge.git",
        "config": {"root_path": "vault"},
    }

    with patch(
        "orchestrator.services.kb_datasources.kb_source_from_datasource",
        return_value=source,
    ):
        result = await probe_kb_datasource(datasource)

    assert result["status"] == "ok"
    assert result["note_count"] == 1


@pytest.mark.asyncio
async def test_external_reindex_concurrency_is_bounded_across_distinct_kbs(monkeypatch):
    import orchestrator.services.kb_datasources as service

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_reindex(**_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if not first_started.is_set():
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        active -= 1
        return {"status": "completed"}

    datasource = {
        "type": "kb",
        "connection_url": "https://github.com/acme/wiki.git",
        "config": {},
    }
    monkeypatch.setattr(service, "_external_reindex_semaphore", asyncio.Semaphore(1))
    with patch.object(service, "kb_source_from_datasource", return_value=MagicMock()):
        first = asyncio.create_task(
            reindex_kb_datasource(
                {**datasource, "id": UUID(int=1)},
                store=object(),
                embedding_service=object(),
                is_active=AsyncMock(return_value=True),
                reindex_fn=fake_reindex,
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            reindex_kb_datasource(
                {**datasource, "id": UUID(int=2)},
                store=object(),
                embedding_service=object(),
                is_active=AsyncMock(return_value=True),
                reindex_fn=fake_reindex,
            )
        )
        await asyncio.sleep(0)
        assert not second_started.is_set()
        release_first.set()
        await asyncio.gather(first, second)

    assert second_started.is_set()
    assert max_active == 1


@pytest.mark.asyncio
async def test_thread_attachment_rejects_an_inaccessible_private_kb():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    db = MagicMock()
    db.get_datasource = AsyncMock(
        return_value={"id": datasource_id, "type": "kb", "is_global": False}
    )

    with (
        patch("main.postgres_db", db),
        patch("main.user_can_access_datasource", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as exc,
    ):
        await _authorize_thread_datasource_ids(
            {"id": UUID(int=1)},
            [str(datasource_id)],
            workspace_backend="virtual",
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_thread_attachment_allows_kb_but_not_clone_repo_on_lite_tier():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    db = MagicMock()
    db.get_datasource = AsyncMock(
        return_value={"id": datasource_id, "type": "kb", "is_global": False}
    )
    allowed = AsyncMock(return_value=True)

    with (
        patch("main.postgres_db", db),
        patch("main.user_can_access_datasource", allowed),
    ):
        selected = await _authorize_thread_datasource_ids(
            {"id": UUID(int=1)},
            [str(datasource_id), str(datasource_id)],
            workspace_backend="virtual",
        )
        db.get_datasource.return_value = {
            "id": datasource_id,
            "type": "repository",
            "is_global": False,
        }
        with pytest.raises(HTTPException) as exc:
            await _authorize_thread_datasource_ids(
                {"id": UUID(int=1)},
                [str(datasource_id)],
                workspace_backend="virtual",
            )

    assert selected == [str(datasource_id)]
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_persisted_thread_datasource_is_denied_after_access_revocation():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    owner_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    db = MagicMock()
    db.get_user = AsyncMock(return_value={"id": owner_id, "is_admin": False})
    db.get_datasource = AsyncMock(
        return_value={
            "id": datasource_id,
            "type": "kb",
            "is_global": False,
        }
    )

    with (
        patch("main.postgres_db", db),
        patch("main.user_can_access_datasource", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as exc,
    ):
        await _revalidate_thread_datasource_ids(
            {"id": "thread-1", "user_id": owner_id},
            [str(datasource_id)],
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "One or more selected datasources are unavailable"
    assert str(datasource_id) not in exc.value.detail


@pytest.mark.asyncio
async def test_persisted_thread_revalidation_preserves_global_and_system_semantics():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    owner_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    db = MagicMock()
    db.get_user = AsyncMock(return_value={"id": owner_id, "is_admin": False})
    db.get_datasource = AsyncMock(
        return_value={"id": datasource_id, "type": "kb", "is_global": True}
    )
    access = AsyncMock(return_value=False)

    with (
        patch("main.postgres_db", db),
        patch("main.user_can_access_datasource", access),
    ):
        global_selection = await _revalidate_thread_datasource_ids(
            {"id": "thread-user", "user_id": owner_id},
            [str(datasource_id)],
        )
        system_selection = await _revalidate_thread_datasource_ids(
            {"id": "thread-system", "user_id": None},
            [str(datasource_id), str(datasource_id)],
        )

    assert global_selection == [str(datasource_id)]
    assert system_selection == [str(datasource_id)]
    access.assert_not_awaited()
    db.get_user.assert_awaited_once_with(str(owner_id))


@pytest.mark.asyncio
async def test_persisted_thread_project_scope_is_denied_after_membership_revocation():
    project_id = UUID("99999999-2222-3333-4444-555555555555")
    owner_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    db = MagicMock()
    db.get_user = AsyncMock(return_value={"id": owner_id, "is_admin": False})
    db.get_project = AsyncMock(return_value={"id": project_id})
    db.get_user_role_in_project = AsyncMock(return_value=None)

    with (
        patch("main.postgres_db", db),
        pytest.raises(HTTPException) as exc,
    ):
        await _revalidate_thread_project_ids(
            {"id": "thread-1", "user_id": owner_id},
            [str(project_id)],
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "One or more attached projects are unavailable"
    assert str(project_id) not in exc.value.detail


@pytest.mark.asyncio
async def test_thread_creation_rejects_unavailable_project_without_enumeration():
    project_id = UUID("99999999-2222-3333-4444-555555555555")
    owner_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    user = {"id": owner_id, "is_admin": False, "settings": {}}
    db = MagicMock()
    db.get_project = AsyncMock(return_value={"id": project_id})
    db.get_user_role_in_project = AsyncMock(return_value=None)
    db.create_thread = AsyncMock()

    with (
        patch("main.require_approved_user", AsyncMock(return_value=user)),
        patch("main.postgres_db", db),
        patch("main._enforce_readiness_gate", AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as exc,
    ):
        await create_thread(
            ThreadCreateRequest(project_ids=[str(project_id)]), object()
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "One or more attached projects are unavailable"
    assert str(project_id) not in exc.value.detail
    db.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_project_authorization_preserves_admin_access():
    project_id = UUID("99999999-2222-3333-4444-555555555555")
    db = MagicMock()
    db.get_project = AsyncMock(return_value={"id": project_id})
    db.get_user_role_in_project = AsyncMock()

    with patch("main.postgres_db", db):
        selected = await _authorize_thread_project_ids(
            {"id": UUID(int=1), "is_admin": True},
            [str(project_id), str(project_id)],
        )

    assert selected == [str(project_id)]
    db.get_user_role_in_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_revalidates_datasources_before_mutating_thread_status():
    datasource_id = UUID("11111111-2222-3333-4444-555555555555")
    thread = {
        "id": "thread-1",
        "user_id": UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        "status": "ended",
        "metadata": {"datasource_ids": [str(datasource_id)]},
    }
    db = MagicMock()
    db.resume_thread = AsyncMock()
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.get_thread = AsyncMock(return_value=thread)
    denied = HTTPException(
        status_code=403,
        detail="One or more selected datasources are unavailable",
    )

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=({}, thread))),
        patch("main.postgres_db", db),
        patch(
            "main._revalidate_thread_datasource_ids",
            AsyncMock(side_effect=denied),
        ),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        pytest.raises(HTTPException) as exc,
    ):
        await resume_thread("thread-1", object())

    assert exc.value.status_code == 403
    db.resume_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_blocks_revoked_native_project_scope_before_status_mutation():
    project_id = "99999999-2222-3333-4444-555555555555"
    thread = {
        "id": "thread-1",
        "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "status": "ended",
        "metadata": {},
    }
    db = MagicMock()
    db.resume_thread = AsyncMock()
    denied = HTTPException(
        status_code=403,
        detail="One or more attached projects are unavailable",
    )

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=({}, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[project_id])),
        patch(
            "main._revalidate_thread_project_ids",
            AsyncMock(side_effect=denied),
        ),
        patch(
            "main._revalidate_thread_datasource_ids", AsyncMock(return_value=[])
        ) as datasource_check,
        pytest.raises(HTTPException) as exc,
    ):
        await resume_thread("thread-1", object())

    assert exc.value.status_code == 403
    db.resume_thread.assert_not_awaited()
    datasource_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_kb_credential_gate_is_narrow():
    db = MagicMock()
    db.get_datasource = AsyncMock(
        side_effect=[
            {"id": UUID(int=1), "type": "postgres"},
            {"id": UUID(int=2), "type": "kb"},
        ]
    )

    with patch("main.postgres_db", db):
        assert not await _thread_has_knowledge_scope(
            project_ids=[], datasource_ids=[str(UUID(int=1))]
        )
        assert await _thread_has_knowledge_scope(
            project_ids=[], datasource_ids=[str(UUID(int=2))]
        )
        assert await _thread_has_knowledge_scope(
            project_ids=[str(UUID(int=3))], datasource_ids=[]
        )

    # Native project scope short-circuits without an unnecessary datasource read.
    assert db.get_datasource.await_count == 2


@pytest.mark.asyncio
async def test_metadata_only_kb_edit_does_not_schedule_full_rebuild():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    existing = {
        "id": datasource_id,
        "type": "kb",
        "name": "Old Name",
        "connection_url": "https://example.test/knowledge.git",
        "credentials": {"token": "stored"},
        "default_branch": "main",
        "config": {"root_path": "vault"},
    }
    db = MagicMock()
    db.update_datasource = AsyncMock(return_value=True)
    db.list_datasource_projects = AsyncMock(return_value=[])
    pending = AsyncMock()
    schedule = MagicMock()

    with (
        patch("main.require_datasource_owner", AsyncMock(return_value=({}, existing))),
        patch("main.postgres_db", db),
        patch("main._mark_kb_datasource_pending", pending),
        patch("main._schedule_kb_datasource_reindex", schedule),
    ):
        result = await update_datasource(
            object(),
            datasource_id,
            DatasourceUpdate(
                name="New Name",
                connection_url=existing["connection_url"],
                default_branch="main",
                config={"root_path": "vault"},
            ),
        )

    assert result == {"status": "updated"}
    pending.assert_not_awaited()
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_root_change_schedules_full_rebuild():
    datasource_id = "11111111-2222-3333-4444-555555555555"
    existing = {
        "id": datasource_id,
        "type": "kb",
        "connection_url": "https://example.test/knowledge.git",
        "credentials": {},
        "default_branch": "main",
        "config": {"root_path": "vault"},
    }
    db = MagicMock()
    db.update_datasource = AsyncMock(return_value=True)
    db.list_datasource_projects = AsyncMock(return_value=[])
    pending = AsyncMock()
    schedule = MagicMock()

    with (
        patch("main.require_datasource_owner", AsyncMock(return_value=({}, existing))),
        patch("main.postgres_db", db),
        patch("main._mark_kb_datasource_pending", pending),
        patch("main._schedule_kb_datasource_reindex", schedule),
    ):
        await update_datasource(
            object(),
            datasource_id,
            DatasourceUpdate(config={"root_path": "handbook"}),
        )

    pending.assert_awaited_once_with(datasource_id)
    schedule.assert_called_once_with(datasource_id, force_full=True)
