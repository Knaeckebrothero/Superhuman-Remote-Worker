"""Unit tests for the Phase 3c (D7) drift-check pure helpers in main.py.

``_home_relative_path`` is the load-bearing guard: the on-view drift check only
re-fetches a cited cloud file when it's provably inside the *viewing user's* own
cloud home, which both prevents comparing a same-named-different file and yields
the path for the re-fetch. ``_source_cloud_meta`` coerces the sources.metadata
JSONB (dict or string) down to the cloud block.

Importing orchestrator ``main`` pulls the whole app (conftest sets up the path +
license/credential gates); skip cleanly if that env isn't available.
"""

import pytest

main = pytest.importorskip("main")


# ---------------------------------------------------------------------------
# _home_relative_path
# ---------------------------------------------------------------------------


def test_home_relative_path_under_home():
    rel = main._home_relative_path(
        "https://cloud.example/remote.php/dav/files/u/Documents/report.pdf",
        "https://cloud.example/remote.php/dav/files/u/",
    )
    assert rel == "Documents/report.pdf"


def test_home_relative_path_trailing_slash_insensitive():
    rel = main._home_relative_path(
        "https://c.ex/dav/files/u/a/b.txt",
        "https://c.ex/dav/files/u",  # no trailing slash
    )
    assert rel == "a/b.txt"


def test_home_relative_path_not_under_home_returns_none():
    # Different cloud / external datasource → not re-fetchable on the user's behalf.
    assert (
        main._home_relative_path(
            "https://other.host/dav/Documents/report.pdf",
            "https://cloud.example/remote.php/dav/files/u/",
        )
        is None
    )


def test_home_relative_path_exact_home_returns_none():
    # The home root itself is not a file.
    assert (
        main._home_relative_path(
            "https://c.ex/dav/files/u/",
            "https://c.ex/dav/files/u/",
        )
        is None
    )


def test_home_relative_path_empty_inputs_return_none():
    assert main._home_relative_path("", "https://c.ex/dav/") is None
    assert main._home_relative_path("https://c.ex/dav/x", "") is None


# ---------------------------------------------------------------------------
# _source_cloud_meta
# ---------------------------------------------------------------------------


def test_source_cloud_meta_from_dict():
    meta = {"cloud": {"backend": "webdav", "etag": '"e1"'}}
    assert main._source_cloud_meta(meta) == {"backend": "webdav", "etag": '"e1"'}


def test_source_cloud_meta_from_json_string():
    import json

    meta = json.dumps({"cloud": {"snapshot_blob_key": "citations/ab/abcd"}})
    assert main._source_cloud_meta(meta) == {"snapshot_blob_key": "citations/ab/abcd"}


def test_source_cloud_meta_no_cloud_block():
    assert main._source_cloud_meta({"other": 1}) == {}
    assert main._source_cloud_meta(None) == {}
    assert main._source_cloud_meta("not json") == {}
    assert main._source_cloud_meta({"cloud": "not-a-dict"}) == {}
