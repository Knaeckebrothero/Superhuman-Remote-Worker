"""Tests for the per-user code-server IDE settings store.

The store persists a user's code-server config (settings.json, keybindings.json,
snippets) in ``users.settings['ide']['files']`` and reconciles config pulled from
workspaces using filesystem mtimes (newest wins, per file).

The fake DB here faithfully replicates the real
``PostgresDB.update_user_settings`` semantics — a SHALLOW top-level JSONB
``||`` merge with ``jsonb_strip_nulls`` — because the store's correctness hinges
on not clobbering sibling keys through that shallow merge.
"""

import base64

import pytest

from orchestrator.services.ide_settings import (
    CODE_SERVER_USER_DIR,
    IdeSettingsStore,
    OpenVsxClassifier,
    build_extension_install_script,
    build_extensions_list_script,
    build_seed_script,
    parse_extensions_list,
    parse_pull_output,
    pull_ide_config,
    reconcile_extensions,
    reconcile_ide_settings,
    resolve_ssh_target,
    seed_ide_config_for_user,
)


def _strip_nulls(obj):
    """Mirror jsonb_strip_nulls: recursively drop keys whose value is null."""
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


class FakeSettingsDB:
    """In-memory stand-in replicating get/update_user_settings semantics."""

    def __init__(self, initial=None):
        self._settings = {uid: dict(s) for uid, s in (initial or {}).items()}

    async def get_user_settings(self, user_id: str):
        # Real layer returns a fresh dict each call; emulate by copying.
        import copy

        return copy.deepcopy(self._settings.get(user_id, {}))

    async def update_user_settings(self, user_id: str, settings: dict) -> bool:
        # SHALLOW top-level merge (``||``) then strip nulls — exactly the
        # behaviour of the real SQL ``COALESCE(settings,'{}') || $2``.
        current = self._settings.get(user_id, {})
        merged = {**current, **settings}
        self._settings[user_id] = _strip_nulls(merged)
        return True


UID = "11111111-1111-1111-1111-111111111111"


def _f(content, mtime):
    return {"content": content, "mtime": mtime}


class TestApplyPulledFiles:
    @pytest.mark.asyncio
    async def test_stores_files_when_none_exist(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)

        updated = await store.apply_pulled_files(
            UID, {"settings.json": _f('{"theme":"dracula"}', 100.0)}
        )

        assert updated == ["settings.json"]
        files = await store.get_ide_files(UID)
        assert files["settings.json"] == _f('{"theme":"dracula"}', 100.0)

    @pytest.mark.asyncio
    async def test_newer_mtime_overwrites(self):
        db = FakeSettingsDB(
            {UID: {"ide": {"files": {"settings.json": _f("old", 100.0)}}}}
        )
        store = IdeSettingsStore(db)

        updated = await store.apply_pulled_files(
            UID, {"settings.json": _f("new", 200.0)}
        )

        assert updated == ["settings.json"]
        files = await store.get_ide_files(UID)
        assert files["settings.json"] == _f("new", 200.0)

    @pytest.mark.asyncio
    async def test_older_or_equal_mtime_is_skipped(self):
        db = FakeSettingsDB(
            {UID: {"ide": {"files": {"settings.json": _f("current", 200.0)}}}}
        )
        store = IdeSettingsStore(db)

        older = await store.apply_pulled_files(
            UID, {"settings.json": _f("stale", 150.0)}
        )
        equal = await store.apply_pulled_files(
            UID, {"settings.json": _f("same", 200.0)}
        )

        assert older == []
        assert equal == []
        files = await store.get_ide_files(UID)
        assert files["settings.json"] == _f("current", 200.0)

    @pytest.mark.asyncio
    async def test_newest_wins_regardless_of_application_order(self):
        # Two workspaces report the same file at different mtimes. Whichever
        # order the reconciler applies them, the newest must win.
        db1 = FakeSettingsDB()
        db2 = FakeSettingsDB()
        s1, s2 = IdeSettingsStore(db1), IdeSettingsStore(db2)

        await s1.apply_pulled_files(UID, {"settings.json": _f("v100", 100.0)})
        await s1.apply_pulled_files(UID, {"settings.json": _f("v200", 200.0)})

        await s2.apply_pulled_files(UID, {"settings.json": _f("v200", 200.0)})
        await s2.apply_pulled_files(UID, {"settings.json": _f("v100", 100.0)})

        assert (await s1.get_ide_files(UID))["settings.json"] == _f("v200", 200.0)
        assert (await s2.get_ide_files(UID))["settings.json"] == _f("v200", 200.0)

    @pytest.mark.asyncio
    async def test_updating_one_file_preserves_siblings(self):
        # The shallow-merge clobber guard: writing settings.json must not drop
        # keybindings.json already in the store.
        db = FakeSettingsDB(
            {UID: {"ide": {"files": {"keybindings.json": _f("kb", 50.0)}}}}
        )
        store = IdeSettingsStore(db)

        await store.apply_pulled_files(UID, {"settings.json": _f("st", 100.0)})

        files = await store.get_ide_files(UID)
        assert files["keybindings.json"] == _f("kb", 50.0)
        assert files["settings.json"] == _f("st", 100.0)

    @pytest.mark.asyncio
    async def test_preserves_unrelated_top_level_and_ide_keys(self):
        db = FakeSettingsDB(
            {
                UID: {
                    "profile": {"nickname": "ada"},
                    "ide": {"version": 1, "files": {}},
                }
            }
        )
        store = IdeSettingsStore(db)

        await store.apply_pulled_files(UID, {"settings.json": _f("st", 100.0)})

        full = await db.get_user_settings(UID)
        assert full["profile"] == {"nickname": "ada"}  # unrelated top-level key kept
        assert full["ide"]["version"] == 1  # sibling ide sub-key kept
        assert full["ide"]["files"]["settings.json"] == _f("st", 100.0)

    @pytest.mark.asyncio
    async def test_multiple_files_in_one_call(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)

        updated = await store.apply_pulled_files(
            UID,
            {
                "settings.json": _f("st", 100.0),
                "snippets/python.json": _f("sn", 90.0),
            },
        )

        assert set(updated) == {"settings.json", "snippets/python.json"}
        files = await store.get_ide_files(UID)
        assert files["snippets/python.json"] == _f("sn", 90.0)

    @pytest.mark.asyncio
    async def test_empty_pull_is_noop(self):
        db = FakeSettingsDB(
            {UID: {"ide": {"files": {"settings.json": _f("st", 100.0)}}}}
        )
        store = IdeSettingsStore(db)

        updated = await store.apply_pulled_files(UID, {})

        assert updated == []
        assert (await store.get_ide_files(UID))["settings.json"] == _f("st", 100.0)


def _wire(name: str, mtime: int, content: str) -> str:
    """Render one file's section of the remote pull script's stdout."""
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"__SRWFILE__{name}\t{mtime}\n{b64}\n__SRWEND__\n"


class TestParsePullOutput:
    def test_empty_output_is_empty_dict(self):
        assert parse_pull_output("") == {}

    def test_parses_single_file(self):
        out = _wire("settings.json", 1716800000, '{"workbench.colorTheme":"Dracula"}')
        parsed = parse_pull_output(out)
        assert parsed == {
            "settings.json": {
                "content": '{"workbench.colorTheme":"Dracula"}',
                "mtime": 1716800000.0,
            }
        }

    def test_parses_multiple_files_and_preserves_snippet_paths(self):
        out = (
            _wire("settings.json", 100, "S")
            + _wire("keybindings.json", 110, "K")
            + _wire("snippets/python.json", 90, "P")
        )
        parsed = parse_pull_output(out)
        assert set(parsed) == {
            "settings.json",
            "keybindings.json",
            "snippets/python.json",
        }
        assert parsed["snippets/python.json"] == {"content": "P", "mtime": 90.0}

    def test_preserves_multiline_content_with_newlines(self):
        content = '{\n  "a": 1,\n  "b": 2\n}\n'
        out = _wire("settings.json", 5, content)
        parsed = parse_pull_output(out)
        assert parsed["settings.json"]["content"] == content

    def test_mtime_is_float(self):
        out = _wire("settings.json", 42, "x")
        assert isinstance(parse_pull_output(out)["settings.json"]["mtime"], float)


class TestResolveSshTarget:
    def test_container_uses_pod_ip_and_port(self):
        ctx = {
            "workspace_container": {
                "status": "ready",
                "pod_ip": "10.0.0.5",
                "port": 30022,
            }
        }
        assert resolve_ssh_target(ctx) == ("10.0.0.5", 30022)

    def test_vm_uses_ssh_host_and_ssh_port(self):
        ctx = {"vm": {"status": "ready", "ssh_host": "100.64.0.3", "ssh_port": 22}}
        assert resolve_ssh_target(ctx) == ("100.64.0.3", 22)

    def test_ide_session_uses_pod_ip(self):
        ctx = {"ide_session": {"status": "active", "pod_ip": "10.0.0.9"}}
        host, port = resolve_ssh_target(ctx)
        assert host == "10.0.0.9"

    def test_container_preferred_over_vm(self):
        ctx = {
            "workspace_container": {
                "status": "ready",
                "pod_ip": "10.0.0.5",
                "port": 30022,
            },
            "vm": {"status": "ready", "ssh_host": "100.64.0.3"},
        }
        assert resolve_ssh_target(ctx) == ("10.0.0.5", 30022)

    def test_no_target_returns_none(self):
        assert resolve_ssh_target({}) is None
        assert resolve_ssh_target({"workspace_container": {}}) is None


class TestBuildSeedScript:
    def test_empty_files_is_noop_script(self):
        # No files → nothing to write; must not emit destructive commands.
        script = build_seed_script({})
        assert "base64 -d" not in script

    def test_writes_content_path_mtime_and_chown(self):
        files = {"settings.json": {"content": '{"a":1}', "mtime": 1716800000.0}}
        script = build_seed_script(files)
        b64 = base64.b64encode('{"a":1}'.encode()).decode("ascii")

        assert b64 in script  # content delivered base64 (binary-safe)
        assert f"{CODE_SERVER_USER_DIR}/settings.json" in script
        assert "touch -d @1716800000.0" in script
        assert f"chown -R agent-host:agent-host {CODE_SERVER_USER_DIR}" in script

    def test_creates_parent_dir_for_snippets(self):
        files = {"snippets/python.json": {"content": "x", "mtime": 5.0}}
        script = build_seed_script(files)
        assert f"mkdir -p {CODE_SERVER_USER_DIR}/snippets" in script

    def test_single_quotes_in_name_do_not_break_out(self):
        # A snippet filename with a quote must be shell-escaped, not injected.
        files = {"snippets/a'b.json": {"content": "x", "mtime": 1.0}}
        script = build_seed_script(files)
        assert "rm -rf" not in script  # sanity: nothing injected
        assert "'\\''" in script  # the POSIX single-quote escape is present


class TestPullIdeConfig:
    @pytest.mark.asyncio
    async def test_parses_runner_stdout(self):
        out = _wire("settings.json", 100, "S")

        async def fake_runner(host, port, script, key_path=None, timeout=20):
            return 0, out, b""

        result = await pull_ide_config("10.0.0.5", 30022, _runner=fake_runner)
        assert result == {"settings.json": {"content": "S", "mtime": 100.0}}

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_empty(self):
        async def fake_runner(host, port, script, key_path=None, timeout=20):
            return 255, b"", b"ssh: connect timeout"

        result = await pull_ide_config("10.0.0.5", 30022, _runner=fake_runner)
        assert result == {}

    @pytest.mark.asyncio
    async def test_runner_exception_returns_empty(self):
        async def fake_runner(host, port, script, key_path=None, timeout=20):
            raise OSError("boom")

        result = await pull_ide_config("10.0.0.5", 30022, _runner=fake_runner)
        assert result == {}


class TestReconcileIdeSettings:
    @pytest.mark.asyncio
    async def test_pulls_each_workspace_and_applies(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        calls = []

        async def pull_fn(host, port):
            calls.append((host, port))
            return {"settings.json": _f("theme", 100.0)}

        workspaces = [
            {
                "user_id": UID,
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.5",
                        "port": 30022,
                    }
                },
            }
        ]
        count = await reconcile_ide_settings(store, workspaces, pull_fn)

        assert calls == [("10.0.0.5", 30022)]
        assert count == 1
        assert (await store.get_ide_files(UID))["settings.json"] == _f("theme", 100.0)

    @pytest.mark.asyncio
    async def test_newest_across_two_workspaces_same_user(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)

        async def pull_fn(host, port):
            return (
                {"settings.json": _f("old", 100.0)}
                if host == "10.0.0.1"
                else {"settings.json": _f("new", 200.0)}
            )

        workspaces = [
            {
                "user_id": UID,
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.2",
                        "port": 30022,
                    }
                },
            },
            {
                "user_id": UID,
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.1",
                        "port": 30022,
                    }
                },
            },
        ]
        await reconcile_ide_settings(store, workspaces, pull_fn)

        assert (await store.get_ide_files(UID))["settings.json"] == _f("new", 200.0)

    @pytest.mark.asyncio
    async def test_skips_workspace_without_target(self):
        store = IdeSettingsStore(FakeSettingsDB())
        called = False

        async def pull_fn(host, port):
            nonlocal called
            called = True
            return {}

        await reconcile_ide_settings(store, [{"user_id": UID, "context": {}}], pull_fn)
        assert called is False

    @pytest.mark.asyncio
    async def test_skips_rows_without_user_id(self):
        store = IdeSettingsStore(FakeSettingsDB())

        async def pull_fn(host, port):
            raise AssertionError("should not pull for row without user_id")

        await reconcile_ide_settings(
            store,
            [
                {
                    "context": {
                        "workspace_container": {
                            "status": "ready",
                            "pod_ip": "x",
                            "port": 30022,
                        }
                    }
                }
            ],
            pull_fn,
        )

    @pytest.mark.asyncio
    async def test_empty_pull_does_not_write(self):
        db = FakeSettingsDB(
            {UID: {"ide": {"files": {"settings.json": _f("keep", 5.0)}}}}
        )
        store = IdeSettingsStore(db)

        async def pull_fn(host, port):
            return {}

        count = await reconcile_ide_settings(
            store,
            [{"user_id": UID, "context": {"vm": {"status": "ready", "ssh_host": "h"}}}],
            pull_fn,
        )
        assert count == 0
        assert (await store.get_ide_files(UID))["settings.json"] == _f("keep", 5.0)

    @pytest.mark.asyncio
    async def test_string_context_is_parsed(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)

        async def pull_fn(host, port):
            return {"settings.json": _f("t", 9.0)}

        ws = [
            {
                "user_id": UID,
                "context": '{"workspace_container": {"status": "ready", "pod_ip": "10.0.0.5", "port": 30022}}',
            }
        ]
        count = await reconcile_ide_settings(store, ws, pull_fn)
        assert count == 1

    @pytest.mark.asyncio
    async def test_one_failing_pull_does_not_abort_others(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)

        async def pull_fn(host, port):
            if host == "bad":
                raise OSError("boom")
            return {"settings.json": _f("t", 9.0)}

        workspaces = [
            {"user_id": UID, "context": {"vm": {"status": "ready", "ssh_host": "bad"}}},
            {
                "user_id": UID,
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "good",
                        "port": 30022,
                    }
                },
            },
        ]
        count = await reconcile_ide_settings(store, workspaces, pull_fn)
        assert count == 1


class TestSeedIdeConfigForUser:
    @pytest.mark.asyncio
    async def test_no_user_id_is_noop(self):
        called = False

        async def runner(host, port, script, key_path=None, timeout=20):
            nonlocal called
            called = True
            return 0, b"", b""

        ok = await seed_ide_config_for_user(
            FakeSettingsDB(), None, "h", 22, _runner=runner
        )
        assert ok is True
        assert called is False

    @pytest.mark.asyncio
    async def test_no_stored_files_is_noop(self):
        called = False

        async def runner(host, port, script, key_path=None, timeout=20):
            nonlocal called
            called = True
            return 0, b"", b""

        ok = await seed_ide_config_for_user(
            FakeSettingsDB(), UID, "h", 22, _runner=runner
        )
        assert ok is True
        assert called is False

    @pytest.mark.asyncio
    async def test_seeds_stored_files(self):
        db = FakeSettingsDB(
            {UID: {"ide": {"files": {"settings.json": _f('{"t":1}', 100.0)}}}}
        )
        scripts = []

        async def runner(host, port, script, key_path=None, timeout=20):
            scripts.append(script)
            return 0, b"", b""

        ok = await seed_ide_config_for_user(db, UID, "10.0.0.5", 30022, _runner=runner)
        assert ok is True
        assert len(scripts) == 1
        assert base64.b64encode('{"t":1}'.encode()).decode() in scripts[0]


class TestGetIdeFiles:
    @pytest.mark.asyncio
    async def test_returns_empty_when_unset(self):
        store = IdeSettingsStore(FakeSettingsDB())
        assert await store.get_ide_files(UID) == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_ide_present_but_no_files(self):
        db = FakeSettingsDB({UID: {"ide": {"version": 1}}})
        store = IdeSettingsStore(db)
        assert await store.get_ide_files(UID) == {}


class TestExtensionList:
    def test_parses_id_version_and_theme_flag(self):
        out = (
            "monokai.theme-monokai-pro-vscode@2.0.13\tTHEME\n"
            "ms-python.python@2024.4.1\t-\n"
            "garbage-line-no-tab\n"
        )
        result = parse_extensions_list(out)
        assert result == {
            "monokai.theme-monokai-pro-vscode": {"version": "2.0.13", "theme": True},
            "ms-python.python": {"version": "2024.4.1", "theme": False},
        }

    def test_empty_output_is_empty_dict(self):
        assert parse_extensions_list("") == {}

    def test_list_script_targets_extensions_dir(self):
        script = build_extensions_list_script()
        assert "/var/lib/code-server/extensions" in script
        assert "package.json" in script


class TestOpenVsxClassifier:
    @pytest.mark.asyncio
    async def test_available_returns_openvsx(self):
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            return 200  # Open VSX has it

        clf = OpenVsxClassifier(fetch=fake_fetch)
        src = await clf.classify("monokai.theme-monokai-pro-vscode", "2.0.13")
        assert src == "openvsx"
        assert "monokai/theme-monokai-pro-vscode/2.0.13" in calls[0]

    @pytest.mark.asyncio
    async def test_missing_returns_bytes(self):
        async def fake_fetch(url):
            return 404

        clf = OpenVsxClassifier(fetch=fake_fetch)
        assert await clf.classify("acme.private", "1.0.0") == "bytes"

    @pytest.mark.asyncio
    async def test_result_is_cached(self):
        n = {"calls": 0}

        async def fake_fetch(url):
            n["calls"] += 1
            return 200

        clf = OpenVsxClassifier(fetch=fake_fetch)
        await clf.classify("a.b", "1.0.0")
        await clf.classify("a.b", "1.0.0")
        assert n["calls"] == 1  # cached by (id, version)

    @pytest.mark.asyncio
    async def test_fetch_error_defaults_to_bytes(self):
        async def boom(url):
            raise RuntimeError("network down")

        clf = OpenVsxClassifier(fetch=boom)
        assert await clf.classify("a.b", "1.0.0") == "bytes"


class TestExtensionStore:
    @pytest.mark.asyncio
    async def test_apply_then_get_roundtrip(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        items = {
            "monokai.theme-monokai-pro-vscode": {
                "version": "2.0.13",
                "source": "openvsx",
                "theme": True,
            }
        }
        changed = await store.apply_extensions(UID, items)
        assert changed == ["monokai.theme-monokai-pro-vscode"]
        got = await store.get_extensions(UID)
        assert got["monokai.theme-monokai-pro-vscode"]["version"] == "2.0.13"

    @pytest.mark.asyncio
    async def test_newer_version_wins_union_across_calls(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        await store.apply_extensions(
            UID, {"a.b": {"version": "1.0.0", "source": "openvsx", "theme": False}}
        )
        # second workspace has a.b older + a new extension c.d
        await store.apply_extensions(
            UID,
            {
                "a.b": {"version": "0.9.0", "source": "openvsx", "theme": False},
                "c.d": {"version": "3.1.0", "source": "openvsx", "theme": False},
            },
        )
        got = await store.get_extensions(UID)
        assert got["a.b"]["version"] == "1.0.0"  # newer kept (union, not clobber)
        assert got["c.d"]["version"] == "3.1.0"  # new one added

    @pytest.mark.asyncio
    async def test_apply_preserves_sibling_files_subtree(self):
        db = FakeSettingsDB({UID: {"ide": {"files": {"settings.json": _f("x", 1.0)}}}})
        store = IdeSettingsStore(db)
        await store.apply_extensions(
            UID, {"a.b": {"version": "1.0.0", "source": "openvsx", "theme": False}}
        )
        files = await store.get_ide_files(UID)
        assert files["settings.json"] == _f("x", 1.0)  # not clobbered by shallow merge


class TestExtensionInstallScript:
    def test_empty_is_noop(self):
        assert build_extension_install_script({}) == "exit 0\n"

    def test_only_openvsx_items_installed(self):
        items = {
            "monokai.theme-monokai-pro-vscode": {
                "version": "2.0.13",
                "source": "openvsx",
                "theme": True,
            },
            "ms-python.python": {
                "version": "2024.4.1",
                "source": "openvsx",
                "theme": False,
            },
            "acme.private": {"version": "1.0.0", "source": "bytes", "theme": False},
        }
        script = build_extension_install_script(items)
        assert "monokai.theme-monokai-pro-vscode@2.0.13" in script
        assert "ms-python.python@2024.4.1" in script
        assert "acme.private" not in script  # bytes source not installed here
        assert "--extensions-dir /var/lib/code-server/extensions" in script
        # theme installs synchronously before the backgrounded block
        theme_idx = script.index("monokai.theme-monokai-pro-vscode")
        bg_idx = script.index("ms-python.python")
        assert theme_idx < bg_idx


class TestReconcileExtensions:
    @pytest.mark.asyncio
    async def test_lists_classifies_and_stores(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        workspaces = [
            {"user_id": UID, "context": {"workspace_container": {"pod_ip": "10.0.0.1"}}}
        ]

        async def list_fn(host, port):
            assert (host, port) == ("10.0.0.1", 30022)
            return {"a.b": {"version": "1.0.0", "theme": True}}

        class FakeClf:
            async def classify(self, ext_id, version):
                return "openvsx"

        n = await reconcile_extensions(store, workspaces, list_fn, FakeClf())
        assert n == 1
        got = await store.get_extensions(UID)
        assert got["a.b"] == {"version": "1.0.0", "source": "openvsx", "theme": True}

    @pytest.mark.asyncio
    async def test_unreachable_workspace_skipped(self):
        db = FakeSettingsDB()
        store = IdeSettingsStore(db)
        workspaces = [
            {"user_id": UID, "context": {"workspace_container": {"pod_ip": "10.0.0.1"}}}
        ]

        async def list_fn(host, port):
            raise RuntimeError("ssh refused")

        class FakeClf:
            async def classify(self, ext_id, version):
                return "openvsx"

        assert await reconcile_extensions(store, workspaces, list_fn, FakeClf()) == 0
