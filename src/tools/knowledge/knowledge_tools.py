"""Knowledge base tools for the Universal Agent.

Provides tools for interacting with native and datasource-backed OKF knowledge bases:
- Writing: kb_write, kb_update (write-through to Neo4j + pgvector)
- Reading: kb_read, kb_list, kb_search
- Graph: kb_related, kb_contradictions, kb_provenance, kb_unanswered
- Export: kb_export

These tools use the system Neo4j connection (not the external datasource
connector). Connection comes from ToolContext.knowledge_graph.

See docs/features/project_knowledge_base.md for full architecture.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ...services.knowledge_graph import (
    CONFIDENCE_LEVELS,
    NOTE_STATUSES,
    NOTE_TYPES,
    slugify,
)
from ...services.knowledge.bindings import KnowledgeBinding, split_note_handle
from ..context import ToolContext
from .chunker import embedding_version_for_service
from .gardener import (
    Finding,
    dead_url_findings,
    external_url_map,
    is_reserved,
    near_duplicate_findings,
    lint_kb,
    note_title,
    parse_note_md,
    render_index_md,
)

logger = logging.getLogger(__name__)

# Shown by the genuinely graph-shaped tools when Neo4j is absent (slice-3 PR4c).
# CONTRADICTS / DERIVED_FROM / ANSWERS edges and the Neo4j export have no
# files-canonical representation — degrade honestly instead of faking results
# from the generic body-link table (which only carries "references" edges).
_GRAPH_TIER_MSG = (
    "requires the Graph tier (Neo4j), which is not enabled for this knowledge "
    "base. Notes remain searchable (kb_search) and 1-hop links are available "
    "via kb_related."
)


def _content_hash(text: str) -> str:
    """Stable content fingerprint for exact-duplicate detection."""
    return hashlib.sha256((text or "").encode()).hexdigest()


# The kb_lint URL sweep checks at most this many unique external URLs per run;
# the remainder is reported loudly (never a silent cap).
_URL_SWEEP_CAP = 25


def _check_external_url(url: str, timeout: float = 5.0) -> Optional[str]:
    """Reachability probe for the kb_lint dead-URL sweep.

    Returns a failure reason for CLEAR negatives only — HTTP 404/410 and
    DNS/connection-level failures. Bot-shy responses (403/405) and transient
    5xx count as alive, keeping the rule false-positive-shy.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "srw-kb-lint/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}" if e.code in (404, 410) else None
    except urllib.error.URLError as e:
        return f"unreachable: {e.reason}"
    except Exception as e:
        return f"unreachable: {e}"


# Tool metadata for registry
KNOWLEDGE_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    # Write tools
    "kb_write": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_write",
        "description": "Create a new knowledge note (Neo4j + pgvector write-through)",
        "category": "knowledge",
        "short_description": "Create a knowledge note in the project knowledge base.",
        "phases": ["strategic", "tactical"],
    },
    "kb_update": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_update",
        "description": "Update an existing knowledge note",
        "category": "knowledge",
        "short_description": "Update a knowledge note (append, status, tags, links).",
        "phases": ["strategic", "tactical"],
    },
    # Read tools
    "kb_read": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_read",
        "description": "Read a full knowledge note with metadata and relationships",
        "category": "knowledge",
        "short_description": "Read a knowledge note by slug ID.",
        "phases": ["strategic", "tactical"],
    },
    "kb_list": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_list",
        "description": "List knowledge notes with optional filters",
        "category": "knowledge",
        "short_description": "List knowledge notes (filter by type, tag, status).",
        "phases": ["strategic", "tactical"],
    },
    "kb_search": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_search",
        "description": "Hybrid search over knowledge base (semantic + keyword + recency)",
        "category": "knowledge",
        "short_description": "Search knowledge base with hybrid ranking.",
        "phases": ["strategic", "tactical"],
    },
    # Graph query tools
    "kb_related": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_related",
        "description": "Find notes related to a given note (graph traversal)",
        "category": "knowledge",
        "short_description": "Find related notes within N hops.",
        "phases": ["strategic", "tactical"],
    },
    "kb_contradictions": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_contradictions",
        "description": "Find contradicting notes in the knowledge base",
        "category": "knowledge",
        "short_description": "List notes connected by CONTRADICTS edges.",
        "phases": ["strategic", "tactical"],
    },
    "kb_provenance": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_provenance",
        "description": "Trace a note's derivation chain (DERIVED_FROM)",
        "category": "knowledge",
        "short_description": "Trace DERIVED_FROM chain for a note.",
        "phases": ["strategic", "tactical"],
    },
    "kb_unanswered": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_unanswered",
        "description": "List open questions with no answers",
        "category": "knowledge",
        "short_description": "List question notes without ANSWERS edges.",
        "phases": ["strategic", "tactical"],
    },
    # Export
    "kb_export": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_export",
        "description": "Export knowledge base as OKF/markdown files",
        "category": "knowledge",
        "short_description": "Export knowledge base to OKF .md files.",
        "phases": ["strategic", "tactical"],
    },
    # Maintenance / gardener (slice 2)
    "kb_lint": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_lint",
        "description": "Lint an OKF knowledge base for structural/link/id issues",
        "category": "knowledge",
        "short_description": "Lint the knowledge base (frontmatter, links, ids).",
        "phases": ["strategic", "tactical"],
    },
    "kb_index": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_index",
        "description": "Regenerate the OKF index.md for a knowledge base",
        "category": "knowledge",
        "short_description": "Regenerate index.md (grouped links by type).",
        "phases": ["strategic", "tactical"],
    },
}


def _get_project_id(context: ToolContext) -> Optional[str]:
    """Get the sole writable native KB id, preserving legacy contexts."""
    bindings = _explicit_bindings(context)
    if bindings:
        for binding in bindings:
            if binding.is_native and binding.writable:
                return str(binding.kb_id)
        return None
    return context.project_id


def _get_project_ids(context: ToolContext) -> List[str]:
    """Get every authorized KB id, preserving legacy project scoping."""
    bindings = _explicit_bindings(context)
    if bindings:
        return [str(binding.kb_id) for binding in bindings]
    return list(context.project_ids)


def _explicit_bindings(context: ToolContext) -> List[KnowledgeBinding]:
    """Return real runtime bindings without mistaking test mocks for them."""
    value = getattr(context, "knowledge_bindings", None)
    if not isinstance(value, (list, tuple)):
        return []
    return [binding for binding in value if isinstance(binding, KnowledgeBinding)]


def _read_bindings(context: ToolContext) -> List[KnowledgeBinding]:
    """Authorized KBs, synthesized from project ids for old runtimes/tests."""
    bindings = _explicit_bindings(context)
    if bindings:
        return bindings

    result: List[KnowledgeBinding] = []
    for index, raw_id in enumerate(context.project_ids):
        try:
            kb_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
        alias = "project" if index == 0 else f"project-{kb_id.hex[:8]}"
        result.append(
            KnowledgeBinding(
                kb_id=kb_id,
                alias=alias,
                name="Project Knowledge"
                if index == 0
                else f"Project Knowledge {kb_id.hex[:8]}",
                kind="native",
                writable=index == 0,
                root_path="knowledge",
            )
        )
    return result


def _native_bindings(context: ToolContext) -> List[KnowledgeBinding]:
    """Native project scopes eligible for graph-only operations."""
    return [binding for binding in _read_bindings(context) if binding.is_native]


def _resolve_binding(context: ToolContext, selector: str) -> Optional[KnowledgeBinding]:
    needle = str(selector or "").strip().lower()
    for binding in _read_bindings(context):
        if binding.alias.lower() == needle or str(binding.kb_id).lower() == needle:
            return binding
    return None


def _binding_choices(bindings: List[KnowledgeBinding]) -> str:
    return ", ".join(f"{binding.alias} ({binding.name})" for binding in bindings)


def _select_bindings(
    context: ToolContext, selector: Optional[str]
) -> tuple[List[KnowledgeBinding], Optional[str]]:
    bindings = _read_bindings(context)
    if not bindings:
        return [], "Error: No project_id available."
    if not selector:
        return bindings, None
    binding = _resolve_binding(context, selector)
    if binding is None:
        return [], (
            f"Error: Knowledge base '{selector}' is not selected. Available: "
            f"{_binding_choices(bindings)}."
        )
    return [binding], None


def _write_scope_error(context: ToolContext) -> str:
    if _explicit_bindings(context):
        return (
            "Error: No writable native knowledge base is selected. "
            "External knowledge bases are read-only."
        )
    return "Error: No project_id available."


def _native_scope_error(context: ToolContext) -> str:
    if _explicit_bindings(context):
        return "No native knowledge base with a Graph tier is selected."
    return "Error: No project_id available."


def _resolve_note_scope(
    context: ToolContext,
    note: str,
    kb: Optional[str],
) -> tuple[List[KnowledgeBinding], str, bool, Optional[str]]:
    """Resolve a note handle and optional selector into authorized bindings."""
    handle_alias, slug = split_note_handle(note)
    if not slug:
        return [], slug, False, "Error: A note slug is required."
    if handle_alias and kb:
        handle_binding = _resolve_binding(context, handle_alias)
        kb_binding = _resolve_binding(context, kb)
        if (
            handle_binding is None
            or kb_binding is None
            or handle_binding.kb_id != kb_binding.kb_id
        ):
            return (
                [],
                slug,
                False,
                (f"Error: Note handle selects '{handle_alias}' but kb selects '{kb}'."),
            )
    selector = handle_alias or kb
    bindings, error = _select_bindings(context, selector)
    return bindings, slug, selector is not None, error


# =============================================================================
# OKF markdown serialization (slice 1 — files-canonical dual-write)
# See docs/features/okf_knowledge_base.md §7, §11.
# =============================================================================


def _derive_description(content: str) -> str:
    """One-sentence description from a note body (OKF progressive disclosure).

    Skips heading lines; takes the first sentence of the first prose line,
    capped at 200 chars. Slice 1 has no ``description`` input on most write
    paths, but every OKF note's frontmatter should still carry one — the whole
    economy of ``index.md`` files runs on it (§7).
    """
    for line in (content or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(.+?[.!?])(\s|$)", line)
        desc = match.group(1) if match else line
        return desc[:200].strip()
    return ""


def _yaml_quote(value: str) -> str:
    """Quote a free-text YAML scalar, escaping quotes and collapsing newlines."""
    flat = " ".join(str(value).splitlines()).replace('"', '\\"')
    return f'"{flat}"'


def _render_note_md(note: Dict[str, Any]) -> str:
    """Serialize a note dict to an OKF/markdown document (pure function).

    OKF conventions (docs/features/okf_knowledge_base.md §7): ``type`` +
    ``description`` frontmatter, standard **markdown** links (never wikilinks)
    for the emergent graph, in-note ``author``/``job``/``branch`` provenance
    (squash-merge erases git authorship). Optional fields are omitted when
    absent. Shared by ``kb_write``/``kb_update`` dual-write and ``kb_export``.

    Expected keys (all optional except ``id``/``type``): ``id``, ``type``,
    ``title``, ``description``, ``content``, ``tags``, ``keywords``,
    ``confidence``, ``status``, ``author``, ``job``, ``branch``, ``created``,
    ``modified``, ``superseded_by``, ``relationships`` ([{type, target}]).
    """
    note_id = note.get("id", "unknown")
    content = note.get("content", "") or ""
    description = note.get("description") or _derive_description(content)

    fm: List[str] = ["---", f"id: {note_id}", f"type: {note.get('type', 'unknown')}"]
    if description:
        fm.append(f"description: {_yaml_quote(description)}")
    if note.get("tags"):
        fm.append(f"tags: [{', '.join(_yaml_quote(t) for t in note['tags'])}]")
    if note.get("keywords"):
        fm.append(f"keywords: [{', '.join(_yaml_quote(k) for k in note['keywords'])}]")
    if note.get("confidence"):
        fm.append(f"confidence: {note['confidence']}")
    fm.append(f"status: {note.get('status', 'active')}")
    # Provenance (§7) — origin binding that git authorship can't carry.
    if note.get("author"):
        fm.append(f"author: {note['author']}")
    if note.get("job"):
        fm.append(f"job: {note['job']}")
    if note.get("branch"):
        fm.append(f"branch: {note['branch']}")
    if note.get("created"):
        fm.append(f"created: {note['created']}")
    if note.get("modified"):
        fm.append(f"modified: {note['modified']}")
    if note.get("superseded_by"):
        fm.append(f"superseded_by: {note['superseded_by']}")
    fm.append("---")
    fm.append("")

    # Prepend the title as an H1 — unless the body already opens with its own
    # H1, which would render the title twice (run-8 nit, docs §11.1).
    if content.lstrip().startswith("# "):
        body: List[str] = [content]
    else:
        body = [f"# {note.get('title') or note_id}", "", content]

    # Relationships as standard markdown links, grouped by type (§7).
    rels = note.get("relationships") or []
    if rels:
        body.extend(["", "## Relationships"])
        by_type: Dict[str, List[str]] = {}
        for r in rels:
            rt = r.get("type", "REFERENCES")
            by_type.setdefault(rt, []).append(r.get("target", "?"))
        for rel_type, targets in by_type.items():
            links = ", ".join(f"[{t}]({t}.md)" for t in targets)
            body.append(f"**{rel_type}:** {links}")

    return "\n".join(fm) + "\n".join(body) + "\n"


def _note_provenance(context: ToolContext) -> Dict[str, Optional[str]]:
    """Resolve in-note provenance (author role, job, git branch) from context."""
    author: Optional[str] = None
    meta = getattr(context, "_job_metadata", None)
    if isinstance(meta, dict):
        author = meta.get("config_name")
    if not author:
        try:
            author = context.config.get("agent_id")
        except Exception:
            author = None
    branch: Optional[str] = None
    try:
        branch = context.workspace_manager.git_manager.current_branch()
    except Exception:
        branch = None
    return {"author": author, "job": context.job_id, "branch": branch}


def _dual_write_note(context: ToolContext, slug: str, note: Dict[str, Any]) -> None:
    """Materialize a note as flat ``knowledge/<slug>.md`` on the workspace.

    Slice 1 of the files-canonical KB. Guarded on ``has_git()`` (stricter than
    ``has_workspace()``: persistent sessions, repo-less projects and lite tiers
    keep their DB-only write) — skip + log otherwise, never fall back to local
    I/O (that writes to the ephemeral agent host, invisible to the workspace
    pod's clone; the citation-engine stub-mode bug and ``kb_export``'s old
    local-``Path`` fallback are the cautionary tales). Non-fatal, mirroring the
    pgvector write-through: a failed file write must never fail the tool.
    """
    if not context.has_git():
        return
    try:
        enriched = dict(note)
        for key, value in _note_provenance(context).items():
            if value:
                enriched[key] = value
        context.workspace_manager.write_file(
            f"knowledge/{slug}.md", _render_note_md(enriched)
        )
    except Exception as e:
        logger.warning(f"knowledge/ dual-write failed for {slug}: {e}")


def create_kb_tools(
    context: ToolContext,
    verdict_service: Any = None,
    verdict_prompt: Optional[str] = None,
) -> List[Any]:
    """Create knowledge base tools with injected context.

    Args:
        context: ToolContext with knowledge_graph and knowledge_store
        verdict_service: Optional KnowledgeVerdictService (OKF KB slice 2 PR2).
            When present (only the curator passes it), ``kb_write`` routes each
            candidate through the ingestion verdict gate before writing —
            ADD / UPDATE / SUPERSEDE / DISCARD. Every other caller (loop/worker
            agents) omits it and writes ungated, exactly as before.
        verdict_prompt: The verdict system prompt, resolved at event time.

    Returns:
        List of LangChain tool functions
    """
    kg = context.knowledge_graph
    ks = context.knowledge_store

    # Capture the event loop at tool creation time (async graph setup).
    # Sync tools run in executor threads — they must schedule async work
    # on the original loop to preserve asyncpg connection pool affinity.
    # Using asyncio.run() from a thread would create a NEW loop, breaking
    # connections tied to the original one ("attached to a different loop").
    try:
        _creator_loop = asyncio.get_running_loop()
    except RuntimeError:
        _creator_loop = None

    def _run_async(coro):
        """Run async coroutine from sync tool context on the original event loop."""
        if _creator_loop and _creator_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, _creator_loop)
            return future.result()
        return asyncio.run(coro)

    # Neo4j is OPTIONAL (slice-3 PR4c): the pgvector index is canonical for
    # retrieval and the OKF files for content, so the KB works without a graph.
    # Only the store is required; graph-shaped tools degrade when kg is None.
    if not ks:
        raise ValueError("Knowledge tools require knowledge_store in ToolContext")

    _has_bound_scopes = bool(_explicit_bindings(context))

    def _read_from_binding(
        binding: KnowledgeBinding, note_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read one note through the backend appropriate to its KB kind."""
        if binding.is_native and kg is not None:
            return kg.read_note(str(binding.kb_id), note_id)
        return _run_async(ks.get_note_by_slug(binding.kb_id, note_id))

    def _matching_notes(
        bindings: List[KnowledgeBinding], note_id: str
    ) -> List[tuple[KnowledgeBinding, Dict[str, Any]]]:
        matches: List[tuple[KnowledgeBinding, Dict[str, Any]]] = []
        for binding in bindings:
            data = _read_from_binding(binding, note_id)
            if data:
                matches.append((binding, data))
                # Legacy multi-project reads historically returned the first hit.
                if not _has_bound_scopes:
                    break
        return matches

    def _ambiguous_note(note_id: str, matches: List[Any]) -> str:
        aliases = [
            match[0].alias if isinstance(match, tuple) else match.alias
            for match in matches
        ]
        handles = ", ".join(f"{alias}:{note_id}" for alias in aliases)
        return (
            f"Error: Note '{note_id}' is ambiguous across selected knowledge "
            f"bases. Use a qualified handle or kb selector: {handles}."
        )

    def _qualified(binding: KnowledgeBinding, note_id: str) -> str:
        return binding.handle(note_id) if _has_bound_scopes else note_id

    def _external_snapshot_marker(bindings: List[KnowledgeBinding]) -> str:
        """Best-effort convergence marker for external indexed content."""
        snapshots: List[str] = []
        for binding in bindings:
            if binding.is_native:
                continue
            commit = (
                binding.indexed_commit
                if isinstance(binding.indexed_commit, str)
                else None
            )
            status = "ready"
            source_head: Optional[str] = None
            try:
                watermark = _run_async(ks.get_watermark(binding.kb_id))
                live_commit = getattr(watermark, "indexed_commit", None)
                if isinstance(live_commit, str) and live_commit:
                    commit = live_commit
                live_status = getattr(watermark, "status", None)
                if isinstance(live_status, str) and live_status:
                    status = live_status
                live_head = getattr(watermark, "source_head", None)
                if isinstance(live_head, str) and live_head:
                    source_head = live_head
            except Exception as e:
                logger.debug(
                    "Knowledge watermark lookup skipped for %s: %s",
                    binding.alias,
                    e,
                )
            if status != "ready":
                last_clean = (
                    f"last clean @ {commit[:12]}" if commit else "no clean commit"
                )
                attempted = f", source @ {source_head[:12]}" if source_head else ""
                snapshots.append(
                    f"[{binding.alias}] {status} — {last_clean}{attempted}"
                )
            else:
                snapshots.append(
                    f"[{binding.alias}] @ {commit[:12]}"
                    if commit
                    else f"[{binding.alias}] watermark unavailable"
                )
        if not snapshots:
            return ""
        label = (
            "External indexed snapshot"
            if len(snapshots) == 1
            else "External indexed snapshots"
        )
        return f"**{label}:** {', '.join(snapshots)}"

    def _index_readiness_notice(bindings: List[KnowledgeBinding]) -> str:
        """Advisory for the zero-result branches when a bound KB is still indexing.

        Covers native *and* external bindings (unlike ``_external_snapshot_marker``,
        which is external-only). Without this, an agent querying a KB that is still
        embedding gets an empty result indistinguishable from a genuine miss and may
        wrongly conclude the KB is empty. Only emits for an explicit non-ready
        watermark status; a missing watermark or lookup failure stays silent so we
        never raise a false "indexing" alarm.
        """
        notices: List[str] = []
        for binding in bindings:
            try:
                watermark = _run_async(ks.get_watermark(binding.kb_id))
            except Exception as e:
                logger.debug(
                    "Knowledge readiness lookup skipped for %s: %s",
                    binding.alias,
                    e,
                )
                continue
            status = getattr(watermark, "status", None)
            if not isinstance(status, str) or status == "ready":
                continue
            commit = getattr(watermark, "indexed_commit", None)
            source_head = getattr(watermark, "source_head", None)
            clean = (
                f"last clean @ {commit[:12]}"
                if isinstance(commit, str) and commit
                else "no clean commit yet"
            )
            attempted = (
                f", source @ {source_head[:12]}"
                if isinstance(source_head, str) and source_head
                else ""
            )
            notices.append(f"[{binding.alias}] {status} — {clean}{attempted}")
        if not notices:
            return ""
        return "⚠️ Still indexing — results may be incomplete: " + "; ".join(notices)

    # =========================================================================
    # Write Tools
    # =========================================================================

    def _update_existing_kgless(
        note: str,
        content: Optional[str],
        append: Optional[str],
        status: Optional[str],
        confidence: Optional[str],
        add_tags: Optional[List[str]],
        add_links: Optional[List[dict]],
    ) -> str:
        """Neo4j-less update: read the row from the store, apply the mutation in
        Python (the graph does this in Cypher), write back to store + OKF file.

        Same return contract and status/confidence validation as the Neo4j path.
        ``add_links`` round-trip as generic body links via the reindexer — the
        graph-only relationship *type* is not preserved (no Neo4j to hold it),
        consistent with the honest graph-tier degrade elsewhere in PR4c.
        """
        project_id = _get_project_id(context)
        if not project_id:
            return _write_scope_error(context)

        if status is not None and status not in NOTE_STATUSES:
            return f"Error: Invalid status: {status}"
        if confidence is not None and confidence not in CONFIDENCE_LEVELS:
            return f"Error: Invalid confidence: {confidence}"

        try:
            existing = _run_async(ks.get_note_by_slug(uuid.UUID(project_id), note))
            if not existing:
                return f"Error: Note '{note}' not found in project."

            if content is not None:
                new_content = content
            elif append is not None:
                new_content = (existing.get("content") or "") + "\n\n" + append
            else:
                new_content = existing.get("content") or ""

            new_status = status or existing.get("status") or "active"
            new_confidence = (
                confidence if confidence is not None else existing.get("confidence")
            )
            new_type = existing.get("type") or "learning"
            new_title = existing.get("title") or note

            merged_tags = list(existing.get("tags") or [])
            for t in add_tags or []:
                tl = t.lower()
                if tl not in merged_tags:
                    merged_tags.append(tl)

            _jid = existing.get("job_id")
            job_id_arg = uuid.UUID(_jid) if isinstance(_jid, str) else _jid

            # OKF file is canonical — write it first (mirrors kb_write ordering),
            # so the edit survives even if the disposable pgvector write fails.
            _dual_write_note(
                context,
                note,
                {
                    "id": note,
                    "type": new_type,
                    "title": new_title,
                    "description": existing.get("description"),
                    "content": new_content,
                    "tags": merged_tags,
                    "keywords": existing.get("keywords", []),
                    "confidence": new_confidence,
                    "status": new_status,
                    "relationships": add_links or [],
                },
            )
            try:
                _run_async(
                    ks.upsert_note(
                        note_id=note,
                        project_id=uuid.UUID(project_id),
                        title=new_title,
                        note_type=new_type,
                        content=new_content,
                        status=new_status,
                        confidence=new_confidence,
                        tags=merged_tags,
                        keywords=existing.get("keywords", []),
                        job_id=job_id_arg,
                        phase=existing.get("phase"),
                        modified_at=datetime.now(timezone.utc),
                    )
                )
            except Exception as e:
                logger.warning(f"pgvector write-through failed for {note}: {e}")

            changes = []
            if content is not None:
                changes.append("content replaced")
            if append is not None:
                changes.append("content appended")
            if status:
                changes.append(f"status → {status}")
            if confidence:
                changes.append(f"confidence → {confidence}")
            if add_tags:
                changes.append(f"+{len(add_tags)} tag(s)")
            if add_links:
                changes.append(f"+{len(add_links)} link(s)")
            return f"Updated **{note}**: {', '.join(changes)}"
        except Exception as e:
            logger.error(f"kb_update failed: {e}")
            return f"Error updating note: {e}"

    def _update_existing(
        note: str,
        content: Optional[str] = None,
        append: Optional[str] = None,
        status: Optional[str] = None,
        confidence: Optional[str] = None,
        add_tags: Optional[List[str]] = None,
        add_links: Optional[List[dict]] = None,
    ) -> str:
        """Update an existing note: Neo4j + OKF dual-write + pgvector write-through.

        Shared by the ``kb_update`` tool and the verdict gate's UPDATE/SUPERSEDE
        routing, so both apply an edit the same way.
        """
        if kg is None:
            return _update_existing_kgless(
                note, content, append, status, confidence, add_tags, add_links
            )

        project_id = _get_project_id(context)
        if not project_id:
            return _write_scope_error(context)

        try:
            updated = kg.update_note(
                project_id=project_id,
                note_id=note,
                content=content,
                append=append,
                status=status,
                confidence=confidence,
                add_tags=add_tags,
                add_links=add_links,
            )

            if not updated:
                return f"Error: Note '{note}' not found in project."

            # Write-through: re-read the note from Neo4j and upsert into pgvector
            try:
                full_note = kg.read_note(project_id, note)
                if full_note:
                    # Files-canonical dual-write (slice 1) — before the
                    # disposable-index upsert, so the canonical file lands even
                    # if the pgvector write fails.
                    _dual_write_note(
                        context,
                        note,
                        {
                            "id": note,
                            "type": full_note.get("type", "learning"),
                            "title": full_note.get("title", note),
                            "description": full_note.get("description"),
                            "content": full_note.get("content", ""),
                            "tags": full_note.get("tags", []),
                            "keywords": full_note.get("keywords", []),
                            "confidence": full_note.get("confidence"),
                            "status": full_note.get("status", "active"),
                            "superseded_by": full_note.get("superseded_by"),
                            "relationships": full_note.get("relationships", []),
                        },
                    )
                    _run_async(
                        ks.upsert_note(
                            note_id=note,
                            project_id=uuid.UUID(project_id),
                            title=full_note.get("title", ""),
                            note_type=full_note.get("type", "learning"),
                            content=full_note.get("content", ""),
                            status=full_note.get("status", "active"),
                            confidence=full_note.get("confidence"),
                            tags=full_note.get("tags", []),
                            keywords=full_note.get("keywords", []),
                            job_id=uuid.UUID(full_note["job_id"])
                            if full_note.get("job_id")
                            else None,
                            phase=full_note.get("phase"),
                            retrieval_messages=full_note.get("retrieval_messages", []),
                            modified_at=datetime.now(timezone.utc),
                        )
                    )
            except Exception as e:
                logger.warning(f"pgvector write-through failed for {note}: {e}")

            changes = []
            if content is not None:
                changes.append("content replaced")
            if append is not None:
                changes.append("content appended")
            if status:
                changes.append(f"status → {status}")
            if confidence:
                changes.append(f"confidence → {confidence}")
            if add_tags:
                changes.append(f"+{len(add_tags)} tag(s)")
            if add_links:
                changes.append(f"+{len(add_links)} link(s)")

            return f"Updated **{note}**: {', '.join(changes)}"

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"kb_update failed: {e}")
            return f"Error updating note: {e}"

    @tool
    def kb_write(
        title: str,
        type: str,
        content: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        confidence: Optional[str] = None,
        links: Optional[List[dict]] = None,
        retrieval_messages: Optional[List[str]] = None,
    ) -> str:
        """Create a new knowledge note in the project knowledge base.

        Write-through: creates the note in Neo4j (source of truth), upserts into
        the pgvector search index, AND materializes the note as an OKF markdown
        file at ``knowledge/<slug>.md`` on the workspace (delivered to the repo's
        ``main`` by the normal commit/merge flow). The note is immediately
        available for search and graph queries.

        Args:
            title: Note title (generates the slug ID, e.g. "chose-jwt-over-oauth")
            type: Note type — one of: goal, plan, decision, learning, code, source, question, state, retrospective
            content: Full markdown body of the note
            description: One-sentence summary for progressive-disclosure indexes.
                         Strongly recommended; derived from the content's first
                         sentence when omitted.
            tags: List of tag names (e.g. ["authentication", "security"])
            keywords: List of keyword strings for search
            confidence: Confidence level — high, medium, or low
            links: Relationships to other notes — list of {"target": "note-slug", "type": "RELATIONSHIP_TYPE"}.
                   Types: REFERENCES, DERIVED_FROM, SUPPORTS, CONTRADICTS, ANSWERS, DEPENDS_ON, SUPERSEDES, IMPLEMENTS
            retrieval_messages: Synthetic queries describing when this note should be retrieved
                               (e.g. ["What auth approach should I use?", "Why JWT over OAuth?"])

        Returns:
            Confirmation with the note's slug ID, or error message
        """
        project_id = _get_project_id(context)
        if not project_id:
            return _write_scope_error(context)

        # Exact-duplicate short-circuit (Step 1 hardening, docs §11.1): a
        # same-slug write with byte-identical content is a pure no-op for every
        # writer — skip the gate and the create. This kills the run-8 twin-file
        # duplication for bare loop agents the verdict gate never reaches.
        # `read_note` returns the note dict (or None); a non-dict means no match.
        candidate_slug = slugify(title)
        if kg is None:
            existing = _run_async(
                ks.get_note_by_slug(uuid.UUID(project_id), candidate_slug)
            )
        else:
            existing = kg.read_note(project_id, candidate_slug)
        if isinstance(existing, dict) and _content_hash(content) == _content_hash(
            existing.get("content", "")
        ):
            return (
                f"Note '{candidate_slug}' already exists with identical content "
                f"— no change written."
            )

        # Ingestion verdict gate (slice 2 PR2) — only when the curator wired a
        # service. Adjudicate the candidate against its nearest active notes
        # before writing: DISCARD skips, UPDATE redirects the edit onto the
        # duplicate, SUPERSEDE writes then retires the stale note(s), ADD (or any
        # non-fatal gate error) falls through to a normal create.
        supersede_targets: List[Any] = []
        if verdict_service is not None and verdict_prompt:
            try:
                from src.services.knowledge.ingestion import gate_candidate

                decision = _run_async(
                    gate_candidate(
                        verdict_service,
                        ks,
                        uuid.UUID(project_id),
                        content=content,
                        prompt=verdict_prompt,
                    )
                )
                action = decision.verdict.action
                if action == "DISCARD":
                    dup = (
                        f" (duplicate of {decision.targets[0].note_id})"
                        if decision.targets
                        else ""
                    )
                    return (
                        f"Skipped note '{title}' — verdict DISCARD{dup}: "
                        f"{decision.verdict.reason}"
                    )
                if action == "UPDATE" and decision.targets:
                    return _update_existing(
                        decision.targets[0].note_id, content=content
                    )
                if action == "SUPERSEDE" and decision.targets:
                    supersede_targets = decision.targets
            except Exception as e:
                logger.warning(
                    f"knowledge verdict gate failed (non-fatal, writing ungated): {e}"
                )

        try:
            if kg is None:
                # Validate up-front, exactly where kg.create_note would (the
                # graph path raises ValueError here) — otherwise an invalid type
                # slips through to a silent DB CHECK failure and the tool would
                # misleadingly report "Created".
                if type not in NOTE_TYPES:
                    raise ValueError(
                        f"Invalid note_type: {type}. Must be one of {NOTE_TYPES}"
                    )
                if confidence and confidence not in CONFIDENCE_LEVELS:
                    raise ValueError(
                        f"Invalid confidence: {confidence}. "
                        f"Must be one of {CONFIDENCE_LEVELS}"
                    )
                # Neo4j-less: derive the slug ourselves (the graph normally does
                # this). Base slug, or a deterministic content-hash fork when the
                # base is taken by a *different* note (identical content already
                # short-circuited above). Empty slug → deterministic fallback
                # (content-hashed, not random, so re-writes converge).
                if not candidate_slug:
                    slug = f"note-{_content_hash(content)[:8]}"
                elif isinstance(existing, dict):
                    slug = f"{candidate_slug}-{_content_hash(content)[:6]}"
                else:
                    slug = candidate_slug
            else:
                slug = kg.create_note(
                    project_id=project_id,
                    title=title,
                    note_type=type,
                    content=content,
                    tags=tags,
                    keywords=keywords,
                    confidence=confidence,
                    job_id=context.job_id,
                    phase=context.config.get("current_phase"),
                    retrieval_messages=retrieval_messages,
                    links=links,
                )

            # Write to pgvector — the primary write when Neo4j-less, a
            # write-through otherwise.
            now = datetime.now(timezone.utc)
            try:
                _run_async(
                    ks.upsert_note(
                        note_id=slug,
                        project_id=uuid.UUID(project_id),
                        title=title,
                        note_type=type,
                        content=content,
                        tags=tags,
                        keywords=keywords,
                        confidence=confidence,
                        job_id=uuid.UUID(context.job_id) if context.job_id else None,
                        phase=context.config.get("current_phase"),
                        retrieval_messages=retrieval_messages,
                        created_at=now,
                        modified_at=now,
                    )
                )
            except Exception as e:
                logger.warning(f"pgvector write-through failed for {slug}: {e}")
                # Durable truth is Neo4j (graph path) or the OKF file (kg-less);
                # the reindexer can rebuild pgvector from either.

            # Files-canonical dual-write (slice 1): materialize knowledge/<slug>.md.
            _dual_write_note(
                context,
                slug,
                {
                    "id": slug,
                    "type": type,
                    "title": title,
                    "description": description,
                    "content": content,
                    "tags": tags,
                    "keywords": keywords,
                    "confidence": confidence,
                    "status": "active",
                    "relationships": links or [],
                },
            )

            # Verdict SUPERSEDE: retire the stale note(s) the candidate replaces,
            # pointing them at the new note (status=superseded + SUPERSEDED_BY).
            if supersede_targets:
                retired = []
                for t in supersede_targets:
                    try:
                        _update_existing(
                            t.note_id,
                            status="superseded",
                            add_links=[{"target": slug, "type": "SUPERSEDED_BY"}],
                        )
                        retired.append(t.note_id)
                    except Exception as e:
                        logger.warning(f"supersede retire failed for {t.note_id}: {e}")
                if retired:
                    return (
                        f"Created knowledge note: **{slug}** (type={type}) — "
                        f"superseded {', '.join(retired)}"
                    )

            link_info = ""
            if links:
                link_info = f", {len(links)} link(s)"

            return f"Created knowledge note: **{slug}** (type={type}{link_info})"

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"kb_write failed: {e}")
            return f"Error creating knowledge note: {e}"

    @tool
    def kb_update(
        note: str,
        content: Optional[str] = None,
        append: Optional[str] = None,
        status: Optional[str] = None,
        confidence: Optional[str] = None,
        add_tags: Optional[List[str]] = None,
        add_links: Optional[List[dict]] = None,
    ) -> str:
        """Update an existing knowledge note.

        Write-through: updates both Neo4j and pgvector search index.

        Args:
            note: Note slug ID (e.g. "chose-jwt-over-oauth")
            content: Replace the entire content (mutually exclusive with append)
            append: Append text to existing content (mutually exclusive with content)
            status: New status — active, resolved, superseded, or archived
            confidence: New confidence level — high, medium, or low
            add_tags: Additional tags to add
            add_links: Additional relationships — list of {"target": "slug", "type": "RELATIONSHIP_TYPE"}

        Returns:
            Confirmation or error message
        """
        alias, note_slug = split_note_handle(note)
        if alias and _has_bound_scopes:
            binding = _resolve_binding(context, alias)
            if binding is None:
                return (
                    f"Error: Knowledge base '{alias}' is not selected. Available: "
                    f"{_binding_choices(_read_bindings(context))}."
                )
            if not binding.is_native or not binding.writable:
                return (
                    f"Error: Knowledge base '{binding.alias}' is read-only. "
                    "External knowledge bases cannot be updated."
                )
            note = note_slug
        return _update_existing(
            note,
            content=content,
            append=append,
            status=status,
            confidence=confidence,
            add_tags=add_tags,
            add_links=add_links,
        )

    # =========================================================================
    # Read Tools
    # =========================================================================

    @tool
    def kb_read(note: str, kb: Optional[str] = None) -> str:
        """Read a full knowledge note with metadata and relationships.

        Args:
            note: Note slug or qualified ``alias:slug`` handle
            kb: Optional selected knowledge-base alias or UUID

        Returns:
            Full note content with metadata, tags, and relationships
        """
        bindings, note_slug, _scoped, error = _resolve_note_scope(context, note, kb)
        if error:
            return error

        try:
            matches = _matching_notes(bindings, note_slug)
            if not matches:
                notice = _index_readiness_notice(bindings)
                if notice:
                    return f"Note '{note}' not found.\n\n{notice}"
                return f"Note '{note}' not found."
            if len(matches) > 1:
                return _ambiguous_note(note_slug, matches)
            binding, data = matches[0]

            # Format output
            lines = [
                f"# {data.get('title', note_slug)}",
                "",
                f"**ID:** {data.get('id', note_slug)}",
                f"**Type:** {data.get('type', 'unknown')}",
                f"**Status:** {data.get('status', 'unknown')}",
            ]
            if _has_bound_scopes:
                lines.extend(
                    [
                        f"**Knowledge Base:** {binding.name} (`{binding.alias}`)",
                        f"**Handle:** `{binding.handle(note_slug)}`",
                    ]
                )
                snapshot_marker = _external_snapshot_marker([binding])
                if snapshot_marker:
                    lines.append(snapshot_marker)

            if data.get("confidence"):
                lines.append(f"**Confidence:** {data['confidence']}")
            if data.get("tags"):
                lines.append(f"**Tags:** {', '.join(data['tags'])}")
            if data.get("keywords"):
                lines.append(f"**Keywords:** {', '.join(data['keywords'])}")
            if data.get("job_id"):
                lines.append(f"**Job:** {data['job_id']}")
            if data.get("phase") is not None:
                lines.append(f"**Phase:** {data['phase']}")
            if data.get("created"):
                lines.append(f"**Created:** {data['created']}")
            if data.get("modified"):
                lines.append(f"**Modified:** {data['modified']}")

            lines.extend(["", "---", "", data.get("content", "(no content)")])

            # Relationships
            rels = data.get("relationships", [])
            if rels:
                lines.extend(["", "## Outgoing Relationships"])
                for r in rels:
                    target = _qualified(binding, r["target"])
                    lines.append(
                        f"- **{r['type']}** → [[{target}]] "
                        f"({r.get('target_title', '')})"
                    )

            incoming = data.get("incoming_relationships", [])
            if incoming:
                lines.extend(["", "## Incoming Relationships"])
                for r in incoming:
                    source = _qualified(binding, r["source"])
                    lines.append(
                        f"- [[{source}]] ({r.get('source_title', '')}) "
                        f"**{r['type']}** → this"
                    )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_read failed: {e}")
            return f"Error reading note: {e}"

    @tool
    def kb_list(
        type: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[str] = None,
        job_id: Optional[str] = None,
        kb: Optional[str] = None,
    ) -> str:
        """List knowledge notes with optional filters.

        Args:
            type: Filter by note type (goal, plan, decision, learning, code, source, question, state, retrospective)
            tag: Filter by tag name
            status: Filter by status (active, resolved, superseded, archived). Default: all
            job_id: Filter by creating job UUID
            kb: Optional selected knowledge-base alias or UUID

        Returns:
            Formatted list of matching notes
        """
        bindings, error = _select_bindings(context, kb)
        if error:
            return error

        try:
            notes: List[tuple[KnowledgeBinding, Dict[str, Any]]] = []
            for binding in bindings:
                if binding.is_native and kg is not None:
                    found = kg.list_notes(
                        project_id=str(binding.kb_id),
                        note_type=type,
                        tag=tag,
                        status=status,
                        job_id=job_id,
                    )
                else:
                    found = _run_async(
                        ks.list_notes(
                            kb_id=binding.kb_id,
                            note_type=type,
                            tag=tag,
                            status=status,
                            job_id=job_id,
                        )
                    )
                notes.extend((binding, item) for item in found)

            if not notes:
                filters = []
                if type:
                    filters.append(f"type={type}")
                if tag:
                    filters.append(f"tag={tag}")
                if status:
                    filters.append(f"status={status}")
                filter_str = f" (filters: {', '.join(filters)})" if filters else ""
                base = f"No knowledge notes found{filter_str}."
                notice = _index_readiness_notice(bindings)
                return f"{base}\n\n{notice}" if notice else base

            lines = [f"**Knowledge Notes** ({len(notes)} results):", ""]

            for binding, n in notes:
                status_icon = "●" if n.get("status") == "active" else "○"
                confidence = f" [{n['confidence']}]" if n.get("confidence") else ""
                note_id = n.get("id", "?")
                handle = _qualified(binding, note_id)
                source = f"[{binding.alias}] " if _has_bound_scopes else ""
                lines.append(
                    f"{status_icon} {source}**{handle}** — "
                    f"{n.get('title', '(untitled)')} "
                    f"({n.get('type', '?')}{confidence})"
                )

            snapshot_marker = _external_snapshot_marker(bindings)
            if snapshot_marker:
                lines.extend(["", snapshot_marker])

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_list failed: {e}")
            return f"Error listing notes: {e}"

    @tool
    def kb_search(query: str, max_results: int = 10, kb: Optional[str] = None) -> str:
        """Search the project knowledge base using hybrid ranking.

        Combines semantic vector search, keyword matching, and recency
        via Reciprocal Rank Fusion (RRF) over the chunk index. Searches active
        notes only.

        Args:
            query: Search query (natural language or keywords)
            max_results: Maximum number of results (default 10)
            kb: Optional selected knowledge-base alias or UUID

        Returns:
            Ranked search results with note summaries
        """
        bindings, error = _select_bindings(context, kb)
        if error:
            return error
        kb_ids = [binding.kb_id for binding in bindings]
        binding_by_id = {str(binding.kb_id): binding for binding in bindings}

        # Filter to the live pipeline stamp so mixed-model/chunker vectors can't
        # drift into the result set. Resolved from the same EmbeddingService that
        # embeds the query, so it matches what the reindexer stamped; fall back to
        # no filter if the service can't report a model (never over-filter blind).
        try:
            current_version = embedding_version_for_service(ks.embedding_service)
        except Exception:
            current_version = None

        try:
            results = _run_async(
                ks.search_chunks(
                    kb_ids=kb_ids,
                    query=query,
                    embedding_version=current_version,
                    match_count=max_results,
                )
            )

            if not results:
                base = f"No knowledge notes match '{query}'."
                notice = _index_readiness_notice(bindings)
                return f"{base}\n\n{notice}" if notice else base

            header = f"**Search Results** ({len(results)} matches for '{query}')"
            # Native single-KB compatibility header. External bindings use the
            # richer convergence marker below so a partial mixed cache is never
            # mislabeled as an immutable snapshot at the last clean commit.
            if len(kb_ids) == 1 and bindings[0].is_native:
                try:
                    wm = _run_async(ks.get_watermark(kb_ids[0]))
                    commit = getattr(wm, "indexed_commit", None)
                    if commit:
                        header += f" — index @ {str(commit)[:8]}"
                except Exception as e:
                    logger.debug(f"kb_search watermark lookup skipped: {e}")

            lines = [f"{header}:", ""]

            for i, note in enumerate(results, 1):
                meta_parts = [note.note_type]
                if note.confidence:
                    meta_parts.append(note.confidence)
                meta = ", ".join(meta_parts)

                # Truncate content for display
                preview = note.content[:200].replace("\n", " ")
                if len(note.content) > 200:
                    preview += "..."

                result_binding = binding_by_id.get(str(getattr(note, "kb_id", "")))
                if result_binding is None and len(bindings) == 1:
                    result_binding = bindings[0]
                note_handle = note.note_id
                source = ""
                if _has_bound_scopes and result_binding is not None:
                    note_handle = result_binding.handle(note.note_id)
                    source = f"[{result_binding.alias}] "
                lines.append(
                    f"**[{i}]** {source}**{note_handle}** — {note.title} ({meta})"
                )
                lines.append(f"  {preview}")
                lines.append("")

            if _has_bound_scopes and len(kb_ids) > 1:
                snapshots = []
                for binding in (item for item in bindings if item.is_native):
                    try:
                        watermark = _run_async(ks.get_watermark(binding.kb_id))
                        commit = getattr(watermark, "indexed_commit", None)
                    except Exception as e:
                        logger.debug(
                            "kb_search watermark lookup skipped for %s: %s",
                            binding.alias,
                            e,
                        )
                        commit = None
                    snapshots.append(
                        f"[{binding.alias}] @ {str(commit)[:8]}"
                        if commit
                        else f"[{binding.alias}] unavailable"
                    )
                if snapshots:
                    lines.append(f"**Native index snapshots:** {', '.join(snapshots)}")

            snapshot_marker = _external_snapshot_marker(bindings)
            if snapshot_marker:
                lines.append(snapshot_marker)

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_search failed: {e}")
            return f"Error searching knowledge base: {e}"

    # =========================================================================
    # Graph Query Tools
    # =========================================================================

    @tool
    def kb_related(note: str, max_hops: int = 2, kb: Optional[str] = None) -> str:
        """Find notes related to a given note via graph traversal.

        Traverses all relationship types up to max_hops edges away.

        Args:
            note: Note slug or qualified ``alias:slug`` handle
            max_hops: Maximum relationship hops (1-3, default 2)
            kb: Optional selected knowledge-base alias or UUID

        Returns:
            List of related notes with relationship types and distance
        """
        bindings, note_slug, _scoped, error = _resolve_note_scope(context, note, kb)
        if error:
            return error

        try:
            if _has_bound_scopes:
                matches = _matching_notes(bindings, note_slug)
                if not matches:
                    return f"Note '{note}' not found."
                if len(matches) > 1:
                    return _ambiguous_note(note_slug, matches)
                target_bindings = [matches[0][0]]
            else:
                # Preserve the legacy cross-project aggregation contract.
                target_bindings = bindings

            results: List[tuple[KnowledgeBinding, Dict[str, Any]]] = []
            for binding in target_bindings:
                if binding.is_native and kg is not None:
                    found = kg.get_related(
                        project_id=str(binding.kb_id),
                        note_id=note_slug,
                        max_hops=max_hops,
                    )
                else:
                    # Datasource KBs and graph-less native KBs use the indexed
                    # body-link table. It deliberately provides one hop only.
                    found = _run_async(
                        ks.get_related_notes(kb_id=binding.kb_id, note_id=note_slug)
                    )
                results.extend((binding, item) for item in found)

            if not results:
                return f"No related notes found for '{note}'."

            display_note = _qualified(target_bindings[0], note_slug)
            lines = [
                f"**Related to '{display_note}'** ({len(results)} notes):",
                "",
            ]
            for binding, r in results:
                distance = r.get("distance", "?")
                rel_types = r.get("rel_types", [])
                rel_str = " → ".join(rel_types) if rel_types else "?"
                status = f" [{r['status']}]" if r.get("status") != "active" else ""
                related_id = _qualified(binding, r.get("id", "?"))
                source = f"[{binding.alias}] " if _has_bound_scopes else ""
                lines.append(
                    f"  ({distance} hop{'s' if distance != 1 else ''}) "
                    f"{source}**{related_id}** — {r.get('title', '?')} "
                    f"({r.get('type', '?')}{status}) via {rel_str}"
                )

            snapshot_marker = _external_snapshot_marker(target_bindings)
            if snapshot_marker:
                lines.extend(["", snapshot_marker])

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_related failed: {e}")
            return f"Error finding related notes: {e}"

    @tool
    def kb_contradictions() -> str:
        """Find all active contradiction pairs in the knowledge base.

        Returns notes connected by CONTRADICTS relationships where both
        notes are still active. Use this to identify conflicting knowledge
        that needs resolution.

        Returns:
            List of contradiction pairs
        """
        if kg is None:
            return f"Contradiction detection {_GRAPH_TIER_MSG}"

        bindings = _native_bindings(context)
        if not bindings:
            return _native_scope_error(context)

        try:
            results: List[tuple[KnowledgeBinding, Dict[str, Any]]] = []
            for binding in bindings:
                found = kg.get_contradictions(str(binding.kb_id))
                results.extend((binding, item) for item in found)

            if not results:
                return "No active contradictions found in the knowledge base."

            lines = [f"**Active Contradictions** ({len(results)}):", ""]
            for binding, r in results:
                note_a = _qualified(binding, r.get("note_a", "?"))
                note_b = _qualified(binding, r.get("note_b", "?"))
                lines.append(
                    f"  **{note_a}** ({r.get('title_a', '?')}) "
                    f"⟷ CONTRADICTS ⟷ "
                    f"**{note_b}** ({r.get('title_b', '?')})"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_contradictions failed: {e}")
            return f"Error finding contradictions: {e}"

    @tool
    def kb_provenance(note: str) -> str:
        """Trace the derivation chain of a knowledge note.

        Follows DERIVED_FROM relationships backwards to find the original
        sources that a note was derived from.

        Args:
            note: Note slug ID to trace

        Returns:
            Ordered provenance chain from the note back to its sources
        """
        if kg is None:
            return f"Provenance tracing {_GRAPH_TIER_MSG}"

        alias, note_slug = split_note_handle(note)
        if alias and _has_bound_scopes:
            selected = _resolve_binding(context, alias)
            if selected is None:
                return (
                    f"Error: Knowledge base '{alias}' is not selected. Available: "
                    f"{_binding_choices(_read_bindings(context))}."
                )
            if not selected.is_native:
                return f"Provenance tracing {_GRAPH_TIER_MSG}"
            bindings = [selected]
        else:
            bindings = _native_bindings(context)
        if not bindings:
            return _native_scope_error(context)

        try:
            results: List[tuple[KnowledgeBinding, Dict[str, Any]]] = []
            for binding in bindings:
                found = kg.get_provenance(str(binding.kb_id), note_slug)
                results.extend((binding, item) for item in found)

            if not results:
                return f"No provenance chain found for '{note}'."

            lines = [f"**Provenance of '{note}'**:", ""]
            for binding, r in results:
                depth = r.get("depth", "?")
                result_id = _qualified(binding, r.get("id", "?"))
                lines.append(
                    f"  {'  ' * (depth - 1)}↑ **{result_id}** — "
                    f"{r.get('title', '?')} ({r.get('type', '?')})"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_provenance failed: {e}")
            return f"Error tracing provenance: {e}"

    @tool
    def kb_unanswered() -> str:
        """List open questions that have no answers in the knowledge base.

        Returns question notes (type="question", status="active") that
        have no ANSWERS relationship pointing to or from them.

        Returns:
            List of unanswered questions
        """
        if kg is None:
            return f"Unanswered-question tracking {_GRAPH_TIER_MSG}"

        bindings = _native_bindings(context)
        if not bindings:
            return _native_scope_error(context)

        try:
            results: List[tuple[KnowledgeBinding, Dict[str, Any]]] = []
            for binding in bindings:
                found = kg.get_unanswered(str(binding.kb_id))
                results.extend((binding, item) for item in found)

            if not results:
                return "No unanswered questions in the knowledge base."

            lines = [f"**Unanswered Questions** ({len(results)}):", ""]
            for binding, r in results:
                content = r.get("content", "")
                preview = content[:150].replace("\n", " ")
                if len(content) > 150:
                    preview += "..."
                result_id = _qualified(binding, r.get("id", "?"))
                lines.append(f"  ❓ **{result_id}** — {r.get('title', preview)}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_unanswered failed: {e}")
            return f"Error finding unanswered questions: {e}"

    # =========================================================================
    # Export
    # =========================================================================

    @tool
    def kb_export(path: str) -> str:
        """Export the project knowledge base as OKF/markdown files.

        Dumps all notes from Neo4j as .md files with YAML frontmatter and
        standard markdown links for relationships (OKF convention). Requires a
        workspace backend; one-way export for human browsing or migration.

        Args:
            path: Directory path to write the export files

        Returns:
            Summary of exported files
        """
        if kg is None:
            # kb_export is the one-time Neo4j → OKF migration dump. Without Neo4j
            # the vault's `knowledge/*.md` files ARE the canonical OKF export
            # already, so there is nothing to migrate out of the graph.
            return (
                "Nothing to export: this knowledge base has no Graph tier (Neo4j). "
                "The `knowledge/*.md` files in the workspace are already the "
                "canonical OKF export."
            )

        bindings = _native_bindings(context)
        if not bindings:
            return _native_scope_error(context)

        workspace = context.workspace_manager if context.has_workspace() else None

        try:
            notes = []
            for binding in bindings:
                notes.extend(kg.get_all_notes_for_export(str(binding.kb_id)))
            if not notes:
                return "Knowledge base is empty — nothing to export."

            # A workspace backend is required: never write to the local agent
            # host (ephemeral, invisible to the workspace pod's clone). Uses the
            # shared OKF serializer (markdown links, provenance) — same output
            # as the kb_write/kb_update dual-write.
            if workspace is None:
                return (
                    "Error: kb_export requires a workspace backend; refusing to "
                    "write to the local agent host (invisible to the workspace)."
                )

            export_rel = path.rstrip("/")
            workspace.create_directory(export_rel)

            exported = 0
            for note in notes:
                note_id = note.get("id", "unknown")
                workspace.write_file(
                    f"{export_rel}/{note_id}.md", _render_note_md(note)
                )
                exported += 1

            return (
                f"Exported {exported} note(s) to `{path}/`.\n"
                "Open in any OKF-compatible Markdown viewer."
            )

        except Exception as e:
            logger.error(f"kb_export failed: {e}")
            return f"Error exporting knowledge base: {e}"

    # =========================================================================
    # Maintenance / gardener tools (slice 2)
    # =========================================================================

    @tool
    def kb_lint(path: str = "knowledge", check_urls: bool = False) -> str:
        """Lint an OKF knowledge base for structural, id and link issues.

        Reads every `*.md` note under `path` and checks frontmatter validity,
        required keys (id/type/description), id format/uniqueness, dead and
        broken-supersede links, orphans, missing titles, oversized notes,
        slug-forked twins and embedding-near-duplicates. Read-only — returns
        a report; it never edits notes. Point it at the project KB
        (`knowledge/`), a repository datasource, or any markdown vault.

        Args:
            path: Directory to lint (default "knowledge").
            check_urls: Also probe external http(s) links and flag clearly
                dead ones (404/410, unreachable host). Off by default —
                it is slow (network) and capped per run.

        Returns:
            A markdown lint report (errors then warnings), or a status message.
        """
        if not context.has_workspace():
            return "Error: kb_lint requires a workspace backend to read notes."
        ws = context.workspace_manager
        root = path.rstrip("/") or "knowledge"
        try:
            entries = ws.list_files(root, "*.md")
        except Exception as e:
            return f"Error listing `{root}/`: {e}"

        notes: List[Dict[str, str]] = []
        for rel in entries:
            if rel.endswith("/"):
                continue
            try:
                notes.append({"path": rel, "text": ws.read_file(rel)})
            except Exception as e:
                logger.warning(f"kb_lint: could not read {rel}: {e}")
        if not notes:
            return f"No markdown notes found under `{root}/`."
        report = lint_kb(notes)

        # Embedding-backed near-duplicate pass (the slice-2 deferred rule):
        # the pgvector index already holds one embedding per note, so one
        # self-join yields every high-similarity pair — no re-embedding here.
        # Non-fatal by design, matching the write-through posture: the
        # deterministic report stands alone when the index is unreachable.
        project_id = _get_project_id(context)
        if project_id:
            try:
                # Lint policy owns its floor: 0.97 (07-05 runbook decision) —
                # the store default (0.9) is unusable noise for a merge signal.
                pairs = _run_async(
                    ks.find_near_duplicate_pairs(
                        uuid.UUID(project_id), min_similarity=0.97
                    )
                )
                report.findings.extend(
                    near_duplicate_findings(pairs, [n["path"] for n in notes])
                )
            except Exception as e:
                logger.warning(f"kb_lint: near-duplicate pass skipped: {e}")

        # Opt-in dead-external-URL sweep: probe each unique http(s) link once,
        # flag only clear negatives, and report any capped remainder loudly.
        if check_urls:
            url_map = external_url_map(notes)
            urls = sorted(url_map)
            checked, skipped = urls[:_URL_SWEEP_CAP], urls[_URL_SWEEP_CAP:]
            dead: Dict[str, str] = {}
            for url in checked:
                reason = _check_external_url(url)
                if reason:
                    dead[url] = reason
            report.findings.extend(dead_url_findings(dead, url_map))
            if skipped:
                report.findings.append(
                    Finding(
                        "url-sweep-truncated",
                        "warning",
                        root,
                        f"{len(skipped)} external URL(s) not checked this run "
                        f"(cap {_URL_SWEEP_CAP}) — re-run to continue",
                    )
                )
        return report.format_markdown()

    @tool
    def kb_index(path: str = "knowledge") -> str:
        """Regenerate the OKF `index.md` for a knowledge base.

        Reads every `*.md` note under `path`, groups them by `type`, and writes
        a heading-grouped `[Title](slug.md) - description` index to
        `<path>/index.md` (OKF §6 shape, no frontmatter). Content outside the
        auto-generated markers is preserved, so human-authored sections survive.
        Malformed and reserved files (index.md/log.md) are skipped.

        Args:
            path: Directory to index (default "knowledge").

        Returns:
            A status message naming the file written and note count.
        """
        if _has_bound_scopes and not _get_project_id(context):
            return _write_scope_error(context)
        if not context.has_workspace():
            return "Error: kb_index requires a workspace backend."
        ws = context.workspace_manager
        root = path.rstrip("/") or "knowledge"
        try:
            entries = ws.list_files(root, "*.md")
        except Exception as e:
            return f"Error listing `{root}/`: {e}"

        metas: List[Dict[str, Any]] = []
        for rel in entries:
            if rel.endswith("/") or is_reserved(rel):
                continue
            try:
                fm, body = parse_note_md(ws.read_file(rel))
            except Exception as e:
                # Malformed YAML (ValueError) or an unreadable file — kb_lint
                # surfaces the former; skip it for indexing either way.
                logger.warning(f"kb_index: skipping {rel}: {e}")
                continue
            note_id = fm.get("id") if fm else None
            if not note_id:
                continue  # unindexable (kb_lint reports the missing id)
            metas.append(
                {
                    "id": note_id,
                    "type": fm.get("type") or "misc",
                    "description": fm.get("description"),
                    "title": note_title(body) or note_id,
                }
            )
        if not metas:
            return f"No indexable notes found under `{root}/`."

        index_rel = f"{root}/index.md"
        existing = ws.read_file(index_rel) if ws.exists(index_rel) else None
        try:
            ws.write_file(index_rel, render_index_md(metas, existing=existing))
        except Exception as e:
            return f"Error writing `{index_rel}`: {e}"
        return f"Regenerated `{index_rel}` from {len(metas)} note(s)."

    # =========================================================================
    # Return all tools
    # =========================================================================

    return [
        kb_write,
        kb_update,
        kb_read,
        kb_list,
        kb_search,
        kb_related,
        kb_contradictions,
        kb_provenance,
        kb_unanswered,
        kb_export,
        kb_lint,
        kb_index,
    ]
