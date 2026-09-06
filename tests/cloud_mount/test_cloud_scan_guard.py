from types import SimpleNamespace

from agent.services.cloud_mount.guardrails import (
    CloudScanRisk,
    detect_cloud_delete_risk,
    detect_cloud_scan_risk,
    format_cloud_delete_guard_message,
)
from agent.tools.shell.shell_tools import create_shell_tools


class FakeShellManager:
    def __init__(self) -> None:
        self.run_calls: list[str] = []

    def ensure_tab(self, name: str) -> None:
        self.name = name

    def format_tab_header(self) -> str:
        return "[Shells: default]"

    def run_sync(
        self,
        command: str,
        tab_name: str,
        timeout=None,
        working_dir=None,
    ) -> str:
        self.run_calls.append(command)
        return "Exit code: 0\n--- stdout ---\nok"


class FakeCloudMountManager:
    def __init__(self, cache_message: str | None = None) -> None:
        self.cache_message = cache_message

    def cache_limit_message(self) -> str | None:
        return self.cache_message

    def status(self) -> str:
        return "Cloud mount status:\n- home mounted"


class FakeOverlayManager:
    def __init__(self, quota_message: str | None = None) -> None:
        self.quota_message = quota_message

    def quota_guard_message(self) -> str | None:
        return self.quota_message


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


def test_run_command_blocks_cloud_write_when_upperdir_over_quota():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "scan_guard": "warn",
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke(
        {"command": "cp report.pdf /workspace/cloud/report.pdf"}
    )

    assert "Cloud staging guard: full" in result
    assert shell_manager.run_calls == []


def test_run_command_does_not_upperdir_check_non_cloud_command():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "git status --short"})

    assert "Cloud staging guard" not in result
    assert shell_manager.run_calls == ["git status --short"]


def test_run_command_upperdir_guard_exception_fails_open():
    class RaisingOverlayManager:
        def quota_guard_message(self) -> str | None:
            raise RuntimeError("probe unreachable")

    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {"active": True, "_overlay_manager": RaisingOverlayManager()},
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke(
        {"command": "cp report.pdf /workspace/cloud/report.pdf"}
    )

    assert "Cloud staging guard" not in result
    assert shell_manager.run_calls == ["cp report.pdf /workspace/cloud/report.pdf"]


def test_run_command_no_overlay_manager_skips_upperdir_guard():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "warn"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke(
        {"command": "cp report.pdf /workspace/cloud/report.pdf"}
    )

    assert "Cloud staging guard" not in result
    assert shell_manager.run_calls == ["cp report.pdf /workspace/cloud/report.pdf"]


def test_detects_rm_rf_over_cloud_mount():
    assert detect_cloud_delete_risk("rm -rf /workspace/cloud/archive") is not None
    assert detect_cloud_delete_risk("rm -r /cloud/merged/old") is not None


def test_detects_find_delete_over_cloud_mount():
    assert (
        detect_cloud_delete_risk("find /workspace/cloud -name '*.tmp' -delete")
        is not None
    )


def test_ignores_deletes_outside_cloud_and_single_file_rm_elsewhere():
    assert detect_cloud_delete_risk("rm -rf /home/agent-host/workspace/build") is None
    assert (
        detect_cloud_delete_risk("rm /workspace/cloud/one.txt") is None
    )  # single file, no -r


def test_format_cloud_delete_guard_message_live_by_default():
    risk = CloudScanRisk("recursive rm over a cloud mount whiteouts each file")
    message = format_cloud_delete_guard_message(
        "rm -rf /workspace/cloud/archive", risk, protected=False
    )

    assert "LIVE" in message
    assert "STAGED" not in message
    assert "untouched until you apply" not in message


def test_format_cloud_delete_guard_message_staged_when_protected():
    risk = CloudScanRisk("recursive rm over a cloud mount whiteouts each file")
    message = format_cloud_delete_guard_message(
        "rm -rf /workspace/cloud/archive", risk, protected=True
    )

    assert "STAGED" in message
    assert "LIVE: a delete removes the real" not in message


def test_run_command_blocks_guarded_cloud_delete():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "block"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "rm -rf /workspace/cloud/archive"})

    assert "Cloud delete guard" in result
    assert "was not run" in result
    assert shell_manager.run_calls == []


def test_run_command_warn_mode_allows_guarded_cloud_delete():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "warn"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "rm -rf /workspace/cloud/archive"})

    assert "Cloud delete guard" in result
    assert "Exit code: 0" in result
    assert shell_manager.run_calls == ["rm -rf /workspace/cloud/archive"]


def test_run_command_allows_single_file_delete_outside_cloud():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "block"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "rm -rf /home/agent-host/workspace/build"})

    assert "Cloud delete guard" not in result
    assert "Exit code: 0" in result
    assert shell_manager.run_calls == ["rm -rf /home/agent-host/workspace/build"]


def test_run_command_cloud_delete_guard_defaults_to_live_wording():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(shell_manager, {"active": True, "scan_guard": "block"})
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "rm -rf /workspace/cloud/archive"})

    assert "LIVE" in result
    assert "STAGED" not in result
    assert "untouched until you apply" not in result


def test_run_command_cloud_delete_guard_staged_wording_when_protected():
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {"active": True, "scan_guard": "block", "protected": True},
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "rm -rf /workspace/cloud/archive"})

    assert "STAGED" in result
    assert "LIVE: a delete removes the real" not in result


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


def test_read_only_commands_pass_at_quota():
    """Read-only commands should not trigger the upperdir quota guard, even when over quota."""
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "scan_guard": "warn",
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    # Test various read-only commands (must use absolute paths for guard detection)
    read_commands = [
        "cat /workspace/cloud/a.txt",
        "ls -la /workspace/cloud",
        "grep r foo /workspace/cloud",
        "du -sh /workspace/cloud",
    ]

    for cmd in read_commands:
        result = run_command.invoke({"command": cmd})
        assert "Cloud staging guard: full" not in result, (
            f"Read command '{cmd}' should not trigger upperdir guard"
        )
        assert "Exit code: 0" in result, f"Read command '{cmd}' should execute"
        assert cmd in shell_manager.run_calls, f"Command '{cmd}' should have been run"


def test_write_commands_blocked_at_quota():
    """Write-indicating commands should trigger the upperdir quota guard when over quota."""
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "scan_guard": "warn",
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    # Test various write-indicating commands (must use absolute paths for guard detection)
    write_commands = [
        "echo hi > /workspace/cloud/x",
        "tee /workspace/cloud/x",
        "rm /workspace/cloud/x",
        "cp a /workspace/cloud/",
        "mv a /workspace/cloud/",
        "touch /workspace/cloud/x",
        "mkdir /workspace/cloud/d",
        "sed -i s/a/b/ /workspace/cloud/x",
        "rsync a /workspace/cloud/",
        "dd of /workspace/cloud/x",
        "truncate -s0 /workspace/cloud/x",
    ]

    for cmd in write_commands:
        shell_manager.run_calls.clear()
        result = run_command.invoke({"command": cmd})
        assert "Cloud staging guard: full" in result, (
            f"Write command '{cmd}' should trigger upperdir guard"
        )
        assert shell_manager.run_calls == [], (
            f"Command '{cmd}' should not have been run"
        )


def test_pipeline_with_redirect_detected():
    """Pipelines with redirect operators should be detected as write operations."""
    shell_manager = FakeShellManager()
    tools = create_shell_tools(
        _context(
            shell_manager,
            {
                "active": True,
                "scan_guard": "warn",
                "_overlay_manager": FakeOverlayManager("Cloud staging guard: full"),
            },
        )
    )
    run_command = next(tool for tool in tools if tool.name == "run_command")

    result = run_command.invoke({"command": "sort a | uniq >> /workspace/cloud/out"})

    assert "Cloud staging guard: full" in result
    assert shell_manager.run_calls == []
