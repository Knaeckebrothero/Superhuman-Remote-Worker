"""Tests for :mod:`src.utils.git_url`.

The helper picks the bare repository name from whatever URL the user pasted
into the datasource form, regardless of provider or transport. Used at clone
time so the workspace gets ``repos/<upstream-name>`` instead of
``repos/read-only-version-of-...``.
"""

from __future__ import annotations

import pytest

from src.utils.git_url import repo_name_from_url


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://github.com/Knaeckebrothero/Superhuman-Remote-Worker.git",
            "Superhuman-Remote-Worker",
        ),
        ("https://github.com/foo/bar.git", "bar"),
        ("https://github.com/foo/bar", "bar"),
        ("https://github.com/foo/bar/", "bar"),
        ("https://github.com/foo/bar.git/", "bar"),
        # SSH shorthand (git@host:owner/repo)
        ("git@github.com:foo/bar.git", "bar"),
        ("git@gitea.example.com:user/baz.git", "baz"),
        # ssh:// transport
        ("ssh://git@github.com/foo/bar.git", "bar"),
        ("ssh://git@gitea.h4ll.app:2222/u/baz.git", "baz"),
        # Nested groups (GitLab)
        ("https://gitlab.com/group/subgroup/project.git", "project"),
        # Bare names (not really URLs, but should still degrade gracefully)
        ("just-a-name", "just-a-name"),
        ("repo.git", "repo"),
        # Mixed-case preserved
        ("https://github.com/Org/MixedCase-Repo.git", "MixedCase-Repo"),
    ],
)
def test_extracts_repo_name(url, expected):
    assert repo_name_from_url(url) == expected


class TestFallback:
    def test_empty_string_uses_fallback(self):
        assert repo_name_from_url("") == "repo"
        assert repo_name_from_url("", fallback="my-ds") == "my-ds"

    def test_none_uses_fallback(self):
        assert repo_name_from_url(None) == "repo"  # type: ignore[arg-type]

    def test_unparseable_uses_fallback(self):
        # No path component left after stripping — falls back.
        assert repo_name_from_url("/", fallback="x") == "x"
        assert repo_name_from_url("///", fallback="x") == "x"

    def test_non_string_uses_fallback(self):
        assert repo_name_from_url(12345, fallback="num") == "num"  # type: ignore[arg-type]


class TestSanitization:
    def test_disallowed_chars_replaced(self):
        # Spaces and other filesystem-hostile chars collapse to a single dash.
        assert (
            repo_name_from_url("https://example.com/u/weird repo name")
            == "weird-repo-name"
        )

    def test_unicode_replaced(self):
        # Non-ASCII isn't in the allowed set — gets dashed out.
        assert repo_name_from_url("https://example.com/u/héllo") == "h-llo"

    def test_preserves_dots_and_underscores(self):
        assert repo_name_from_url("https://example.com/u/my.tool_v2") == "my.tool_v2"

    def test_strips_leading_trailing_separators(self):
        # Even after sanitization, leading/trailing -._ chars are trimmed.
        assert repo_name_from_url("https://example.com/u/-name-") == "name"
