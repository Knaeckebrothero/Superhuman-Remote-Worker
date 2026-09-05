"""Agent image entrypoint invariants.

The Kubernetes drain contract requires the Python agent to own PID 1.  A
shell-owned PID 1 does not forward kubelet's SIGTERM and turns every graceful
drain into a grace-period SIGKILL.
"""

import json
import os
from pathlib import Path
import re
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


@pytest.mark.parametrize(
    "dockerfile",
    ["docker/Dockerfile.agent", "docker/Dockerfile.agent.dev"],
)
def test_image_only_prestop_module_preserves_package_identity_and_args(
    dockerfile, tmp_path
):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    block = re.search(
        r"RUN python - <<'SRW_TERMINATION_COMPAT'\n(.*?)\nSRW_TERMINATION_COMPAT",
        text,
        re.DOTALL,
    )
    assert block is not None
    assert text.index("SRW_TERMINATION_COMPAT") < text.index("USER srw")
    assert "RUN --network=none python - <<'SRW_TERMINATION_SMOKE'" in text

    # Execute the recipe's installer against synthetic system site-packages.
    # The namespace contains only the one legacy preStop entry; the canonical
    # implementation belongs to the agent package under its editable root.
    site_packages = tmp_path / "site-packages"
    installer = (
        "import sysconfig\n"
        f"sysconfig.get_path = lambda name: {str(site_packages)!r}\n" + block.group(1)
    )
    subprocess.run([sys.executable, "-c", installer], check=True, timeout=10)
    assert sorted(
        str(path.relative_to(site_packages))
        for path in site_packages.rglob("*")
        if path.is_file()
    ) == ["src/api/persistent_termination.py"]

    source_root = tmp_path / "application-source"
    package = source_root / "agent"
    api = package / "api"
    api.mkdir(parents=True)
    for directory in (package, api):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    helper = api / "persistent_termination.py"
    helper.write_text(
        "import json, sys\n"
        "print(json.dumps({'file': __file__, 'package': __package__, "
        "'argv': sys.argv}))\n",
        encoding="utf-8",
    )
    args = ["--compatibility-smoke", "preserve this argument"]
    result = subprocess.run(
        [sys.executable, "-m", "src.api.persistent_termination", *args],
        cwd=tmp_path,
        env={
            "PYTHONPATH": os.pathsep.join((str(site_packages), str(source_root))),
            "PYTHONSAFEPATH": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "file": str(helper),
        "package": "agent.api",
        "argv": [str(helper), *args],
    }
