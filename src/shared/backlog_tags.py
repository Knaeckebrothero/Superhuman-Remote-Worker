"""Machine tags on backlog tickets — the shared namespace, nothing more.

Backlog tickets are ordinary ``knowledge_index`` notes; category, expert pin and
dispatch authorization ride their existing ``tags TEXT[]`` rather than new
columns (knowledge-base/knowledge/features/officer_backlog_pools.md §4). Four tag shapes are
therefore *machine* vocabulary rather than human labels::

    ready              dispatch authorization — officer provenance only
    parallel-safe      executor singleton exemption — officer provenance only
    category:<name>    which pool may pull this ticket
    expert:<config>    which expert config to spawn

This module lives in ``src/shared`` because both sides of the wire must agree on
that namespace and neither may import the other: the agent's knowledge tools
strip the officer-only tags on write (a worker that could set ``ready`` could
self-authorize dispatch onto the VM-backed executor slot) and exclude the whole
namespace from the sparse search document, while the orchestrator's tick reads
them back to decide what to dispatch. A list that drifted between the two would
be a privilege-escalation hole, not a cosmetic inconsistency — so there is one
list, here.

Deliberately narrow: this module knows the *shape* of machine tags. What a
category means, which experts belong to it, and what contract it carries are the
orchestrator's business (``orchestrator/services/work_categories.py``).
"""

from __future__ import annotations

from typing import Iterable

# Dispatch authorization. Officer/Legate provenance only — the anti-amplification
# firewall between "a worker filed a ticket" and "a worker started work".
READY_TAG = "ready"

# Exempts an executor ticket from the singleton rule (§5.5). Same provenance rule:
# a worker that could grant itself parallelism defeats the serialization that
# keeps two executors out of the same story.
PARALLEL_SAFE_TAG = "parallel-safe"

CATEGORY_PREFIX = "category:"
EXPERT_PREFIX = "expert:"

# Stripped from worker-authored writes. The officer's own kb_* calls keep them.
OFFICER_ONLY_TAGS: frozenset[str] = frozenset({READY_TAG, PARALLEL_SAFE_TAG})

_MACHINE_PREFIXES: tuple[str, ...] = (CATEGORY_PREFIX, EXPERT_PREFIX)


def normalize_tag(tag: str) -> str:
    """Canonicalize one tag: stripped and lowercased.

    Every tag, not only the machine ones. That is the convention the system
    already ran on — ``kb_update`` and the Neo4j ``:TAGGED`` writer have always
    folded case — and tags are matched by exact string: ``tags @> ARRAY['ready']``
    does not see ``Ready``. Folding at the write paths is what makes a tag
    mean the same thing to the tick, to search, and to a human typing it into
    a frontmatter block. ``kb_write`` and the reindexer are brought onto the
    same footing here; they preserved case before, which is precisely how a
    hand-edited ``Category:Executor`` would have been invisible to its pool.
    """
    return tag.strip().lower()


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
    """``normalize_tag`` over a collection, dropping blanks, preserving order."""
    out: list[str] = []
    for tag in tags or ():
        if not isinstance(tag, str):
            continue
        normalized = normalize_tag(tag)
        if normalized:
            out.append(normalized)
    return out


def is_machine_tag(tag: str) -> bool:
    """True for any tag in the machine namespace (all four shapes)."""
    lowered = tag.strip().lower()
    return lowered in OFFICER_ONLY_TAGS or lowered.startswith(_MACHINE_PREFIXES)


def is_officer_only_tag(tag: str) -> bool:
    """True for the two tags a worker may never set (``ready``, ``parallel-safe``)."""
    return tag.strip().lower() in OFFICER_ONLY_TAGS


def strip_officer_tags(tags: Iterable[str] | None) -> list[str]:
    """Drop officer-provenance tags from a worker-authored tag list.

    Silent removal is deliberate: a worker asking for ``ready`` is either
    confused or relaying injected content, and neither case wants an error path
    that leaks how the authorization boundary works. The caller reports what it
    dropped in its own tool result if it wants the worker to learn.
    """
    return [t for t in normalize_tags(tags) if not is_officer_only_tag(t)]


def strip_machine_tags(tags: Iterable[str] | None) -> list[str]:
    """Drop the whole machine namespace — used to build search text.

    Ticket plumbing must not rank in hybrid search or land in a worker's KB
    injection: ``category:researcher`` is dispatch metadata, not something a
    search for "researcher" should surface.
    """
    return [t for t in normalize_tags(tags) if not is_machine_tag(t)]


def category_tag(category: str) -> str:
    """Build the ``category:<name>`` tag."""
    return f"{CATEGORY_PREFIX}{category.strip().lower()}"


def expert_tag(expert: str) -> str:
    """Build the ``expert:<config>`` tag."""
    return f"{EXPERT_PREFIX}{expert.strip().lower()}"


def _values_for(tags: Iterable[str] | None, prefix: str) -> list[str]:
    """Every non-empty value carried under ``prefix``, de-duplicated in order.

    Returns a list rather than a single value on purpose: two ``category:`` tags
    is an ambiguity the tick must *see* and skip, never silently resolve by
    picking the first one.
    """
    seen: list[str] = []
    for tag in normalize_tags(tags):
        if not tag.startswith(prefix):
            continue
        value = tag[len(prefix) :].strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def category_values(tags: Iterable[str] | None) -> list[str]:
    """Values carried under ``category:`` — more than one means ambiguous."""
    return _values_for(tags, CATEGORY_PREFIX)


def expert_values(tags: Iterable[str] | None) -> list[str]:
    """Values carried under ``expert:`` — more than one means ambiguous."""
    return _values_for(tags, EXPERT_PREFIX)


def has_tag(tags: Iterable[str] | None, tag: str) -> bool:
    """Case-insensitive membership for a machine tag."""
    return normalize_tag(tag) in set(normalize_tags(tags))
