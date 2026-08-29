"""Channel proxying for the SSH gateway.

Why this exists rather than asyncssh's own proxy mode, both reproduced on 2.24.0:

1. ``SSHClientConnection.forward_tunneled_session`` constructs
   ``SSHServerProcess(process_factory, None, MIN_SFTP_VERSION, False)``. That
   ``None`` is ``sftp_factory``, and ``SSHServerStreamSession.subsystem_requested``
   returns ``bool(self._sftp_factory)`` -- so the sftp subsystem is refused, and
   JetBrains Gateway cannot work through it at all.
2. It never calls ``process.exit()``. The downstream channel is never closed and
   ``ssh gateway some-command`` hangs forever with ``exit_status=None``.

``session_started`` must also be overridden: the stock implementation
special-cases sftp and runs a *local* sftp server instead of forwarding.
"""

from __future__ import annotations

import inspect

import asyncssh
from asyncssh.constants import EXTENDED_DATA_STDERR
from asyncssh.stream import SSHReader, SSHWriter

ALLOWED_SUBSYSTEMS = frozenset({"sftp"})

# Ruling G16: this isn't a resolved-target refusal (ssh_gateway_client's
# REFUSAL_MESSAGES table owns those, keyed by workspace state) -- it's a
# generic proxy-layer failure, so there is no per-state message to look up.
# 69 mirrors that table's own EX_UNAVAILABLE bucket ("broken or gone, not
# fixed by retrying alone"), the honest characterization of an upstream that
# refused to start a process at all.
UPSTREAM_FAILURE_EXIT_CODE = 69


class ProxyProcess(asyncssh.SSHServerProcess):
    """A server process whose streams are spliced to an upstream connection."""

    def subsystem_requested(self, subsystem: str) -> bool:
        return subsystem in ALLOWED_SUBSYSTEMS

    def session_started(self) -> None:
        # Binary, not UTF-8: session data is arbitrary bytes, and this also
        # disables asyncssh's line editor as a side effect.
        self._chan.set_encoding(None)
        self._encoding = None
        handler = self._start_process(
            SSHReader(self, self._chan),
            SSHWriter(self, self._chan),
            SSHWriter(self, self._chan, EXTENDED_DATA_STDERR),
        )
        if inspect.isawaitable(handler):
            self._conn.create_task(handler, self._chan.logger)


async def proxy_session(process, upstream) -> None:
    """Open the matching upstream process and mirror it back down.

    Exit status and exit signal are mirrored explicitly because asyncssh omits
    both when forwarding; without this the client never sees the channel close.
    """
    try:
        upstream_process = await upstream.create_process(
            command=process.command,
            subsystem=process.subsystem,
            env=process.env,
            term_type=process.term_type,
            term_size=process.term_size,
            term_modes=process.term_modes,
            # `errors` is meaningless once encoding is forced to None, and is
            # deliberately not passed (Ruling G17). That's only correct
            # *because* ProxyProcess.session_started above forces the
            # downstream channel binary first -- if a future edit ever stops
            # doing that, this hardcoded None must be revisited alongside it.
            encoding=None,
            stdin=process.stdin,
            stdout=process.stdout,
            stderr=process.stderr,
        )
    except Exception:
        # Ruling G16: asyncssh's own forwarder lets this escape uncaught, so
        # the downstream channel never closes and `ssh gw cmd` hangs forever
        # -- the exact bug item 2 above describes, reintroduced through the
        # error path instead of the happy path. The channel is binary
        # (encoding=None, forced above), so stderr takes bytes, not str.
        try:
            process.stderr.write(b"srw: failed to start the session on the workspace\n")
        except Exception:
            # The downstream client can disconnect in this same window --
            # SSHWriter.write raises BrokenPipeError once the channel has
            # left the 'open' state. Nobody is left to read this message
            # either way; what matters is that exit() below still runs.
            pass
        process.exit(UPSTREAM_FAILURE_EXIT_CODE)
        return

    await upstream_process.wait_closed()

    if upstream_process.exit_signal:
        process.exit_with_signal(*upstream_process.exit_signal)
    else:
        status = upstream_process.exit_status
        process.exit(status if status is not None else 0)
