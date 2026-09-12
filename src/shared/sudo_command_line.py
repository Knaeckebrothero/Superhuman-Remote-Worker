"""The sudo command line as the gate evaluated it, for approval surfaces.

``sudo_approval_requests.command`` is only the resolved binary — for
``sudo -u agent-host bash --login -c '…'`` it is ``/bin/bash``, and the content
a human must judge lives in ``arguments``. Every surface where somebody decides
(MCP listing, cockpit card, notification body) renders this instead, so the
approver reads what ran rather than the name of a shell.

Shared because the MCP server may not import the orchestrator (import-linter
"Applications are independent"); both may import ``shared``.
"""

import shlex
from collections.abc import Iterable
from typing import Any, Optional


def render_sudo_command_line(
    command: Optional[str], arguments: Optional[Iterable[Any]] = None
) -> str:
    """Render ``command`` plus shell-quoted ``arguments`` on one line.

    Display only: the rules table and the metacharacter check keep matching the
    raw argv join, so existing fnmatch patterns are unaffected. argv[0] repeats
    the program name by convention (``bash --login`` under ``/bin/bash``); it is
    dropped when it adds nothing, and kept when it differs, because
    ``sudo -u root busybox sh`` must not hide which applet was asked for.
    """
    argv = [str(a) for a in (arguments or [])]
    binary = (command or "").strip()
    if argv and binary and argv[0] in (binary, binary.rsplit("/", 1)[-1]):
        argv = argv[1:]
    parts = ([binary] if binary else []) + [shlex.quote(a) for a in argv]
    return " ".join(parts)
