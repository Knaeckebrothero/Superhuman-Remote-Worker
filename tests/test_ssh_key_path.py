"""Runtime-safe resolution of the orchestrator's workspace SSH identity."""

import os
import stat

from orchestrator import services


def _reset_stage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(services, "_ssh_key_stage_dir", tmp_path / "staged")
    monkeypatch.setattr(services, "_ssh_key_stage_state", None)


def test_private_runtime_owned_key_is_used_directly(monkeypatch, tmp_path):
    source = tmp_path / "identity"
    source.write_text("private-key-material", encoding="utf-8")
    source.chmod(0o600)
    monkeypatch.setenv("SSH_KEY_PATH", str(source))
    _reset_stage(monkeypatch, tmp_path)

    assert services.resolve_ssh_key_path() == str(source)


def test_permissive_projected_key_is_staged_as_runtime_owned_0600(
    monkeypatch, tmp_path
):
    source = tmp_path / "projected-identity"
    source.write_text("private-key-material", encoding="utf-8")
    source.chmod(0o444)
    monkeypatch.setenv("SSH_KEY_PATH", str(source))
    _reset_stage(monkeypatch, tmp_path)

    resolved = services.resolve_ssh_key_path()

    assert resolved != str(source)
    assert open(resolved, encoding="utf-8").read() == "private-key-material"
    resolved_stat = os.stat(resolved)
    assert resolved_stat.st_uid == os.geteuid()
    assert stat.S_IMODE(resolved_stat.st_mode) == 0o600


def test_projected_key_rotation_refreshes_the_staged_copy(monkeypatch, tmp_path):
    source = tmp_path / "projected-identity"
    source.write_text("first-key", encoding="utf-8")
    source.chmod(0o444)
    monkeypatch.setenv("SSH_KEY_PATH", str(source))
    _reset_stage(monkeypatch, tmp_path)

    resolved = services.resolve_ssh_key_path()
    source.chmod(0o644)
    source.write_text("replacement-key", encoding="utf-8")
    source.chmod(0o444)

    assert services.resolve_ssh_key_path() == resolved
    assert open(resolved, encoding="utf-8").read() == "replacement-key"


def test_missing_explicit_key_keeps_path_for_caller_diagnostics(monkeypatch, tmp_path):
    missing = tmp_path / "missing-identity"
    monkeypatch.setenv("SSH_KEY_PATH", str(missing))
    _reset_stage(monkeypatch, tmp_path)

    assert services.resolve_ssh_key_path() == str(missing)
