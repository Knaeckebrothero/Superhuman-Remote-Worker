"""Helpers for parsing git remote URLs.

Used to derive a filesystem-safe directory name from whatever URL the user
attached to a repository datasource, regardless of provider (GitHub, Gitea,
self-hosted GitLab, ...) or transport (https://, git@host:owner/repo,
ssh://git@host/owner/repo).
"""

from __future__ import annotations

import re


_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]+")


def repo_name_from_url(url: str, fallback: str = "repo") -> str:
    """Return the bare repository name from ``url``.

    Examples::

        repo_name_from_url("https://github.com/foo/bar.git")          # -> "bar"
        repo_name_from_url("git@github.com:foo/bar.git")              # -> "bar"
        repo_name_from_url("ssh://git@gitea.example.com/u/baz/")      # -> "baz"
        repo_name_from_url("https://example.com/group/sub/repo")      # -> "repo"

    Preserves the original case (GitHub names are case-sensitive in URLs).
    Replaces characters outside ``[A-Za-z0-9._-]`` with ``-`` and trims any
    leading/trailing separators. Returns ``fallback`` if the input is empty
    or yields nothing usable.
    """
    if not url or not isinstance(url, str):
        return fallback

    s = url.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]

    # Pick the last segment after the rightmost "/" or ":" — covers both
    # https://host/owner/repo and the SSH shorthand git@host:owner/repo.
    last_slash = s.rfind("/")
    last_colon = s.rfind(":")
    cut = max(last_slash, last_colon)
    if cut >= 0:
        s = s[cut + 1 :]

    if not s:
        return fallback

    cleaned = _DISALLOWED.sub("-", s).strip("-._")
    return cleaned or fallback


__all__ = ["repo_name_from_url"]
