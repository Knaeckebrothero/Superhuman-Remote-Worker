from __future__ import annotations

import pytest

from orchestrator.services.cloud.etag_baseline import (
    PropfindError,
    capture_etag_baseline,
)
from orchestrator.services.cloud.handles import ProjectFolderEntry


def _f(path, etag):
    return ProjectFolderEntry(path=path, is_dir=False, etag=etag)


def _d(path):
    return ProjectFolderEntry(path=path, is_dir=True, etag="dir-etag")


@pytest.mark.asyncio
async def test_infinity_path_used_when_list_tree_succeeds():
    tree = [_d("docs"), _f("knowledge-base/knowledge/a.md", "e1"), _f("b.md", "e2")]
    calls = {"children": 0}

    async def list_tree():
        return tree

    async def list_children(_sub):
        calls["children"] += 1
        return []

    out = await capture_etag_baseline(
        root_subpath="", list_children=list_children, list_tree=list_tree
    )
    assert out == {
        "knowledge-base/knowledge/a.md": "e1",
        "b.md": "e2",
    }  # files only, no dirs
    assert calls["children"] == 0  # infinity short-circuits the BFS


@pytest.mark.asyncio
async def test_falls_back_to_bfs_when_infinity_rejected():
    async def list_tree():
        raise PropfindError("Depth: infinity rejected (400)")

    tree = {
        "": [_d("docs"), _f("top.md", "e0")],
        "docs": [_d("docs/sub"), _f("knowledge-base/knowledge/a.md", "e1")],
        "docs/sub": [_f("docs/sub/c.md", "e3")],
    }

    async def list_children(sub):
        return tree[sub]

    out = await capture_etag_baseline(
        root_subpath="", list_children=list_children, list_tree=list_tree
    )
    assert out == {
        "top.md": "e0",
        "knowledge-base/knowledge/a.md": "e1",
        "docs/sub/c.md": "e3",
    }


@pytest.mark.asyncio
async def test_bfs_does_not_revisit_and_terminates_on_cycle_guard():
    async def list_tree():
        raise PropfindError("no infinity")

    # Malformed backend that returns the same dir as its own child; the seen
    # set must prevent an infinite loop.
    async def list_children(sub):
        return [ProjectFolderEntry(path="loop", is_dir=True, etag="d")]

    out = await capture_etag_baseline(
        root_subpath="", list_children=list_children, list_tree=list_tree
    )
    assert out == {}  # no files, and it returned rather than hanging
