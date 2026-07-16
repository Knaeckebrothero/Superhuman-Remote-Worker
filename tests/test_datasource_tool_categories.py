"""Tests for the shared datasource→tool-category map (live_session_settings.md P0.2).

The map lives once in ``src/core/datasource_setup.datasource_tool_categories``
and both trust boundaries delegate to it: the orchestrator's
``_build_datasource_tool_override`` and the agent's session attach path
(including the hydrated-attach fold via
``_apply_datasource_enrichment_to_resolved``). Before this, two
hand-maintained copies disagreed on read-write managed connectors
(agent: write tools; orchestrator: CLI-only).
"""

import pytest

from src.core.datasource_setup import (
    DATASOURCE_TOOL_MAP,
    datasource_tool_categories,
)


def _ds(ds_type: str, read_only: bool = False, name: str = "ds") -> dict:
    return {"type": ds_type, "name": name, "project_read_only": read_only}


class TestDatasourceToolCategories:
    def test_no_datasources_strips_all_categories(self):
        assert datasource_tool_categories([]) == {
            "graph": [],
            "sql": [],
            "mongodb": [],
            "webdav": [],
        }

    def test_read_only_managed_connector_gets_read_tools(self):
        cats = datasource_tool_categories([_ds("postgresql", read_only=True)])
        assert cats["sql"] == ["sql_query", "sql_schema"]
        # Other categories stripped, not omitted — stale tools from a
        # previously attached datasource must not survive.
        assert cats["graph"] == []
        assert cats["mongodb"] == []
        assert cats["webdav"] == []

    def test_read_write_managed_connector_is_cli_mode(self):
        """RW postgresql/neo4j/mongodb → no bound tools (CLI-mode policy,
        kept as the reconciled behavior; its remote-backend deadness is
        docs/issues/datasource_cli_mode_dead_on_remote.md)."""
        for ds_type, category in (
            ("postgresql", "sql"),
            ("neo4j", "graph"),
            ("mongodb", "mongodb"),
        ):
            cats = datasource_tool_categories([_ds(ds_type, read_only=False)])
            assert cats[category] == [], ds_type

    def test_read_write_webdav_gets_write_tools(self):
        cats = datasource_tool_categories([_ds("webdav", read_only=False)])
        assert "webdav_write" in cats["webdav"]
        assert "webdav_delete" in cats["webdav"]

    def test_mixed_same_type_any_read_write_wins(self):
        """Multiple datasources of one type: ALL must be read-only for read
        tools; a single RW one flips the type to the RW branch (the old
        agent-side copy keyed off the FIRST entry only)."""
        cats = datasource_tool_categories(
            [
                _ds("postgresql", read_only=True, name="a"),
                _ds("postgresql", read_only=False, name="b"),
            ]
        )
        assert cats["sql"] == []

        cats = datasource_tool_categories(
            [
                _ds("postgresql", read_only=True, name="a"),
                _ds("postgresql", read_only=True, name="b"),
            ]
        )
        assert cats["sql"] == ["sql_query", "sql_schema"]

    def test_unmapped_types_are_ignored(self):
        cats = datasource_tool_categories(
            [_ds("repository"), _ds("kb"), _ds("generic"), {"name": "typeless"}]
        )
        assert cats == {"graph": [], "sql": [], "mongodb": [], "webdav": []}

    def test_returns_copies_not_map_references(self):
        cats = datasource_tool_categories([_ds("webdav", read_only=True)])
        cats["webdav"].append("mutated")
        assert "mutated" not in DATASOURCE_TOOL_MAP["webdav"]["read"]


class TestOrchestratorDelegation:
    """The orchestrator wrapper must produce exactly the shared map's
    categories while preserving unrelated override content."""

    @pytest.fixture(scope="class")
    def build_override(self):
        from orchestrator.main import _build_datasource_tool_override

        return _build_datasource_tool_override

    def test_wrapper_matches_shared_function(self, build_override):
        datasources = [
            _ds("postgresql", read_only=True),
            _ds("webdav", read_only=False),
        ]
        override = build_override(datasources, None)
        assert override["tools"] == datasource_tool_categories(datasources)

    def test_wrapper_preserves_non_datasource_categories(self, build_override):
        override = build_override(
            [_ds("neo4j", read_only=True)],
            {"llm": {"model": "m"}, "tools": {"shell": ["run_command"]}},
        )
        assert override["llm"] == {"model": "m"}
        assert override["tools"]["shell"] == ["run_command"]
        assert override["tools"]["graph"] == ["cypher_query", "get_database_schema"]


class TestApplyDatasourceEnrichmentToResolved:
    """Hydrated attaches load resolved_config["agent"] and skip the
    config_override merge — the enrichment must be folded into the blob
    (dedicated-pod parity with warm-pool, live_session_settings.md P0.2)."""

    def test_folds_categories_and_cli_types_into_agent_dict(self):
        from src.api.persistent_app import _apply_datasource_enrichment_to_resolved

        resolved = {
            "agent": {
                "agent_id": "a",
                "tools": {"core": ["read_file"], "sql": ["stale_tool"]},
            },
            "prompts": {},
        }
        cats = {"sql": ["sql_query", "sql_schema"], "graph": []}
        _apply_datasource_enrichment_to_resolved(resolved, cats, ["postgresql"])

        agent = resolved["agent"]
        # Categories merged; unrelated categories preserved; stale replaced.
        assert agent["tools"]["sql"] == ["sql_query", "sql_schema"]
        assert agent["tools"]["graph"] == []
        assert agent["tools"]["core"] == ["read_file"]
        # _cli_datasources goes at the TOP level of the agent dict —
        # serialize_resolved_config flattens extra there, and
        # load_agent_config_from_dict folds unknown top-level keys back
        # into config.extra (which loader.py reads at prompt render).
        assert agent["_cli_datasources"] == ["postgresql"]
        assert "extra" not in agent

    def test_top_level_cli_key_reaches_config_extra_via_loader(self):
        """End-to-end through the real loader: the enrichment written by
        _apply_datasource_enrichment_to_resolved must surface as
        config.extra['_cli_datasources'] and config.tools.sql after
        load_config_from_resolved."""
        from src.api.persistent_app import _apply_datasource_enrichment_to_resolved
        from src.core.loader import load_config_from_resolved

        resolved = {
            "agent": {"agent_id": "a", "display_name": "A", "tools": {}},
            "prompts": {},
            "instructions": {},
        }
        _apply_datasource_enrichment_to_resolved(
            resolved, {"sql": ["sql_query", "sql_schema"]}, ["postgresql"]
        )
        config = load_config_from_resolved(resolved)
        assert config.extra["_cli_datasources"] == ["postgresql"]
        assert config.tools.sql == ["sql_query", "sql_schema"]

    def test_noop_on_missing_or_malformed_blob(self):
        from src.api.persistent_app import _apply_datasource_enrichment_to_resolved

        # None blob: nothing to do, must not raise.
        _apply_datasource_enrichment_to_resolved(None, {"sql": []}, ["postgresql"])

        # Malformed agent key: left untouched.
        resolved = {"agent": "not-a-dict"}
        _apply_datasource_enrichment_to_resolved(resolved, {"sql": []}, ["x"])
        assert resolved == {"agent": "not-a-dict"}

    def test_no_cli_types_leaves_top_level_unset(self):
        from src.api.persistent_app import _apply_datasource_enrichment_to_resolved

        resolved = {"agent": {"agent_id": "a", "tools": {}}}
        _apply_datasource_enrichment_to_resolved(resolved, {"sql": []}, [])
        assert "_cli_datasources" not in resolved["agent"]
