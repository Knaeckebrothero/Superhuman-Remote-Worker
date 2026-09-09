"""The extracted recipient resolution: comparison, never reconstruction.

R1.B06 lane A. ``agent_process_generation`` and
``local_pinned_session_target_matches`` had no direct coverage before the
extraction, so their branches are characterized here field by field. The
Pod-backed path's attestation and the ``/ready`` rollout fence are re-proved
against the extracted module rather than against ``orchestrator.main``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services import pinned_session_mutation_target as target_module
from orchestrator.services.pinned_session_mutation_target import (
    PinnedSessionMutationTarget,
    PinnedSessionMutationTargetDependencies,
    agent_process_generation,
    attest_pinned_session_mutation_pod,
    local_pinned_session_target_matches,
    pinned_session_mutation_target_is_current,
    prepare_pinned_session_mutation_target,
)
from shared.pinned_session_identity import PinnedSessionBinding

THREAD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AGENT_ID = "11111111-1111-4111-8111-111111111111"
GENERATION = "22222222-2222-4222-8222-222222222222"
ATTACH_TOKEN = "33333333-3333-4333-8333-333333333333"
PROCESS_GENERATION = "process-a"
_UNSET = object()


def _binding(**updates) -> PinnedSessionBinding:
    fields = {
        "thread_id": THREAD_ID,
        "runtime_generation": GENERATION,
        "agent_id": AGENT_ID,
        "runtime_attach_token": ATTACH_TOKEN,
        "agent_hostname": "agent-a",
        "pod_namespace": "srw",
        "pod_uid": "pod-a",
        "pod_ip": "10.42.0.17",
        "pod_port": 8001,
        "agent_status": "session",
    }
    fields.update(updates)
    return PinnedSessionBinding(**fields)


def _thread(**updates):
    row = {
        "id": THREAD_ID,
        "status": "created",
        "runtime_generation": GENERATION,
        "runtime_retirement_token": None,
        "runtime_attach_token": ATTACH_TOKEN,
        "agent_id": AGENT_ID,
    }
    row.update(updates)
    return row


def _agent(**updates):
    row = {
        "id": AGENT_ID,
        "thread_id": THREAD_ID,
        "hostname": "agent-a",
        "status": "session",
        "current_job_id": None,
        "pod_uid": None,
        "pod_ip": "10.42.0.17",
        "pod_port": 8001,
        "metadata": {"dispatch_process_generation": PROCESS_GENERATION},
    }
    row.update(updates)
    return row


def _deps(*, store=None, attest=None, is_current=None, agent_prov=None, persist=None):
    return PinnedSessionMutationTargetDependencies(
        store=store or MagicMock(),
        agent_provisioner=agent_prov or MagicMock(),
        persistent_provisioner=persist or MagicMock(),
        attest_pinned_session_mutation_pod=attest or AsyncMock(return_value=True),
        pinned_session_mutation_target_is_current=(
            is_current if is_current is not None else AsyncMock(return_value=True)
        ),
    )


class _ReadyClient:
    """Stand-in for ``httpx.AsyncClient`` serving one canned ``/ready``."""

    payload: object = {
        "thread_id": None,
        "capabilities": {"pinned_session_recipient_binding": True},
    }
    urls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        type(self).urls.append(url)
        payload = type(self).payload
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(json=lambda: payload)


@pytest.fixture
def ready_client(monkeypatch):
    _ReadyClient.urls = []
    _ReadyClient.payload = {
        "thread_id": None,
        "capabilities": {"pinned_session_recipient_binding": True},
    }
    monkeypatch.setattr(target_module.httpx, "AsyncClient", _ReadyClient)
    return _ReadyClient


class TestAgentProcessGeneration:
    def test_reads_the_dispatch_marker_from_a_dict(self):
        assert (
            agent_process_generation(
                {"metadata": {"dispatch_process_generation": " gen-a "}}
            )
            == "gen-a"
        )

    def test_reads_it_from_a_json_string_column(self):
        assert (
            agent_process_generation(
                {"metadata": '{"dispatch_process_generation": "gen-b"}'}
            )
            == "gen-b"
        )

    @pytest.mark.parametrize(
        "metadata",
        [None, {}, "not json", "[1, 2]", {"dispatch_process_generation": ""}, "null"],
        ids=["null", "empty", "garbage", "json-list", "blank", "json-null"],
    )
    def test_anything_unreadable_is_the_empty_generation_never_a_wildcard(
        self, metadata
    ):
        assert agent_process_generation({"metadata": metadata}) == ""


class TestLocalPinnedSessionTargetMatches:
    def _call(self, *, thread=_UNSET, agent=_UNSET, **overrides):
        kwargs = {
            "thread": _thread() if thread is _UNSET else thread,
            "agent": _agent() if agent is _UNSET else agent,
            "thread_id": THREAD_ID,
            "agent_id": AGENT_ID,
            "runtime_generation": GENERATION,
            "attach_token": ATTACH_TOKEN,
            "process_generation": PROCESS_GENERATION,
            "pod_ip": "10.42.0.17",
            "pod_port": 8001,
        }
        kwargs.update(overrides)
        return local_pinned_session_target_matches(**kwargs)

    def test_the_fully_reciprocal_pool_process_matches(self):
        assert self._call() is True

    def test_a_default_pod_port_of_8001_is_accepted(self):
        assert self._call(agent=_agent(pod_port=None)) is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("thread_id", "different-thread"),
            ("status", "ready"),
            ("current_job_id", "job-1"),
            ("pod_ip", "10.42.0.99"),
            ("pod_port", 9999),
            ("id", "different-agent"),
        ],
    )
    def test_any_moved_agent_field_refuses(self, field, value):
        assert self._call(agent=_agent(**{field: value})) is False

    def test_a_row_that_grew_a_pod_uid_is_no_longer_a_local_target(self):
        """A Pod-backed row must go through attestation, never this path."""
        assert self._call(agent=_agent(pod_uid="pod-a")) is False

    def test_a_moved_process_generation_refuses(self):
        assert (
            self._call(
                agent=_agent(metadata={"dispatch_process_generation": "process-b"})
            )
            is False
        )

    @pytest.mark.parametrize(
        "thread",
        [
            None,
            {},
            _thread(agent_id="other-agent"),
            _thread(runtime_attach_token="44444444-4444-4444-8444-444444444444"),
            _thread(status="ended"),
            _thread(runtime_retirement_token="t"),
            _thread(runtime_generation="55555555-5555-4555-8555-555555555555"),
        ],
        ids=[
            "no-thread",
            "empty-thread",
            "rebound",
            "rotated-token",
            "ended",
            "retiring",
            "rotated-generation",
        ],
    )
    def test_any_moved_thread_authority_refuses(self, thread):
        assert self._call(thread=thread) is False


class TestAttestPinnedSessionMutationPod:
    @pytest.mark.asyncio
    async def test_a_dedicated_hostname_routes_to_the_persistent_provisioner(self):
        binding = _binding(agent_hostname=f"persistent-{THREAD_ID[:12]}")
        persistent = MagicMock()
        persistent.attest_pinned_session_recipient = AsyncMock(return_value=True)
        agent_prov = MagicMock()
        agent_prov.attest_pinned_session_recipient = AsyncMock(return_value=True)

        assert await attest_pinned_session_mutation_pod(
            binding=binding,
            dependencies=_deps(agent_prov=agent_prov, persist=persistent),
        )
        persistent.attest_pinned_session_recipient.assert_awaited_once_with(
            binding.agent_hostname,
            thread_id=THREAD_ID,
            expected_runtime_generation=GENERATION,
            expected_pod_uid="pod-a",
            expected_pod_ip="10.42.0.17",
            namespace="srw",
        )
        agent_prov.attest_pinned_session_recipient.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_pool_hostname_routes_to_the_agent_provisioner_with_authority_kind(
        self,
    ):
        binding = _binding(pod_authority_kind="warm_pool")
        agent_prov = MagicMock()
        agent_prov.attest_pinned_session_recipient = AsyncMock(return_value=True)

        assert await attest_pinned_session_mutation_pod(
            binding=binding, dependencies=_deps(agent_prov=agent_prov)
        )
        agent_prov.attest_pinned_session_recipient.assert_awaited_once_with(
            "agent-a",
            thread_id=THREAD_ID,
            expected_runtime_generation=GENERATION,
            expected_pod_uid="pod-a",
            expected_pod_ip="10.42.0.17",
            authority_kind="warm_pool",
            namespace="srw",
        )


class TestPreparePinnedSessionMutationTarget:
    async def _prepare(self, deps):
        return await prepare_pinned_session_mutation_target(
            thread_id=THREAD_ID,
            agent_id=AGENT_ID,
            runtime_generation=GENERATION,
            attach_token=ATTACH_TOKEN,
            dependencies=deps,
        )

    @pytest.mark.asyncio
    async def test_a_local_pool_process_resolves_and_carries_a_null_pod_uid(
        self, ready_client
    ):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent()),
            get_thread=AsyncMock(return_value=_thread()),
        )
        target = await self._prepare(_deps(store=store))

        assert target is not None
        assert target.binding is None
        assert target.process_generation == PROCESS_GENERATION
        assert target.recipient == {
            "expected_thread_id": THREAD_ID,
            "expected_agent_id": AGENT_ID,
            "expected_pod_uid": None,
            "expected_process_generation": PROCESS_GENERATION,
        }
        assert ready_client.urls == ["http://10.42.0.17:8001/ready"]

    @pytest.mark.asyncio
    async def test_a_pod_backed_agent_uses_the_binding_endpoint_for_ready(
        self, ready_client
    ):
        binding = _binding(pod_ip="10.42.0.30", pod_port=8443)
        store = SimpleNamespace(
            get_agent=AsyncMock(
                return_value=_agent(pod_uid="pod-a", pod_ip="10.42.0.30", pod_port=8443)
            ),
            get_pinned_session_binding=AsyncMock(return_value=binding),
        )
        attest = AsyncMock(return_value=True)
        target = await self._prepare(_deps(store=store, attest=attest))

        assert target is not None and target.binding is binding
        assert ready_client.urls == ["http://10.42.0.30:8443/ready"]
        attest.assert_awaited_once_with(binding=binding)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent",
        [None, _agent(metadata={}), _agent(pod_ip=None)],
        ids=["missing-agent", "no-process-generation", "no-pod-ip"],
    )
    async def test_an_unidentifiable_agent_refuses_before_any_transport(
        self, agent, ready_client
    ):
        store = SimpleNamespace(get_agent=AsyncMock(return_value=agent))
        assert await self._prepare(_deps(store=store)) is None
        assert ready_client.urls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "binding_update",
        [
            {"agent_id": "99999999-9999-4999-8999-999999999999"},
            {"runtime_attach_token": "44444444-4444-4444-8444-444444444444"},
            {"agent_status": "ready"},
            {"agent_hostname": "agent-b"},
            {"pod_uid": "pod-b"},
            {"pod_ip": "10.42.0.99"},
            {"pod_port": 9999},
        ],
    )
    async def test_a_binding_that_disagrees_with_the_agent_row_refuses(
        self, binding_update, ready_client
    ):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent(pod_uid="pod-a")),
            get_pinned_session_binding=AsyncMock(
                return_value=_binding(**binding_update)
            ),
        )
        assert await self._prepare(_deps(store=store)) is None
        assert ready_client.urls == []

    @pytest.mark.asyncio
    async def test_an_absent_binding_refuses_rather_than_downgrading_to_local(
        self, ready_client
    ):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent(pod_uid="pod-a")),
            get_pinned_session_binding=AsyncMock(return_value=None),
            get_thread=AsyncMock(return_value=_thread()),
        )
        assert await self._prepare(_deps(store=store)) is None
        assert ready_client.urls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"capabilities": {}},
            {"capabilities": {"pinned_session_recipient_binding": False}},
            {"capabilities": {"pinned_session_recipient_binding": "yes"}},
            {
                "thread_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "capabilities": {"pinned_session_recipient_binding": True},
            },
            "not-a-mapping",
            RuntimeError("transport down"),
        ],
        ids=[
            "no-capability",
            "capability-false",
            "capability-truthy-not-true",
            "other-thread",
            "non-mapping-body",
            "transport-error",
        ],
    )
    async def test_the_ready_fence_refuses_anything_it_cannot_prove(
        self, payload, ready_client
    ):
        ready_client.payload = payload
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent()),
            get_thread=AsyncMock(return_value=_thread()),
        )
        assert await self._prepare(_deps(store=store)) is None

    @pytest.mark.asyncio
    async def test_a_ready_body_claiming_this_thread_is_accepted(self, ready_client):
        ready_client.payload = {
            "thread_id": THREAD_ID,
            "capabilities": {"pinned_session_recipient_binding": True},
        }
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent()),
            get_thread=AsyncMock(return_value=_thread()),
        )
        assert await self._prepare(_deps(store=store)) is not None

    @pytest.mark.asyncio
    async def test_a_failed_attestation_refuses_the_target(self, ready_client):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent(pod_uid="pod-a")),
            get_pinned_session_binding=AsyncMock(return_value=_binding()),
        )
        is_current = AsyncMock(return_value=True)
        assert (
            await self._prepare(
                _deps(
                    store=store,
                    attest=AsyncMock(return_value=False),
                    is_current=is_current,
                )
            )
            is None
        )
        is_current.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_tail_currency_check_is_the_last_word(self, ready_client):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent()),
            get_thread=AsyncMock(return_value=_thread()),
        )
        is_current = AsyncMock(return_value=False)
        assert await self._prepare(_deps(store=store, is_current=is_current)) is None
        is_current.assert_awaited_once()


class TestPinnedSessionMutationTargetIsCurrent:
    def _target(self, *, binding=None, agent=None):
        return PinnedSessionMutationTarget(
            agent=agent or _agent(),
            binding=binding,
            recipient={
                "expected_thread_id": THREAD_ID,
                "expected_agent_id": AGENT_ID,
                "expected_pod_uid": None if binding is None else binding.pod_uid,
                "expected_process_generation": PROCESS_GENERATION,
            },
            process_generation=PROCESS_GENERATION,
            runtime_generation=GENERATION,
            attach_token=ATTACH_TOKEN,
        )

    @pytest.mark.asyncio
    async def test_a_local_target_re_reads_thread_and_agent(self):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent()),
            get_thread=AsyncMock(return_value=_thread()),
        )
        assert (
            await pinned_session_mutation_target_is_current(
                self._target(), dependencies=_deps(store=store)
            )
            is True
        )
        store.get_thread.assert_awaited_once_with(THREAD_ID)

    @pytest.mark.asyncio
    async def test_a_local_target_whose_agent_moved_is_no_longer_current(self):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent(status="ready")),
            get_thread=AsyncMock(return_value=_thread()),
        )
        assert (
            await pinned_session_mutation_target_is_current(
                self._target(), dependencies=_deps(store=store)
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_a_pod_target_re_attests_the_freshly_read_binding(self):
        fresh = _binding()
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent(pod_uid="pod-a")),
            get_pinned_session_binding=AsyncMock(return_value=fresh),
        )
        attest = AsyncMock(return_value=True)
        assert (
            await pinned_session_mutation_target_is_current(
                self._target(binding=_binding()),
                dependencies=_deps(store=store, attest=attest),
            )
            is True
        )
        attest.assert_awaited_once_with(binding=fresh)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fresh_binding", "agent_update"),
        [
            (None, {}),
            (_binding(pod_uid="pod-b"), {}),
            (_binding(agent_status="ready"), {}),
            (_binding(), {"thread_id": "other"}),
            (_binding(), {"status": "ready"}),
            (_binding(), {"current_job_id": "job-1"}),
            (_binding(), {"metadata": {"dispatch_process_generation": "process-b"}}),
        ],
        ids=[
            "binding-gone",
            "different-target-key",
            "agent-left-session",
            "rebound-agent",
            "agent-status-moved",
            "agent-took-a-job",
            "process-restarted",
        ],
    )
    async def test_any_moved_fact_refuses_without_attesting(
        self, fresh_binding, agent_update
    ):
        store = SimpleNamespace(
            get_agent=AsyncMock(return_value=_agent(pod_uid="pod-a", **agent_update)),
            get_pinned_session_binding=AsyncMock(return_value=fresh_binding),
        )
        attest = AsyncMock(return_value=True)
        assert (
            await pinned_session_mutation_target_is_current(
                self._target(binding=_binding()),
                dependencies=_deps(store=store, attest=attest),
            )
            is False
        )
        attest.assert_not_awaited()
