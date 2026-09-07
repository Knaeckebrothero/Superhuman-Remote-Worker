"""Workspace preparation preserves sequencing without owning admission policy."""

import asyncio
from dataclasses import replace
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from fastapi import HTTPException
import pytest

from orchestrator.services.job_admission_workspace import (
    JobAdmissionWorkspaceDependencies,
    prepare_job_admission_workspace,
)


USER = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
LOGGER = "orchestrator.services.job_admission_workspace"


class Capabilities:
    def __init__(self):
        self.available = Mock(return_value=True)
        self.cluster = Mock(return_value=True)

    @property
    def is_available(self):
        return self.available()

    @property
    def in_cluster(self):
        return self.cluster()


@pytest.fixture
def deps():
    return JobAdmissionWorkspaceDependencies(
        store=SimpleNamespace(get_user=AsyncMock(return_value={"id": USER})),
        needs_vm=Mock(return_value=False),
        needs_sandbox=Mock(return_value=True),
        check_vm_permission=AsyncMock(),
        resolve_execution_lane=Mock(return_value=None),
        stateless_default_enabled=Mock(return_value=False),
        stateless_enabled=Mock(return_value=True),
        vm_workspaces_on_pod_network=Mock(return_value=True),
        provisioner=Capabilities(),
        enforce_grants=AsyncMock(),
    )


async def prepare(deps, **fields):
    return await prepare_job_admission_workspace(
        **{
            "context": {},
            "config_override": {"workspace": {"backend": "sandbox"}},
            "effective_user_id": USER,
            "project_id": PROJECT,
            "requested_lane": None,
            "root_creation": True,
            **fields,
        },
        dependencies=deps,
    )


def test_import_does_not_load_application_or_runtime_collaborators():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from orchestrator.services.job_admission_workspace import prepare_job_admission_workspace
for prefix in ('orchestrator.main', 'orchestrator.security', 'orchestrator.database',
               'orchestrator.services.container_provisioner',
               'orchestrator.services.vm_provisioner', 'agent', 'shared.runtime'):
    assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, {}, {"llm": {"model": "officer-slot"}}])
async def test_classifiers_and_grants_receive_original_prepared_references(
    deps, override
):
    context = {"officer_slot": "researchers", "nested": {"keep": True}}
    user_id = UUID(USER)
    result = await prepare(
        deps, context=context, config_override=override, effective_user_id=user_id
    )
    assert result is None
    for classifier in (deps.needs_vm, deps.needs_sandbox):
        observed = classifier.call_args.args[0]
        assert observed["context"] is context
        assert observed["config_override"] is override
        assert set(observed) == {"context", "config_override"}
    assert deps.enforce_grants.await_args.args[0] is override
    assert deps.enforce_grants.await_args.kwargs == {
        "user_id": USER,
        "project_ids": [PROJECT],
    }
    assert context == {"officer_slot": "researchers", "nested": {"keep": True}}
    deps.store.get_user.assert_not_awaited()
    deps.check_vm_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_finishes_before_capability_state_is_observed(deps):
    deps.needs_vm.return_value = True
    order = Mock()
    for name, collaborator in (
        ("vm", deps.needs_vm),
        ("creator", deps.store.get_user),
        ("permission", deps.check_vm_permission),
        ("sandbox", deps.needs_sandbox),
        ("default", deps.stateless_default_enabled),
        ("lane", deps.resolve_execution_lane),
        ("grants", deps.enforce_grants),
    ):
        order.attach_mock(collaborator, name)

    async def permission(*_args, **_kwargs):
        deps.stateless_default_enabled.assert_not_called()
        deps.needs_sandbox.assert_not_called()
        deps.provisioner.available.assert_not_called()
        deps.stateless_default_enabled.return_value = True

    deps.check_vm_permission.side_effect = permission
    deps.resolve_execution_lane.return_value = "stateless"
    assert await prepare(deps) == "stateless"
    assert [call[0] for call in order.mock_calls] == [
        "vm",
        "creator",
        "permission",
        "sandbox",
        "default",
        "lane",
        "default",
        "grants",
    ]
    deps.check_vm_permission.assert_awaited_once_with(
        deps.store.get_user.return_value, job_needs_vm=True
    )
    deps.resolve_execution_lane.assert_called_once_with(
        None, default_stateless=True, needs_vm=True, needs_sandbox=True
    )
    deps.stateless_enabled.assert_not_called()
    deps.vm_workspaces_on_pod_network.assert_not_called()
    deps.provisioner.available.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_lane,root,default_reads",
    [("pinned", True, 1), ("stateless", True, 1), (None, False, 2)],
)
async def test_explicit_or_child_lane_skips_default_diagnostics(
    deps, requested_lane, root, default_reads, caplog
):
    deps.stateless_default_enabled.return_value = True
    deps.resolve_execution_lane.return_value = requested_lane
    with caplog.at_level("DEBUG", logger=LOGGER):
        assert (
            await prepare(deps, requested_lane=requested_lane, root_creation=root)
            == requested_lane
        )
    assert deps.stateless_default_enabled.call_count == default_reads
    assert deps.resolve_execution_lane.call_args.kwargs["default_stateless"] is root
    deps.stateless_enabled.assert_not_called()
    deps.vm_workspaces_on_pod_network.assert_not_called()
    deps.provisioner.available.assert_not_called()
    deps.provisioner.cluster.assert_not_called()
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defaults,expected_log", [([True, False], False), ([False, True], True)]
)
async def test_default_flag_is_read_again_for_diagnostics(
    deps, defaults, expected_log, caplog
):
    deps.stateless_default_enabled.side_effect = defaults
    deps.stateless_enabled.return_value = False
    with caplog.at_level("DEBUG", logger=LOGGER):
        assert await prepare(deps) is None
    assert (
        deps.resolve_execution_lane.call_args.kwargs["default_stateless"] is defaults[0]
    )
    assert deps.stateless_default_enabled.call_count == 2
    assert bool(caplog.records) is expected_log
    assert deps.stateless_enabled.call_count == int(expected_log)
    deps.provisioner.available.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vm,pod,enabled,available,cluster,sandbox,reads,reason",
    [
        (
            True,
            False,
            True,
            True,
            True,
            True,
            ["pod"],
            "external VM jobs require pinned workers",
        ),
        (
            False,
            True,
            False,
            True,
            True,
            True,
            ["enabled"],
            "stateless worker admission is disabled",
        ),
        (
            False,
            True,
            True,
            False,
            True,
            True,
            ["enabled", "available"],
            "the Kubernetes workspace provisioner is unavailable",
        ),
        (
            False,
            True,
            True,
            True,
            False,
            True,
            ["enabled", "available", "cluster"],
            "the workspace provisioner is not in-cluster",
        ),
        (
            False,
            True,
            True,
            True,
            True,
            False,
            ["enabled", "available", "cluster"],
            "the job does not require a Kubernetes sandbox",
        ),
        (
            False,
            True,
            True,
            True,
            True,
            True,
            ["enabled", "available", "cluster"],
            "the worker capability check declined stateless",
        ),
    ],
)
async def test_fallback_diagnostic_stops_reading_at_first_reason(
    deps, vm, pod, enabled, available, cluster, sandbox, reads, reason, caplog
):
    deps.needs_vm.return_value = vm
    deps.needs_sandbox.return_value = sandbox
    deps.stateless_default_enabled.return_value = True
    deps.vm_workspaces_on_pod_network.return_value = pod
    deps.stateless_enabled.return_value = enabled
    deps.provisioner.available.return_value = available
    deps.provisioner.cluster.return_value = cluster
    order = Mock()
    for name, collaborator in (
        ("pod", deps.vm_workspaces_on_pod_network),
        ("enabled", deps.stateless_enabled),
        ("available", deps.provisioner.available),
        ("cluster", deps.provisioner.cluster),
    ):
        order.attach_mock(collaborator, name)
    with caplog.at_level("DEBUG", logger=LOGGER):
        assert await prepare(deps) is None
    assert [call[0] for call in order.mock_calls] == reads
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        (
            "DEBUG",
            f"Job create: stateless worker lane default fell back to pinned ({reason})",
        )
    ]
    deps.enforce_grants.assert_awaited_once()


@pytest.mark.asyncio
async def test_diagnostic_observes_provisioner_again_after_resolver_returns(
    deps, caplog
):
    deps.stateless_default_enabled.return_value = True

    def resolve(*_args, **_kwargs):
        assert deps.stateless_enabled()
        assert deps.provisioner.is_available
        assert deps.provisioner.in_cluster
        deps.provisioner.available.return_value = False
        return None

    deps.resolve_execution_lane.side_effect = resolve
    with caplog.at_level("DEBUG", logger=LOGGER):
        assert await prepare(deps) is None
    assert deps.stateless_enabled.call_count == 2
    assert deps.provisioner.available.call_count == 2
    assert deps.provisioner.cluster.call_count == 1
    assert "Kubernetes workspace provisioner is unavailable" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [RuntimeError("creator unavailable"), HTTPException(503, "creator unavailable")],
)
async def test_creator_read_failure_still_runs_permission_with_missing_user(
    deps, error
):
    deps.needs_vm.return_value = True
    deps.store.get_user.side_effect = error
    assert await prepare(deps) is None
    deps.store.get_user.assert_awaited_once_with(USER)
    deps.check_vm_permission.assert_awaited_once_with(None, job_needs_vm=True)
    deps.enforce_grants.assert_awaited_once()


@pytest.mark.asyncio
async def test_userless_vm_still_requires_permission_without_creator_lookup(deps):
    deps.needs_vm.return_value = True
    assert await prepare(deps, effective_user_id=None, project_id=None) is None
    deps.store.get_user.assert_not_awaited()
    deps.check_vm_permission.assert_awaited_once_with(None, job_needs_vm=True)
    assert deps.enforce_grants.await_args.kwargs == {"user_id": None, "project_ids": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["check_vm_permission", "resolve_execution_lane", "enforce_grants"]
)
@pytest.mark.parametrize(
    "error", [HTTPException(403, {"code": "refused"}), RuntimeError("port failed")]
)
async def test_authority_errors_propagate_unchanged_and_stop_later_admission(
    deps, boundary, error
):
    deps.needs_vm.return_value = True
    getattr(deps, boundary).side_effect = error
    with pytest.raises(type(error)) as exc:
        await prepare(deps)
    assert exc.value is error
    if boundary == "check_vm_permission":
        deps.needs_sandbox.assert_not_called()
        deps.stateless_default_enabled.assert_not_called()
        deps.resolve_execution_lane.assert_not_called()
    if boundary != "enforce_grants":
        deps.enforce_grants.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_read_cancellation_is_not_treated_as_missing_user(deps):
    deps.needs_vm.return_value = True
    deps.store.get_user.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await prepare(deps)
    deps.check_vm_permission.assert_not_awaited()
    deps.resolve_execution_lane.assert_not_called()
    deps.enforce_grants.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_preparations_keep_each_request_and_authority(deps):
    entered, release = asyncio.Event(), asyncio.Event()
    context = {"request": "first"}
    override = {"llm": {"model": "first"}}
    deps.needs_vm.return_value = True
    deps.resolve_execution_lane.return_value = "stateless"

    async def delayed_permission(*_args, **_kwargs):
        entered.set()
        await release.wait()

    deps.check_vm_permission.side_effect = delayed_permission
    error = HTTPException(403, "second creator denied")
    other = replace(
        deps,
        store=SimpleNamespace(get_user=AsyncMock(return_value={"id": "second"})),
        check_vm_permission=AsyncMock(side_effect=error),
        resolve_execution_lane=Mock(return_value="pinned"),
        enforce_grants=AsyncMock(),
    )
    first = asyncio.create_task(
        prepare(deps, context=context, config_override=override)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(HTTPException) as exc:
            await prepare(
                other, effective_user_id="second", context={"request": "second"}
            )
        assert exc.value is error
        deps.enforce_grants.assert_not_awaited()
    finally:
        release.set()
        assert await asyncio.wait_for(first, 2) == "stateless"
    assert deps.needs_sandbox.call_args.args[0]["context"] is context
    assert deps.enforce_grants.await_args.args[0] is override
    assert deps.enforce_grants.await_args.kwargs["user_id"] == USER
    other.check_vm_permission.assert_awaited_once_with(
        {"id": "second"}, job_needs_vm=True
    )
    other.resolve_execution_lane.assert_not_called()
    other.enforce_grants.assert_not_awaited()
