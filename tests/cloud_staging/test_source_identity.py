from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from orchestrator.services.cloud.handles import ProjectFolderHandle
from orchestrator.services.cloud_staging.source_identity import (
    PROTECTED_SOURCE_BINDING_VERSION,
    ProtectedMountSourceIdentity,
)


PROJECT_ID = "11111111-1111-4111-8111-aaaaaaaaaaaa"
BACKEND_INSTANCE_ID = "99999999-9999-4999-8999-aaaaaaaaaaaa"


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "thread_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "mount_kind": "project",
        "target_path": "cloud",
        "source_kind": "project_folder",
        "source_ref": PROJECT_ID,
        "backend_id": "nextcloud",
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "cloud_handle": ProjectFolderHandle(
            backend="nextcloud",
            native_id="42",
            vendor_meta={
                "mountpoint": "Project Alpha",
                "browser_url": "https://ignored",
            },
        ).to_db(),
        "webdav_url": "https://ignored.example/remote.php/dav/files/reader/Project/",
        "session_runtime_generation": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "reader_id": "secret-adjacent-reader-coordinate",
    }
    row.update(overrides)
    return row


def test_source_identity_is_stable_across_row_runtime_and_reader_replacement() -> None:
    first = ProtectedMountSourceIdentity.from_mount_row(_row())
    recreated = ProtectedMountSourceIdentity.from_mount_row(
        _row(
            id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            thread_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            session_runtime_generation="ffffffff-ffff-4fff-8fff-ffffffffffff",
            reader_id="new-reader",
            webdav_url="https://another.invalid/credential-route",
        )
    )

    assert first is not None and recreated is not None
    assert first == recreated
    assert first.sha256 == recreated.sha256
    assert first.binding == {
        "version": PROTECTED_SOURCE_BINDING_VERSION,
        "backend": "nextcloud",
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "mount_kind": "project",
        "source_kind": "project_folder",
        "source_ref": PROJECT_ID,
        "target_path": "cloud",
        "handle": {"native_id": "42", "mountpoint": "Project Alpha"},
    }
    serialized = first.canonical_json
    for excluded in (
        str(_row()["id"]),
        str(_row()["thread_id"]),
        str(_row()["session_runtime_generation"]),
        str(_row()["reader_id"]),
        "ignored.example",
        "browser_url",
    ):
        assert excluded not in serialized


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_ref", "22222222-2222-4222-8222-222222222222"),
        ("backend_instance_id", "88888888-8888-4888-8888-888888888888"),
        ("target_path", "projects/alpha"),
        (
            "cloud_handle",
            ProjectFolderHandle(
                backend="nextcloud",
                native_id="99",
                vendor_meta={"mountpoint": "Project Alpha"},
            ).to_db(),
        ),
        (
            "cloud_handle",
            ProjectFolderHandle(
                backend="nextcloud",
                native_id="42",
                vendor_meta={"mountpoint": "Project Beta"},
            ).to_db(),
        ),
    ],
)
def test_source_identity_changes_for_every_logical_destination_coordinate(
    field: str, replacement: str
) -> None:
    original = ProtectedMountSourceIdentity.from_mount_row(_row())
    changed = ProtectedMountSourceIdentity.from_mount_row(_row(**{field: replacement}))

    assert original is not None and changed is not None
    assert changed.sha256 != original.sha256


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend_id": "opencloud"},
        {"backend_instance_id": None},
        {"backend_instance_id": BACKEND_INSTANCE_ID.upper()},
        {"backend_instance_id": BACKEND_INSTANCE_ID.replace("-", "")},
        {"backend_instance_id": "00000000-0000-0000-0000-000000000000"},
        {"mount_kind": "project_default"},
        {"mount_kind": "repo"},
        {"source_kind": "user_home"},
        {"source_ref": None},
        {"source_ref": "not-a-uuid"},
        {"source_ref": PROJECT_ID.upper()},
        {"source_ref": "00000000-0000-0000-0000-000000000000"},
        {"target_path": ""},
        {"target_path": "/cloud"},
        {"target_path": "cloud//nested"},
        {"target_path": "cloud/../other"},
        {"target_path": "cloud\\other"},
        {"cloud_handle": ""},
        {"cloud_handle": "{malformed"},
        {"cloud_handle": "42"},
        {
            "cloud_handle": ProjectFolderHandle(
                backend="opencloud",
                native_id="42",
                vendor_meta={"mountpoint": "Project Alpha"},
            ).to_db()
        },
        {
            "cloud_handle": ProjectFolderHandle(
                backend="nextcloud",
                native_id="",
                vendor_meta={"mountpoint": "Project Alpha"},
            ).to_db()
        },
        {
            "cloud_handle": ProjectFolderHandle(
                backend="nextcloud", native_id="42", vendor_meta={}
            ).to_db()
        },
        {
            "cloud_handle": ProjectFolderHandle(
                backend="nextcloud",
                native_id="42",
                vendor_meta={"mountpoint": "folder/child"},
            ).to_db()
        },
        {
            "cloud_handle": ProjectFolderHandle(
                backend="nextcloud",
                native_id="42",
                vendor_meta={"mountpoint": ".."},
            ).to_db()
        },
    ],
)
def test_source_identity_rejects_ineligible_or_malformed_mounts(
    overrides: dict[str, object],
) -> None:
    assert ProtectedMountSourceIdentity.from_mount_row(_row(**overrides)) is None


def test_persisted_source_binding_requires_exact_shape_and_digest() -> None:
    source = ProtectedMountSourceIdentity.from_mount_row(_row())
    assert source is not None
    assert (
        ProtectedMountSourceIdentity.from_binding(
            json.loads(source.canonical_json), expected_sha256=source.sha256
        )
        == source
    )

    assert (
        ProtectedMountSourceIdentity.from_binding(
            source.binding, expected_sha256="0" * 64
        )
        is None
    )
    assert (
        ProtectedMountSourceIdentity.from_binding(
            {**source.binding, "row_id": str(_row()["id"])},
            expected_sha256=source.sha256,
        )
        is None
    )
    malformed_handle = source.binding
    malformed_handle["handle"] = {"native_id": "42"}
    assert (
        ProtectedMountSourceIdentity.from_binding(
            malformed_handle, expected_sha256=source.sha256
        )
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda binding: binding.update(version=True),
        lambda binding: binding.update(version=1.0),
        lambda binding: binding.update(backend_instance_id=BACKEND_INSTANCE_ID.upper()),
        lambda binding: binding.update(
            backend_instance_id=BACKEND_INSTANCE_ID.replace("-", "")
        ),
        lambda binding: binding.update(source_ref=f"{{{PROJECT_ID}}}"),
        lambda binding: binding.update(source_ref=PROJECT_ID.upper()),
    ],
)
def test_persisted_source_binding_rejects_values_that_only_normalize_as_valid(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    source = ProtectedMountSourceIdentity.from_mount_row(_row())
    assert source is not None
    binding = source.binding
    mutate(binding)

    assert (
        ProtectedMountSourceIdentity.from_binding(
            binding, expected_sha256=source.sha256
        )
        is None
    )


@pytest.mark.parametrize(
    "handle",
    [
        {"native_id": "42", "vendor_meta": {"mountpoint": "Project Alpha"}},
        {
            "backend": "nextcloud",
            "native_id": None,
            "vendor_meta": {"mountpoint": "Project Alpha"},
        },
        {
            "backend": "nextcloud",
            "native_id": 42,
            "vendor_meta": {"mountpoint": "Project Alpha"},
        },
        {
            "backend": "nextcloud",
            "native_id": {"id": "42"},
            "vendor_meta": {"mountpoint": "Project Alpha"},
        },
        {"backend": "nextcloud", "native_id": "42", "vendor_meta": []},
        {
            "backend": "nextcloud",
            "native_id": "0",
            "vendor_meta": {"mountpoint": "Project Alpha"},
        },
        {
            "backend": "nextcloud",
            "native_id": "042",
            "vendor_meta": {"mountpoint": "Project Alpha"},
        },
    ],
)
def test_source_identity_rejects_malformed_serialized_handle_authority(
    handle: dict[str, object],
) -> None:
    assert (
        ProtectedMountSourceIdentity.from_mount_row(
            _row(cloud_handle=json.dumps(handle))
        )
        is None
    )


def test_source_identity_rebuilds_only_the_bound_project_handle() -> None:
    source = ProtectedMountSourceIdentity.from_mount_row(_row())
    assert source is not None

    assert source.to_project_folder_handle() == ProjectFolderHandle(
        backend="nextcloud",
        native_id="42",
        vendor_meta={"mountpoint": "Project Alpha"},
    )
