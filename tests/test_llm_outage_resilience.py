"""Unit tests for LLM-outage pause + backoff re-dispatch.

Covers the pure orchestrator-side logic added by
docs/features/llm_outage_pause_and_backoff_redispatch.md:

* evaluate_llm_outage — the auto-reset + give-up-ceiling decision
* llm_outage_backoff_seconds — Full/Equal jitter bounds + Retry-After floor
* determine_job_status — the llm_unavailable -> paused/failed mapping
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.completion import (  # noqa: E402
    LLM_OUTAGE_BACKOFF_CAP_SECONDS,
    LLM_OUTAGE_CEILING_SECONDS,
    LLM_OUTAGE_MAX_ATTEMPTS,
    LLM_OUTAGE_RESET_WINDOW_SECONDS,
    determine_job_status,
    evaluate_llm_outage,
    llm_outage_backoff_seconds,
)

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _outage_ctx(attempt, first_ago, last_ago):
    """Build a context dict with a llm_outage sub-object (times = seconds ago)."""
    return {
        "llm_outage": {
            "attempt": attempt,
            "first_failed_at": (NOW - timedelta(seconds=first_ago)).isoformat(),
            "last_failed_at": (NOW - timedelta(seconds=last_ago)).isoformat(),
        }
    }


# =============================================================================
# evaluate_llm_outage
# =============================================================================


class TestEvaluateLlmOutage:
    def test_fresh_outage_starts_at_attempt_zero(self):
        ev = evaluate_llm_outage({}, NOW)
        assert ev["attempt"] == 0
        assert ev["first_failed_at"] == NOW
        assert ev["reset"] is False
        assert ev["over_ceiling"] is False

    def test_ongoing_outage_short_gap_no_reset(self):
        # attempt 5, started 1h ago, last failure 60s ago (a short backoff)
        ev = evaluate_llm_outage(_outage_ctx(5, 3600, 60), NOW)
        assert ev["reset"] is False
        assert ev["attempt"] == 5
        assert ev["over_ceiling"] is False

    def test_long_backoff_gap_does_not_spuriously_reset(self):
        # The reset trap: a 50-min gap is a long backoff wait, NOT the job
        # running fine. With the window > backoff cap it must not reset.
        ev = evaluate_llm_outage(_outage_ctx(8, 3 * 3600, 50 * 60), NOW)
        assert ev["reset"] is False
        assert ev["attempt"] == 8

    def test_genuine_long_gap_resets(self):
        # A multi-hour productive gap (> reset window) is a new outage episode.
        gap = LLM_OUTAGE_RESET_WINDOW_SECONDS + 600
        ev = evaluate_llm_outage(_outage_ctx(8, gap + 3600, gap), NOW)
        assert ev["reset"] is True
        assert ev["attempt"] == 0
        assert ev["first_failed_at"] == NOW  # duration ceiling restarts too

    def test_duration_ceiling_trips_after_24h(self):
        ev = evaluate_llm_outage(
            _outage_ctx(8, LLM_OUTAGE_CEILING_SECONDS + 3600, 60), NOW
        )
        assert ev["over_ceiling"] is True
        assert ev["ceiling_reason"] == "duration"

    def test_attempts_backstop_trips(self):
        # Under 24h but at the attempts cap — the fast-refail backstop.
        ev = evaluate_llm_outage(_outage_ctx(LLM_OUTAGE_MAX_ATTEMPTS, 3600, 60), NOW)
        assert ev["over_ceiling"] is True
        assert ev["ceiling_reason"] == "attempts"

    def test_sustained_outage_trips_ceiling_despite_long_last_gap(self):
        # 25h continuous outage whose most-recent failure was a 40-min backoff
        # ago: the ceiling must still trip (no spurious reset masking it).
        ev = evaluate_llm_outage(_outage_ctx(8, 25 * 3600, 40 * 60), NOW)
        assert ev["over_ceiling"] is True
        assert ev["ceiling_reason"] == "duration"

    def test_reset_window_exceeds_backoff_cap(self):
        # Invariant the whole reset design depends on.
        assert LLM_OUTAGE_RESET_WINDOW_SECONDS > LLM_OUTAGE_BACKOFF_CAP_SECONDS

    def test_malformed_outage_object_is_safe(self):
        ev = evaluate_llm_outage({"llm_outage": "garbage"}, NOW)
        assert ev["attempt"] == 0
        assert ev["over_ceiling"] is False


# =============================================================================
# llm_outage_backoff_seconds
# =============================================================================


class TestLlmOutageBackoff:
    def _max(self, attempt, **kw):
        return llm_outage_backoff_seconds(attempt, rng=lambda a, b: b, **kw)

    def _min(self, attempt, **kw):
        return llm_outage_backoff_seconds(attempt, rng=lambda a, b: a, **kw)

    def test_envelope_doubles_each_attempt(self):
        assert self._max(1) == 30
        assert self._max(2) == 60
        assert self._max(3) == 120
        assert self._max(5) == 480

    def test_envelope_caps_at_60_min(self):
        assert self._max(8) == 3600
        assert self._max(20) == 3600
        assert self._max(200) == 3600  # no overflow on absurd attempts

    def test_full_jitter_spans_zero_to_envelope(self):
        # Full jitter draws from the whole [0, envelope] band.
        assert self._min(5) == 0
        assert self._max(5) == 480

    def test_equal_jitter_has_50pct_floor(self):
        # Equal jitter: envelope/2 + uniform(0, envelope/2).
        assert self._min(5, jitter="equal") == 240  # envelope/2
        assert self._max(5, jitter="equal") == 480  # full envelope

    def test_retry_after_floors_the_delay(self):
        # A server-directed Retry-After wins over a small jitter draw.
        assert self._min(1, retry_after_seconds=90) == 90
        # ...but not over a larger computed delay.
        assert self._max(8, retry_after_seconds=90) == 3600

    def test_all_draws_within_envelope(self):
        import random as _random

        for attempt in (1, 4, 9):
            envelope = min(LLM_OUTAGE_BACKOFF_CAP_SECONDS, 30 * 2 ** (attempt - 1))
            for _ in range(200):
                d = llm_outage_backoff_seconds(attempt, rng=_random.uniform)
                assert 0 <= d <= envelope


# =============================================================================
# determine_job_status — llm_unavailable mapping
# =============================================================================


def _real_outage_ctx(attempt, first_ago, last_ago):
    """llm_outage context relative to the REAL wall-clock now.

    determine_job_status reads ``datetime.now(timezone.utc)`` internally (it is
    not injectable), so its ceiling/reset decision must be exercised against
    real-now-relative timestamps, not the fixed ``NOW`` used elsewhere.
    """
    real_now = datetime.now(timezone.utc)
    return {
        "llm_outage": {
            "attempt": attempt,
            "first_failed_at": (real_now - timedelta(seconds=first_ago)).isoformat(),
            "last_failed_at": (real_now - timedelta(seconds=last_ago)).isoformat(),
        }
    }


def _llm_job(context):
    return {
        "id": "job-1",
        "status": "processing",
        "parent_job_id": None,
        "context": context,
        "freeze_data": {
            "freeze_type": "llm_unavailable",
            "classification": "rate_limit",
            "error_summary": "Connection error.",
            "model": "some-model",
        },
    }


STOP = {"should_stop": True}


class TestDetermineJobStatusLlmUnavailable:
    def test_fresh_outage_pauses(self):
        status, err = determine_job_status(_llm_job({}), STOP)
        assert status == "paused"
        assert err is None

    def test_ongoing_under_ceiling_pauses(self):
        job = _llm_job(_real_outage_ctx(5, 3600, 60))
        status, err = determine_job_status(job, STOP)
        assert status == "paused"

    def test_duration_ceiling_fails_loudly(self):
        job = _llm_job(_real_outage_ctx(8, LLM_OUTAGE_CEILING_SECONDS + 3600, 60))
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert err is not None
        assert "unavailable" in err.lower()
        assert "Admin" in err  # actionable operator pointer

    def test_attempts_backstop_fails(self):
        job = _llm_job(_real_outage_ctx(LLM_OUTAGE_MAX_ATTEMPTS, 3600, 60))
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert "attempts" in err.lower()

    def test_not_stopped_stays_processing(self):
        # should_stop False -> job keeps running, no pause.
        status, err = determine_job_status(_llm_job({}), {"should_stop": False})
        assert status is None
