"""Tool-name namespacing for MCP-provided tools.

Namespaced names must satisfy OpenAI-compatible function-name rules:
<=64 chars, [a-zA-Z0-9_-]. Server slug is capped at 16 chars; overflow or
collision appends a deterministic 4-char hash.
"""

import re

from agent.tools.mcp.naming import mcp_server_slug, namespace_mcp_tool


class TestServerSlug:
    def test_basic(self):
        assert mcp_server_slug("GitHub MCP") == "github_mcp"

    def test_strips_special_chars_and_collapses(self):
        assert mcp_server_slug("My--Server!! (prod)") == "my_server_prod"

    def test_caps_at_16(self):
        slug = mcp_server_slug("a very long server name indeed")
        assert len(slug) <= 16 and not slug.endswith("_")

    def test_empty_falls_back(self):
        assert mcp_server_slug("!!!") == "server"


class TestNamespaceTool:
    def test_shape(self):
        name = namespace_mcp_tool("github", "create_issue", set())
        assert name == "mcp__github__create_issue"

    def test_valid_charset_and_length(self):
        name = namespace_mcp_tool(
            mcp_server_slug("Some Extremely Long Server Name"),
            "a_tool_with_an_extremely_long_name_beyond_all_reason_x" * 2,
            set(),
        )
        assert len(name) <= 64
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)

    def test_collision_gets_hash_suffix(self):
        taken = {"mcp__github__create_issue"}
        name = namespace_mcp_tool("github", "create_issue", taken)
        assert name != "mcp__github__create_issue"
        assert len(name) <= 64

    def test_deterministic(self):
        a = namespace_mcp_tool("srv", "toolname" * 20, set())
        b = namespace_mcp_tool("srv", "toolname" * 20, set())
        assert a == b
