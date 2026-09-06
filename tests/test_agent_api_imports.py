"""Light API helpers must import without constructing application dependencies."""

import subprocess
import sys

import pytest


def _run_fresh(code):
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module", ["agent.api", "agent.api.job_stream", "agent.api.session_transport"]
)
def test_light_api_import_does_not_load_an_application(module):
    _run_fresh(
        f"""
import importlib, sys
importlib.import_module({module!r})
from agent import api
assert set(api.__all__) <= set(dir(api))
assert not {{'agent.api.app', 'agent.api.persistent_app', 'agent.api.dual_app',
            'agent.agent', 'fastapi', 'langgraph'}} & sys.modules.keys()
"""
    )


def test_legacy_exports_star_import_and_submodule_imports_keep_their_identities():
    _run_fresh(
        """
from agent import api
namespace = {}
exec('from agent.api import *', namespace)
from agent.api import app, dual_app, persistent_app, models
expected = {
    'create_app': app.create_app,
    'set_config_path': app.set_config_path,
    'JobStatus': models.JobStatus,
    'HealthStatus': models.HealthStatus,
    'JobSubmitRequest': models.JobSubmitRequest,
    'JobSubmitResponse': models.JobSubmitResponse,
    'JobStatusResponse': models.JobStatusResponse,
    'HealthResponse': models.HealthResponse,
    'ErrorResponse': models.ErrorResponse,
}
assert list(expected) == api.__all__
for name, value in expected.items():
    assert namespace[name] is getattr(api, name) is value
    assert vars(api)[name] is value
assert dual_app.__name__ == 'agent.api.dual_app'
assert persistent_app.__name__ == 'agent.api.persistent_app'
try:
    api.not_an_export
except AttributeError:
    pass
else:
    raise AssertionError('Unknown package attribute was accepted')
"""
    )
