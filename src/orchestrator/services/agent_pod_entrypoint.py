"""The agent pod's shell entrypoint and the one caller-controlled value in it.

Both pod provisioners (``agent_provisioner`` for jobs and sessions,
``persistent_provisioner`` for dedicated persistent sessions) start the agent
through ``sh -c "exec python agent.py ..."``: the shell keeps the entrypoint
construction shared with older manifests, and ``exec`` makes python — not
``sh`` — PID 1 so the kubelet's SIGTERM reaches the drain handler.
``config_name`` lands on that command line from the job/thread creation APIs,
i.e. from the caller, and the pod carries the platform Secret via ``envFrom``.
Security audit 2026-08-27, finding #3: it used to be f-spliced unquoted, which
was command execution with every platform key.

Two independent guards live here so the two provisioners cannot drift apart:

* :func:`validate_config_name` — the ONE allow-list for the value. Each
  provisioner applies it at its boundary, before any pod spec exists, and the
  manifest builders apply it again at the sink. A bundled config selector is
  a bare name (``scholar``), a role base (``worker_base``) or a relative YAML
  path (``config/experts/scholar/config.yaml``); nothing else has a reason to
  reach a pod.
* :func:`agent_exec_command` — the entrypoint is built from an argv list and
  shell-quoted with :mod:`shlex`, so even a value that somehow bypassed the
  allow-list is one argument to python, never shell syntax.

Pure stdlib, no I/O — importable by both provisioners without adding a
provisioner-to-provisioner import.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

CONFIG_NAME_MAX_LENGTH = 200

# Bare names, role bases and relative paths to bundled YAML. Deliberately no
# whitespace, quotes, shell metacharacters, backslash or non-ASCII: none of
# the bundled selectors need them and every injection needs at least one.
_CONFIG_NAME_CHARS = re.compile(r"[A-Za-z0-9._/-]+")


class InvalidConfigNameError(ValueError):
    """``config_name`` is not a bundled-config selector we will hand a pod.

    A ``ValueError`` so callers that already treat bad provisioner input as a
    value error (``PERSISTENT_AGENT_IMAGE_PULL_POLICY``) need no new branch.
    """


def validate_config_name(config_name: str | None) -> str | None:
    """Return *config_name* unchanged when it is a safe config selector.

    ``None`` and ``""`` pass through untouched so each provisioner keeps its
    default (the role's base config). Anything else must be a ``str`` of at
    most :data:`CONFIG_NAME_MAX_LENGTH` characters drawn from
    ``[A-Za-z0-9._/-]``, must not start with ``-`` (argparse would read it as
    a flag) and every ``/``-separated segment must be non-empty and not
    ``..`` — no absolute paths, no ``//``, no trailing ``/``, no traversal out
    of the config tree.

    Raises:
        InvalidConfigNameError: naming the violated rule. The value is echoed
            truncated so the log line cannot itself become unwieldy.
    """
    if config_name is None or config_name == "":
        return config_name
    if not isinstance(config_name, str):
        raise InvalidConfigNameError(
            f"config_name must be a string, got {type(config_name).__name__}"
        )
    shown = config_name[:64] + ("..." if len(config_name) > 64 else "")
    if len(config_name) > CONFIG_NAME_MAX_LENGTH:
        raise InvalidConfigNameError(
            f"config_name is {len(config_name)} characters, the limit is "
            f"{CONFIG_NAME_MAX_LENGTH}: {shown!r}"
        )
    if not _CONFIG_NAME_CHARS.fullmatch(config_name):
        raise InvalidConfigNameError(
            "config_name may only contain letters, digits, '.', '_', '/' "
            f"and '-': {shown!r}"
        )
    if config_name.startswith("-"):
        raise InvalidConfigNameError(f"config_name must not start with '-': {shown!r}")
    for segment in config_name.split("/"):
        if segment == "":
            raise InvalidConfigNameError(
                "config_name must be a relative path without an empty "
                f"segment: {shown!r}"
            )
        if segment == "..":
            raise InvalidConfigNameError(
                f"config_name must not contain a '..' segment: {shown!r}"
            )
    return config_name


def agent_exec_command(argv: Iterable[object]) -> list[str]:
    """Return the ``["sh", "-c", "exec <argv>"]`` entrypoint for an agent pod.

    Every element of *argv* is one argument to the program, quoted by
    :func:`shlex.join`, so the string parses back (``shlex.split``) to exactly
    ``["exec", *argv]``. ``exec`` is left bare on purpose: it is the shell
    builtin that replaces ``sh`` with python as PID 1.
    """
    return ["sh", "-c", "exec " + shlex.join(str(word) for word in argv)]
