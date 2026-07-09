"""Expert and skill catalog tools for persistent sessions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from langchain_core.tools import tool

from ..context import ToolContext
from .jobs import _get_client, _get_orchestrator_url, _truncate

CATALOG_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "list_experts": {
        "module": "orchestrator.catalog",
        "function": "list_experts",
        "description": (
            "List bundled and visible user/global experts. Read-only expert "
            "catalog inspection."
        ),
        "category": "agent_catalog",
        "short_description": "List visible experts.",
        "phases": ["strategic", "tactical"],
    },
    "get_expert": {
        "module": "orchestrator.catalog",
        "function": "get_expert",
        "description": (
            "Get a compact summary of an expert's merged configuration, "
            "instructions preview, enabled tool categories, and effective models."
        ),
        "category": "agent_catalog",
        "short_description": "Inspect an expert.",
        "phases": ["strategic", "tactical"],
    },
    "list_skills": {
        "module": "orchestrator.catalog",
        "function": "list_skills",
        "description": "List bundled and visible user/global skills.",
        "category": "agent_catalog",
        "short_description": "List visible skills.",
        "phases": ["strategic", "tactical"],
    },
    "search_skills": {
        "module": "orchestrator.catalog",
        "function": "search_skills",
        "description": (
            "Search visible skills by id, name, display name, description, "
            "source, and tags."
        ),
        "category": "agent_catalog",
        "short_description": "Search visible skills.",
        "phases": ["strategic", "tactical"],
    },
    "get_skill": {
        "module": "orchestrator.catalog",
        "function": "get_skill",
        "description": (
            "Get a compact summary of a skill, including metadata, file index, "
            "and SKILL.md preview. Does not dump the full file tree by default."
        ),
        "category": "agent_catalog",
        "short_description": "Inspect a skill.",
        "phases": ["strategic", "tactical"],
    },
}


def _safe_limit(limit: int, *, default: int, maximum: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _tags_text(item: Dict[str, Any]) -> str:
    tags = item.get("tags") or []
    if not isinstance(tags, list) or not tags:
        return ""
    return ", ".join(str(tag) for tag in tags if tag)


def _matches_query(item: Dict[str, Any], query: Optional[str]) -> bool:
    if not query:
        return True
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = [
        item.get("id"),
        item.get("name"),
        item.get("display_name"),
        item.get("description"),
        item.get("source"),
        item.get("expert_type"),
        *_tags_text(item).split(", "),
    ]
    return any(needle in str(value or "").lower() for value in haystack)


def _filter_catalog_items(
    items: List[Dict[str, Any]],
    *,
    query: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    normalized_source = source.strip().lower() if source else None
    for item in items:
        if (
            normalized_source
            and str(item.get("source", "")).lower() != normalized_source
        ):
            continue
        if not _matches_query(item, query):
            continue
        filtered.append(item)
    return filtered


def _format_catalog_item(item: Dict[str, Any], *, kind: str) -> List[str]:
    item_id = item.get("id", "unknown")
    lines = [f"--- {kind}: {item_id} ---"]
    if item.get("name") and item.get("name") != item_id:
        lines.append(f"  Name: {_truncate(item.get('name'), limit=120)}")
    if item.get("display_name"):
        lines.append(
            f"  Display name: {_truncate(item.get('display_name'), limit=160)}"
        )
    if item.get("source"):
        lines.append(f"  Source: {item['source']}")
    if item.get("expert_type"):
        lines.append(f"  Type: {item['expert_type']}")
    if item.get("version") is not None:
        lines.append(f"  Version: {item['version']}")
    tags = _tags_text(item)
    if tags:
        lines.append(f"  Tags: {_truncate(tags, limit=160)}")
    if item.get("description"):
        lines.append(f"  Description: {_truncate(item.get('description'), limit=260)}")
    return lines


async def _fetch_experts(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    expert_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if expert_type:
        params["type"] = expert_type
    resp = await client.get(f"{base_url}/api/experts", params=params)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("experts", [])


async def _fetch_skills(
    client: httpx.AsyncClient,
    base_url: str,
) -> List[Dict[str, Any]]:
    resp = await client.get(f"{base_url}/api/skills")
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("skills", [])


def _format_expert_detail(expert_id: str, detail: Dict[str, Any]) -> str:
    lines: List[str] = [f"Expert ID: {detail.get('id') or expert_id}"]
    if detail.get("name"):
        lines.append(f"Name: {_truncate(detail.get('name'), limit=160)}")
    if detail.get("display_name"):
        lines.append(
            f"Display name: {_truncate(detail.get('display_name'), limit=160)}"
        )
    if detail.get("source"):
        lines.append(f"Source: {detail['source']}")
    if detail.get("expert_type"):
        lines.append(f"Type: {detail['expert_type']}")
    tags = _tags_text(detail)
    if tags:
        lines.append(f"Tags: {_truncate(tags, limit=180)}")
    if detail.get("description"):
        lines.append(f"Description: {_truncate(detail.get('description'), limit=300)}")

    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    if config.get("display_name") and not detail.get("display_name"):
        lines.append(
            f"Display name: {_truncate(config.get('display_name'), limit=160)}"
        )
    if config.get("description") and not detail.get("description"):
        lines.append(f"Description: {_truncate(config.get('description'), limit=300)}")
    if config.get("autonomy"):
        lines.append(f"Autonomy: {config['autonomy']}")
    workspace = (
        config.get("workspace") if isinstance(config.get("workspace"), dict) else {}
    )
    if workspace.get("backend"):
        lines.append(f"Workspace backend: {workspace['backend']}")
    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    if llm.get("model"):
        lines.append(f"Model: {llm['model']}")

    tools = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    if tools:
        enabled = sorted(k for k, value in tools.items() if value)
        disabled = sorted(k for k, value in tools.items() if value == [])
        if enabled:
            lines.append(f"Enabled tool categories: {', '.join(enabled)}")
        if disabled:
            lines.append(f"Disabled tool categories: {', '.join(disabled)}")

    effective_models = detail.get("effective_models")
    if isinstance(effective_models, dict) and effective_models:
        model_parts = []
        for slot in ("session", "strategic", "tactical", "subagent"):
            value = effective_models.get(slot)
            if isinstance(value, dict) and value.get("model"):
                source = value.get("source") or "unknown"
                model_parts.append(f"{slot}={value['model']} ({source})")
        if model_parts:
            lines.append(f"Effective models: {', '.join(model_parts)}")

    instructions = detail.get("instructions")
    if instructions:
        text = _truncate(instructions, limit=1200)
        lines.append("Instructions preview:")
        lines.append(text)
    return "\n".join(lines)


def _format_skill_detail(
    skill_id: str,
    detail: Dict[str, Any],
    *,
    include_files: bool,
) -> str:
    lines = _format_catalog_item(
        {"id": skill_id, **detail},
        kind="Skill",
    )
    files = detail.get("files") if isinstance(detail.get("files"), dict) else {}
    if files:
        lines.append(f"  Files: {len(files)}")
        for path, content in sorted(files.items())[:30]:
            lines.append(f"    - {path} ({len(str(content))} chars)")

        skill_md = files.get("SKILL.md")
        if skill_md:
            lines.append("")
            lines.append("SKILL.md preview:")
            lines.append(_truncate(skill_md, limit=1400))

        if include_files:
            lines.append("")
            lines.append("Selected file contents:")
            for path, content in sorted(files.items())[:8]:
                lines.append(f"--- {path} ({len(str(content))} chars) ---")
                lines.append(_truncate(content, limit=1200))
    return "\n".join(lines)


def create_catalog_tools(context: ToolContext) -> List[Any]:
    """Create expert and skill catalog tools with injected session context."""
    base_url = _get_orchestrator_url()

    @tool
    async def list_experts(
        expert_type: Optional[str] = None,
        source: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 30,
    ) -> str:
        """List visible experts.

        Args:
            expert_type: Optional DB expert type filter, such as worker or session.
            source: Optional source filter: bundled, user, or global.
            query: Optional text filter over id/name/description/tags.
            limit: Maximum experts to return.
        """
        effective_limit = _safe_limit(limit, default=30, maximum=100)
        async with _get_client(user_id=context.user_id) as client:
            try:
                experts = await _fetch_experts(
                    client, base_url, expert_type=expert_type
                )
                experts = _filter_catalog_items(experts, query=query, source=source)
                shown = experts[:effective_limit]
                if not shown:
                    return "No matching experts found."
                lines = [
                    f"Found {len(experts)} matching expert(s); showing {len(shown)}:\n"
                ]
                for expert in shown:
                    lines.extend(_format_catalog_item(expert, kind="Expert"))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to list experts: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_expert(expert_id: str) -> str:
        """Inspect an expert by id or bundled expert name.

        Args:
            expert_id: Expert UUID or bundled expert id/name.
        """
        if not expert_id:
            return "expert_id is required."
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/experts/{quote(str(expert_id), safe='')}"
                )
                resp.raise_for_status()
                return _format_expert_detail(str(expert_id), resp.json())
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Expert '{expert_id}' not found or not visible."
                return f"Failed to get expert: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def list_skills(
        source: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 40,
    ) -> str:
        """List visible skills.

        Args:
            source: Optional source filter: bundled, user, or global.
            query: Optional text filter over id/name/description/tags.
            limit: Maximum skills to return.
        """
        effective_limit = _safe_limit(limit, default=40, maximum=150)
        async with _get_client(user_id=context.user_id) as client:
            try:
                skills = await _fetch_skills(client, base_url)
                skills = _filter_catalog_items(skills, query=query, source=source)
                shown = skills[:effective_limit]
                if not shown:
                    return "No matching skills found."
                lines = [
                    f"Found {len(skills)} matching skill(s); showing {len(shown)}:\n"
                ]
                for skill in shown:
                    lines.extend(_format_catalog_item(skill, kind="Skill"))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to list skills: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def search_skills(query: str, limit: int = 10) -> str:
        """Search visible skills by text.

        Args:
            query: Search text. Matches id, name, display name, description, source,
                and tags.
            limit: Maximum skills to return.
        """
        if not query or not query.strip():
            return "query is required."
        effective_limit = _safe_limit(limit, default=10, maximum=50)
        async with _get_client(user_id=context.user_id) as client:
            try:
                skills = await _fetch_skills(client, base_url)
                skills = _filter_catalog_items(skills, query=query)
                shown = skills[:effective_limit]
                if not shown:
                    return f"No skills matched query '{query}'."
                lines = [
                    f"Found {len(skills)} skill(s) matching '{query}'; showing {len(shown)}:\n"
                ]
                for skill in shown:
                    lines.extend(_format_catalog_item(skill, kind="Skill"))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to search skills: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_skill(skill_id: str, include_files: bool = False) -> str:
        """Inspect a skill by UUID or bundled skill name.

        Args:
            skill_id: Skill UUID or bundled skill id/name.
            include_files: Include truncated file contents beyond the SKILL.md preview.
        """
        if not skill_id:
            return "skill_id is required."
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/skills/{quote(str(skill_id), safe='')}"
                )
                resp.raise_for_status()
                return _format_skill_detail(
                    str(skill_id), resp.json(), include_files=include_files
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Skill '{skill_id}' not found or not visible."
                return f"Failed to get skill: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    return [
        list_experts,
        get_expert,
        list_skills,
        search_skills,
        get_skill,
    ]
