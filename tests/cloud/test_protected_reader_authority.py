from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import fields

import pytest

from orchestrator.services.cloud.handles import ProjectFolderHandle
from orchestrator.services.cloud.protected_reader_authority import (
    PROTECTED_NEXTCLOUD_GRANT_VERSION,
    ProtectedNextcloudReaderGrantPlan,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)


ATTEMPT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_ATTEMPT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BACKEND_INSTANCE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_BACKEND_INSTANCE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SOURCE_REF = "11111111-1111-4111-8111-111111111111"


def _source(**overrides: object) -> ProtectedMountSourceIdentity:
    values: dict[str, object] = {
        "backend_instance_id": BACKEND_INSTANCE,
        "source_ref": SOURCE_REF,
        "target_path": "cloud",
        "native_id": "42",
        "mountpoint": "Project Alpha",
    }
    values.update(overrides)
    return ProtectedMountSourceIdentity(**values)  # type: ignore[arg-type]


def _plan(
    *,
    engage_attempt: str = ATTEMPT,
    backend_instance_id: str = BACKEND_INSTANCE,
    source: ProtectedMountSourceIdentity | None = None,
) -> ProtectedNextcloudReaderGrantPlan:
    resolved_source = source or _source(backend_instance_id=backend_instance_id)
    return ProtectedNextcloudReaderGrantPlan(
        engage_attempt=engage_attempt,
        backend_instance_id=backend_instance_id,
        source=resolved_source,
    )


def _canonical_handle(binding: dict[str, object]) -> str:
    return json.dumps(
        binding,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_plan_derives_full_attempt_scoped_names_and_exact_handle() -> None:
    plan = _plan()

    assert plan.reader_id == f"srw-reader-a-{ATTEMPT.replace('-', '')}"
    assert plan.group_id == f"srw-rog-a-{ATTEMPT.replace('-', '')}"
    assert plan.grant_handle_binding == {
        "version": PROTECTED_NEXTCLOUD_GRANT_VERSION,
        "backend": "nextcloud",
        "backend_instance_id": BACKEND_INSTANCE,
        "engage_attempt": ATTEMPT,
        "reader_id": f"srw-reader-a-{ATTEMPT.replace('-', '')}",
        "group_id": f"srw-rog-a-{ATTEMPT.replace('-', '')}",
        "folder_id": "42",
        "mountpoint": "Project Alpha",
        "source_sha256": plan.source.sha256,
    }
    assert plan.grant_handle == _canonical_handle(plan.grant_handle_binding)
    assert plan.grant_handle_sha256 == _sha256(plan.grant_handle)


def test_plan_is_deterministic_and_contains_no_runtime_or_secret_coordinates() -> None:
    first = _plan()
    recreated = _plan()

    assert first == recreated
    assert first.grant_handle == recreated.grant_handle
    assert first.grant_handle_sha256 == recreated.grant_handle_sha256
    assert {field.name for field in fields(first)} == {
        "engage_attempt",
        "backend_instance_id",
        "source",
        "version",
        "backend",
    }
    assert set(first.grant_handle_binding) == {
        "version",
        "backend",
        "backend_instance_id",
        "engage_attempt",
        "reader_id",
        "group_id",
        "folder_id",
        "mountpoint",
        "source_sha256",
    }
    for excluded in (
        "thread_id",
        "user_id",
        "password",
        "credential",
        "webdav",
        "https://",
    ):
        assert excluded not in first.grant_handle.lower()


def test_backend_instance_is_mandatory_and_cannot_be_inferred_from_a_url() -> None:
    with pytest.raises(TypeError, match="backend_instance_id"):
        ProtectedNextcloudReaderGrantPlan(  # type: ignore[call-arg]
            engage_attempt=ATTEMPT,
            source=_source(),
        )


def test_plan_recovers_only_the_bound_project_folder_handle() -> None:
    assert _plan().to_project_folder_handle() == ProjectFolderHandle(
        backend="nextcloud",
        native_id="42",
        vendor_meta={"mountpoint": "Project Alpha"},
    )


@pytest.mark.parametrize(
    "engage_attempt",
    [
        ATTEMPT.upper(),
        ATTEMPT.replace("-", ""),
        f"{{{ATTEMPT}}}",
        f"urn:uuid:{ATTEMPT}",
        f" {ATTEMPT}",
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        "",
        None,
        True,
    ],
)
def test_plan_rejects_uuid_values_that_only_normalize_or_are_malformed(
    engage_attempt: object,
) -> None:
    with pytest.raises(ValueError, match="canonical UUID"):
        ProtectedNextcloudReaderGrantPlan(
            engage_attempt=engage_attempt,  # type: ignore[arg-type]
            backend_instance_id=BACKEND_INSTANCE,
            source=_source(),
        )


@pytest.mark.parametrize(
    "backend_instance_id",
    [
        BACKEND_INSTANCE.upper(),
        BACKEND_INSTANCE.replace("-", ""),
        f"{{{BACKEND_INSTANCE}}}",
        f"urn:uuid:{BACKEND_INSTANCE}",
        f" {BACKEND_INSTANCE}",
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        "",
        None,
        True,
    ],
)
def test_plan_rejects_backend_instance_values_that_only_normalize_or_are_malformed(
    backend_instance_id: object,
) -> None:
    with pytest.raises(ValueError, match="backend instance.*canonical UUID"):
        ProtectedNextcloudReaderGrantPlan(
            engage_attempt=ATTEMPT,
            backend_instance_id=backend_instance_id,  # type: ignore[arg-type]
            source=_source(),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"version": True}, "version"),
        ({"version": 1.0}, "version"),
        ({"backend": "opencloud"}, "backend"),
        ({"source": object()}, "source"),
    ],
)
def test_plan_rejects_noncanonical_authority_inputs(
    kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "engage_attempt": ATTEMPT,
        "backend_instance_id": BACKEND_INSTANCE,
        "source": _source(),
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        ProtectedNextcloudReaderGrantPlan(**values)  # type: ignore[arg-type]


def test_plan_rejects_backend_instance_that_does_not_match_source() -> None:
    with pytest.raises(ValueError, match="does not match its source"):
        ProtectedNextcloudReaderGrantPlan(
            engage_attempt=ATTEMPT,
            backend_instance_id=OTHER_BACKEND_INSTANCE,
            source=_source(backend_instance_id=BACKEND_INSTANCE),
        )


def test_instance_attempt_and_every_source_coordinate_change_the_grant_digest() -> None:
    original = _plan()
    alternatives = [
        _plan(backend_instance_id=OTHER_BACKEND_INSTANCE),
        _plan(engage_attempt=OTHER_ATTEMPT),
        _plan(source=_source(source_ref="22222222-2222-4222-8222-222222222222")),
        _plan(source=_source(target_path="projects/alpha")),
        _plan(source=_source(native_id="99")),
        _plan(source=_source(mountpoint="Project Beta")),
    ]

    assert all(
        replacement.grant_handle_sha256 != original.grant_handle_sha256
        for replacement in alternatives
    )


def test_parser_round_trips_only_with_exact_attempt_source_and_digest() -> None:
    plan = _plan()

    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            plan.grant_handle,
            expected_sha256=plan.grant_handle_sha256,
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=plan.source,
        )
        == plan
    )
    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            plan.grant_handle,
            expected_sha256="0" * 64,
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=plan.source,
        )
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(version=True),
        lambda value: value.update(version=1.0),
        lambda value: value.update(backend="opencloud"),
        lambda value: value.update(backend_instance_id=OTHER_BACKEND_INSTANCE),
        lambda value: value.update(backend_instance_id=BACKEND_INSTANCE.upper()),
        lambda value: value.update(engage_attempt=OTHER_ATTEMPT),
        lambda value: value.update(reader_id="srw-reader-a-forged"),
        lambda value: value.update(group_id="srw-rog-a-forged"),
        lambda value: value.update(folder_id="99"),
        lambda value: value.update(folder_id="042"),
        lambda value: value.update(mountpoint="Project Beta"),
        lambda value: value.update(source_sha256="f" * 64),
        lambda value: value.update(source_sha256=value["source_sha256"].upper()),
        lambda value: value.update(thread_id="not-authority"),
        lambda value: value.pop("backend_instance_id"),
        lambda value: value.pop("group_id"),
    ],
)
def test_parser_rejects_rehashed_field_mutations(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    plan = _plan()
    mutated = plan.grant_handle_binding
    mutate(mutated)
    handle = _canonical_handle(mutated)

    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            handle,
            expected_sha256=_sha256(handle),
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=plan.source,
        )
        is None
    )


def test_parser_rejects_a_self_consistent_handle_for_the_wrong_attempt() -> None:
    original = _plan()
    replacement = _plan(engage_attempt=OTHER_ATTEMPT)

    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            replacement.grant_handle,
            expected_sha256=replacement.grant_handle_sha256,
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=original.source,
        )
        is None
    )


def test_parser_rejects_a_self_consistent_handle_for_another_backend_instance() -> None:
    original = _plan()
    replacement = _plan(backend_instance_id=OTHER_BACKEND_INSTANCE)

    # Installation identity is authority, but it never changes the names of
    # the attempt-scoped remote principals.
    assert replacement.reader_id == original.reader_id
    assert replacement.group_id == original.group_id
    assert replacement.grant_handle_sha256 != original.grant_handle_sha256
    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            replacement.grant_handle,
            expected_sha256=replacement.grant_handle_sha256,
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=original.source,
        )
        is None
    )


@pytest.mark.parametrize(
    "replacement_source",
    [
        _source(source_ref="22222222-2222-4222-8222-222222222222"),
        _source(target_path="projects/alpha"),
        _source(native_id="99"),
        _source(mountpoint="Project Beta"),
    ],
)
def test_parser_rejects_a_handle_for_any_other_source_coordinate(
    replacement_source: ProtectedMountSourceIdentity,
) -> None:
    plan = _plan()

    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            plan.grant_handle,
            expected_sha256=plan.grant_handle_sha256,
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=replacement_source,
        )
        is None
    )


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda plan: json.dumps(plan.grant_handle_binding),
        lambda plan: json.dumps(plan.grant_handle_binding, indent=2, sort_keys=True),
        lambda plan: plan.grant_handle + "\n",
        lambda plan: plan.grant_handle.replace("Project Alpha", "Project \\u0041lpha"),
    ],
)
def test_parser_rejects_noncanonical_json_even_with_its_recomputed_digest(
    rewrite: Callable[[ProtectedNextcloudReaderGrantPlan], str],
) -> None:
    plan = _plan()
    handle = rewrite(plan)
    assert json.loads(handle) == plan.grant_handle_binding

    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            handle,
            expected_sha256=_sha256(handle),
            expected_engage_attempt=ATTEMPT,
            expected_backend_instance_id=BACKEND_INSTANCE,
            expected_source=plan.source,
        )
        is None
    )


@pytest.mark.parametrize(
    ("handle", "digest", "attempt", "backend_instance", "source"),
    [
        ("", "0" * 64, ATTEMPT, BACKEND_INSTANCE, _source()),
        ("{malformed", "0" * 64, ATTEMPT, BACKEND_INSTANCE, _source()),
        ("[]", "0" * 64, ATTEMPT, BACKEND_INSTANCE, _source()),
        (_plan().grant_handle, "A" * 64, ATTEMPT, BACKEND_INSTANCE, _source()),
        (_plan().grant_handle, "short", ATTEMPT, BACKEND_INSTANCE, _source()),
        (
            _plan().grant_handle,
            _plan().grant_handle_sha256,
            ATTEMPT.upper(),
            BACKEND_INSTANCE,
            _source(),
        ),
        (
            _plan().grant_handle,
            _plan().grant_handle_sha256,
            ATTEMPT,
            BACKEND_INSTANCE.upper(),
            _source(),
        ),
        (
            _plan().grant_handle,
            _plan().grant_handle_sha256,
            ATTEMPT,
            BACKEND_INSTANCE,
            object(),
        ),
    ],
)
def test_parser_rejects_malformed_expected_authority(
    handle: str,
    digest: str,
    attempt: str,
    backend_instance: str,
    source: object,
) -> None:
    assert (
        ProtectedNextcloudReaderGrantPlan.from_grant_handle(
            handle,
            expected_sha256=digest,
            expected_engage_attempt=attempt,
            expected_backend_instance_id=backend_instance,
            expected_source=source,  # type: ignore[arg-type]
        )
        is None
    )


def test_ro_mount_row_parser_requires_every_redundant_authority_field() -> None:
    plan = _plan()
    row = {
        "backend": "nextcloud",
        "backend_instance_id": plan.backend_instance_id,
        "engage_attempt": plan.engage_attempt,
        "reader_id": plan.reader_id,
        "grant_group_id": plan.group_id,
        "grant_handle": plan.grant_handle,
        "grant_handle_sha256": plan.grant_handle_sha256,
        "source_binding": plan.source.binding,
        "source_binding_sha256": plan.source.sha256,
    }

    assert ProtectedNextcloudReaderGrantPlan.from_ro_mount_row(row) == plan
    for field in (
        "backend_instance_id",
        "engage_attempt",
        "reader_id",
        "grant_group_id",
        "grant_handle",
        "grant_handle_sha256",
        "source_binding",
        "source_binding_sha256",
    ):
        malformed = dict(row)
        malformed[field] = None
        assert ProtectedNextcloudReaderGrantPlan.from_ro_mount_row(malformed) is None
