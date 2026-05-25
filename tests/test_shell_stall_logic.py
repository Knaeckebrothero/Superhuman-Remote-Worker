"""Pure-logic tests for the shared shell no-change/stall detection helper.

These are deliberately tmux-free so they run in CI even where the tmux binary
is unavailable. They pin the behaviour that both shell backends
(`shell_manager.run_sync` and `remote.shell_run`) share via
`compute_no_change_state`, including the regression that the OLD code only
hashed the last 20 lines (so output scrolling above the visible tail was
mistaken for a stall).
"""

from src.tools.shell.shell_manager import (
    NO_CHANGE_TIMEOUT_SECONDS,
    compute_no_change_state,
)


def test_default_no_change_threshold_is_generous():
    # 30s matches OpenHands; the old value (5s) false-fired on heavy installs.
    assert NO_CHANGE_TIMEOUT_SECONDS == 30.0


def test_first_observation_never_times_out():
    new_hash, stall_start, timed_out = compute_no_change_state(
        ["a", "b"],
        prev_hash=None,
        stall_start=None,
        now=100.0,
        soft_enabled=True,
        threshold=30.0,
    )
    assert timed_out is False
    assert stall_start is None  # vs None prev_hash counts as "changed"
    assert new_hash == hash(("a", "b"))


def test_change_resets_stall_clock():
    new_hash, stall_start, timed_out = compute_no_change_state(
        ["a", "b"],
        prev_hash=hash(("a",)),
        stall_start=50.0,
        now=100.0,
        soft_enabled=True,
        threshold=30.0,
    )
    assert timed_out is False
    assert stall_start is None  # changed -> clock reset
    assert new_hash == hash(("a", "b"))


def test_unchanged_starts_clock_then_times_out():
    lines = ["a", "b"]
    h = hash(tuple(lines))
    # First unchanged observation: clock starts, not yet timed out.
    h1, ss1, t1 = compute_no_change_state(
        lines,
        prev_hash=h,
        stall_start=None,
        now=100.0,
        soft_enabled=True,
        threshold=30.0,
    )
    assert t1 is False
    assert ss1 == 100.0
    # Still unchanged once the threshold has elapsed -> timed out.
    _, ss2, t2 = compute_no_change_state(
        lines,
        prev_hash=h1,
        stall_start=ss1,
        now=130.0,
        soft_enabled=True,
        threshold=30.0,
    )
    assert t2 is True
    assert ss2 == 100.0


def test_just_under_threshold_not_timed_out():
    lines = ["x"]
    h = hash(tuple(lines))
    _, _, timed_out = compute_no_change_state(
        lines,
        prev_hash=h,
        stall_start=100.0,
        now=129.9,
        soft_enabled=True,
        threshold=30.0,
    )
    assert timed_out is False


def test_soft_disabled_never_times_out():
    lines = ["x"]
    h = hash(tuple(lines))
    _, stall_start, timed_out = compute_no_change_state(
        lines,
        prev_hash=h,
        stall_start=100.0,
        now=10_000.0,
        soft_enabled=False,
        threshold=30.0,
    )
    assert timed_out is False
    # Clock is still tracked so the value is stable, it just never fires.
    assert stall_start == 100.0


def test_change_outside_last_20_lines_is_detected():
    """Regression: the old code hashed only all_lines[-20:], so a change above
    the visible tail (e.g. a long pip download scrolling) looked like a stall.
    The full-buffer hash must treat it as a change and reset the clock."""
    base = [f"line{i}" for i in range(40)]
    changed = ["CHANGED"] + base[1:]  # differs only at index 0 (scrolled off tail)
    _, stall_start, timed_out = compute_no_change_state(
        changed,
        prev_hash=hash(tuple(base)),
        stall_start=50.0,
        now=100.0,
        soft_enabled=True,
        threshold=30.0,
    )
    assert stall_start is None  # detected as change -> reset, NOT a stall
    assert timed_out is False
