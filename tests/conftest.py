"""Pytest configuration for tests."""

import os
import sys
import tempfile
from pathlib import Path

# Add project root AND orchestrator/ to path. orchestrator/ is on sys.path
# at runtime (uvicorn --app-dir orchestrator + PYTHONPATH=orchestrator:.)
# so modules under it use sibling imports (``from security.crypto import``
# rather than ``from orchestrator.security.crypto``). Tests that import
# ``orchestrator.database.postgres`` would otherwise fail to resolve the
# transitive ``from security.crypto import`` inside it.
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "orchestrator"))

# orchestrator/main.py SystemExits at import time unless the license gate is
# accepted. Tests only exercise its utility functions — auto-accept here so
# CI and local runs don't need extra setup.
os.environ.setdefault("LICENSE_TERMS_ACCEPTED", "true")

# Set WORKSPACE_PATH to a temp directory for tests so that workspace files
# (logs, checkpoints, uploads) don't get created inside the repository.
if "WORKSPACE_PATH" not in os.environ:
    _test_workspace = tempfile.mkdtemp(prefix="srw_test_workspace_")
    os.environ["WORKSPACE_PATH"] = _test_workspace

# Provide a deterministic encryption key so any code path that touches
# orchestrator.security.crypto during tests has a working cipher. Real
# credentials never run through this key.
os.environ.setdefault(
    "APP_ENCRYPTION_KEY", "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="
)
