"""Catalogue readers share state within an app and isolate it across apps."""

from orchestrator.services.catalogue_resources import CatalogueResources


def test_two_catalogues_share_one_app_cache_but_another_app_reads_its_own(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_matrix = first_dir / "model_config_matrix.yaml"
    first_matrix.write_text("family:\n  settings:\n    temperature: 0.2\n")
    (second_dir / "model_config_matrix.yaml").write_text(
        "family:\n  settings:\n    temperature: 0.9\n"
    )
    first = CatalogueResources(config_dir=lambda: first_dir)
    second = CatalogueResources(config_dir=lambda: second_dir)
    expert_reader = first.load_settings_matrix
    model_reader = first.load_settings_matrix
    expert_settings = expert_reader(first.get_config_dir())
    first_matrix.write_text("family:\n  settings:\n    temperature: 0.5\n")

    assert model_reader(first.get_config_dir()) is expert_settings
    assert expert_settings["family"]["temperature"] == 0.2
    assert (
        second.load_settings_matrix(second.get_config_dir())["family"]["temperature"]
        == 0.9
    )


def test_missing_matrix_is_cached_without_affecting_a_new_app(tmp_path):
    first = CatalogueResources(config_dir=lambda: tmp_path)
    assert first.load_settings_matrix(tmp_path) == {}
    (tmp_path / "model_config_matrix.yaml").write_text(
        "legacy:\n  temperature: 0.7\nprompt_only:\n  prompts: {}\n"
    )
    assert first.load_settings_matrix(tmp_path) == {}
    second = CatalogueResources(config_dir=lambda: tmp_path)
    assert second.load_settings_matrix(tmp_path) == {"legacy": {"temperature": 0.7}}
