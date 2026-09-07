"""Contracts and support code shared by SRW applications.

The root and all children except ``shared.runtime`` stay lightweight: no
LangChain/LangGraph and no application imports. Their dependencies are limited
to the small common set used by the agent, orchestrator and MCP images.

``shared.runtime`` holds reusable configuration, model, memory, storage and
workspace implementations used by the agent and orchestrator. It may use their
runtime dependencies, but cannot depend on any application package. Lightweight
shared code, MCP and the VM controller must not import that subtree. Import
Linter enforces these boundaries in ``pyproject.toml``.
"""
