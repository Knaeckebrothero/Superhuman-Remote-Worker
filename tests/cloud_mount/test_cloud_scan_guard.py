from types import SimpleNamespace

from src.services.cloud_mount.guardrails import detect_cloud_scan_risk
from src.tools.shell.shell_tools import create_shell_tools


class FakeShellManager:
    def __init__(self) -> None:
        self.run_calls: list[str] = []

    def ensure_tab(self, name: str) -> None:
        self.name = name

    def format_tab_header(self) -> str:
        return "[Shells: default]"

    def run_sync(self, command: str, tab_name: str, timeout=None) -> str:
        self.run_calls.append(command)
        return "Exit code: 0\n--- stdout ---\nok"


def _context(shell_manager: FakeShellManager, cloud_mount: dict | None = None):
    config = {
        "shell": {"mode": "stateless"},
        "max_output_chars": 50000,
        "shell_max_read_lines": 200,
    }
    if cloud_mount is not None:
        config["cloud_mount"] = cloud_mount
    return SimpleNamespace(
        shell_manager=shell_manager,
        get_config=lambda key, default=None: config.get(key, default),
        request_freeze=lambda payload: None,
    )


def test_detects_obvious_recursive_cloud_scan():
    risk = detect_cloud_scan_risk("grep -R invoice /workspace/cloud")

    assert risk is not None
    assert "recursive grep" in risk.reason


def test_allows_targeted_cloud_file_read():
    assert detect_cloud_scan_risk("cat /workspace/cloud/notes/todo.md") is None


def test_run_command_blocks_guarded_cloud_scan():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "block"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "grep -R invoice /workspace/cloud"})

    assert "Cloud scan guard" in result
    assert "was not run" in result
    assert shell_manager.run_calls == []


def test_run_command_warn_mode_allows_guarded_cloud_scan():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "warn"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "du -sh /cloud"})

    assert "Cloud scan guard" in result
    assert "Exit code: 0" in result
    assert shell_manager.run_calls == ["du -sh /cloud"]


def test_run_command_without_cloud_mount_skips_guard():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(_context(shell_manager, {"active": False}))
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "grep -R invoice /workspace/cloud"})

    assert "Cloud scan guard" not in result
    assert "Exit code: 0" in result
    assert shell_manager.run_calls == ["grep -R invoice /workspace/cloud"]
