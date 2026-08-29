"""Tests for orchestrator/services/ssh_gateway_limits.py.

Covers the three abuse controls the gateway must provide because asyncssh
does not (pre-auth admission, per-connection channels, per-workspace
attachments), plus three properties a review found broken in the first cut:

- G18: every store must stay bounded under connections that are refused,
  released, or simply never seen again (background scan traffic) -- a
  defaultdict that never deletes a key is itself an unbounded-memory DoS.
- G19: a per-source pre-auth concurrency cap is required in addition to
  the global cap and the per-source rate, or a handful of sources can hold
  most of the global pool open indefinitely.
- G20: the 60-second rate-limit window must be testable, which means the
  clock has to be injectable rather than calling time.monotonic() directly.
"""

from collections import OrderedDict

from services.ssh_gateway_limits import GatewayLimiter


class FakeClock:
    """A manually-advanced stand-in for time.monotonic."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _CountingItemsView:
    """Wraps an OrderedDict's items() view to count how many (key, value)
    pairs are actually consumed during iteration.

    Exists to prove a reap loop's cost in operations rather than
    wall-clock time -- a wall-clock assertion would flake under CI load,
    per fix-round review finding (Critical)."""

    def __init__(self, view, counter_holder):
        self._view = view
        self._counter_holder = counter_holder

    def __iter__(self):
        for item in self._view:
            self._counter_holder.entries_inspected += 1
            yield item


class _CountingOrderedDict(OrderedDict):
    """An OrderedDict that counts every entry inspected via items()."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries_inspected = 0

    def items(self):
        return _CountingItemsView(super().items(), self)


def _limiter(**kw):
    defaults = dict(
        max_preauth_connections=3,
        preauth_rate_per_minute=5,
        max_channels_per_connection=2,
        max_attachments_per_workspace=2,
    )
    defaults.update(kw)
    return GatewayLimiter(**defaults)


def test_preauth_connections_are_capped():
    limiter = _limiter()
    assert [limiter.try_admit("1.2.3.4") for _ in range(4)] == [True, True, True, False]


def test_release_frees_a_slot():
    limiter = _limiter()
    for _ in range(3):
        limiter.try_admit("1.2.3.4")
    limiter.release("1.2.3.4")
    assert limiter.try_admit("1.2.3.4") is True


def test_rate_limit_is_per_ip():
    """One noisy source must not lock everyone else out."""
    limiter = _limiter(max_preauth_connections=100, preauth_rate_per_minute=2)
    assert limiter.try_admit("1.1.1.1") is True
    assert limiter.try_admit("1.1.1.1") is True
    assert limiter.try_admit("1.1.1.1") is False
    assert limiter.try_admit("2.2.2.2") is True


def test_channels_are_capped_per_connection():
    """300 channels on one connection would exhaust the workspace's
    MaxSessions 16 and present as workspace death."""
    limiter = _limiter()
    assert [limiter.try_open_channel("c1") for _ in range(3)] == [True, True, False]
    assert limiter.try_open_channel("c2") is True


def test_closing_a_channel_frees_the_slot():
    limiter = _limiter()
    limiter.try_open_channel("c1")
    limiter.try_open_channel("c1")
    limiter.close_channel("c1")
    assert limiter.try_open_channel("c1") is True


def test_attachments_are_capped_per_workspace():
    limiter = _limiter()
    assert [limiter.try_attach("w1") for _ in range(3)] == [True, True, False]


def test_detach_frees_an_attachment():
    limiter = _limiter()
    limiter.try_attach("w1")
    limiter.detach("w1")
    assert limiter.try_attach("w1") is True


def test_counters_never_go_negative():
    limiter = _limiter()
    limiter.release("1.2.3.4")
    limiter.close_channel("c1")
    limiter.detach("w1")
    assert limiter.try_admit("1.2.3.4") is True


# -- G18: the abuse-control module must not itself be an unbounded-memory
#    DoS. All three stores used to be defaultdicts that never deleted a
#    key, and defaultdict.__getitem__ creates the key on a mere *read* --
#    so even a refused probe, or a release()/close_channel()/detach() call
#    for an id that was never admitted, allocated a permanent entry. -----


def test_unmatched_release_close_and_detach_allocate_nothing():
    """release/close_channel/detach on an id that was never admitted must
    not leave a zero-valued entry behind -- that is itself the unbounded-
    memory bug this module exists to prevent, just relocated into it."""
    limiter = _limiter()
    limiter.release("1.2.3.4")
    limiter.close_channel("c1")
    limiter.detach("w1")
    assert limiter._preauth_by_source == {}
    assert limiter._channels == {}
    assert limiter._attachments == {}


def test_a_refused_admission_leaves_no_trace_of_the_refused_source():
    limiter = _limiter(max_preauth_connections=1, preauth_rate_per_minute=10)
    assert limiter.try_admit("1.1.1.1") is True
    assert limiter.try_admit("9.9.9.9") is False
    assert "9.9.9.9" not in limiter._recent
    assert "9.9.9.9" not in limiter._preauth_by_source


def test_admit_release_cycles_across_many_sources_leave_stores_empty():
    """N distinct sources connecting and disconnecting cleanly -- ordinary
    traffic, not just an attack -- must not accrete one entry per source
    forever."""
    limiter = _limiter(max_preauth_connections=50, preauth_rate_per_minute=50)
    sources = [f"10.0.0.{i}" for i in range(20)]
    for ip in sources:
        assert limiter.try_admit(ip) is True
        assert limiter.try_open_channel(ip) is True
        assert limiter.try_attach(ip) is True
    for ip in sources:
        limiter.release(ip)
        limiter.close_channel(ip)
        limiter.detach(ip)
    assert limiter._preauth_by_source == {}
    assert limiter._channels == {}
    assert limiter._attachments == {}


def test_rate_windows_do_not_accumulate_from_one_shot_sources():
    """Background scan traffic is characteristically many distinct source
    IPs that each connect once and never return. Reaping a window only
    when that SAME ip reconnects would not bound this at all -- reaping
    has to happen opportunistically across every tracked source."""
    clock = FakeClock(start=0.0)
    limiter = _limiter(
        max_preauth_connections=100, preauth_rate_per_minute=100, time_fn=clock
    )
    for i in range(25):
        limiter.try_admit(f"10.0.0.{i}")
    assert len(limiter._recent) == 25

    clock.advance(61.0)
    limiter.try_admit("192.0.2.1")  # any subsequent activity, from anyone

    assert set(limiter._recent) == {"192.0.2.1"}


# -- G19: per-source pre-auth concurrency cap. Without this, one source
#    connecting at its own permitted rate can sustain roughly
#    rate_per_minute/60 * login_timeout of the global pool by itself. ----


def test_one_noisy_source_cannot_consume_all_global_preauth_slots():
    limiter = _limiter(
        max_preauth_connections=64,
        preauth_rate_per_minute=60,
        max_preauth_connections_per_source=8,
    )
    admitted = [limiter.try_admit("6.6.6.6") for _ in range(20)]
    assert admitted.count(True) == 8
    # Plenty of the global pool is still free for someone else.
    assert limiter.try_admit("7.7.7.7") is True


def test_per_source_preauth_cap_has_a_default_below_the_global_cap():
    """Task 8 constructs GatewayLimiter with exactly these four keyword
    arguments and no per-source override, so the default must actually
    constrain a single source rather than merely equalling the global cap.

    preauth_rate_per_minute is set well above the number of attempts made
    here so the per-source *rate* cannot be what stops the 64th admission --
    if it were, this test would pass whether or not a per-source
    concurrency cap existed at all, and would prove nothing about it.
    """
    limiter = GatewayLimiter(
        max_preauth_connections=64,
        preauth_rate_per_minute=1000,
        max_channels_per_connection=12,
        max_attachments_per_workspace=4,
    )
    admitted = [limiter.try_admit("6.6.6.6") for _ in range(64)]
    assert admitted.count(True) == 16


def test_release_frees_a_per_source_preauth_slot():
    limiter = _limiter(max_preauth_connections_per_source=2)
    limiter.try_admit("1.2.3.4")
    limiter.try_admit("1.2.3.4")
    assert limiter.try_admit("1.2.3.4") is False
    limiter.release("1.2.3.4")
    assert limiter.try_admit("1.2.3.4") is True


# -- G20: the 60-second window must be testable, so the clock is
#    injectable. No test previously covered expiry at all. --------------


def test_rate_limit_entry_ages_out_of_the_window():
    clock = FakeClock(start=0.0)
    limiter = _limiter(
        max_preauth_connections=100, preauth_rate_per_minute=2, time_fn=clock
    )
    assert limiter.try_admit("1.1.1.1") is True
    assert limiter.try_admit("1.1.1.1") is True
    assert limiter.try_admit("1.1.1.1") is False

    clock.advance(61.0)

    assert limiter.try_admit("1.1.1.1") is True


def test_rate_limit_window_boundary_is_exclusive_at_exactly_60_seconds():
    """Pins the `> 60.0`, not `>= 60.0`, boundary: an entry exactly 60
    seconds old still counts against the source."""
    clock = FakeClock(start=0.0)
    limiter = _limiter(
        max_preauth_connections=100, preauth_rate_per_minute=1, time_fn=clock
    )
    assert limiter.try_admit("1.1.1.1") is True

    clock.advance(60.0)
    assert limiter.try_admit("1.1.1.1") is False  # exactly 60s old: still in window

    clock.advance(1e-6)
    assert limiter.try_admit("1.1.1.1") is True  # now strictly older than 60s


# -- Fix round 1: review found the G18 sweep itself was an O(N)-per-call
#    DoS (attacker-controlled N, paid even by refused attempts), and a
#    second uncovered path where an unmatched release() could forge a
#    global pre-auth slot (mutation M6 in the review's mutation testing
#    caught nothing). ------------------------------------------------


def test_reaping_does_not_scale_with_the_number_of_tracked_sources():
    """The first cut swept every tracked source on every try_admit call
    (O(N) per call, N attacker-controlled, paid even by refused attempts --
    measured by review at 8,000 tracked sources: 577us per call vs 0.6us
    baseline, a cheaper DoS than the unbounded-memory leak it replaced,
    since a connect-and-drop attacker holds no pre-auth slot and so is
    never throttled by any cap).

    The fix keeps `_recent` ordered by recency (move_to_end on each
    admission) so the front-eviction reap can stop at the first still-live
    entry instead of inspecting every tracked source. Proven here by
    counting dict entries actually inspected, not wall-clock time, since a
    timing assertion would flake under CI load.
    """
    clock = FakeClock(start=0.0)
    limiter = _limiter(
        max_preauth_connections=10_000, preauth_rate_per_minute=10_000, time_fn=clock
    )

    # 3 sources admitted now -- stale once the clock moves past 60s.
    for i in range(3):
        assert limiter.try_admit(f"10.0.0.{i}") is True

    clock.advance(55.0)

    # A large cohort admitted later: still fresh at measurement time, and
    # must never be inspected by the front-eviction reap.
    for i in range(5000):
        assert limiter.try_admit(f"10.1.0.{i}") is True

    limiter._recent = _CountingOrderedDict(limiter._recent)
    clock.advance(6.0)  # now: the first 3 are 61s old; the 5000 are 6s old

    assert limiter.try_admit("192.0.2.1") is True

    # Only the 3 now-stale entries, plus the one live entry the loop stops
    # at, may have been inspected -- nowhere near the ~5,003 tracked.
    assert limiter._recent.entries_inspected <= 10


def test_an_unmatched_release_cannot_forge_a_global_preauth_slot():
    """A release() for a source that was never admitted must not free a
    slot on the gateway-wide counter. Task 8's own brief flags a real
    double-release hazard (websocket.accept() can raise between try_admit
    and the surrounding try/finally); without this guard, that hazard --
    or a source spoofing repeated releases -- silently inflates the
    effective global cap."""
    limiter = _limiter(max_preauth_connections=2, preauth_rate_per_minute=10)
    assert limiter.try_admit("1.1.1.1") is True
    assert limiter.try_admit("2.2.2.2") is True
    assert limiter.try_admit("3.3.3.3") is False  # global cap (2) reached

    for _ in range(20):
        limiter.release("9.9.9.9")  # never admitted; must be a no-op

    assert limiter.try_admit("4.4.4.4") is False  # still full
