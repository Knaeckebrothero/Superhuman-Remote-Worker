"""Deliverable-contract helpers (agent side).

P1-C of knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md: jobs may
carry ``context.required_deliverables`` — workspace-relative paths,
``kb:<slug>`` knowledge-note slugs, or one exact
``pr:<owner>/<repository>`` publication promise. This
module is the agent-side half of that contract:

* :func:`parse_required_deliverables` — tolerant manifest reader (the value
  arrives via user-supplied job context, so never trust its shape);
* :func:`normalize_deliverable_path` — the F14 fix: ``repo/`` prefixes and
  ``./`` are presentation, not identity. The validator that rejected job
  ``58027ee7``'s *correct* deliverable list over a missing ``repo/`` prefix
  turned a complete job into an honest-floor seal;
* :func:`resolve_workspace_deliverable` — existence lookup that accepts both
  the prefixed and unprefixed form against the live workspace;
* :func:`format_deliverable_contract_block` — the "Required deliverables
  (contract)" block rendered into the worker's task brief at workspace init.

The orchestrator-side gate (src/orchestrator/services/deliverable_gate.py) applies
the same path normalization against the job branch HEAD in Gitea.
"""

from typing import Any, List, Optional, Tuple

from shared.deliverable_contract import (
    KB_DELIVERABLE_PREFIX,
    PR_DELIVERABLE_PREFIX,
    normalize_deliverable,
    parse_required_deliverables as _parse_required_deliverables,
)


def normalize_deliverable_path(path: Any) -> Optional[str]:
    """Canonicalize a deliverable path; ``None`` when it isn't one.

    Canonical form is workspace-relative WITHOUT the ``repo/`` prefix:
    ``./repo/output/report.md`` → ``output/report.md``. ``kb:<slug>`` entries
    are lowered/stripped but keep their prefix. Empty or non-string input
    returns ``None`` so callers can drop it.
    """
    return normalize_deliverable(path)


def parse_required_deliverables(source: Any) -> List[str]:
    """Extract a clean manifest from job metadata/context.

    Accepts the metadata dict itself, a context dict, or the raw list.
    Deduplicates (order-preserving) after normalization; anything that
    doesn't normalize is dropped. Always returns a list (possibly empty).
    """
    return _parse_required_deliverables(source)


def deliverable_path_variants(path: str) -> Tuple[str, ...]:
    """Both acceptable workspace spellings of a canonical path.

    The workspace may hold the file at ``repo/<path>`` (repository jobs
    check out under ``repo/``) or directly at ``<path>`` — F14's lesson is
    that neither spelling may fail a seal when the file exists at the other.
    """
    canonical = normalize_deliverable_path(path)
    if not canonical or canonical.startswith(
        (KB_DELIVERABLE_PREFIX, PR_DELIVERABLE_PREFIX)
    ):
        return ()
    return (canonical, f"repo/{canonical}")


def resolve_workspace_deliverable(
    workspace: Any, path: str
) -> Tuple[Optional[str], bool]:
    """Find a deliverable in the live workspace, tolerant of ``repo/`` prefix.

    Returns ``(resolved_path, exists)`` — ``resolved_path`` is the variant
    that exists (or the canonical form when none does). ``kb:`` entries are
    not workspace files and resolve to ``(path, False)``.

    Errors PROPAGATE rather than becoming "missing": reporting a broken
    probe as worker non-delivery is the F1 error-laundering anti-pattern
    (knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md), and a dead
    workspace (``WorkspaceUnavailableError``) is a lifecycle signal, not a
    bad deliverable (Defect 8). A probe error on ONE variant is tolerated
    when another variant answers ``True``.
    """
    from shared.runtime.core.workspace_backend import WorkspaceUnavailableError

    canonical = normalize_deliverable_path(path)
    if not canonical:
        return None, False
    if canonical.startswith((KB_DELIVERABLE_PREFIX, PR_DELIVERABLE_PREFIX)):
        return canonical, False
    first_error: Optional[Exception] = None
    for variant in deliverable_path_variants(canonical):
        try:
            if workspace.exists(variant):
                return variant, True
        except WorkspaceUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001 — keep probing other variants
            if first_error is None:
                first_error = e
    if first_error is not None:
        # No variant answered — "missing" would be a lie; surface the error.
        raise first_error
    return canonical, False


def format_deliverable_contract_block(deliverables: Any) -> str:
    """Render the "Required deliverables (contract)" block for the task brief.

    Returns ``""`` when there is no manifest so callers can append
    unconditionally. The wording is the authoritative source feeding
    plan.md's "## Deliverables" section (strategic templates reference it).
    """
    manifest = parse_required_deliverables(deliverables)
    if not manifest:
        return ""
    lines = [
        "\n\n## Required Deliverables (Contract)",
        "",
        "This job carries a deliverable contract. The seal (`job_complete`) is",
        "validated against the EXISTENCE of these artifacts — a completion that",
        "claims success while any of them is missing will be bounced back to",
        "you with the missing paths listed. A PR publication promise also",
        "requires proof when you report that delivery was not possible; it",
        "ends blocked/undelivered rather than completed if retries are exhausted.",
        "",
    ]
    for path in manifest:
        if path.startswith(KB_DELIVERABLE_PREFIX):
            lines.append(
                f"- `{path}` (knowledge note — write it with kb_write using "
                f"this exact slug)"
            )
        elif path.startswith(PR_DELIVERABLE_PREFIX):
            lines.append(
                f"- `{path}` (pull request — use the attached repository's "
                "repo_open_pr tool; a note or local file cannot satisfy this)"
            )
        else:
            lines.append(f"- `{path}`")
    lines += [
        "",
        "Rules:",
        "- Scaffold each deliverable file EARLY (phase 1), even as an outline.",
        "- Keep each deliverable current as work progresses; Git-backed workspaces",
        "  commit and push progress automatically.",
        "- Do not create a separate manifest or status file for this contract; the",
        "  platform validates the listed artifacts directly.",
        '- Keep plan.md\'s "## Deliverables" section mapped to these paths.',
        "- Paths are workspace-relative; `repo/` prefix is accepted either way.",
        "- A `pr:owner/repository` item is satisfied only by a server-recorded,",
        "  forge-verifiable pull request for that exact attached repository.",
    ]
    return "\n".join(lines)
