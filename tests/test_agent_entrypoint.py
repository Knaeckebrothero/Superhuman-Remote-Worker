"""Agent image entrypoint invariants.

The Kubernetes drain contract requires the Python agent to own PID 1.  A
shell-owned PID 1 does not forward kubelet's SIGTERM and turns every graceful
drain into a grace-period SIGKILL.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "dockerfile",
    ["docker/Dockerfile.agent", "docker/Dockerfile.agent.dev"],
)
def test_agent_image_shell_entrypoint_execs_python(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert (
        'CMD ["sh", "-c", "exec python agent.py '
        '--config ${AGENT_CONFIG} --port ${AGENT_PORT} --host 0.0.0.0"]' in text
    )
