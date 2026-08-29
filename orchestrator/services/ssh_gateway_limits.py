"""Abuse controls the gateway must provide because asyncssh does not.

OpenSSH ships MaxStartups, PerSourceMaxStartups, LoginGraceTime, MaxAuthTries
and MaxSessions. asyncssh ships none of them, which is why the claim that
this gateway "is just an sshd on the internet" is false until this module is
wired in. Measured on 2.24.0: 1000 silent pre-auth connections accepted in
0.3s, and 300 concurrent session channels on a single connection.

Deliberately in-process and single-event-loop: the gateway runs as a single
Deployment, and these are resource guards -- not a security boundary that
must survive a restart -- so no locking is used anywhere in this module. A
multi-replica gateway would need a shared backend (e.g. Redis) instead of
this class.

This module's own memory use is itself an abuse surface: every counting
structure below used to be a ``defaultdict``, and ``defaultdict.__getitem__``
creates its entry on a mere *read* -- so a refused admission, or a
release()/close_channel()/detach() call for an id that was never admitted,
would silently allocate a permanent zero-valued entry. On an internet-facing
gateway that receives constant background scan traffic from source IPs that
never come back, that grows forever. Every store here is a plain ``dict``
(``_recent`` is an ``OrderedDict``, for the recency-ordered eviction
``_reap_expired_front`` relies on -- see its docstring), and every
mutating method deletes a key the instant its count or window empties,
and never creates one for an admission it refused.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable


class GatewayLimiter:
    """In-process pre-auth, channel, and workspace-attachment abuse controls."""

    def __init__(
        self,
        max_preauth_connections: int,
        preauth_rate_per_minute: int,
        max_channels_per_connection: int,
        max_attachments_per_workspace: int,
        max_preauth_connections_per_source: int = 16,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        """
        max_preauth_connections_per_source bounds how many of the global
        pre-auth slots a single source IP may hold at once. Without it, one
        source connecting at its own full permitted rate can sustain
        roughly ``preauth_rate_per_minute/60 * login_timeout`` of the
        global pool by itself (e.g. ~20 of a 64-slot pool at the
        GatewayConfig defaults of rate=60/min and login_timeout=20s), so
        three or four such sources can starve everyone else. The default
        of 16 caps that at a quarter of the GatewayConfig default global
        pool (64): it takes at least four fully-saturated sources to
        exhaust the pool, instead of one, while still leaving headroom for
        a legitimate NAT'd office or CI fleet opening several connections
        at once. Callers that construct this without overriding the
        parameter (see Task 8) get this default.

        time_fn defaults to time.monotonic and exists so tests can inject
        a fake, advanceable clock -- the 60-second rate window is
        otherwise untestable without a real 60-second sleep. It is
        expected to be monotonically non-decreasing, like time.monotonic
        itself. Verified (not just assumed): a clock that jumps backwards
        does not corrupt state -- the `> 60.0` comparison simply stops
        finding anything old enough to expire, so affected sources stay
        rate-limited (fail closed) rather than being let through, and
        normal expiry resumes on its own once the clock catches back up.
        """
        self._max_preauth = max_preauth_connections
        self._max_preauth_per_source = max_preauth_connections_per_source
        self._rate = preauth_rate_per_minute
        self._max_channels = max_channels_per_connection
        self._max_attachments = max_attachments_per_workspace
        self._time_fn = time_fn

        self._preauth_total: int = 0
        self._preauth_by_source: dict[str, int] = {}
        self._recent: OrderedDict[str, deque[float]] = OrderedDict()
        self._channels: dict[str, int] = {}
        self._attachments: dict[str, int] = {}

    # -- pre-auth admission --------------------------------------------

    def try_admit(self, client_ip: str) -> bool:
        """Admit one pre-auth connection from client_ip, or refuse it.

        Three independent gates must all pass: this source's 60-second
        admission rate, this source's concurrent pre-auth count, and the
        gateway-wide concurrent pre-auth count. A refusal at any gate
        leaves no bookkeeping behind for an unrecognized client_ip.

        A refused attempt is never appended to its source's rate window.
        This is deliberate -- a refusal must not burn a legitimate
        client's own future admission budget -- but it also means a
        connect-and-drop source that never holds a pre-auth slot is never
        slowed by any gate here: the rate gate never records it, and the
        concurrency gates only bind on connections that stay open. That
        traffic shape is exactly what made the reap cost in
        `_reap_expired_front` worth keeping cheap; see its docstring.
        """
        now = self._time_fn()

        # Trim only THIS source's own expired timestamps first. Bounded by
        # preauth_rate_per_minute (a small constant, e.g. 60), not by how
        # many other sources are tracked -- cheap no matter what
        # _reap_expired_front below costs.
        window = self._recent.get(client_ip)
        if window is not None:
            while window and now - window[0] > 60.0:
                window.popleft()
            if not window:
                del self._recent[client_ip]
                window = None

        self._reap_expired_front(now)

        rate_count = len(window) if window is not None else 0
        if rate_count >= self._rate:
            return False
        if self._preauth_by_source.get(client_ip, 0) >= self._max_preauth_per_source:
            return False
        if self._preauth_total >= self._max_preauth:
            return False

        if window is None:
            window = self._recent[client_ip] = deque()
        window.append(now)
        self._recent.move_to_end(client_ip)
        self._preauth_by_source[client_ip] = (
            self._preauth_by_source.get(client_ip, 0) + 1
        )
        self._preauth_total += 1
        return True

    def release(self, client_ip: str) -> None:
        """Release one pre-auth slot previously admitted for client_ip.

        A release for a source with no tracked slot (a double release, or
        a release with no matching try_admit) is a no-op: it must not
        create a new entry, and must not free a gateway-wide slot that
        this client_ip was never charged for. This guard is load-bearing,
        not defensive filler: Task 8 has a real hazard where
        websocket.accept() can raise between try_admit and the
        surrounding try/finally, producing exactly this kind of unmatched
        release, and removing the guard would let that hazard (or a
        source simply calling release() speculatively) silently inflate
        the effective global cap. Covered by
        test_an_unmatched_release_cannot_forge_a_global_preauth_slot.
        """
        count = self._preauth_by_source.get(client_ip)
        if not count:
            return
        if count <= 1:
            del self._preauth_by_source[client_ip]
        else:
            self._preauth_by_source[client_ip] = count - 1
        self._preauth_total = max(0, self._preauth_total - 1)

    def _reap_expired_front(self, now: float) -> None:
        """Evict fully-expired sources from the front of `_recent`.

        `_recent` is an OrderedDict kept in recency order: every
        successful admission calls `move_to_end(client_ip)`, so the front
        is always the least-recently-admitted source and the back is the
        most recent. That ordering is what makes this cheap: as long as
        the front entry's *newest* timestamp is still within the
        60-second window, every entry behind it is at least as recent, so
        it must be within the window too, and the loop can stop without
        looking at it. It never inspects a live entry, let alone all of
        them.

        A prior version of this comment claimed the equivalent full-scan
        sweep was "acceptable... for a resource guard that is explicitly
        not a hot request-per-second path." That was false and is exactly
        what a review measured: try_admit runs on every inbound TCP
        connection to an internet-facing SSH listener, which is precisely
        an attacker-controlled request-per-second path, and the old
        unconditional per-call scan over every tracked source was 970x
        slower at 8,000 tracked sources than at zero -- a cheaper DoS than
        the unbounded-memory leak it replaced, since a connect-and-drop
        source holds no slot and so was never throttled by any cap (see
        try_admit's docstring). This version's cost is bounded by how many
        entries this call actually evicts, not by how many are tracked:
        each entry is created once and evicted at most once over its
        lifetime, so total eviction work across the module's lifetime is
        bounded by total entries ever created -- amortized O(1) per
        try_admit call, though any single call landing right after a
        large cohort ages out will do work proportional to that cohort,
        not to unrelated still-live entries.
        `test_reaping_does_not_scale_with_the_number_of_tracked_sources`
        verifies the stop-at-the-first-live-entry behavior directly, by
        counting dict entries inspected rather than wall-clock time (a
        timing assertion would flake under CI load).
        """
        while self._recent:
            oldest_ip, oldest_window = next(iter(self._recent.items()))
            if now - oldest_window[-1] > 60.0:
                del self._recent[oldest_ip]
            else:
                break

    # -- channels ---------------------------------------------------------

    def try_open_channel(self, connection_id: str) -> bool:
        count = self._channels.get(connection_id, 0)
        if count >= self._max_channels:
            return False
        self._channels[connection_id] = count + 1
        return True

    def close_channel(self, connection_id: str) -> None:
        count = self._channels.get(connection_id)
        if not count:
            return
        if count <= 1:
            del self._channels[connection_id]
        else:
            self._channels[connection_id] = count - 1

    # -- workspace attachments ---------------------------------------------

    def try_attach(self, workspace_id: str) -> bool:
        count = self._attachments.get(workspace_id, 0)
        if count >= self._max_attachments:
            return False
        self._attachments[workspace_id] = count + 1
        return True

    def detach(self, workspace_id: str) -> None:
        count = self._attachments.get(workspace_id)
        if not count:
            return
        if count <= 1:
            del self._attachments[workspace_id]
        else:
            self._attachments[workspace_id] = count - 1
