"""Shared packages — code imported by two or more SRW applications.

Modules here ship in every image (agent, orchestrator, MCP), so the bar for
entry is: used by >=2 apps, framework-free (no langchain/langgraph, no
imports from ``src.core``/``src.tools``/``orchestrator.*``), and dependencies
limited to stdlib + the small common set every image already installs
(httpx, tenacity). This is also the shared-code location the source-tree
flattening targets (knowledge-base/knowledge/features/source_tree_unification.md), so packages
born here do not move when the tree flattens.
"""
