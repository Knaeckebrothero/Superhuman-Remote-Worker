"""``load_agent_config_from_dict(asdict(cfg))`` must round-trip ``config.extra``.

The live ``config.update`` path (``src/api/persistent_app.py:7107-7121``) rebuilds
the session config by serializing it with ``dataclasses.asdict``, deep-merging the
override, and re-loading. ``asdict`` emits a top-level ``"extra"`` key, so unless
the loader recognizes it the whole namespace is re-buried as ``extra["extra"]``
and every ``extra`` key silently disappears from the new config.

That is not a hypothetical: it is the root cause of
``knowledge-base/knowledge/issues/live_config_update_buries_extra_and_empties_the_shell_group.md``,
where the loss of ``extra.shell.mode`` dropped the session to the stateless floor
and the shell tool group bound only ``shell_read``.
"""

import dataclasses

from shared.runtime.core.loader import (
    SubagentsConfig,
    get_all_tool_names,
    load_agent_config,
    load_agent_config_from_dict,
    resolve_config_path,
)


def _session_config():
    path, deployment_dir = resolve_config_path("session_base")
    return load_agent_config(path, deployment_dir)


def _round_trip(config):
    """Exactly what the live ``config.update`` path does, minus the override."""
    return load_agent_config_from_dict(
        dataclasses.asdict(config), deployment_dir=config._deployment_dir
    )


def test_asdict_round_trip_preserves_extra_keys():
    """The round-trip identity the live-update path depends on."""
    config = _session_config()
    config.extra["shell"] = {"mode": "persistent"}
    config.extra["cloud_scan_guard"] = "block"

    restored = _round_trip(config)

    assert restored.extra.get("shell") == {"mode": "persistent"}
    assert restored.extra.get("cloud_scan_guard") == "block"


def test_asdict_round_trip_does_not_nest_extra_under_itself():
    """``extra["extra"]`` is the burial signature — it must never appear."""
    config = _session_config()
    config.extra["shell"] = {"mode": "persistent"}

    restored = _round_trip(config)

    assert "extra" not in restored.extra


def test_shell_mode_survives_the_round_trip():
    """The concrete regression: losing ``extra.shell.mode`` silently rewrites the
    shell tool names, because ``get_all_tool_names`` aliases
    ``shell_execute``/``run_command`` off that mode (``loader.py:4668-4678``)."""
    config = _session_config()
    config.extra["shell"] = {"mode": "persistent"}
    config.tools.shell = [
        "cancel_command",
        "run_command",
        "shell_execute",
        "shell_read",
    ]
    # Assigning the field directly bypasses ToolsConfig.__post_init__, which
    # drops `git` whenever `shell` is present. The round trip below goes
    # through the constructor and WOULD apply it, so mirror what a properly
    # constructed shell-having config looks like — otherwise this test fails
    # on the git suppression rather than on the shell-mode aliasing it exists
    # to guard.
    config.tools.git = []

    before = get_all_tool_names(config)
    after = get_all_tool_names(_round_trip(config))

    assert "shell_execute" in before, (
        "precondition: persistent mode aliases to shell_execute"
    )
    assert before == after


def test_round_trip_does_not_leak_deployment_dir_into_extra():
    """``asdict`` also emits ``_deployment_dir``, which the loader takes as an
    explicit argument — it is config plumbing, not an ``extra`` key."""
    config = _session_config()

    restored = _round_trip(config)

    assert "_deployment_dir" not in restored.extra


def test_round_trip_is_idempotent_for_a_config_with_no_extra():
    """Guard the empty case so the fix cannot special-case its way to green."""
    config = _session_config()
    config.extra.clear()

    restored = _round_trip(config)

    assert "extra" not in restored.extra


def test_tags_and_subagents_survive_the_round_trip_and_stay_out_of_extra():
    """U1 WP3: ``tags`` and ``subagents`` are parsed fields. ``asdict`` emits
    them at the top level; the loader must take them back as the typed
    fields — never bury them in ``extra`` (which is what happened to
    ``subagents`` before WP3, when the light runner read it from there)."""
    config = _session_config()
    config.tags = ["session", "assistant"]
    config.subagents = SubagentsConfig(
        default="explorer",
        llm={"model": "claude-haiku-4-5"},
        roster={
            "explorer": {
                "agent_id": "explorer",
                "display_name": "Explorer",
                "llm": {"model": "claude-haiku-4-5"},
                "tools": {"workspace": ["read_file"]},
                "isolation": "shared",
            }
        },
    )

    restored = _round_trip(config)

    assert restored.tags == ["session", "assistant"]
    assert restored.subagents == config.subagents
    assert isinstance(restored.subagents, SubagentsConfig)
    assert "tags" not in restored.extra
    assert "subagents" not in restored.extra


def test_default_tags_and_subagents_round_trip_empty():
    """The empty shapes must not turn into extra keys either."""
    config = _session_config()
    assert config.tags == []
    assert config.subagents == SubagentsConfig()

    restored = _round_trip(config)

    assert restored.tags == []
    assert restored.subagents == SubagentsConfig()
    assert "subagents" not in restored.extra and "tags" not in restored.extra
