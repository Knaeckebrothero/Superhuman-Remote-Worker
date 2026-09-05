"""Agent image entrypoint invariants.

The Kubernetes drain contract requires the Python agent to own PID 1.  A
shell-owned PID 1 does not forward kubelet's SIGTERM and turns every graceful
drain into a grace-period SIGKILL.
"""

import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "dockerfile",
    ["docker/Dockerfile.agent", "docker/Dockerfile.agent.dev"],
)
def test_agent_image_shell_entrypoint_execs_python(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert (
        'CMD ["sh", "-c", "exec python -m agent '
        '--config ${AGENT_CONFIG} --port ${AGENT_PORT} --host 0.0.0.0"]' in text
    )


@pytest.mark.parametrize(
    "dockerfile",
    ["docker/Dockerfile.agent", "docker/Dockerfile.agent.dev"],
)
def test_image_only_legacy_entry_preserves_package_identity_and_args(
    dockerfile, tmp_path
):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert "PYTHONSAFEPATH=1" in text
    assert "PYTHONPATH=/app" not in text
    assert text.index("pip install --no-") < text.index("USER srw")
    assert text.index("--no-deps -e .") < text.index("ENV PIP_TARGET=")

    # Execute the actual statements emitted by the image's printf directive.
    # The source-root PYTHONPATH models the import root supplied by its editable
    # install, while the adjacent agent.py would shadow it without safe-path.
    instruction = next(
        line
        for line in text.replace("\\\n", "").splitlines()
        if line.startswith("RUN printf ")
    )
    command = shlex.split(instruction.removeprefix("RUN "))
    assert command[-2:] == [">", "/app/agent.py"]
    shim = tmp_path / "agent.py"
    shim.write_text("\n".join(command[2:-2]) + "\n", encoding="utf-8")

    source_root = tmp_path / "src"
    package = source_root / "agent"
    package.mkdir(parents=True)
    initializer = package / "__init__.py"
    initializer.write_text("", encoding="utf-8")
    main = package / "__main__.py"
    main.write_text(
        "import agent, json, sys\n"
        "print(json.dumps({'package_file': agent.__file__, "
        "'main_file': __file__, 'argv': sys.argv, 'package': __package__}))\n",
        encoding="utf-8",
    )
    args = ["--mode", "persistent", "--config", "session_base", "--loop"]
    result = subprocess.run(
        [sys.executable, str(shim), *args],
        cwd=tmp_path,
        env={"PYTHONPATH": str(source_root), "PYTHONSAFEPATH": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "package_file": str(initializer),
        "main_file": str(main),
        "argv": [str(main), *args],
        "package": "agent",
    }
