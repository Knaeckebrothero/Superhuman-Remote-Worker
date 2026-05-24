"""Delegation resume message must reference grafted outputs, not branch merges."""
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `src` is importable as a package
_root = str(Path(__file__).parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from src.agent import _format_delegation_results  # noqa: E402


def test_format_references_output_paths_not_branches():
    msg = _format_delegation_results(
        [
            {
                "creation_order": 0, "status": "completed", "job_id": "c0",
                "config_name": "scholar", "summary": "found X",
                "output_path": "outputs/001-scholar-c0aaaaaa",
            },
        ]
    )
    assert "outputs/001-scholar-c0aaaaaa" in msg
    assert "scholar" in msg                 # config_name rendered (was the 'config' bug)
    assert "git diff" not in msg.lower()    # no branch-merge instructions
    assert "squash-merg" not in msg.lower()
    assert "git_merge_squash" not in msg
