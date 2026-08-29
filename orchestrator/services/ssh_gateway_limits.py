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
never come back, that grows forever. Every store here is a plain ``dict``,
and every mutating method deletes a key the instant its count or window
empties, and never creates one for an admission it refused.
"""

from __future__ import annotations

import time
from collections import deque
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
        otherwise untestable without a real 60-second sleep.
        """
        self._max_preauth = max_preauth_connections
        self._max_preauth_per_source = max_preauth_connections_per_source
        self._rate = preauth_rate_per_minute
        self._max_channels = max_channels_per_connection
        self._max_attachments = max_attachments_per_workspace
        self._time_fn = time_fn

        self._preauth_total: int = 0
        self._preauth_by_source: dict[str, int] = {}
        self._recent: dict[str, deque[float]] = {}
        self._channels: dict[str, int] = {}
        self._attachments: dict[str, int] = {}

    # -- pre-auth admission --------------------------------------------

    def try_admit(self, client_ip: str) -> bool:
        """Admit one pre-auth connection from client_ip, or refuse it.

        Three independent gates must all pass: this source's 60-second
        admission rate, this source's concurrent pre-auth count, and the
        gateway-wide concurrent pre-auth count. A refusal at any gate
        leaves no bookkeeping behind for an unrecognized client_ip.
        """
        now = self._time_fn()
        self._reap_expired_windows(now)

        if len(self._recent.get(client_ip, ())) >= self._rate:
            return False
        if self._preauth_by_source.get(client_ip, 0) >= self._max_preauth_per_source:
            return False
        if self._preauth_total >= self._max_preauth:
            return False

        window = self._recent.get(client_ip)
        if window is None:
            window = self._recent[client_ip] = deque()
        window.append(now)
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
        this client_ip was never charged for.
        """
        count = self._preauth_by_source.get(client_ip)
        if not count:
            return
        if count <= 1:
            del self._preauth_by_source[client_ip]
        else:
            self._preauth_by_source[client_ip] = count - 1
        self._preauth_total = max(0, self._preauth_total - 1)

    def _reap_expired_windows(self, now: float) -> None:
        """Drop every source's rate-limit window once it empties.

        try_admit is the only method invoked for connections that never
        complete auth, so it is the only place background scan traffic --
        many distinct source IPs, most never seen again -- actually
        touches this module. Purging only the *current caller's* own
        window would leave one permanent entry behind per one-shot
        scanning source; sweeping every tracked window here instead
        bounds ``_recent`` to "sources seen within the trailing 60
        seconds," which is the bound the rate limiter is already meant to
        enforce. Cost is O(distinct recent sources) per admission
        attempt -- bounded by real incoming connections, not by all-time
        scanning history, which is acceptable for a resource guard that
        is explicitly not a hot request-per-second path.
        """
        stale = []
        for ip, window in self._recent.items():
            while window and now - window[0] > 60.0:
                window.popleft()
            if not window:
                stale.append(ip)
        for ip in stale:
            del self._recent[ip]

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
