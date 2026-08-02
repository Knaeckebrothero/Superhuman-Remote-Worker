"""src/core/tool_report.py — measured vs predicted, and the three states.

The property that matters most here is the one D6 exists to protect: when the
agent has reported, the agent's answer wins. ``compose_tool_view`` may use the
merged config to EXPLAIN a measurement and must never use it to correct one —
a "fix-up" is a second implementation of the same fact, which is the original
bug in this series.
"""

import pytest

from src.core.capability_grants import CATALOG, evaluate
from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES
from src.core.tool_report import (
    DECIDED_BY_BACKEND,
    DECIDED_BY_GRANT,
    DECIDED_BY_REGISTRY,
    DECIDED_BY_RUNTIME,
    DECIDED_BY_UNSET,
    GRANT_GATED_CATEGORIES,
    REPORT_VERSION,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    UNCLASSIFIED_CATEGORY,
    ToolReportError,
    backend_blocked_categories,
    backend_capabilities,
    build_agent_toolset_report,
    categorize_tool_names,
    code_granted_categories,
    compose_tool_view,
    grant_blocked_categories,
    layer_provenance,
    read_agent_toolset_report,
    report_categories,
    tool_groups_from_view,
)


class _Backend:
    def __init__(self, shell=True, files=True, canvas=True):
        self.supports_shell = shell
        self.supports_file_tools = files
        self.supports_canvas_presentation = canvas


# =============================================================================
# Vocabulary — every category a config can name is a category we answer for
# =============================================================================
class TestVocabulary:
    def test_covers_every_config_addressable_category(self):
        from src.core.tool_policy import config_tool_categories

        assert set(report_categories()) == set(config_tool_categories())

    def test_covers_far_more_than_the_four_closed_groups(self):
        """The shipped endpoint answered for 4 of 24; that was the defect."""
        assert set(SESSION_TOOL_OVERRIDE_NAMES) < set(report_categories())
        assert len(report_categories()) >= 24

    def test_mcp_is_in_the_vocabulary_despite_having_no_static_membership(self):
        assert "mcp" in report_categories()


class TestCategorizeToolNames:
    def test_groups_by_registry_category(self):
        grouped = categorize_tool_names(["read_file", "web_search", "read_file"])
        assert grouped["workspace"] == ["read_file", "read_file"]
        assert grouped["research"] == ["web_search"]

    def test_preserves_author_order(self):
        grouped = categorize_tool_names(["write_file", "read_file"])
        assert grouped["workspace"] == ["write_file", "read_file"]

    def test_unknown_names_are_named_not_dropped(self):
        """An MCP tool categorised outside the agent must not vanish."""
        grouped = categorize_tool_names(["definitely_not_a_registered_tool"])
        assert grouped[UNCLASSIFIED_CATEGORY] == ["definitely_not_a_registered_tool"]


# =============================================================================
# The agent's half — measurement
# =============================================================================
class TestAgentReport:
    def test_reports_the_names_it_was_given_verbatim(self):
        report = build_agent_toolset_report(
            thread_id="t1", tool_names=["read_file", "web_search"]
        )
        assert report["tools"] == ["read_file", "web_search"]
        assert report["categories"]["workspace"] == ["read_file"]
        assert report["version"] == REPORT_VERSION
        assert report["observed_at"]

    def test_backend_is_none_when_there_is_no_backend(self):
        report = build_agent_toolset_report(thread_id="t1", tool_names=[])
        assert report["backend"] is None

    def test_backend_capabilities_are_carried(self):
        report = build_agent_toolset_report(
            thread_id="t1", tool_names=[], backend=_Backend(shell=False)
        )
        assert report["backend"] == {
            "supports_shell": False,
            "supports_file_tools": True,
            "supports_canvas_presentation": True,
        }

    def test_empty_toolset_is_a_legitimate_measurement(self):
        report = build_agent_toolset_report(thread_id="t1", tool_names=[])
        assert report["tools"] == []
        assert report["categories"] == {}

    def test_round_trips_through_the_reader(self):
        report = build_agent_toolset_report(
            thread_id="t1", tool_names=["read_file", "web_search"]
        )
        assert read_agent_toolset_report(report) == {
            "workspace": ["read_file"],
            "research": ["web_search"],
        }


class TestReaderRefusesRatherThanGuesses:
    """A mis-parsed report would be served as a MEASUREMENT. Refuse instead."""

    def test_unknown_version_is_refused(self):
        with pytest.raises(ToolReportError, match="version"):
            read_agent_toolset_report({"version": 99, "categories": {}})

    def test_missing_version_is_refused(self):
        with pytest.raises(ToolReportError, match="version"):
            read_agent_toolset_report({"categories": {}})

    def test_missing_categories_is_refused(self):
        with pytest.raises(ToolReportError, match="categories"):
            read_agent_toolset_report({"version": REPORT_VERSION})

    def test_non_dict_is_refused(self):
        with pytest.raises(ToolReportError):
            read_agent_toolset_report(["read_file"])

    def test_malformed_category_value_is_refused(self):
        with pytest.raises(ToolReportError, match="malformed"):
            read_agent_toolset_report(
                {"version": REPORT_VERSION, "categories": {"workspace": "read_file"}}
            )


# =============================================================================
# Gates
# =============================================================================
class TestBackendGate:
    def test_no_backend_gates_nothing(self):
        assert backend_capabilities(None) is None
        assert backend_blocked_categories(None) == {}

    def test_no_shell_blocks_exactly_the_execution_categories(self):
        from src.tools.registry import _EXECUTION_CATEGORIES

        blocked = backend_blocked_categories(
            backend_capabilities(_Backend(shell=False))
        )
        assert set(blocked) == set(_EXECUTION_CATEGORIES)

    def test_no_file_tools_blocks_workspace_and_canvas(self):
        blocked = backend_blocked_categories(
            backend_capabilities(_Backend(files=False))
        )
        assert "workspace" in blocked and "canvas" in blocked

    def test_no_canvas_presentation_blocks_only_canvas(self):
        blocked = backend_blocked_categories(
            backend_capabilities(_Backend(canvas=False))
        )
        assert set(blocked) == {"canvas"}

    def test_a_fully_capable_backend_blocks_nothing(self):
        assert backend_blocked_categories(backend_capabilities(_Backend())) == {}


class TestGrantGate:
    def test_admin_bypass_blocks_nothing(self):
        assert grant_blocked_categories(None) == {}

    def test_missing_shell_tools_blocks_shell_with_a_reason(self):
        blocked = grant_blocked_categories({"shell_tools": False})
        assert "shell_tools" in blocked["shell"]

    def test_absent_key_falls_back_to_the_catalog_default(self):
        """`{}` is deny-by-default for shell_tools and permissive for browser."""
        blocked = grant_blocked_categories({})
        assert "shell" in blocked
        assert "browser_direct" not in blocked

    def test_datasource_tools_blocks_every_connector_category(self):
        blocked = grant_blocked_categories({"datasource_tools": False})
        assert GRANT_GATED_CATEGORIES["datasource_tools"] <= set(blocked)


class TestGrantMapMatchesThePDP:
    """Re-derive GRANT_GATED_CATEGORIES by running the real PDP.

    This module explains a denial; ``capability_grants.evaluate`` enforces it.
    Two statements of one rule is exactly the shape of bug this series exists
    to kill, so the literal is checked against the PDP rather than trusted.
    """

    @pytest.mark.parametrize("grant_key", sorted(GRANT_GATED_CATEGORIES))
    def test_each_gate_denies_exactly_the_categories_claimed(self, grant_key):
        denying = {
            k: CATALOG[k]["default"] for k in CATALOG if CATALOG[k]["type"] != "list"
        }
        denying[grant_key] = False
        actually_denied = set()
        for category in report_categories():
            fragment = {"tools": {category: ["_probe_"]}}
            violations = evaluate(fragment, denying)
            if any(v.startswith(f"{grant_key}:") for v in violations):
                actually_denied.add(category)
        assert actually_denied == GRANT_GATED_CATEGORIES[grant_key]


class TestCodeGrantedCategories:
    def test_matches_the_registry(self):
        from src.tools.registry import CODE_GRANTED_CATEGORIES

        assert set(code_granted_categories()) == set(CODE_GRANTED_CATEGORIES)


# =============================================================================
# Provenance
# =============================================================================
class TestLayerProvenance:
    def test_the_last_layer_naming_a_category_owns_it(self):
        prov = layer_provenance(
            [
                ("base", {"tools": {"workspace": ["read_file"], "shell": []}}),
                ("expert", {"tools": {"shell": ["run_command"]}}),
                ("request", {"tools": {"canvas": []}}),
            ]
        )
        assert prov == {"workspace": "base", "shell": "expert", "canvas": "request"}

    def test_layers_without_tools_are_skipped(self):
        prov = layer_provenance([("base", {"llm": {}}), ("expert", None)])
        assert prov == {}

    def test_legacy_coding_alias_maps_to_shell(self):
        prov = layer_provenance([("expert", {"tools": {"coding": ["run_command"]}})])
        assert prov == {"shell": "expert"}


# =============================================================================
# Composition — the heart of D6
# =============================================================================
class TestMeasurementWins:
    def test_agent_answer_overrides_a_disagreeing_config(self):
        """The config says core is empty; the agent bound two core tools.

        This is the runtime-injection layer, and the whole reason D6 says ask
        the agent. A view that reported ``off`` here would be the original bug
        wearing a new endpoint.
        """
        view = compose_tool_view(
            measured={"core": ["read_product_guide", "srw_cloud_status"]},
            configured={"core": []},
        )
        assert view["core"]["state"] == STATE_ON
        assert view["core"]["tools"] == ["read_product_guide", "srw_cloud_status"]
        assert view["core"]["decided_by"] == DECIDED_BY_RUNTIME

    def test_agent_answer_overrides_a_config_that_promised_tools(self):
        view = compose_tool_view(
            measured={},
            configured={"research": ["web_search"]},
        )
        assert view["research"]["state"] == STATE_OFF
        assert view["research"]["tools"] == []
        assert view["research"]["configured"] == ["web_search"]

    def test_measured_entries_carry_the_config_alongside_for_comparison(self):
        view = compose_tool_view(
            measured={"research": ["web_search"]},
            configured={"research": ["web_search", "web_fetch"]},
        )
        assert view["research"]["tools"] == ["web_search"]
        assert view["research"]["configured"] == ["web_search", "web_fetch"]

    def test_prediction_has_no_configured_key(self):
        """No measurement means nothing to compare against — say so by shape."""
        view = compose_tool_view(measured=None, configured={"research": ["web_search"]})
        assert "configured" not in view["research"]
        assert view["research"]["tools"] == ["web_search"]


class TestThreeStates:
    def test_on_off_and_unavailable_are_all_reachable(self):
        view = compose_tool_view(
            measured={"research": ["web_search"]},
            configured={"research": ["web_search"], "citation": []},
            grants={"shell_tools": False},
        )
        assert view["research"]["state"] == STATE_ON
        assert view["citation"]["state"] == STATE_OFF
        assert view["shell"]["state"] == STATE_UNAVAILABLE

    def test_unavailable_always_carries_a_reason(self):
        view = compose_tool_view(
            measured={},
            configured={},
            grants={"shell_tools": False},
            backend_caps={"supports_shell": False},
        )
        for name, entry in view.items():
            if entry["state"] == STATE_UNAVAILABLE:
                assert entry["reason"], f"{name} unavailable with no reason"

    def test_off_never_carries_a_reason(self):
        view = compose_tool_view(measured={}, configured={"citation": []})
        assert view["citation"]["state"] == STATE_OFF
        assert view["citation"]["reason"] is None
        assert view["citation"]["settable"] is True

    def test_grant_denial_beats_a_plain_off(self):
        """D4/1.4: a user without shell_tools must see a REASON, not a blank box."""
        view = compose_tool_view(
            measured={}, configured={"shell": []}, grants={"shell_tools": False}
        )
        assert view["shell"]["state"] == STATE_UNAVAILABLE
        assert view["shell"]["settable"] is False
        assert "shell_tools" in view["shell"]["reason"]
        assert view["shell"]["decided_by"] == DECIDED_BY_GRANT

    def test_backend_denial_reads_as_the_tier_not_the_config(self):
        view = compose_tool_view(
            measured={},
            configured={"shell": ["run_command"]},
            grants={"shell_tools": True},
            backend_caps={"supports_shell": False, "supports_file_tools": True},
        )
        assert view["shell"]["state"] == STATE_UNAVAILABLE
        assert view["shell"]["decided_by"] == DECIDED_BY_BACKEND
        assert "workspace tier" in view["shell"]["reason"]

    def test_grant_denial_outranks_the_backend(self):
        view = compose_tool_view(
            measured={},
            configured={},
            grants={"shell_tools": False},
            backend_caps={"supports_shell": False},
        )
        assert view["shell"]["decided_by"] == DECIDED_BY_GRANT
        assert "shell_tools" in view["shell"]["reason"]

    def test_code_granted_category_is_unavailable_when_empty(self):
        view = compose_tool_view(measured={}, configured={"sql": []})
        assert view["sql"]["state"] == STATE_UNAVAILABLE
        assert view["sql"]["settable"] is False
        assert "not by config" in view["sql"]["reason"]
        assert view["sql"]["decided_by"] == DECIDED_BY_REGISTRY

    def test_code_granted_category_can_still_be_on(self):
        """A datasource attach turns `sql` on without config saying anything."""
        view = compose_tool_view(
            measured={"sql": ["sql_query"]}, configured={"sql": []}
        )
        assert view["sql"]["state"] == STATE_ON
        assert view["sql"]["settable"] is False
        assert view["sql"]["reason"]


class TestProvenanceInTheView:
    def test_falls_through_to_the_authored_layer_when_nothing_intervened(self):
        view = compose_tool_view(
            measured={"research": ["web_search"]},
            configured={"research": ["web_search"]},
            provenance={"research": "expert"},
        )
        assert view["research"]["decided_by"] == "expert"

    def test_unset_when_no_layer_named_it(self):
        view = compose_tool_view(measured={}, configured={})
        assert view["evaluation"]["decided_by"] == DECIDED_BY_UNSET

    def test_runtime_wins_over_the_authored_layer_when_they_disagree(self):
        view = compose_tool_view(
            measured={"research": ["web_search", "extra_injected"]},
            configured={"research": ["web_search"]},
            provenance={"research": "expert"},
        )
        assert view["research"]["decided_by"] == DECIDED_BY_RUNTIME


class TestToolGroupsProjection:
    def test_derives_the_four_booleans_from_the_view(self):
        view = compose_tool_view(
            measured={"orchestrator": ["create_worker_job"], "canvas": []},
            configured={},
        )
        groups = tool_groups_from_view(view)
        assert set(groups) == set(SESSION_TOOL_OVERRIDE_NAMES)
        assert groups["orchestrator"] is True
        assert groups["canvas"] is False

    def test_a_group_absent_from_the_view_reads_false(self):
        assert tool_groups_from_view({}) == {
            g: False for g in SESSION_TOOL_OVERRIDE_NAMES
        }


class TestCategoriesOutsideTheVocabulary:
    def test_an_unclassified_measured_bucket_is_still_reported(self):
        view = compose_tool_view(
            measured={UNCLASSIFIED_CATEGORY: ["some_mcp_tool"]}, configured={}
        )
        assert view[UNCLASSIFIED_CATEGORY]["state"] == STATE_ON
        assert view[UNCLASSIFIED_CATEGORY]["tools"] == ["some_mcp_tool"]

    def test_every_known_category_appears_even_with_no_data(self):
        view = compose_tool_view(measured={}, configured={})
        assert set(report_categories()) <= set(view)
