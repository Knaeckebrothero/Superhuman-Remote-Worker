"""The application-owned replacement for main's two background-task dicts.

Both halves kept the eviction mechanism they had in ``main``, because the two
are observably different and both are relied on:

* protected-engage clears through ``add_done_callback``, guarded on task
  identity, so a resume re-engage firing right after a create engage is not
  clobbered by the older registration's callback;
* stage clears in the running coroutine's own ``finally``, so awaiting the task
  is enough to see the slot free — a done-callback would move that one event
  loop tick later and silently break callers that await and then look.

The properties below are what a later refactor must not quietly change.
"""

import asyncio

import pytest

from orchestrator.services.cloud_task_registry import CloudTaskRegistry


THREAD = "11111111-1111-4111-8111-111111111111"
GEN_A = "22222222-2222-4222-8222-222222222222"
GEN_B = "33333333-3333-4333-8333-333333333333"


class TestProtectedEngage:
    @pytest.mark.asyncio
    async def test_the_slot_is_published_before_the_task_runs(self):
        """A caller racing attach must be able to await it, not fall through."""
        registry = CloudTaskRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        async def body():
            started.set()
            await release.wait()

        task = asyncio.create_task(body())
        registry.protected_engage_register((THREAD, GEN_A), task)

        assert registry.protected_engage_get((THREAD, GEN_A)) is task
        await started.wait()
        release.set()
        await task
        await asyncio.sleep(0)
        assert registry.protected_engage_get((THREAD, GEN_A)) is None

    @pytest.mark.asyncio
    async def test_a_stale_callback_never_clobbers_a_newer_registration(self):
        """The identity check main had, and the reason it is there.

        A resume re-engage registers for a *new* generation while the create
        engage is still settling. If the older task's done-callback popped by
        key alone, the newer task would become unawaitable and the caller would
        fall through to a bare poll.
        """
        registry = CloudTaskRegistry()
        old_release = asyncio.Event()

        async def old_body():
            await old_release.wait()

        async def new_body():
            await asyncio.sleep(3600)

        old = asyncio.create_task(old_body())
        new = asyncio.create_task(new_body())
        key = (THREAD, GEN_A)
        registry.protected_engage_register(key, old)
        # Same key, newer task — exactly the create-then-resume race.
        registry.protected_engage_register(key, new)

        old_release.set()
        await old
        await asyncio.sleep(0)

        assert registry.protected_engage_get(key) is new
        new.cancel()

    @pytest.mark.asyncio
    async def test_generations_are_separate_slots(self):
        registry = CloudTaskRegistry()

        async def body():
            return None

        a = asyncio.create_task(body())
        b = asyncio.create_task(body())
        registry.protected_engage_register((THREAD, GEN_A), a)
        registry.protected_engage_register((THREAD, GEN_B), b)

        assert registry.protected_engage_get((THREAD, GEN_A)) is a
        assert registry.protected_engage_get((THREAD, GEN_B)) is b
        await asyncio.gather(a, b)


class TestCloudStage:
    @pytest.mark.asyncio
    async def test_a_second_trigger_for_the_same_key_is_a_no_op(self):
        """De-dupe: a slow stage must not be raced by the next turn's ping."""
        registry = CloudTaskRegistry()
        release = asyncio.Event()
        runs = []

        async def body():
            runs.append(1)
            await release.wait()

        registry.stage_start("k", body)
        assert registry.stage_has("k")
        first = registry.cloud_stage_tasks["k"]

        registry.stage_start("k", body)
        assert registry.cloud_stage_tasks["k"] is first

        release.set()
        await first
        assert runs == [1]

    @pytest.mark.asyncio
    async def test_awaiting_the_task_is_enough_to_see_the_slot_free(self):
        """``main`` popped in the coroutine's own ``finally``; so does this.

        A done-callback would evict one tick later, and existing suites assert
        ``key not in tasks`` immediately after ``await task``.
        """
        registry = CloudTaskRegistry()

        async def body():
            return None

        registry.stage_start("k", body)
        await registry.cloud_stage_tasks["k"]

        assert not registry.stage_has("k")

    @pytest.mark.asyncio
    async def test_a_failing_stage_still_frees_its_slot(self):
        registry = CloudTaskRegistry()

        async def body():
            raise RuntimeError("stage failed")

        registry.stage_start("k", body)
        task = registry.cloud_stage_tasks["k"]
        with pytest.raises(RuntimeError):
            await task

        assert not registry.stage_has("k")

    @pytest.mark.asyncio
    async def test_a_seeded_sentinel_is_observed(self):
        """The exposed mapping is the LIVE dict, not a copy.

        Existing suites seed a sentinel task to prove the de-dupe refuses, and
        a copy would make the identity they mutate different from the identity
        the code reads.
        """
        registry = CloudTaskRegistry()

        async def never():
            await asyncio.sleep(3600)

        sentinel = asyncio.create_task(never())
        registry.cloud_stage_tasks["k"] = sentinel

        assert registry.stage_has("k")
        ran = []

        async def body():
            ran.append(1)

        registry.stage_start("k", body)
        await asyncio.sleep(0)

        assert registry.cloud_stage_tasks["k"] is sentinel
        assert ran == []
        sentinel.cancel()


class TestOwnership:
    def test_two_registries_share_nothing(self):
        """One instance per application, so two apps in one process are apart."""
        a, b = CloudTaskRegistry(), CloudTaskRegistry()

        assert a.protected_engage_tasks is not b.protected_engage_tasks
        assert a.cloud_stage_tasks is not b.cloud_stage_tasks

    def test_the_two_halves_are_separate_structures(self):
        """They answer different questions and are keyed differently."""
        registry = CloudTaskRegistry()

        assert registry.protected_engage_tasks is not registry.cloud_stage_tasks
