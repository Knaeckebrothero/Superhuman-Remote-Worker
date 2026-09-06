"""Shared catalog import isolation and live runtime registration contracts."""

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_catalog_policy_and_reporting_do_not_import_agent_execution():
    """Use a fresh process so the main test harness cannot hide eager imports."""
    script = """
import sys

from shared import tool_catalog
from shared.runtime.core.tool_policy import config_tool_categories, expand_category_true
from shared.runtime.core.tool_report import (
    categorize_tool_names,
    code_granted_categories,
    code_granted_tools,
)

assert tool_catalog.TOOL_REGISTRY['read_file']['category'] == 'workspace'
assert 'workspace' in config_tool_categories()
assert 'job_complete' in expand_category_true('core')
assert expand_category_true('mcp') == ['*']
assert categorize_tool_names(['read_file']) == {'workspace': ['read_file']}
assert 'session_task' in code_granted_categories()
assert 'srw_cloud_status' in code_granted_tools()

for prefix in (
    'agent.tools', 'agent.agent', 'agent.api', 'agent.graph',
    'shared.runtime.core.loader', 'agent.core.context',
    'langchain', 'langchain_core', 'langgraph',
):
    loaded = [name for name in sys.modules if name == prefix or name.startswith(prefix + '.')]
    assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def live_catalog():
    """Restore entries without replacing the shared dictionary identity."""
    from shared import tool_catalog
    from agent.tools import registry

    assert registry.TOOL_REGISTRY is tool_catalog.TOOL_REGISTRY
    original = tool_catalog.TOOL_REGISTRY.copy()
    try:
        yield tool_catalog, registry
    finally:
        tool_catalog.TOOL_REGISTRY.clear()
        tool_catalog.TOOL_REGISTRY.update(original)


def test_custom_registration_is_visible_to_shared_policy_and_reports(live_catalog):
    from shared.runtime.core.tool_policy import expand_category_true
    from shared.runtime.core.tool_report import categorize_tool_names

    catalog, runtime = live_catalog
    runtime.register_tool(
        "_catalog_contract_tool",
        module="custom",
        function="custom",
        description="A dynamically registered tool.",
        category="catalog_contract",
        phases=["strategic"],
    )

    assert catalog.get_tools_by_category("catalog_contract") == [
        "_catalog_contract_tool"
    ]
    assert expand_category_true("catalog_contract") == ["_catalog_contract_tool"]
    assert categorize_tool_names(["_catalog_contract_tool"]) == {
        "catalog_contract": ["_catalog_contract_tool"]
    }
    assert runtime.unregister_tool("_catalog_contract_tool") is True
    assert "catalog_contract" not in catalog.get_categories()


def test_mcp_discovery_replacement_and_detach_share_the_same_catalog(live_catalog):
    from shared.runtime.core.tool_policy import expand_category_true
    from shared.runtime.core.tool_report import categorize_tool_names

    catalog, runtime = live_catalog
    static_read_file = catalog.TOOL_REGISTRY["read_file"]

    def manager(name):
        tool = SimpleNamespace(
            name=name,
            description="Remote tool",
            metadata={
                "mcp_server": "remote",
                "mcp_server_slug": "remote",
                "mcp_tool_name": "search",
            },
        )
        return SimpleNamespace(get_langchain_tools=lambda: [tool], statuses={})

    runtime.register_mcp_tools(manager("mcp_remote_first"))
    assert catalog.get_tools_by_category("mcp") == ["mcp_remote_first"]
    assert categorize_tool_names(["mcp_remote_first"]) == {"mcp": ["mcp_remote_first"]}
    assert expand_category_true("mcp") == ["*"]

    runtime.register_mcp_tools(manager("mcp_remote_second"))
    assert catalog.get_tools_by_category("mcp") == ["mcp_remote_second"]
    assert runtime.expand_tool_wildcards(["read_file", "*"]) == [
        "read_file",
        "mcp_remote_second",
    ]
    assert catalog.TOOL_REGISTRY["mcp_remote_second"]["mcp_tool_name"] == "search"

    runtime.register_mcp_tools(None)
    assert catalog.get_tools_by_category("mcp") == []
    assert catalog.TOOL_REGISTRY["read_file"] is static_read_file
    assert expand_category_true("mcp") == ["*"]
