"""Unit tests for LLM-outage pause + backoff re-dispatch.

Covers the pure orchestrator-side logic added by
knowledge-base/knowledge/features/llm_outage_pause_and_backoff_redispatch.md:

* evaluate_llm_outage — the auto-reset + give-up-ceiling decision
* llm_outage_backoff_seconds — Full/Equal jitter bounds + Retry-After floor
* determine_job_status — the llm_unavailable -> paused/failed mapping
"""

from datetime import datetime, timedelta, timezone

from orchestrator.services.completion import (  # noqa: E402
    LLM_OUTAGE_BACKOFF_CAP_SECONDS,
    LLM_OUTAGE_CEILING_SECONDS,
    LLM_OUTAGE_MAX_ATTEMPTS,
    LLM_OUTAGE_REPEAT_CEILING,
    LLM_OUTAGE_SHAPE_NUDGE,
    LLM_OUTAGE_RESET_WINDOW_SECONDS,
    MEMORY_RETRY_CAP,
    determine_job_status,
    evaluate_llm_outage,
    llm_outage_backoff_seconds,
    llm_outage_fingerprint,
    llm_outage_nudge_state,
    llm_outage_repeat_key,
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

    # --- anchored reset (knowledge-base/knowledge/features/llm_cooldown_pause_and_resume.md) --------
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
# (knowledge-base/knowledge/features/llm_outage_subjob_resilience.md)
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
        # not hard-fail the pause (knowledge-base/knowledge/features/llm_outage_subjob_resilience.md).
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
# (knowledge-base/knowledge/features/outbound_message_hygiene.md, layer 3)
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


# Verbatim freeze summary from job d251e513 (2026-07-29). The Codex proxy's
# sole auth entry flipped to `status: error` after an upstream 408 stream drop,
# so every retry bounced off an instant 503. Identical on all 13 re-dispatches.
CODEX_503 = (
    "Error code: 503 - {'error': {'message': 'auth_unavailable: no auth "
    "available (providers=codex, model=gpt-5.6-sol)', 'type': 'server_error', "
    "'code': 'internal_server_error'}}"
)


class TestLlmOutageRepeatKey:
    def test_5xx_gets_a_repeat_key(self):
        # The strict fingerprint deliberately skips 5xx; the repeat key must
        # not, or a deterministic 503 rides the ceiling forever.
        assert llm_outage_fingerprint({"error_summary": CODEX_503}) is None
        assert llm_outage_repeat_key({"error_summary": CODEX_503}) is not None

    def test_connection_error_gets_a_repeat_key(self):
        assert llm_outage_repeat_key({"error_summary": "Connection error."}) is not None

    def test_digits_normalized_away(self):
        # Version/counter digits must not split the streak.
        other = CODEX_503.replace("503", "500").replace("gpt-5.6-sol", "gpt-9.9-sol")
        assert llm_outage_repeat_key({"error_summary": CODEX_503}) == (
            llm_outage_repeat_key({"error_summary": other})
        )

    def test_long_ids_normalized_away(self):
        # NB: no .format() — the summary carries literal `{'error': ...}` braces.
        base = CODEX_503 + " request_id: "
        a = base + "06a12d1aed49504c11643e132559ac86"
        b = base + "aaaabbbbccccddddeeeeffff00001111"
        assert llm_outage_repeat_key({"error_summary": a}) == (
            llm_outage_repeat_key({"error_summary": b})
        )

    def test_model_name_is_semantic_not_volatile(self):
        # A short model slug survives normalization, so a different model is a
        # different error — switching models must restart the streak.
        other = CODEX_503.replace("gpt-5.6-sol", "some-other-model")
        assert llm_outage_repeat_key({"error_summary": CODEX_503}) != (
            llm_outage_repeat_key({"error_summary": other})
        )

    def test_different_errors_key_differently(self):
        assert llm_outage_repeat_key({"error_summary": CODEX_503}) != (
            llm_outage_repeat_key({"error_summary": "Connection error."})
        )

    def test_rate_limit_text_excluded(self):
        assert (
            llm_outage_repeat_key({"error_summary": "Error code: 429 - slow down"})
            is None
        )

    def test_deterministic_exempt_excluded(self):
        assert (
            llm_outage_repeat_key(
                {"error_summary": NGINX_404_SUMMARY, "deterministic_exempt": True}
            )
            is None
        )

    def test_no_text_returns_none(self):
        assert llm_outage_repeat_key({}) is None


class TestRepeatGiveUp:
    """A 5xx that survives N identical backoff cycles is not an outage."""

    # nudge_attempted defaults True so these exercise the give-up itself; the
    # one-extra-cycle quickfix that precedes it has its own class below.
    def _job(self, summary, *, repeats, key=None, extra=None, nudge_attempted=True):
        ctx = _real_outage_ctx(repeats or 1, 300, 60)
        ctx["llm_outage"]["repeat_key"] = (
            key
            if key is not None
            else llm_outage_repeat_key({"error_summary": summary})
        )
        ctx["llm_outage"]["repeats"] = repeats
        ctx["llm_outage"]["shape_nudge_attempted"] = nudge_attempted
        job = _llm_job(ctx)
        job["freeze_data"]["error_summary"] = summary
        job["freeze_data"].update(extra or {})
        return job

    def test_under_ceiling_keeps_pausing(self):
        job = self._job(CODEX_503, repeats=LLM_OUTAGE_REPEAT_CEILING - 2)
        status, err = determine_job_status(job, STOP)
        assert status == "paused"
        assert err is None

    def test_at_ceiling_fails(self):
        # prior repeats + this pause == the ceiling.
        job = self._job(CODEX_503, repeats=LLM_OUTAGE_REPEAT_CEILING - 1)
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert str(LLM_OUTAGE_REPEAT_CEILING) in err
        assert "Admin" in err  # actionable operator pointer

    def test_first_failure_pauses(self):
        job = self._job(CODEX_503, repeats=0, key=None)
        job["context"]["llm_outage"]["repeat_key"] = None
        status, err = determine_job_status(job, STOP)
        assert status == "paused"

    def test_different_error_breaks_the_streak(self):
        # Prior streak was at the ceiling, but this pause carries a DIFFERENT
        # error — the streak restarts rather than inheriting the give-up.
        job = self._job(
            "Connection error.",
            repeats=LLM_OUTAGE_REPEAT_CEILING - 1,
            key=llm_outage_repeat_key({"error_summary": CODEX_503}),
        )
        status, err = determine_job_status(job, STOP)
        assert status == "paused"

    def test_exempt_edge_outage_never_gives_up(self):
        # A provider gateway that is still down must ride the ceiling, however
        # many identical cycles it takes — that is the feature's whole purpose.
        job = self._job(
            NGINX_404_SUMMARY,
            repeats=LLM_OUTAGE_REPEAT_CEILING + 5,
            key=llm_outage_repeat_key({"error_summary": NGINX_404_SUMMARY}),
            extra={"deterministic_exempt": True},
        )
        status, err = determine_job_status(job, STOP)
        assert status == "paused"

    def test_rate_limit_streak_never_gives_up(self):
        job = self._job(
            "Error code: 429 - too many requests",
            repeats=LLM_OUTAGE_REPEAT_CEILING + 5,
            key="whatever",
        )
        status, err = determine_job_status(job, STOP)
        assert status == "paused"

    def test_message_prefers_the_initial_error(self):
        # The retry storm's LAST error is a symptom; the FIRST is the cause.
        job = self._job(
            CODEX_503,
            repeats=LLM_OUTAGE_REPEAT_CEILING - 1,
            extra={
                "initial_error_summary": "Error code: 408 - stream disconnected",
                "initial_classification": "transient",
            },
        )
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert "408" in err
        assert "stream disconnected" in err

    def test_duration_ceiling_still_wins_first(self):
        # The existing ceiling check runs before the repeat check; a job past
        # both must report the ceiling reason, not the repeat reason.
        job = self._job(CODEX_503, repeats=LLM_OUTAGE_REPEAT_CEILING - 1)
        job["context"] = _real_outage_ctx(8, LLM_OUTAGE_CEILING_SECONDS + 3600, 60)
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert "continuous outage" in err


class TestShapeNudgeQuickfix:
    """TEMPORARY — delete with knowledge-history/done/codex_stream_disconnect_shape_nudge.md.

    Upstream rejects a byte-identical payload deterministically, so the repeat
    give-up must spend exactly ONE more cycle on a shape change before it fires.
    """

    def _job(self, *, repeats, nudge_attempted):
        ctx = _real_outage_ctx(repeats, 300, 60)
        ctx["llm_outage"]["repeat_key"] = llm_outage_repeat_key(
            {"error_summary": CODEX_503}
        )
        ctx["llm_outage"]["repeats"] = repeats
        if nudge_attempted is not None:
            ctx["llm_outage"]["shape_nudge_attempted"] = nudge_attempted
        job = _llm_job(ctx)
        job["freeze_data"]["error_summary"] = CODEX_503
        return job

    def test_ceiling_first_hit_pauses_for_the_nudge(self):
        if not LLM_OUTAGE_SHAPE_NUDGE:
            return
        job = self._job(repeats=LLM_OUTAGE_REPEAT_CEILING - 1, nudge_attempted=False)
        status, err = determine_job_status(job, STOP)
        assert status == "paused"
        assert err is None

    def test_legacy_state_without_the_flag_also_pauses(self):
        # Jobs paused before this shipped have no shape_nudge_attempted key.
        if not LLM_OUTAGE_SHAPE_NUDGE:
            return
        job = self._job(repeats=LLM_OUTAGE_REPEAT_CEILING - 1, nudge_attempted=None)
        status, _ = determine_job_status(job, STOP)
        assert status == "paused"

    def test_after_the_nudge_it_gives_up(self):
        # The nudge is strictly one-shot: a second identical failure fails.
        job = self._job(repeats=LLM_OUTAGE_REPEAT_CEILING, nudge_attempted=True)
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert "nudge was already tried" in err

    def test_give_up_message_omits_nudge_note_when_not_tried(self):
        # Only reachable with the quickfix disabled — the message must not claim
        # a nudge happened when it did not.
        job = self._job(repeats=LLM_OUTAGE_REPEAT_CEILING, nudge_attempted=False)
        status, err = determine_job_status(job, STOP)
        if LLM_OUTAGE_SHAPE_NUDGE:
            assert status == "paused"
        else:
            assert status == "failed"
            assert "nudge was already tried" not in err

    def test_nudge_does_not_extend_the_duration_ceiling(self):
        # The 12h ceiling outranks the quickfix — an armed nudge must not keep a
        # genuinely dead job alive past it.
        job = self._job(repeats=LLM_OUTAGE_REPEAT_CEILING - 1, nudge_attempted=False)
        job["context"] = _real_outage_ctx(8, LLM_OUTAGE_CEILING_SECONDS + 3600, 60)
        status, err = determine_job_status(job, STOP)
        assert status == "failed"
        assert "continuous outage" in err


class TestShapeNudgeLatch:
    """TEMPORARY — the arm/latch state machine behind the shape nudge.

    Lives in the DB write path (increment_job_llm_outage_attempt), which has no
    test coverage of its own, so the logic is extracted and pinned here. The
    latch is the safety property: without it every cycle re-arms and the job
    nudges forever — the exact grinding the repeat ceiling exists to stop.
    """

    CEIL = 4

    def test_below_ceiling_does_not_arm(self):
        for n in range(1, self.CEIL):
            assert llm_outage_nudge_state({}, n, self.CEIL) == (False, False)

    def test_arms_once_at_the_ceiling(self):
        assert llm_outage_nudge_state({}, self.CEIL, self.CEIL) == (True, True)

    def test_latches_so_the_next_cycle_gives_up(self):
        prior = {"shape_nudge_attempted": True}
        pending, attempted = llm_outage_nudge_state(prior, self.CEIL + 1, self.CEIL)
        assert pending is False  # <- the anti-forever-loop property
        assert attempted is True  # <- keeps determine_job_status on the fail path

    def test_stays_latched_for_the_whole_streak(self):
        prior = {"shape_nudge_attempted": True}
        for n in range(self.CEIL + 1, self.CEIL + 8):
            assert llm_outage_nudge_state(prior, n, self.CEIL) == (False, True)

    def test_streak_reset_clears_the_latch(self):
        # repeats == 1 is a fresh streak (different error, or a productive gap):
        # a future unrelated outage deserves its own nudge.
        prior = {"shape_nudge_attempted": True}
        assert llm_outage_nudge_state(prior, 1, self.CEIL) == (False, False)
        assert llm_outage_nudge_state({}, self.CEIL, self.CEIL) == (True, True)

    def test_disabled_never_arms(self):
        for n in range(1, self.CEIL + 5):
            assert llm_outage_nudge_state({}, n, None) == (False, False)

    def test_disabled_preserves_a_prior_latch(self):
        # Flipping the kill switch mid-streak must not re-open the nudge.
        prior = {"shape_nudge_attempted": True}
        assert llm_outage_nudge_state(prior, self.CEIL + 1, None) == (False, True)

    def test_matches_the_ceiling_actually_configured(self):
        # Guards against the arming point drifting away from the give-up point.
        assert llm_outage_nudge_state(
            {}, LLM_OUTAGE_REPEAT_CEILING, LLM_OUTAGE_REPEAT_CEILING
        ) == (
            True,
            True,
        )


class TestBornParkedShape:
    """A born-parked loop member's context.llm_outage carries only
    {attempt: 0, next_retry_at} — evaluate_llm_outage must anchor the outage
    at the wake instant, not at creation
    (knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md)."""

    def test_born_parked_shape_survives_wake(self):
        ctx = {
            "llm_outage": {
                "attempt": 0,
                "next_retry_at": NOW.isoformat(),
            }
        }
        ev = evaluate_llm_outage(ctx, NOW + timedelta(seconds=30))
        assert ev["over_ceiling"] is False
        assert ev["attempt"] == 0
        assert ev["first_failed_at"] == NOW + timedelta(seconds=30)
