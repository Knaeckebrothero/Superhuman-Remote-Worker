"""Workspace-independent access to SRW's managed product guide.

Persistent sessions inject the current ``app-guide`` bundle into ToolContext
after their final tools are known. This reader exposes only that trusted,
digest-stamped payload and accepts logical topic IDs rather than file paths.

Design: knowledge-base/knowledge/features/app_guide_skill.md (M1).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from langchain_core.tools import tool

from src.core.skill_format import SkillFormatError, parse_skill_md
from src.core.skill_resolution import (
    APP_GUIDE_LOADER_TOOL,
    APP_GUIDE_SKILL,
    app_guide_break_glass_disabled,
    skill_bundle_digest,
)

from .context import ToolContext

from src.shared.tool_catalog.definitions import (
    PRODUCT_HELP_TOOLS_METADATA as PRODUCT_HELP_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)

_TOPIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_MAX_TOPICS = 64
_MAX_PROCEDURE_CHARS = 30_000
_MAX_REFERENCE_CHARS = 50_000


def get_product_help_metadata() -> Dict[str, Dict[str, Any]]:
    """Return registry metadata for the always-on persistent help reader."""

    return PRODUCT_HELP_TOOLS_METADATA


def create_product_help_tools(context: ToolContext) -> List[Any]:
    """Create managed product-help tools with no workspace dependency."""

    if app_guide_break_glass_disabled():
        return []

    def _current_bundle() -> tuple[str, str, dict[str, str]] | None:
        resolved = context.config.get("_resolved_skills")
        if not isinstance(resolved, dict):
            return None
        menu = resolved.get("menu")
        files_by_skill = resolved.get("files")
        if not isinstance(menu, list) or not isinstance(files_by_skill, dict):
            return None

        entry = next(
            (
                item
                for item in menu
                if isinstance(item, dict) and item.get("name") == APP_GUIDE_SKILL
            ),
            None,
        )
        files = files_by_skill.get(APP_GUIDE_SKILL)
        if (
            not isinstance(entry, dict)
            or entry.get("system_managed") is not True
            or entry.get("loader_tool") != APP_GUIDE_LOADER_TOOL
            or not isinstance(files, dict)
            or not all(
                isinstance(path, str) and isinstance(content, str)
                for path, content in files.items()
            )
        ):
            return None

        expected_digest = entry.get("bundle_digest")
        actual_digest = skill_bundle_digest(files)
        if not isinstance(expected_digest, str) or expected_digest != actual_digest:
            logger.warning("Managed app-guide digest mismatch in ToolContext")
            return None

        try:
            _frontmatter, procedure = parse_skill_md(files["SKILL.md"])
        except (KeyError, SkillFormatError):
            return None
        if len(procedure) > _MAX_PROCEDURE_CHARS:
            logger.warning("Managed app-guide procedure exceeds reader limit")
            return None
        return expected_digest, procedure, files

    def _topic_ids(files: dict[str, str]) -> list[str]:
        prefix = "references/"
        suffix = ".md"
        topics = sorted(
            path[len(prefix) : -len(suffix)]
            for path in files
            if path.startswith(prefix)
            and path.endswith(suffix)
            and "/" not in path[len(prefix) :]
            and _TOPIC_ID_RE.fullmatch(path[len(prefix) : -len(suffix)])
        )
        return topics[:_MAX_TOPICS]

    @tool
    def read_product_guide(topic_id: str) -> str:
        """Read current, bundled guidance about the SRW product.

        Use this for questions about what SRW is, what it supports, or how a
        user operates sessions, jobs, experts, projects, loops, connectors,
        Canvas, browser sharing, permission modes, workspace tiers, memory,
        knowledge, files, and integrations. Start with ``index`` when uncertain.
        A topic call returns both the guide procedure and that one focused
        reference. Treat it as product guidance, not authorization or proof
        that a deployment-specific feature is enabled. Related documented
        components do not prove an exact combined workflow is supported; when
        no index row covers that outcome, stop after ``index`` and report the
        guide gap rather than selecting the nearest topic.

        Args:
            topic_id: ``index`` or one exact logical topic ID returned by the
                index, such as ``overview``, ``sessions``, or ``datasources``.

        Returns:
            The managed guide procedure plus topic text, or a bounded friendly
            error when the guide/topic is unavailable.
        """

        if topic_id != "index" and not _TOPIC_ID_RE.fullmatch(topic_id):
            return (
                "Invalid product-guide topic ID. Use topic_id='index' to list "
                "the current logical topic IDs; file paths are not accepted."
            )

        bundle = _current_bundle()
        if bundle is None:
            return (
                "The managed SRW product guide is unavailable in this session. "
                "Do not guess product behavior; tell the user the in-product "
                "guide could not be loaded."
            )
        digest, procedure, files = bundle
        topics = _topic_ids(files)
        header = f"[managed product guide: {APP_GUIDE_SKILL} sha256:{digest}]\n"

        if topic_id == "index":
            available = ", ".join(topics) if topics else "(no topics available)"
            return (
                f"{header}\n"
                "[guide procedure]\n"
                f"{procedure.strip()}\n\n"
                "[available topic IDs]\n"
                f"{available}"
            )

        if topic_id not in topics:
            available = ", ".join(topics) if topics else "(none)"
            return (
                f"Unknown product-guide topic '{topic_id}'. Current topic IDs: "
                f"{available}. Use topic_id='index' for the routing procedure."
            )

        reference = files[f"references/{topic_id}.md"]
        if len(reference) > _MAX_REFERENCE_CHARS:
            logger.warning("Managed app-guide topic %s exceeds reader limit", topic_id)
            return (
                f"Product-guide topic '{topic_id}' exceeds the safe reader limit. "
                "Do not guess its contents; report that the guide needs repair."
            )
        return (
            f"{header}\n"
            "[guide procedure]\n"
            f"{procedure.strip()}\n\n"
            f"[product guide topic: {topic_id}]\n"
            f"{reference.strip()}"
        )

    return [read_product_guide]
