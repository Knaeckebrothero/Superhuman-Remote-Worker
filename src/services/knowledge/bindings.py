"""Runtime scope bindings for native and datasource-backed OKF knowledge bases."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional


@dataclass(frozen=True)
class KnowledgeBinding:
    """One authorized knowledge base visible to an agent runtime."""

    kb_id: uuid.UUID
    alias: str
    name: str
    kind: Literal["native", "datasource"]
    writable: bool
    root_path: str = ""
    indexed_commit: Optional[str] = None

    @property
    def is_native(self) -> bool:
        return self.kind == "native"

    def handle(self, note_slug: str) -> str:
        return f"{self.alias}:{note_slug}"


def slugify_kb_alias(value: str) -> str:
    """Stable human-facing selector for a KB binding."""
    alias = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return alias or "kb"


def _unique_alias(base: str, kb_id: uuid.UUID, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    candidate = f"{base}-{kb_id.hex[:8]}"
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{kb_id.hex[:8]}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def build_knowledge_bindings(
    *,
    project_ids: Iterable[str] = (),
    datasources: Iterable[dict[str, Any]] = (),
) -> list[KnowledgeBinding]:
    """Build deterministic native-first bindings from runtime metadata.

    The first native project remains the sole write target, matching the
    existing primary-project behavior. External KB datasource payloads are
    always read-only in Slice 4 v1.
    """
    bindings: list[KnowledgeBinding] = []
    used: set[str] = set()

    for index, raw_project_id in enumerate(project_ids):
        try:
            project_id = uuid.UUID(str(raw_project_id))
        except (TypeError, ValueError):
            continue
        base = "project" if index == 0 else f"project-{project_id.hex[:8]}"
        bindings.append(
            KnowledgeBinding(
                kb_id=project_id,
                alias=_unique_alias(base, project_id, used),
                name="Project Knowledge"
                if index == 0
                else f"Project Knowledge {project_id.hex[:8]}",
                kind="native",
                writable=index == 0,
                root_path="knowledge",
            )
        )

    prepared_datasources: list[
        tuple[str, uuid.UUID, str, dict[str, Any], dict[str, Any]]
    ] = []
    for datasource in datasources:
        if str(datasource.get("type") or "").lower() != "kb":
            continue
        raw_id = datasource.get("datasource_id") or datasource.get("id")
        try:
            kb_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
        name = str(datasource.get("name") or "Knowledge Base")
        config = datasource.get("config") or {}
        prepared_datasources.append(
            (slugify_kb_alias(name), kb_id, name, config, datasource)
        )

    # SQL result ordering is not a sufficient tie-breaker when two selected
    # datasources share the same name. Sort by the UUID before assigning the
    # unsuffixed/suffixed aliases so an identical binding set is stable across
    # jobs, sessions, and database query plans.
    prepared_datasources.sort(key=lambda item: (item[0], item[1].hex))
    for base, kb_id, name, config, datasource in prepared_datasources:
        root_path = str(config.get("root_path") or "")
        bindings.append(
            KnowledgeBinding(
                kb_id=kb_id,
                alias=_unique_alias(base, kb_id, used),
                name=name,
                kind="datasource",
                writable=False,
                root_path=root_path,
                indexed_commit=datasource.get("indexed_commit"),
            )
        )

    return bindings


def split_note_handle(value: str) -> tuple[Optional[str], str]:
    """Split ``alias:slug`` while preserving legacy unqualified slugs."""
    raw = (value or "").strip()
    if ":" not in raw:
        return None, raw
    alias, slug = raw.split(":", 1)
    return alias.strip() or None, slug.strip()
