"""Request config selectors cannot escape the installed harness assets."""

from pathlib import Path

import pytest

from shared.runtime.core import loader


@pytest.fixture
def config_tree(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = root / "config"
    expert = config / "experts" / "example"
    expert.mkdir(parents=True)
    (expert / "config.yaml").write_text("name: example\n")
    (config / "overlays").mkdir()
    (config / "overlays" / "worker.yaml").write_text("name: worker\n")
    outside = root / "config-other"
    outside.mkdir()
    (outside / "config.yaml").write_text("name: outside\n")
    monkeypatch.setattr(loader, "get_project_root", lambda: root)
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize(
    "selector",
    ["example", "experts/example", "config/experts/example/config.yaml"],
)
def test_bundled_selectors_still_load(config_tree, selector):
    path, directory = loader.resolve_bundled_config_path(selector)
    assert Path(path) == config_tree / "config/experts/example/config.yaml"
    assert loader.load_and_merge_config(path)["name"] == "example"
    if directory:
        assert Path(directory) == config_tree / "config/experts/example"


@pytest.mark.parametrize("selector", ["defaults", "worker_base", "overlays/worker"])
def test_role_aliases_still_resolve(config_tree, selector):
    path, directory = loader.resolve_bundled_config_path(selector)
    assert Path(path) == config_tree / "config/overlays/worker.yaml"
    assert directory is None


@pytest.mark.parametrize(
    "selector",
    [
        "../config-other",
        "config/../config-other/config.yaml",
        "config-other/config.yaml",
    ],
)
def test_relative_escape_is_rejected(config_tree, selector):
    with pytest.raises(ValueError, match="installed config tree"):
        loader.resolve_bundled_config_path(selector)


def test_absolute_sibling_with_shared_prefix_is_rejected(config_tree):
    with pytest.raises(ValueError, match="installed config tree"):
        loader.resolve_bundled_config_path(
            str(config_tree / "config-other/config.yaml")
        )


@pytest.mark.parametrize("link_to_directory", [True, False])
def test_symlink_escape_is_rejected(config_tree, link_to_directory):
    if link_to_directory:
        (config_tree / "config/linked").symlink_to(config_tree / "config-other")
        selector = "linked"
    else:
        (config_tree / "config/linked.yaml").symlink_to(
            config_tree / "config-other/config.yaml"
        )
        selector = "config/linked.yaml"
    with pytest.raises(ValueError, match="installed config tree"):
        loader.resolve_bundled_config_path(selector)


def test_cli_can_still_load_an_explicit_external_file(config_tree):
    external = str(config_tree / "config-other/config.yaml")
    path, directory = loader.resolve_config_path(external)
    assert path == external
    assert directory is None
    assert loader.load_and_merge_config(path)["name"] == "outside"


def test_session_reload_rejects_external_file(config_tree):
    from agent.api.persistent_app import _load_expert_config

    with pytest.raises(ValueError, match="installed config tree"):
        _load_expert_config(str(config_tree / "config-other/config.yaml"))


def test_orchestrator_resolution_rejects_external_file(config_tree):
    from orchestrator.services.config_resolver import resolve_config

    with pytest.raises(ValueError, match="installed config tree"):
        resolve_config(base_config_name=str(config_tree / "config-other/config.yaml"))
