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


class FakeCloudMountManager:
    def __init__(self, cache_message: str | None = None) -> None:
        self.cache_message = cache_message

    def cache_limit_message(self) -> str | None:
        return self.cache_message

    def status(self) -> str:
        return "Cloud mount status:\n- home mounted"


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


def test_run_command_blocks_cloud_read_when_cache_limit_reached():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "scan_guard": "warn",
                "_manager": FakeCloudMountManager("Cloud cache guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "cat /workspace/cloud/notes/todo.md"})

    assert "Cloud cache guard: full" in result
    assert shell_manager.run_calls == []


def test_run_command_does_not_cache_check_non_cloud_command():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "_manager": FakeCloudMountManager("Cloud cache guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "git status --short"})

    assert "Cloud cache guard" not in result
    assert shell_manager.run_calls == ["git status --short"]


def test_srw_cloud_status_reports_manager_status():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {"active": True, "_manager": FakeCloudMountManager()},
        )
    )
    status_tool = next(tool for tool in tools if tool.name == "srw_cloud_status")

    result = status_tool.invoke({})

    assert "Cloud mount status" in result
    assert "home mounted" in result
