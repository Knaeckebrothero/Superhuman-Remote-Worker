"""Pytest configuration for tests."""

import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Set WORKSPACE_PATH to a temp directory for tests so that workspace files
# (logs, checkpoints, uploads) don't get created inside the repository.
if "WORKSPACE_PATH" not in os.environ:
    _test_workspace = tempfile.mkdtemp(prefix="srw_test_workspace_")
    os.environ["WORKSPACE_PATH"] = _test_workspace
