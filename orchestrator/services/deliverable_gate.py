"""Deterministic deliverable-contract gate at the seal (P1-C).

knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md §4 P1-C / §7 annex E.

Jobs may declare ``context.required_deliverables`` — workspace-relative paths
(plus ``kb:<slug>`` knowledge-note slugs) that MUST exist before a completion
that claims done-ness is allowed to seal. Verifier #1's "26/27 todos done"
seal shipped 0/7 required deliverables and consumed a human-priced officer
review cycle; the check the officer ran by hand (blind, through broken tools)
is here a platform gate that cannot be argued with by narrative.

Semantics (annex E, each element proven elsewhere):

* Applies only when the agent's report CLAIMS completion (goal_achieved or a
  job_complete freeze) and the status would resolve to ``completed`` /
  ``pending_review`` / ``reviewing``. ``reviewing`` is included so a bounce
  fires BEFORE the critic spawn — cheap deterministic existence checks first,
  critic LLM second. An honest incomplete stop (no claim) is never gated.
* Validates END-STATE ARTIFACTS only, at the job branch HEAD in Gitea —
  never worker narrative, todo counts, or confidence.
* Path normalization (F14): ``repo/`` prefix and ``./`` are tolerated on
  BOTH sides — the manifest and the tree — so a complete job can never be
  bounced over a prefix.
* On failure: bounce, don't seal. The job re-enters the P1-A
  resume-with-feedback lane (``queued_feedback`` + ``queued_feedback_reason``
  → the worker's [FEEDBACK_RESUME] banner) with a PRECISE missing/present
  listing, resuming on its own checkpoint. Bounces are capped
  (:data:`DELIVERABLE_GATE_BOUNCE_CAP`); at the cap the seal falls through —
  ``completed`` demotes to ``pending_review`` (loop jobs keep ``completed``,
  the pending_review loop-wedge rule) with the gate report stamped in
  ``context.deliverable_gate`` so a human sees exactly what's missing.
  Never an infinite loop.
* Gitea unavailable / repo unresolvable → fail-open: skip, log, stamp
  ``{skipped: true, reason}`` (graceful-degradation house rule). ``kb:``
  entries fail-open individually when the vector store can't answer.
* A bounced seal spawns NO critic/curator subjobs (the /complete handler
  early-returns); a passed gate leaves that flow untouched.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Maximum resume-with-feedback bounces per job before the gate stops bouncing
# and lets the seal fall through (with the report stamped) for a human.
DELIVERABLE_GATE_BOUNCE_CAP = 2

# Prefix marking a knowledge-note deliverable (checked against the KB index,
# not the Gitea tree). Mirrors src/core/deliverables.py agent-side.
KB_DELIVERABLE_PREFIX = "kb:"

# Statuses (as resolved by determine_job_status) whose seal the gate may
# intercept. ``reviewing`` is the critic-spawn lane — see module docstring.
_GATED_STATUSES: frozenset[str] = frozenset(
    {"completed", "pending_review", "reviewing"}
)


# ---------------------------------------------------------------------------
# Manifest parsing + path normalization (F14)
# ---------------------------------------------------------------------------


def normalize_deliverable_path(path: Any) -> str | None:
    """Canonicalize a deliverable path; ``None`` when it isn't one.

    Canonical form is workspace-relative WITHOUT the ``repo/`` prefix and
    without ``./``. ``kb:<slug>`` entries keep their prefix.
    """
    if not isinstance(path, str):
        return None
    candidate = path.strip()
    if not candidate:
        return None
    if candidate.startswith(KB_DELIVERABLE_PREFIX):
        slug = candidate[len(KB_DELIVERABLE_PREFIX) :].strip()
        return f"{KB_DELIVERABLE_PREFIX}{slug}" if slug else None
    while candidate.startswith("./"):
        candidate = candidate[2:]
    candidate = candidate.lstrip("/")
    if candidate.startswith("repo/"):
        candidate = candidate[len("repo/") :]
    candidate = candidate.strip()
    return candidate or None


# Cloned repository datasources land here, and the platform gitignores the
# directory at seed time on purpose — src/core/workspace.py,
# src/core/datasource_setup.py and src/tools/orchestrator/repositories.py all
# write it ("working-tree only; never versioned"), guarding the
# contentless-gitlink bug b1758f38. Note the single character between this and
# the F14 ``repo/`` prefix normalized away above: ``repo/`` is the job's OWN
# tree, ``repos/`` is somebody else's repository, mounted and unversioned.
CLONED_REPO_PREFIX = "repos/"


def is_cloned_repo_deliverable(path: Any) -> bool:
    """True for a manifest entry inside a cloned repository datasource.

    Such an entry can never be verified from the job's own versioned tree,
    because the platform refuses to version it. Treating it as missing is a
    false negative, and a costly one: it bounces a job that delivered, and
    teaches the agent that the way to pass is to defeat the .gitignore.
    See knowledge-base/knowledge/issues/deliverable_gate_cannot_see_cloned_repo_deliverables.md.
    """
    if not isinstance(path, str) or path.startswith(KB_DELIVERABLE_PREFIX):
        return False
    if not path.startswith(CLONED_REPO_PREFIX):
        return False
    remainder = path[len(CLONED_REPO_PREFIX) :].strip("/")
    return bool(remainder)


def cloned_repo_deliverables(manifest: list[str]) -> list[str]:
    """The manifest entries this gate can never verify, in declared order.

    Used by the gate to fail open per entry, and by job creation to refuse
    the manifest outright — one definition of the rule, two consumers.
    """
    return [p for p in manifest if is_cloned_repo_deliverable(p)]


def parse_required_deliverables(context: Any) -> list[str]:
    """The job's manifest as a clean, deduplicated list (possibly empty).

    Accepts a job ``context`` dict (or its JSON string form) or the raw
    list value. Entries that don't normalize are dropped.
    """
    value = context
    if isinstance(context, str):
        try:
            value = json.loads(context)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(value, dict):
        value = value.get("required_deliverables")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for entry in value:
        normalized = normalize_deliverable_path(entry)
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _tree_has_path(tree_paths: set[str], canonical: str) -> bool:
    """Membership check tolerant of the ``repo/`` prefix on the TREE side.

    The manifest side is already canonicalized; a repository job's checkout
    may place the file under ``repo/`` in the pushed tree (and a manifest
    written with the prefix was canonicalized away), so both spellings count.
    """
    return canonical in tree_paths or f"repo/{canonical}" in tree_paths


# ---------------------------------------------------------------------------
# Trigger predicate
# ---------------------------------------------------------------------------


def gate_applies(job: dict[str, Any], result: dict[str, Any], new_status: Any) -> bool:
    """True when this completion must pass the deliverable gate.

    Requires: a gated status, a stop, a genuine COMPLETION CLAIM
    (goal_achieved or a job_complete freeze — an honest incomplete stop that
    merely lands on pending_review is not gated), and a non-empty manifest.
    """
    if new_status not in _GATED_STATUSES:
        return False
    if not result.get("should_stop", False):
        return False
    from services.completion import _parse_freeze_data

    fd = _parse_freeze_data(job)
    if not fd:
        fd = result.get("freeze_data")
        if isinstance(fd, str):
            try:
                fd = json.loads(fd)
            except (json.JSONDecodeError, ValueError):
                fd = {}
        if not isinstance(fd, dict):
            fd = {}
    claimed = (
        bool(result.get("goal_achieved"))
        or fd.get("freeze_type") == "job_complete"
        or fd.get("status") == "job_completed"
    )
    if not claimed:
        return False
    from services.completion import _parse_context

    return bool(parse_required_deliverables(_parse_context(job)))


# ---------------------------------------------------------------------------
# Evaluation (Gitea + KB existence)
# ---------------------------------------------------------------------------


async def _resolve_repo_ref(
    job: dict[str, Any], db: Any
) -> tuple[str | None, str | None]:
    """(repo_name, ref) for the job's pushed branch, or (None, None).

    Mirrors main.resolve_job_repo without the HTTP semantics: repo on the
    job row, else the parent's (subjob-on-branch model). Root jobs push to
    the repo default branch — ``main`` for per-job repos.
    """
    repo_name = job.get("repo_name")
    if not repo_name and job.get("parent_job_id") and db is not None:
        try:
            parent = await db.get_job(str(job["parent_job_id"]))
        except Exception:  # noqa: BLE001 — unresolvable → fail-open upstream
            parent = None
        if parent and parent.get("repo_name"):
            repo_name = parent["repo_name"]
    if not repo_name:
        return None, None
    ref = job.get("branch_name") or "main"
    return str(repo_name), str(ref)


async def _kb_note_exists(
    vector_db: Any, project_id: str | None, slug: str
) -> bool | None:
    """KB-note existence via the knowledge_index; ``None`` = unverifiable.

    Same store the MCP knowledge tools read. Any failure (no handle, no
    project, query error) returns None so the caller fails open on that
    entry rather than bouncing a job over infrastructure.
    """
    if vector_db is None or not project_id or not slug:
        return None
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM knowledge_index WHERE project_id = $1 AND note_id = $2",
                str(project_id),
                slug,
            )
        return row is not None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Deliverable gate: KB lookup failed for note %r (project %s): %s",
            slug,
            project_id,
            e,
        )
        return None


async def evaluate_deliverable_gate(
    job: dict[str, Any],
    *,
    db: Any,
    gitea: Any,
    vector_db: Any = None,
) -> dict[str, Any]:
    """Check every manifest entry at the job branch HEAD in Gitea.

    Returns a report dict:
      * ``{"skipped": True, "reason": ...}`` — Gitea down / repo or tree
        unresolvable (fail-open; the caller must not block the seal);
      * otherwise ``{"passed": bool, "missing": [...], "present": [...],
        "unverified": [...], "commit_sha": str|None, "ref": str}`` —
        ``unverified`` entries (kb: without a working KB lookup) never count
        as missing.
    """
    from services.completion import _parse_context, _parse_freeze_data

    manifest = parse_required_deliverables(_parse_context(job))
    if not manifest:
        return {"skipped": True, "reason": "no manifest"}

    # Nothing reached the repository, so "missing from the repository" says
    # nothing about what the agent produced. The agent sets this when its
    # job-ending push does not land (src/core/phase.py,
    # _push_job_ending_state): the deliverables exist, on a workspace pod about
    # to be reclaimed, and Gitea is empty or stale.
    #
    # Without this the gate reads every manifest entry as missing and BOUNCES —
    # resuming the agent to produce files it already produced, onto a workspace
    # that may no longer exist (see run_deliverable_gate). That bounce also
    # early-returns in the caller, so it preempts the verification escalation
    # that would otherwise report the real reason.
    #
    # This belongs with the four skips below rather than beside them: it is the
    # same graceful-degradation rule ("never block a seal on infrastructure the
    # worker cannot fix"), and it is invisible to the others only because it is
    # the one infrastructure failure that looks like a CLEAN read — the tree is
    # readable, it is merely empty.
    # knowledge-history/done/git_push_fails_silently_via_workspace_backend.md
    freeze = _parse_freeze_data(job) or {}
    if isinstance(freeze, dict) and freeze.get("delivery_failed"):
        return {
            "skipped": True,
            "reason": str(
                freeze.get("delivery_error")
                or "the job-ending push failed; deliverables were not delivered"
            ),
        }

    if gitea is None or not getattr(gitea, "is_initialized", False):
        return {"skipped": True, "reason": "gitea unavailable"}

    repo_name, ref = await _resolve_repo_ref(job, db)
    if not repo_name or not ref:
        return {"skipped": True, "reason": "job repo unresolvable"}

    try:
        tree = await gitea.list_tree(repo_name, ref)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Deliverable gate: tree listing failed for %s@%s: %s",
            repo_name,
            ref,
            e,
        )
        tree = None
    if tree is None:
        return {
            "skipped": True,
            "reason": f"tree unreadable at {repo_name}@{ref}",
        }
    tree_paths = {
        str(entry.get("path"))
        for entry in tree
        if entry.get("type") == "blob" and entry.get("path")
    }

    commit_sha: str | None = None
    try:
        commit_sha = await gitea.get_branch_head_sha(repo_name, ref)
    except Exception:  # noqa: BLE001 — sha is provenance, not load-bearing
        commit_sha = None

    project_id = str(job["project_id"]) if job.get("project_id") else None
    missing: list[str] = []
    present: list[str] = []
    unverified: list[str] = []
    for path in manifest:
        if path.startswith(KB_DELIVERABLE_PREFIX):
            slug = path[len(KB_DELIVERABLE_PREFIX) :]
            found = await _kb_note_exists(vector_db, project_id, slug)
            if found is None:
                # Unverifiable ≠ missing: fail open on this entry, log it.
                logger.warning(
                    "Deliverable gate: kb entry %r unverifiable for job %s "
                    "— treating as satisfied",
                    path,
                    job.get("id"),
                )
                unverified.append(path)
            elif found:
                present.append(path)
            else:
                missing.append(path)
            continue
        if _tree_has_path(tree_paths, path):
            present.append(path)
        elif is_cloned_repo_deliverable(path):
            # Checked the tree FIRST on purpose: a path that really is
            # committed (job 29c28492 forced two in by hand) is reported
            # present, not excused. Fail open only where the gate genuinely
            # cannot see — the module's standing rule, "never block a seal on
            # infrastructure the worker cannot fix".
            logger.warning(
                "Deliverable gate: %r is inside a cloned repository datasource "
                "for job %s — unverifiable from the job tree, treating as "
                "satisfied",
                path,
                job.get("id"),
            )
            unverified.append(path)
        else:
            missing.append(path)

    return {
        "passed": not missing,
        "missing": missing,
        "present": present,
        "unverified": unverified,
        "commit_sha": commit_sha,
        "ref": ref,
    }


# ---------------------------------------------------------------------------
# Bounce feedback rendering
# ---------------------------------------------------------------------------


def _render_bounce_feedback(
    report: dict[str, Any], bounce: int, cap: int
) -> tuple[str, str]:
    """(full feedback, short banner reason) for a bounced seal.

    The full text rides ``queued_feedback`` into the worker's compacted
    context; the short reason renders verbatim in the [FEEDBACK_RESUME]
    banner (P1-A honest-cause contract).
    """
    sha = report.get("commit_sha")
    where = f"commit {sha[:12]}" if sha else "the branch HEAD"
    ref = report.get("ref")
    missing = report.get("missing") or []
    present = report.get("present") or []
    lines = [
        "DELIVERABLE CONTRACT GATE: this completion was refused because "
        "required deliverables are missing from the job branch.",
        "",
        f"Checked at {where}" + (f" on branch `{ref}`" if ref else "") + ":",
        "",
        f"MISSING ({len(missing)}):",
    ]
    lines += [f"  - {p}" for p in missing]
    lines.append("")
    lines.append(f"PRESENT ({len(present)}):")
    lines += [f"  - {p}" for p in present] if present else ["  - (none)"]
    lines += [
        "",
        "Produce the MISSING artifacts at exactly these workspace paths "
        "(`repo/` prefix is accepted either way), commit them, and call "
        "job_complete again. Do NOT redo work that is already present — "
        "only the missing deliverables block the seal.",
        f"(Deliverable-gate bounce {bounce}/{cap}: after {cap} bounces the "
        "job is handed to a human with this report instead.)",
    ]
    reason = (
        f"deliverable contract gate: {len(missing)} of "
        f"{len(missing) + len(present)} required deliverables missing at "
        f"{where} (bounce {bounce}/{cap})"
    )
    return "\n".join(lines), reason


# ---------------------------------------------------------------------------
# The gate — called from the /complete handler via services.completion
# ---------------------------------------------------------------------------


async def run_deliverable_gate(
    job: dict[str, Any],
    result: dict[str, Any],
    new_status: str | None,
    *,
    db: Any,
    gitea: Any,
    queue_resume: Callable[..., Any],
    vector_db: Any = None,
) -> tuple[str | None, list[str], bool]:
    """Apply the deliverable-contract gate to a resolved completion status.

    Returns ``(new_status, actions, bounced)``:

    * gate not applicable → status unchanged, no actions, ``bounced=False``;
    * pass / fail-open skip → status unchanged, action + stamp written;
    * missing under the cap → ``queue_resume`` invoked (P1-A feedback lane),
      ``bounced=True`` — the caller must EARLY-RETURN without writing the
      sealed status or spawning critic/curator subjobs;
    * missing at the cap → no bounce; ``completed`` demotes to
      ``pending_review`` (loop jobs keep a terminal ``completed`` — the
      pending_review loop-wedge rule), report stamped for the human.

    All context writes use the atomic top-level merge (never a context
    read-modify-write).
    """
    if not gate_applies(job, result, new_status):
        return new_status, [], False

    job_id = str(job.get("id"))
    from services.completion import _parse_context

    prior = _parse_context(job).get("deliverable_gate")
    prior = prior if isinstance(prior, dict) else {}
    bounces = int(prior.get("bounces", 0) or 0)
    checked_at = datetime.now(timezone.utc).isoformat()

    report = await evaluate_deliverable_gate(
        job, db=db, gitea=gitea, vector_db=vector_db
    )

    async def _stamp(stamp: dict[str, Any]) -> None:
        try:
            await db.merge_job_context(job_id, {"deliverable_gate": stamp})
        except Exception as e:  # noqa: BLE001 — the stamp is observability
            logger.warning(
                "Deliverable gate: failed to stamp context for %s: %s", job_id, e
            )

    if report.get("skipped"):
        # Fail-open (graceful-degradation house rule): never block a seal on
        # infrastructure the worker cannot fix.
        reason = str(report.get("reason"))
        logger.warning(
            "Deliverable gate SKIPPED for job %s: %s — sealing without the check",
            job_id,
            reason,
        )
        await _stamp(
            {
                "skipped": True,
                "reason": reason,
                "checked_at": checked_at,
                "bounces": bounces,
            }
        )
        return new_status, [f"deliverable gate skipped ({reason})"], False

    sha = report.get("commit_sha")
    if report.get("passed"):
        await _stamp(
            {
                "passed": True,
                "checked_at": checked_at,
                "commit_sha": sha,
                "present": report.get("present"),
                "unverified": report.get("unverified"),
                "bounces": bounces,
            }
        )
        n_present = len(report.get("present") or [])
        n_unverified = len(report.get("unverified") or [])
        action = (
            f"deliverable gate passed ({n_present}/{n_present + n_unverified} present"
        )
        if n_unverified:
            # Name the KIND. This line predated cloned-repo entries and called
            # every fail-open a "kb" one; saying kb about a repos/ path
            # describes a check that never ran.
            unverified_paths = report.get("unverified") or []
            kb_n = sum(
                1 for p in unverified_paths if str(p).startswith(KB_DELIVERABLE_PREFIX)
            )
            repo_n = n_unverified - kb_n
            kinds = []
            if kb_n:
                kinds.append(f"{kb_n} kb")
            if repo_n:
                kinds.append(f"{repo_n} cloned-repo")
            noun = "entry" if n_unverified == 1 else "entries"
            action += (
                f", {n_unverified} unverifiable {noun} failed open ({', '.join(kinds)})"
            )
        action += ")"
        return new_status, [action], False

    missing = report.get("missing") or []
    if bounces < DELIVERABLE_GATE_BOUNCE_CAP:
        bounce_n = bounces + 1
        feedback, reason = _render_bounce_feedback(
            report, bounce_n, DELIVERABLE_GATE_BOUNCE_CAP
        )
        # Stamp BEFORE the resume queue flips the row: both are atomic
        # top-level merges on different keys, so ordering is only about
        # observability if the second write fails.
        await _stamp(
            {
                "passed": False,
                "checked_at": checked_at,
                "commit_sha": sha,
                "missing": missing,
                "present": report.get("present"),
                "unverified": report.get("unverified"),
                "bounces": bounce_n,
            }
        )
        try:
            await queue_resume(job_id, feedback, reason=reason)
        except Exception as e:  # noqa: BLE001
            # The bounce could not be queued — do NOT strand the job on a
            # refused-but-unbounced seal. Fall through to the sealed status
            # with the failed-bounce report stamped.
            logger.error(
                "Deliverable gate: bounce queue failed for %s (%s) — "
                "sealing with the gate report instead",
                job_id,
                e,
            )
            return (
                new_status,
                [
                    f"deliverable gate: {len(missing)} missing, bounce "
                    f"FAILED to queue — sealed with report"
                ],
                False,
            )
        logger.warning(
            "Deliverable gate BOUNCED job %s (bounce %d/%d): %d missing at %s",
            job_id,
            bounce_n,
            DELIVERABLE_GATE_BOUNCE_CAP,
            len(missing),
            sha or "HEAD",
        )
        return (
            None,
            [
                f"deliverable gate: bounced to feedback-resume "
                f"({bounce_n}/{DELIVERABLE_GATE_BOUNCE_CAP}) — "
                f"{len(missing)} missing"
            ],
            True,
        )

    # Cap reached: stop bouncing, hand the seal to a human with the report.
    await _stamp(
        {
            "passed": False,
            "cap_reached": True,
            "checked_at": checked_at,
            "commit_sha": sha,
            "missing": missing,
            "present": report.get("present"),
            "unverified": report.get("unverified"),
            "bounces": bounces,
        }
    )
    final_status = new_status
    if new_status == "completed":
        from services.project_loops import job_loop_id
        from services.verification_ledger import escalation_status

        final_status = escalation_status(is_loop_job=bool(job_loop_id(job)))
    action = (
        f"deliverable gate: bounce cap reached ({bounces}) with "
        f"{len(missing)} still missing — sealing as {final_status} with report"
    )
    logger.error("Deliverable gate CAP for job %s: %s", job_id, action)
    return final_status, [action], False
