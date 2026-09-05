"""Runtime scope bindings for native and datasource-backed OKF knowledge bases."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional

from shared.runtime_actor import RuntimeActorContext


# Mirror of ``orchestrator.services.kb_datasources.NATIVE_PROJECT_CONFIG_KEY``
# (the agent image has no orchestrator deps, so the constant is duplicated
# rather than imported). A ``kb`` datasource carrying this key is the
# management surface over a project's OWN knowledge base, not an external one:
# its notes live under ``kb_id = project_id``, never under the datasource UUID.
NATIVE_PROJECT_CONFIG_KEY = "native_project_id"


def native_kb_project_id(datasource: dict[str, Any] | None) -> Optional[str]:
    """Return the project whose native KB this ``kb`` datasource mirrors."""
    if not datasource:
        return None
    config = datasource.get("config") or {}
    if not isinstance(config, dict):
        return None
    value = config.get(NATIVE_PROJECT_CONFIG_KEY)
    return str(value) if value else None


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
    runtime_actor: Optional[RuntimeActorContext] = None

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
    runtime_actor: RuntimeActorContext | None = None,
) -> list[KnowledgeBinding]:
    """Build deterministic native-first bindings from runtime metadata.

    The first native project remains the sole write target, matching the
    existing primary-project behavior. External KB datasource payloads are
    always read-only in Slice 4 v1.

    A project's own KB datasource (auto-attached at project creation) is keyed
    by its *project* id, not its datasource id — its notes are indexed under
    the project. So when it is selected alongside its own project it collapses
    into the native binding already emitted above, which keeps the project's
    knowledge base writable and stops the same KB being offered twice under
    two aliases (knowledge-base/knowledge/features/knowledge_base_repo_separation.md §6).
    """
    bindings: list[KnowledgeBinding] = []
    used: set[str] = set()
    bound_kb_ids: set[uuid.UUID] = set()

    for index, raw_project_id in enumerate(project_ids):
        try:
            project_id = uuid.UUID(str(raw_project_id))
        except (TypeError, ValueError):
            continue
        if project_id in bound_kb_ids:
            continue
        bound_kb_ids.add(project_id)
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
                runtime_actor=(
                    runtime_actor
                    if runtime_actor is not None
                    and index == 0
                    and runtime_actor.project_id == str(project_id)
                    else None
                ),
            )
        )

    prepared_datasources: list[
        tuple[str, uuid.UUID, str, dict[str, Any], dict[str, Any]]
    ] = []
    for datasource in datasources:
        if str(datasource.get("type") or "").lower() != "kb":
            continue
        raw_id = datasource.get("datasource_id") or datasource.get("id")
        native_project = native_kb_project_id(datasource)
        try:
            kb_id = uuid.UUID(str(native_project or raw_id))
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
        if kb_id in bound_kb_ids:
            # Already bound — the project's own KB reached here as a datasource
            # row. A second binding would be the same notes under a second
            # alias, read-only, shadowing the writable native one.
            continue
        bound_kb_ids.add(kb_id)
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
