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
    MEMORY_RETRY_CAP,
    determine_job_status,
    evaluate_llm_outage,
    llm_outage_backoff_seconds,
    llm_outage_fingerprint,
)

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _outage_ctx(attempt, first_ago, last_ago, next_retry_ago=None):
    """Build a context dict with a llm_outage sub-object (times = seconds ago).

    ``next_retry_ago`` (seconds before NOW that the last pause's re-dispatch was
    scheduled) is added only when provided, so legacy-shaped state is unchanged.
    """
    outage = {
        "attempt": attempt,
        "first_failed_at": (NOW - timedelta(seconds=first_ago)).isoformat(),
        "last_failed_at": (NOW - timedelta(seconds=last_ago)).isoformat(),
    }
    if next_retry_ago is not None:
        outage["next_retry_at"] = (NOW - timedelta(seconds=next_retry_ago)).isoformat()
    return {"llm_outage": outage}


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

    def test_duration_ceiling_trips_past_ceiling(self):
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

    # --- anchored reset (docs/features/llm_cooldown_pause_and_resume.md) --------
    # The reset must measure idle time from the END of any scheduled wait we
    # imposed (next_retry_at), not from last_failed_at — else a multi-hour
    # cooldown pause looks like "the job ran fine" and spuriously resets the
    # ceiling, letting a never-clearing cooldown park the job forever.

    def test_still_cooling_redispatch_does_not_reset(self):
        # A cooldown pause scheduled re-dispatch 3h out; the model was still
        # cooling, so the job re-failed ~60s after resuming. The gap since
        # last_failed_at (3h) exceeds the 2h window, but the gap since the
        # scheduled resume (next_retry_at, 60s) does not — so NO reset, and the
        # duration ceiling keeps accumulating toward give-up.
        ev = evaluate_llm_outage(
            _outage_ctx(4, 3 * 3600 + 60, 3 * 3600, next_retry_ago=60), NOW
        )
        assert ev["reset"] is False
        assert ev["attempt"] == 4

    def test_second_cooldown_after_productive_gap_resets(self):
        # The job resumed at the scheduled time and ran productively for >2h
        # before a fresh failure: idle beyond the scheduled wait exceeds the
        # window → an independent outage → reset (no stale first_failed_at).
        idle = LLM_OUTAGE_RESET_WINDOW_SECONDS + 600
        ev = evaluate_llm_outage(
            _outage_ctx(4, 10 * 3600, 5 * 3600, next_retry_ago=idle), NOW
        )
        assert ev["reset"] is True
        assert ev["attempt"] == 0
        assert ev["first_failed_at"] == NOW

    def test_legacy_state_without_next_retry_uses_last_failed_at(self):
        # No next_retry_at (state written before this feature): fall back to
        # today's behavior exactly — a >window gap since last_failed_at resets.
        # Same shape as test_still_cooling_* but without the anchor → opposite
        # verdict, which is the whole point of the graceful fallback.
        ev = evaluate_llm_outage(_outage_ctx(4, 3 * 3600 + 60, 3 * 3600), NOW)
        assert ev["reset"] is True
        assert ev["attempt"] == 0

    def test_never_clearing_cooldown_trips_ceiling(self):
        # 13h of a still-cooling cooldown (each re-dispatch re-fails ~1min after
        # resuming): with the anchor suppressing the spurious reset, the 12h
        # duration ceiling trips → loud fail, never parks forever.
        ev = evaluate_llm_outage(
            _outage_ctx(6, 13 * 3600, 13 * 3600, next_retry_ago=60), NOW
        )
        assert ev["over_ceiling"] is True
        assert ev["ceiling_reason"] == "duration"

    def test_ceiling_default_is_12h(self):
        # Product decision (2026-07-15): the pause budget / give-up ceiling is
        # 12h, fused with the cooldown pause-vs-fail-fast cutoff.
        assert LLM_OUTAGE_CEILING_SECONDS == 43_200


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


# =============================================================================
# Subjob outage routing — scholar/critic/delegate subjobs pause like top-level
# (docs/features/llm_outage_subjob_resilience.md)
# =============================================================================


def _subjob_llm_job(context):
    job = _llm_job(context)
    job["parent_job_id"] = "par-1"
    return job


class TestDetermineJobStatusSubjobOutage:
    """Outage freezes on subjobs route through the shared type-specific
    branches (row-scoped caps/ceilings) instead of the pending_review
    fallback — guarded by the parent-terminal cascade rule, exactly like the
    version_upgrade subjob precedent."""

    def test_subjob_fresh_outage_pauses(self):
        status, err = determine_job_status(
            _subjob_llm_job({}), STOP, parent_status="waiting"
        )
        assert (status, err) == ("paused", None)

    def test_subjob_outage_under_ceiling_pauses(self):
        job = _subjob_llm_job(_real_outage_ctx(5, 3600, 60))
        status, err = determine_job_status(job, STOP, parent_status="waiting")
        assert status == "paused"

    def test_subjob_reviewing_parent_is_not_terminal(self):
        # A critic's parent sits in 'reviewing' while the critic runs — that
        # must count as a live parent, not a terminal one.
        status, err = determine_job_status(
            _subjob_llm_job({}), STOP, parent_status="reviewing"
        )
        assert status == "paused"

    def test_subjob_outage_parent_failed_resolves_cancelled(self):
        # A paused subjob under a failed parent is a silent cascade-guard
        # wedge — resolve terminally instead (version_upgrade precedent).
        status, err = determine_job_status(
            _subjob_llm_job({}), STOP, parent_status="failed"
        )
        assert (status, err) == ("cancelled", None)

    def test_subjob_outage_parent_cancelled_resolves_cancelled(self):
        status, err = determine_job_status(
            _subjob_llm_job({}), STOP, parent_status="cancelled"
        )
        assert (status, err) == ("cancelled", None)

    def test_subjob_over_ceiling_fails_loudly(self):
        # The 12h duration ceiling lives on the SUBJOB's own row and applies
        # unchanged — an over-budget subjob outage fails, never parks.
        job = _subjob_llm_job(
            _real_outage_ctx(8, LLM_OUTAGE_CEILING_SECONDS + 3600, 60)
        )
        status, err = determine_job_status(job, STOP, parent_status="waiting")
        assert status == "failed"
        assert err is not None and "unavailable" in err.lower()

    def test_subjob_attempts_backstop_fails(self):
        job = _subjob_llm_job(_real_outage_ctx(LLM_OUTAGE_MAX_ATTEMPTS, 3600, 60))
        status, err = determine_job_status(job, STOP, parent_status="waiting")
        assert status == "failed"
        assert "attempts" in err.lower()

    def test_subjob_memory_unavailable_pauses_under_cap(self):
        job = _subjob_llm_job({})
        job["freeze_data"] = {"freeze_type": "memory_unavailable", "reason": "x"}
        status, err = determine_job_status(job, STOP, parent_status="waiting")
        assert (status, err) == ("paused", None)

    def test_subjob_memory_unavailable_over_cap_fails(self):
        job = _subjob_llm_job({"memory_retry_count": MEMORY_RETRY_CAP})
        job["freeze_data"] = {"freeze_type": "memory_unavailable", "reason": "x"}
        status, err = determine_job_status(job, STOP, parent_status="waiting")
        assert status == "failed"

    def test_subjob_outage_with_coincident_error_still_pauses(self):
        # The redispatchable carve-out must admit a subjob outage freeze when
        # the parent is live — a teardown blip riding the freeze report must
        # not hard-fail the pause (docs/features/llm_outage_subjob_resilience.md).
        result = {"should_stop": True, "error": {"message": "SSH teardown blip"}}
        status, err = determine_job_status(
            _subjob_llm_job({}), result, parent_status="waiting"
        )
        assert (status, err) == ("paused", None)

    def test_subjob_outage_with_coincident_error_parent_terminal_fails(self):
        # Dead parent → no carve-out; the error fails the subjob as before.
        result = {"should_stop": True, "error": {"message": "boom"}}
        status, err = determine_job_status(
            _subjob_llm_job({}), result, parent_status="failed"
        )
        assert status == "failed"

    def test_subjob_without_freeze_keeps_pending_review_fallback(self):
        job = _subjob_llm_job({})
        job["freeze_data"] = {}
        status, err = determine_job_status(job, STOP, parent_status="waiting")
        assert status == "pending_review"

    def test_subjob_goal_achieved_without_freeze_completes(self):
        job = _subjob_llm_job({})
        job["freeze_data"] = {}
        status, err = determine_job_status(
            job, {"should_stop": True, "goal_achieved": True}, parent_status="waiting"
        )
        assert status == "completed"


# =============================================================================
# Determinism fingerprint — fail identical 4xx on the 2nd consecutive cycle
# (docs/features/outbound_message_hygiene.md, layer 3)
# =============================================================================

MINIMAX_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'bad_request_error', "
    "'message': 'invalid params, invalid function arguments json string, "
    "tool_call_id: call_E7U6VHuNDwmxi6Hl8jkjkrG8 (2013)', 'http_code': '400'}, "
    "'request_id': '06a12d1aed49504c11643e132559ac86'}"
)

# Summary of an edge-shaped failure (nginx HTML 404, 2026-07-17 MiniMax
# incident) as composed by the agent's _summarize_llm_error. Contains a 4xx
# token, so only the deterministic_exempt flag keeps it out of the fingerprint.
NGINX_404_SUMMARY = (
    "LLM endpoint returned HTTP 404 — non-API response from the provider edge "
    "(gateway/proxy); the request never reached the API. "
    "Detail: 404 Not Found 404 Not Found nginx"
)


class TestLlmOutageFingerprint:
    def test_deterministic_400_fingerprints(self):
        assert llm_outage_fingerprint({"error_summary": MINIMAX_400}) is not None

    def test_ids_and_numbers_normalized_away(self):
        other = (
            MINIMAX_400.replace(
                "call_E7U6VHuNDwmxi6Hl8jkjkrG8", "call_Zq9XkPl2MnB4vC7dW1aY5eR8"
            )
            .replace(
                "06a12d1aed49504c11643e132559ac86",
                "aaaabbbbccccddddeeeeffff00001111",
            )
            .replace("(2013)", "(999)")
        )
        assert llm_outage_fingerprint(
            {"error_summary": MINIMAX_400}
        ) == llm_outage_fingerprint({"error_summary": other})

    def test_different_error_types_fingerprint_differently(self):
        a = "Error code: 400 - {'error': {'type': 'bad_request_error', 'message': 'x'}}"
        b = "Error code: 400 - {'error': {'type': 'invalid_api_key_error', 'message': 'x'}}"
        assert llm_outage_fingerprint({"error_summary": a}) != llm_outage_fingerprint(
            {"error_summary": b}
        )

    def test_connection_error_is_not_fingerprinted(self):
        assert llm_outage_fingerprint({"error_summary": "Connection error."}) is None

    def test_rate_limit_texts_are_not_fingerprinted(self):
        assert (
            llm_outage_fingerprint(
                {"error_summary": "Error code: 429 - too many requests"}
            )
            is None
        )
        assert (
            llm_outage_fingerprint({"error_summary": "400: rate limit reached"}) is None
        )

    def test_5xx_is_not_fingerprinted(self):
        assert (
            llm_outage_fingerprint(
                {"error_summary": "Error code: 503 - upstream unavailable"}
            )
            is None
        )

    def test_deterministic_exempt_returns_none(self):
        # Without the flag this summary WOULD fingerprint (contains a 4xx
        # token) — the flag is what lets an infra-edge outage keep pausing.
        assert llm_outage_fingerprint({"error_summary": NGINX_404_SUMMARY}) is not None
        assert (
            llm_outage_fingerprint(
                {"error_summary": NGINX_404_SUMMARY, "deterministic_exempt": True}
            )
            is None
        )


class TestDeterminismFailFast:
    def _job_with_fp(self, fp, summary):
        ctx = _real_outage_ctx(1, 300, 60)
        ctx["llm_outage"]["fingerprint"] = fp
        job = _llm_job(ctx)
        job["freeze_data"]["error_summary"] = summary
        return job

    def test_repeat_identical_400_fails(self):
        fp = llm_outage_fingerprint({"error_summary": MINIMAX_400})
        status, err = determine_job_status(self._job_with_fp(fp, MINIMAX_400), STOP)
        assert status == "failed"
        assert "deterministic" in err.lower()

    def test_first_400_still_pauses(self):
        status, err = determine_job_status(self._job_with_fp(None, MINIMAX_400), STOP)
        assert status == "paused"
        assert err is None

    def test_different_400_after_first_pauses(self):
        fp = llm_outage_fingerprint({"error_summary": MINIMAX_400})
        other = (
            "Error code: 400 - {'error': {'type': 'bad_request_error', "
            "'message': 'image exceeds maximum allowed dimensions'}}"
        )
        status, err = determine_job_status(self._job_with_fp(fp, other), STOP)
        assert status == "paused"

    def test_identical_connection_error_streak_keeps_pausing(self):
        # A genuine outage repeats identical generic text across cycles —
        # it must NEVER trip the determinism fail-fast (pause-not-fail is
        # the outage feature's whole purpose).
        status, err = determine_job_status(
            self._job_with_fp(None, "Connection error."), STOP
        )
        assert status == "paused"

    def test_repeat_identical_edge_page_with_exempt_keeps_pausing(self):
        # Infra-edge freezes (non-API body, e.g. an nginx 404 page) set
        # deterministic_exempt: the identical summary across pause cycles
        # means the provider's gateway is still down, not that the request
        # is deterministic — replaying the 2026-07-17 ~10-min outage must
        # keep pausing instead of failing on the second cycle.
        fp = llm_outage_fingerprint({"error_summary": NGINX_404_SUMMARY})
        job = self._job_with_fp(fp, NGINX_404_SUMMARY)
        job["freeze_data"]["deterministic_exempt"] = True
        status, err = determine_job_status(job, STOP)
        assert status == "paused"
        assert err is None
