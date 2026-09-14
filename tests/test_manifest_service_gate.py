"""The service gate cannot be pointed at an installed or external runtime."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / "scripts/manifests-service-k3d-gate.py"
    spec = importlib.util.spec_from_file_location("manifest_service_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "context,kubeconfig",
    [
        ("k3d-srw", "/tmp/config"),
        ("main", "/tmp/config"),
        ("k3d-srw-native-gate-deadbeef", None),
    ],
)
def test_installed_context_is_refused_before_any_inspection(
    gate, monkeypatch, context, kubeconfig
):
    monkeypatch.setattr(
        gate.native,
        "verify_cluster",
        lambda **_: pytest.fail("must refuse before cluster access"),
    )
    with pytest.raises(gate.GateFailure, match="disposable"):
        gate.verify_disposable_profile(context, kubeconfig)


def test_exception_bodies_never_enter_gate_output(gate, monkeypatch, capsys):
    async def failed(_):
        raise RuntimeError("secret-from-an-API-body")

    monkeypatch.setattr(gate, "run", failed)
    monkeypatch.setattr(
        "sys.argv",
        [
            "gate",
            "--kube-context",
            "k3d-srw-native-gate-deadbeef",
            "--kubeconfig",
            "/tmp/config",
        ],
    )
    assert gate.main() == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "secret-from" not in output


@pytest.mark.asyncio
async def test_fixture_delivery_retirement_continues_after_terminal_pod_disappears(
    gate, monkeypatch
):
    runtime = SimpleNamespace(
        observe=AsyncMock(
            side_effect=[
                SimpleNamespace(containers_terminal=False, pod_absent=False),
                SimpleNamespace(containers_terminal=True, pod_absent=False),
                SimpleNamespace(containers_terminal=False, pod_absent=True),
            ]
        ),
        cancel=AsyncMock(),
        cleanup=AsyncMock(side_effect=[False, True]),
    )
    monkeypatch.setattr(gate, "GenericHarnessRuntime", lambda *_, **__: runtime)
    monkeypatch.setattr(gate.asyncio, "sleep", AsyncMock())
    service = object.__new__(gate.ServiceGate)
    service.core = service.network = None
    service.namespace = "test-only"
    service.provider_fixtures = {"identity": "owned-pod-uid"}
    await service.retire_provider_fixtures()
    runtime.cancel.assert_awaited_once_with(
        "identity", expected_pod_uid="owned-pod-uid"
    )
    assert runtime.cleanup.await_args_list == [
        call("identity", expected_pod_uid="owned-pod-uid"),
        call("identity", expected_pod_uid="owned-pod-uid"),
    ]


@pytest.mark.asyncio
async def test_fixture_absence_without_terminal_evidence_does_not_release_objects(
    gate, monkeypatch
):
    runtime = SimpleNamespace(
        observe=AsyncMock(
            return_value=SimpleNamespace(containers_terminal=False, pod_absent=True)
        ),
        cancel=AsyncMock(),
        cleanup=AsyncMock(),
    )
    times = iter([0, 1, 121])
    monkeypatch.setattr(gate, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(gate, "GenericHarnessRuntime", lambda *_, **__: runtime)
    monkeypatch.setattr(gate.asyncio, "sleep", AsyncMock())
    service = object.__new__(gate.ServiceGate)
    service.core = service.network = None
    service.namespace = "test-only"
    service.provider_fixtures = {"identity": "owned-pod-uid"}
    with pytest.raises(gate.GateFailure, match="retirement"):
        await service.retire_provider_fixtures()
    runtime.cleanup.assert_not_awaited()
    runtime.cancel.assert_not_awaited()
