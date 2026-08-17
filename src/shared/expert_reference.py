"""One expert reference in, one job-creation selection pair out.

Experts live in two stores — bundled definitions on disk and DB rows — and
every read surface already hides that: ``GET /api/experts`` lists both,
``GET /api/experts/{id}`` inspects either. Selection did not, so job creation
carried two mutually-exclusive parameters for one concept and callers had to
infer which store an entry came from by the shape of its id. This module is
the single place that inference lives, shared by the agent/MCP job tool and by
the REST funnel. See
knowledge-base/knowledge/issues/experts_one_catalogue_two_selection_paths.md.

Stdlib-only on purpose: ``src.shared.orch_surface`` may import nothing but the
standard library, httpx and its own package (relatively), and this is one of
the things it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

__all__ = [
    "BASE_WORKER_ALIASES",
    "BASE_WORKER_CONFIG",
    "ExpertChoice",
    "ExpertReferenceConflict",
    "looks_like_expert_uuid",
    "resolve_expert_selection",
]


#: The framework base every worker profile extends. Also the "no bundled
#: expert was named" sentinel on the job-creation surfaces.
BASE_WORKER_CONFIG = "worker_base"

#: Spellings of :data:`BASE_WORKER_CONFIG` that mean the same absence of a
#: choice. Mirrors ``src.core.loader._CONFIG_NAME_ALIASES`` for the worker
#: base; kept as a literal set so this module stays stdlib-pure
#: (tests/test_unified_expert_selection.py asserts the two never drift).
BASE_WORKER_ALIASES = frozenset({BASE_WORKER_CONFIG, "default", "defaults"})


class ExpertReferenceConflict(ValueError):
    """One job creation named two different experts."""


@dataclass(frozen=True)
class ExpertChoice:
    """The (base config, DB overlay) pair one expert reference resolves to.

    ``config_name`` is what the agent loads from disk and ``expert_id`` is the
    DB row merged on top of it. Exactly one of the two ever carries the
    caller's choice — that is the invariant the old mutual-exclusion refusal
    was protecting, restated as a return value instead of a rule callers had
    to know.
    """

    config_name: str
    expert_id: str | None
    kind: Literal["bundled", "db", "default"]
    reference: str | None


def looks_like_expert_uuid(value: Any) -> bool:
    """True when a reference is a DB expert id rather than a bundled slug.

    Bundled experts are directory names (``developer``); DB experts are UUIDs.
    ``GET /api/experts/{expert_id}`` already dispatches on exactly this shape,
    which is why one catalogue id can address either store.
    """
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def resolve_expert_selection(
    *,
    expert: str | None = None,
    config_name: str | None = None,
    expert_id: str | None = None,
) -> ExpertChoice:
    """Resolve one expert reference into the pair job creation persists.

    ``expert`` is the unified selector: it takes either form the catalogue
    prints, a bundled slug or a DB expert UUID, and callers never need to know
    which store an entry came from. ``config_name`` and ``expert_id`` are the
    deprecated single-store aliases kept working for existing callers.

    Resolution:

    * bundled slug -> ``(slug, None)``. A bundled expert is a *complete*
      definition that declares its own ``$extends``, so it is the base and
      nothing is overlaid on it.
    * DB UUID -> ``(worker_base, uuid)``. A DB expert is a fragment, so it
      needs the framework base underneath it.
    * nothing named -> ``(worker_base, None)``, leaving the deployment's
      configured default (``application_expert_defaults``) in charge.

    Precedence between ``expert`` and an alias is deliberately *none*: an
    alias that repeats the same reference is accepted, one that names a
    different expert raises :class:`ExpertReferenceConflict`. Silently
    dropping one of two stated experts is the failure mode this seam exists
    to remove.
    """
    reference = _clean(expert)
    alias_expert_id = _clean(expert_id)
    alias_config = _clean(config_name)
    # Naming the framework base is the absence of a choice, not a choice — the
    # same string has to mean the same thing in whichever parameter it lands,
    # or `expert="worker_base"` would be a refusal while the identical
    # `config_name="worker_base"` is the default every caller already sends.
    if alias_config in BASE_WORKER_ALIASES:
        alias_config = None
    if reference in BASE_WORKER_ALIASES:
        reference = None

    if reference is None:
        if alias_expert_id and alias_config:
            raise ExpertReferenceConflict(
                f"expert_id cannot be combined with config_name={alias_config!r}: "
                "they name two different experts. Pass a single "
                "expert=<bundled slug or DB expert UUID> instead; list_experts "
                "shows both kinds in one catalogue."
            )
        if alias_expert_id:
            return ExpertChoice(
                BASE_WORKER_CONFIG, alias_expert_id, "db", alias_expert_id
            )
        if alias_config:
            return ExpertChoice(alias_config, None, "bundled", alias_config)
        return ExpertChoice(BASE_WORKER_CONFIG, None, "default", None)

    if looks_like_expert_uuid(reference):
        if alias_config:
            raise ExpertReferenceConflict(
                f"expert={reference!r} is a database expert and "
                f"config_name={alias_config!r} is a bundled one; a job runs "
                "one expert. Drop the deprecated config_name."
            )
        if alias_expert_id and alias_expert_id != reference:
            raise ExpertReferenceConflict(
                f"expert={reference!r} and the deprecated "
                f"expert_id={alias_expert_id!r} name different experts."
            )
        return ExpertChoice(BASE_WORKER_CONFIG, reference, "db", reference)

    if alias_expert_id:
        raise ExpertReferenceConflict(
            f"expert={reference!r} is a bundled expert and the deprecated "
            f"expert_id={alias_expert_id!r} is a database one; a job runs one "
            "expert. Drop expert_id — expert accepts either kind."
        )
    if alias_config and alias_config != reference:
        raise ExpertReferenceConflict(
            f"expert={reference!r} and the deprecated "
            f"config_name={alias_config!r} name different experts."
        )
    return ExpertChoice(reference, None, "bundled", reference)
