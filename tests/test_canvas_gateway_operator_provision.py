"""Operator workflow checks for the restricted Canvas gateway database role."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/provision-canvas-gateway-database.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source))
    path.chmod(0o700)


def _environment(tmp_path: Path, fake_bin: Path) -> tuple[dict[str, str], str]:
    viewer_password = "viewer-password-with-32-characters"
    password_file = tmp_path / "viewer-password"
    password_file.write_text(viewer_password)
    password_file.chmod(0o600)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PGHOST": "orchestrator.database.internal",
        "PGPORT": "5432",
        "PGDATABASE": "srw",
        "PGUSER": "srw_admin",
        "PGPASSWORD": "admin-password-must-not-appear",
        "CANVAS_VIEWER_POSTGRES_PASSWORD_FILE": str(password_file),
        "KUBE_CONTEXT": "production-context",
        "KUBE_NAMESPACE": "srw",
        "CANVAS_VIEWER_SECRET_NAME": "srw-canvas-gateway-db",
        "FAKE_COMMAND_LOG": str(tmp_path / "commands.log"),
        "FAKE_ROLE_MARKER": str(tmp_path / "role-applied"),
        "FAKE_SECRET_MARKER": str(tmp_path / "secret-applied"),
        "EXPECTED_VIEWER_PASSWORD": viewer_password,
    }
    return environment, viewer_password


def _fake_commands(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "psql",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'psql %s\n' "$*" >>"$FAKE_COMMAND_LOG"
        if [[ "$*" == *"to_regclass('public.canvas_origin_sessions')"* ]]; then
          printf 't\n'
        elif [[ "$*" == *"canvas-viewer-self-configure.sql"* ]]; then
          [[ "$PGPASSWORD" == "$EXPECTED_VIEWER_PASSWORD" ]]
          touch "$FAKE_ROLE_MARKER"
        elif [[ "$*" == *"--file"* ]]; then
          [[ "$CANVAS_VIEWER_POSTGRES_PASSWORD" == "$EXPECTED_VIEWER_PASSWORD" ]]
          touch "$FAKE_ROLE_MARKER"
        elif [[ "$*" == *"session_user = current_user"* ]]; then
          [[ "$PGPASSWORD" == "$EXPECTED_VIEWER_PASSWORD" ]]
          printf 't\n'
        else
          exit 90
        fi
        """,
    )
    _executable(
        fake_bin / "kubectl",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        [[ -z "${PGPASSWORD+x}" ]]
        [[ -z "${CANVAS_VIEWER_POSTGRES_PASSWORD+x}" ]]
        printf 'kubectl %s\n' "$*" >>"$FAKE_COMMAND_LOG"
        if [[ "$*" == "config get-contexts production-context -o name" ]]; then
          printf 'production-context\n'
        elif [[ "$*" == *"get namespace srw"* ]]; then
          exit 0
        elif [[ "$*" == *"create secret generic srw-canvas-gateway-db"* ]]; then
          printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: placeholder\n'
        elif [[ "$*" == *"label --local -f - cnpg.io/reload=true"* ]]; then
          cat
        elif [[ "$*" == *"apply -f -"* ]]; then
          cat >/dev/null
          touch "$FAKE_SECRET_MARKER"
        elif [[ "$*" == *"get secret srw-canvas-gateway-db -o jsonpath={.type}"* ]]; then
          printf 'kubernetes.io/basic-auth'
        elif [[ "$*" == *"get secret srw-canvas-gateway-db -o jsonpath={.immutable}"* ]]; then
          printf 'false'
        elif [[ "$*" == *"get secret srw-canvas-gateway-db -o jsonpath={.data.username}"* ]]; then
          printf 'c3J3X2NhbnZhc19nYXRld2F5'
        elif [[ "$*" == *"get secret srw-canvas-gateway-db -o jsonpath={.metadata.labels.cnpg\\.io/reload}"* ]]; then
          printf 'true'
        elif [[ "$*" == *"get secret srw-canvas-gateway-db -o go-template="* ]]; then
          printf '%b' "${FAKE_SECRET_KEYS:-CANVAS_VIEWER_POSTGRES_PASSWORD\\npassword\\nusername\\n}"
        elif [[ "$*" == *"get secret srw-canvas-gateway-db"* ]]; then
          exit 0
        else
          exit 91
        fi
        """,
    )
    return fake_bin


def test_operator_preflight_is_read_only(tmp_path: Path) -> None:
    fake_bin = _fake_commands(tmp_path)
    environment, _ = _environment(tmp_path, fake_bin)

    result = subprocess.run(
        [str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "no state changed" in result.stdout
    assert not Path(environment["FAKE_ROLE_MARKER"]).exists()
    assert not Path(environment["FAKE_SECRET_MARKER"]).exists()


def test_operator_apply_secret_uses_files_and_explicit_context(tmp_path: Path) -> None:
    fake_bin = _fake_commands(tmp_path)
    environment, viewer_password = _environment(tmp_path, fake_bin)

    result = subprocess.run(
        [str(SCRIPT), "--apply-secret"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert Path(environment["FAKE_ROLE_MARKER"]).exists()
    assert Path(environment["FAKE_SECRET_MARKER"]).exists()
    command_log = Path(environment["FAKE_COMMAND_LOG"]).read_text()
    assert "--from-file=username=" in command_log
    assert "--from-file=password=" in command_log
    assert "--from-file=CANVAS_VIEWER_POSTGRES_PASSWORD=" in command_log
    assert "--type=kubernetes.io/basic-auth" in command_log
    assert "label --local -f - cnpg.io/reload=true" in command_log
    assert "canvas-viewer-role.sql" in command_log
    assert "canvas-viewer-role-safety.sql" in command_log
    assert "canvas-viewer-self-configure.sql" in command_log
    assert "canvas-viewer-grants.sql" in command_log
    assert "--context production-context --namespace srw" in command_log
    assert viewer_password not in command_log
    assert environment["PGPASSWORD"] not in command_log
    assert viewer_password not in result.stdout
    assert viewer_password not in result.stderr


def test_conflicting_existing_secret_is_rejected_before_role_change(
    tmp_path: Path,
) -> None:
    fake_bin = _fake_commands(tmp_path)
    environment, viewer_password = _environment(tmp_path, fake_bin)
    environment["FAKE_SECRET_KEYS"] = "username\npassword\nunrelated-key\n"

    result = subprocess.run(
        [str(SCRIPT), "--apply-secret"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "exactly username, password" in result.stderr
    assert not Path(environment["FAKE_ROLE_MARKER"]).exists()
    assert not Path(environment["FAKE_SECRET_MARKER"]).exists()
    assert viewer_password not in result.stdout
    assert viewer_password not in result.stderr
