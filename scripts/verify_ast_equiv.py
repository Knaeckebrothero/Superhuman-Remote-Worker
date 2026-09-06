#!/usr/bin/env python3
"""Verify a flattening manifest against frozen old sources; see sibling tool."""

from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    sys.argv.insert(1, "--verify")
    runpy.run_path(
        str(Path(__file__).with_name("flatten_source_tree.py")), run_name="__main__"
    )
