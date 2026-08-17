"""A job paused for a transient-infra retry must keep its VM.

Regression guard for Defect 1b of
knowledge-base/knowledge/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

Pausing a job does NOT keep its workspace. Nothing in the completion path tore
down job c6dd288d's VM — the REAPER did: ``failed`` is in
``_TERMINAL_JOB_STATUSES``. And a paused-and-frozen job is likewise idle, so it
is collected once ``paused_within_grace`` expires.

So without this carve-out the whole "pause, keep the VM, resume in place" design
is a no-op: the retry would come back to a reprovisioned, empty workspace —
exactly the loss the retry exists to prevent.
"""

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.services.lifecycle.workspace_manager import (
    infra_transient_retry_pending,
)


def _meta(freeze=None, **kw):
    return {"job_status": "paused", "job_freeze": freeze, **kw}


def _freeze(seconds_from_now: float, **kw):
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return {
        "freeze_type": "infra_transient",
        "next_retry_at": when.isoformat(),
        "attempts": 1,
        **kw,
    }


class TestHoldsTheWorkspace:
    def test_pending_retry_pins_the_vm(self):
        assert infra_transient_retry_pending(_meta(_freeze(300)))

    def test_naive_timestamp_is_treated_as_utc(self):
        """isoformat() without tzinfo must not crash or silently release."""
        when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=300)
        freeze = {"freeze_type": "infra_transient", "next_retry_at": when.isoformat()}
        assert infra_transient_retry_pending(_meta(freeze))


class TestReleasesTheWorkspace:
    def test_elapsed_retry_no_longer_pins(self):
        """Once due, the job is dispatchable and the dispatcher owns bring-up."""
        assert not infra_transient_retry_pending(_meta(_freeze(-1)))

    def test_a_different_freeze_type_does_not_pin(self):
        assert not infra_transient_retry_pending(
            _meta(
                {
                    "freeze_type": "llm_unavailable",
                    "next_retry_at": "2099-01-01T00:00:00+00:00",
                }
            )
        )

    def test_no_freeze_does_not_pin(self):
        assert not infra_transient_retry_pending(_meta(None))
        assert not infra_transient_retry_pending({"job_status": "paused"})

    @pytest.mark.parametrize("bad", ["", "not-a-timestamp", None, 12345, {}])
    def test_unparseable_timestamp_never_pins_a_vm_forever(self, bad):
        """Fail open: a malformed freeze must not leak a VM indefinitely."""
        assert not infra_transient_retry_pending(
            _meta({"freeze_type": "infra_transient", "next_retry_at": bad})
        )

    def test_missing_next_retry_at_does_not_pin(self):
        assert not infra_transient_retry_pending(
            _meta({"freeze_type": "infra_transient"})
        )

    def test_freeze_that_is_not_a_dict_does_not_pin(self):
        assert not infra_transient_retry_pending(_meta("infra_transient"))


class TestBoundedness:
    def test_the_hold_is_bounded_by_the_backoff_cap(self):
        """A pin can never outlast the longest backoff (1h), so a VM cannot be
        held indefinitely even if the sweeper stops running."""
        from orchestrator.services.completion import (
            INFRA_TRANSIENT_MAX_ATTEMPTS,
            infra_transient_backoff_seconds,
        )

        longest = max(
            infra_transient_backoff_seconds(n)
            for n in range(1, INFRA_TRANSIENT_MAX_ATTEMPTS + 2)
        )
        assert not infra_transient_retry_pending(_meta(_freeze(-longest - 1)))
