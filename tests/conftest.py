"""Pytest configuration for tests."""

import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# orchestrator/main.py SystemExits at import time unless the license gate is
# accepted. Tests only exercise its utility functions — auto-accept here so
# CI and local runs don't need extra setup.
os.environ.setdefault("LICENSE_TERMS_ACCEPTED", "true")

# Set WORKSPACE_PATH to a temp directory for tests so that workspace files
# (logs, checkpoints, uploads) don't get created inside the repository.
if "WORKSPACE_PATH" not in os.environ:
    _test_workspace = tempfile.mkdtemp(prefix="srw_test_workspace_")
    os.environ["WORKSPACE_PATH"] = _test_workspace
