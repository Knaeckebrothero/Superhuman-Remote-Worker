"""Structured per-job change records in PostgreSQL.

``project_jobs_repo_retirement.md`` keeps the useful invariant from
``workspace_and_change_records.md``—every terminal job leaves one honest
record—but moves the authority out of an agent-visible Git tree. One immutable
``job_change_records`` row is keyed by job id; duplicate completion callbacks
therefore no-op without listing ``retros/``.

The Markdown renderer remains temporarily for legacy export/tests, but neither
writer calls or commits it. ``write_loop_retro`` and ``write_job_record`` both
insert the same structured schema through the database facade.

Who writes what (§5.1) — the split that must survive every edit here:

* The ORCHESTRATOR stamps what it knows first-hand (branch, merge outcome)
  and what it verified cheaply against its own stores (note ids present in
  ``knowledge_index``) — those entries carry ``verified: true``.
* The AGENT's declared entries (``freeze_data.changes``) pass through with
  ``verified: false`` ALWAYS — recorded as claims, never silently promoted.
  v1 does not fetch external URLs to check PR links; a record that cannot
  distinguish "did it" from "said it did" is worth less than no record.
* A PULL REQUEST opened through ``repo_open_pr`` is orchestrator knowledge,
  not an agent claim: the tool persists ``{forge, repo, number, url, head,
  base}`` into ``jobs.context`` itself, on success, at call time. Reading it
  back fetches nothing, so it carries ``verified: true`` without weakening
  the rule above — the record is the orchestrator's own, and a malformed one
  is dropped rather than downgraded.

Best-effort throughout: a record failure must never block completion
handling or a loop advance.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

import yaml

from services.job_delivery import parse_job_pull_request

logger = logging.getLogger(__name__)

# Caps keep one record small enough to read at a glance and bound what an
# agent's freeze_data can push onto permanent shared history.
_RETRO_NOTES_LIMIT = 6000
_ERROR_LIMIT = 2000
_MAX_KNOWLEDGE_REFS = 50
_MAX_AGENT_CHANGES = 20
_MAX_AGENT_REF_ITEMS = 20
_AGENT_FIELD_LIMIT = 300
_AGENT_REF_LIMIT = 500

# Rendering order inside a change entry — the uniform shape is the point
# (§5): ``kind`` distinguishes git / knowledge / cloud / sql / file;
# everything else reads the same regardless of destination.
_CHANGE_KEYS = ("datasource", "kind", "action", "ref", "summary", "verified")


# ---------------------------------------------------------------------------
# freeze_data helpers (shared by both writers)
# ---------------------------------------------------------------------------


def _parse_freeze_raw(job: dict[str, Any]) -> Any:
    """``freeze_data`` as the loop writer has always parsed it.

    A dict passes through, a JSONB string is parsed, and an unparseable
    string becomes ``{"notes": <raw>}`` so the raw text still lands in the
    record. May return a non-dict for pathological JSON — callers choose
    their own tolerance (the loop wrapper keeps the historical behaviour).
    """
    freeze = job.get("freeze_data")
    if isinstance(freeze, str):
        try:
            freeze = json.loads(freeze)
        except (json.JSONDecodeError, ValueError):
            freeze = {"notes": freeze}
    return freeze


def _freeze_notes(freeze: Any) -> str:
    """The agent's completion notes, capped at the historical retro limit."""
    notes = (freeze or {}).get("notes") or "(none recorded)"
    if len(notes) > _RETRO_NOTES_LIMIT:
        notes = notes[:_RETRO_NOTES_LIMIT] + "\n\n[truncated]"
    return notes


def _sanitize_role(role: Any) -> str:
    """Path-safe role/config token for the general record's filename."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(role or "")).strip("-.")
    return cleaned or "unknown"


def _clip(value: Any, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def loop_role_iteration(job: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, int]:
    """(role, iteration) exactly as the loop retro writer derives them."""
    role = ctx.get("loop_role") or job.get("config_name") or "unknown"
    try:
        iteration = int(ctx.get("loop_iteration") or 0)
    except (TypeError, ValueError):
        iteration = 0
    return role, iteration


def loop_retro_path(job: dict[str, Any], ctx: dict[str, Any]) -> str:
    """The loop retro's path on ``main`` — ``retros/NNN-<role>-<jobid8>.md``.

    Shared by :func:`write_loop_retro` and the curated merge's PR comment
    (§6.4 names the record in the closed PR), so the named path and the
    written path can never drift.
    """
    role, iteration = loop_role_iteration(job, ctx)
    return f"retros/{iteration:03d}-{role}-{str(job.get('id'))[:8]}.md"


# ---------------------------------------------------------------------------
# The ``changes:`` block (§5)
# ---------------------------------------------------------------------------


def _agent_declared_changes(freeze: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Agent-declared entries from ``freeze_data.changes`` — as claims.

    §5.1: only the agent knows the PR URL it just created, so its entries are
    passed through — but ``verified`` is forced to ``false`` regardless of
    what the agent set. Promoting a claim to a verified fact is the one
    corruption this record format exists to prevent. Non-dict entries are
    dropped; strings are clipped so a runaway freeze cannot bloat ``main``.
    """
    raw = (freeze or {}).get("changes")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw[:_MAX_AGENT_CHANGES]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        for key in ("datasource", "kind", "action", "summary"):
            value = item.get(key)
            if value is not None:
                entry[key] = _clip(value, _AGENT_FIELD_LIMIT)
        ref = item.get("ref")
        if isinstance(ref, list):
            entry["ref"] = [
                _clip(r, _AGENT_REF_LIMIT) for r in ref[:_MAX_AGENT_REF_ITEMS]
            ]
        elif ref is not None:
            entry["ref"] = _clip(ref, _AGENT_REF_LIMIT)
        if not entry:
            continue  # nothing recognisable to record
        entry["verified"] = False  # never silently promoted (§5.1)
        entries.append(entry)
    return entries


def persisted_pull_request(job: dict[str, Any]) -> Any:
    """The pull request ``repo_open_pr`` recorded against this job, if any.

    JSONB-tolerant (asyncpg hands ``context`` back as text) and fails
    closed: anything that is not a complete, well-formed record — including
    agent prose parked under the same key — reads as *no* pull request. A
    guard that accepts a malformed record is worse than no guard, because it
    reports delivery that may never have happened.
    """
    return parse_job_pull_request(job.get("context"))


def job_delivered_nothing(job: dict[str, Any], *, delivery_status: str | None) -> bool:
    """True when an execution turn landed work on no known path.

    Replaces "did ``main`` move?" as the loop's F29 signal. That question
    has the wrong answer by construction under review-based delivery: a job
    that pushes a branch and opens a pull request leaves ``main`` untouched,
    on purpose, and would be scored as having delivered nothing.

    Narrow on purpose. ``no-changes`` is the only status this treats as
    *possibly* empty — it is also the status a source-repository project
    reports legitimately on every code turn, because code never goes to the
    project cloud folder. Everything else already names a real destination.
    """
    if delivery_status != "no-changes":
        return False
    return persisted_pull_request(job) is None


def derive_changes(
    job: dict[str, Any],
    *,
    merge_status: str | None = None,
    merged_sha: str | None = None,
    knowledge_note_ids: list[str] | None = None,
    freeze: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The §5 ``changes:`` entries for one job, in a fixed order.

    1. ``kind: git`` — always present for a repo-backed job: the branch the
       work lived on plus the merge outcome as known by the caller.
       ``merged`` and ``curated`` (§6.4's legacy contracted-deliverables merge)
       are both real merges → ``action: merge``. Current isolated execution
       and cloud-delivery outcomes use ``action: none`` here, with a separate
       verified cloud entry when applicable.
       First-hand orchestrator knowledge → ``verified: true``.
    2. ``kind: pull_request`` — only when ``repo_open_pr`` persisted one
       against this job. This is the delivery signal for a project whose
       code compounds into a source repository: ``main`` has deliberately
       not moved, because the work is on a branch under review.
    3. ``kind: knowledge`` — only when ``knowledge_index`` rows exist for
       this job id; the orchestrator checked its own store → ``verified:
       true``.
    4. Agent-declared entries — see :func:`_agent_declared_changes`
       (``verified: false``, declared order preserved).
    """
    changes: list[dict[str, Any]] = []

    if merge_status == "merged":
        action = "merge"
        summary = "squash-merged to main" + (
            f" ({merged_sha[:8]})" if merged_sha else ""
        )
    elif merge_status == "curated":
        action = "merge"
        summary = "curated merge to main" + (
            f" ({merged_sha[:8]})" if merged_sha else ""
        )
    elif merge_status:
        action = "none"
        summary = f"merge_status: {merge_status}"
    else:
        action = "none"
        summary = "no merge step ran; work remains on the job branch"
    if job.get("repo_name") or job.get("branch_name"):
        changes.append(
            {
                "datasource": job.get("repo_name"),
                "kind": "git",
                "action": action,
                "ref": job.get("branch_name"),
                "summary": summary,
                "verified": True,
            }
        )

    pull_request = persisted_pull_request(job)
    if pull_request is not None:
        changes.append(
            {
                "datasource": pull_request.repo,
                "kind": "pull_request",
                "action": "open",
                "ref": pull_request.url,
                "summary": (
                    f"{pull_request.forge} PR #{pull_request.number}: "
                    f"{pull_request.head} \u2192 {pull_request.base}"
                ),
                "verified": True,
            }
        )

    if merge_status == "cloud-applied":
        changes.append(
            {
                "datasource": "project-cloud",
                "kind": "cloud",
                "action": "apply",
                "ref": "project-folder",
                "summary": "isolated job diff applied to the project cloud folder",
                "verified": True,
            }
        )

    note_ids = list(knowledge_note_ids or [])[:_MAX_KNOWLEDGE_REFS]
    if note_ids:
        changes.append(
            {
                "datasource": "project-kb",
                "kind": "knowledge",
                "action": "upsert",
                "ref": note_ids,
                "summary": f"{len(note_ids)} note(s) in knowledge_index for this job",
                "verified": True,
            }
        )

    changes.extend(_agent_declared_changes(freeze))
    return changes


def _delivery_ref(job: dict[str, Any], delivery_status: str) -> str | None:
    """Human-safe destination pointer for the structured history row."""
    if delivery_status == "cloud-applied":
        return "project-cloud"
    if delivery_status in ("merged", "curated"):
        repo_name = job.get("repo_name")
        return f"{repo_name}@main" if repo_name else "main"
    return job.get("branch_name") or job.get("repo_name")


async def fetch_job_knowledge_note_ids(
    vector_db: Any, job: dict[str, Any]
) -> list[str]:
    """Note ids this job wrote LAST, verified against ``knowledge_index``.

    Reuses the orchestrator's existing DB path for "which notes did job X
    write" — the ``knowledge_index.job_id`` scan the loop notifier and the
    loop-plan existence check already use (orchestrator/main.py). Note that
    the column records the job that *last wrote* the row, not the one that
    authored the note: every canonical write stamps it, so a job that merely
    edited someone else's note now appears here for that note, and the
    original author stops appearing for it. Restoring author provenance is a
    separate, already-filed follow-up. Scoped to
    the job's project when it has one. Best-effort: a down/absent vector
    store returns ``[]`` (KB failures are non-fatal by convention) — the
    record then simply carries no knowledge entry rather than a false claim.
    """
    if vector_db is None:
        return []
    job_id = str(job.get("id"))
    project_id = job.get("project_id")
    try:
        async with vector_db.acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    "SELECT note_id FROM knowledge_index "
                    "WHERE job_id = $1::uuid AND project_id = $2::uuid "
                    "ORDER BY indexed_at ASC LIMIT " + str(_MAX_KNOWLEDGE_REFS),
                    job_id,
                    str(project_id),
                )
            else:
                rows = await conn.fetch(
                    "SELECT note_id FROM knowledge_index "
                    "WHERE job_id = $1::uuid "
                    "ORDER BY indexed_at ASC LIMIT " + str(_MAX_KNOWLEDGE_REFS),
                    job_id,
                )
        return [str(r["note_id"]) for r in rows]
    except Exception:
        logger.debug(
            "job record: knowledge_index scan unavailable (non-fatal)",
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_changes_lines(changes: list[dict[str, Any]]) -> list[str]:
    """The ``changes:`` block as frontmatter lines.

    ``yaml.safe_dump`` owns quoting/escaping — summaries and refs contain
    agent-supplied text, and a stray ``---`` or newline must not be able to
    break out of the frontmatter fence. Key order is normalized to
    ``_CHANGE_KEYS`` so records diff cleanly.
    """
    ordered = [
        {key: entry[key] for key in _CHANGE_KEYS if key in entry} for entry in changes
    ]
    dumped = yaml.safe_dump(
        ordered,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=88,
    )
    return ["changes:"] + [
        f"  {line}" if line else line for line in dumped.rstrip("\n").split("\n")
    ]


def render_job_record(
    *,
    record_type: str,
    role: str,
    job_id: str,
    branch: str | None,
    status: str,
    merge_status: str,
    merge_sha: str | None,
    created: str,
    title: str,
    description: str,
    notes: str,
    iteration: int | None = None,
    project: str | None = None,
    changes: list[dict[str, Any]] | None = None,
    merge_notes: list[str] | None = None,
    error: str | None = None,
) -> str:
    """Render one record file (frontmatter + body).

    One renderer for both record types: ``type: retro`` (loop —
    ``iteration`` set, no ``project``/``changes``) renders byte-identically
    to the pre-extraction loop writer; ``type: job_record`` adds ``project``
    and the §5 ``changes:`` block. Optional fields are omitted entirely, not
    nulled, so existing retro readers see the exact historical shape.

    ``merge_notes`` (§6.4) are ORCHESTRATOR observations from the merge step
    — curated-merge outcomes, fallback warnings, contracted paths missing
    from the branch. They render as their own ``## Merge notes`` section so
    they can never be mistaken for the agent's self-report; empty/absent
    renders nothing.
    """
    lines = ["---", f"type: {record_type}"]
    if iteration is not None:
        lines.append(f"iteration: {iteration}")
    lines.append(f"role: {role}")
    lines.append(f"job: {job_id}")
    if project is not None:
        lines.append(f"project: {project}")
    lines += [
        f"branch: {branch or '~'}",
        f"status: {status}",
        f"merge_status: {merge_status}",
        f"merge_sha: {merge_sha or '~'}",
        f"created: {created}",
    ]
    if changes:
        lines += _render_changes_lines(changes)
    lines += [
        "---",
        "",
        title,
        "",
        description,
        "",
        "## Agent completion notes",
        "",
        notes,
    ]
    if merge_notes:
        lines += ["", "## Merge notes", ""]
        lines += [f"- {note}" for note in merge_notes]
    if error:
        lines += ["", "## Error", "", str(error)[:_ERROR_LIMIT]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Legacy export helper
# ---------------------------------------------------------------------------


async def record_exists_for_job(gitea_client: Any, repo_name: str, job_id: str) -> bool:
    """True if a legacy ``retros/`` tree holds a record for this job.

    This is retained for migration/export tooling only. New records are
    idempotent through ``job_change_records.job_id`` and never inspect Git.
    """
    suffix = f"-{str(job_id)[:8]}.md"
    entries = await gitea_client.list_contents(repo_name, "retros", ref="main")
    if not entries:
        return False
    return any(
        entry.get("type") == "file" and str(entry.get("name", "")).endswith(suffix)
        for entry in entries
    )


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


async def write_loop_retro(
    postgres_db: Any,
    job: dict[str, Any],
    *,
    ctx: dict[str, Any],
    merge_status: str,
    merged_sha: str | None,
    failed: bool = False,
    outcome_kind: str | None = None,
    error: str | None = None,
    merge_notes: list[str] | None = None,
    vector_db: Any = None,
) -> bool:
    """Insert one immutable structured history row for a loop job.

    The legacy function name keeps the migration narrow for callers. This
    function no longer commits a ``retros/`` file or mutates a repository.
    The database primary key makes retries idempotent. Best-effort: an audit
    write must never block the loop state machine.
    """
    if postgres_db is None:
        return False
    try:
        job_id = str(job.get("id"))
        role, iteration = loop_role_iteration(job, ctx)
        freeze = _parse_freeze_raw(job)
        freeze = freeze if isinstance(freeze, dict) else {}
        note_ids = await fetch_job_knowledge_note_ids(vector_db, job)
        delivery_status = merge_status or "none"
        changes = derive_changes(
            job,
            merge_status=delivery_status,
            merged_sha=merged_sha,
            knowledge_note_ids=note_ids,
            freeze=freeze,
        )
        return bool(
            await postgres_db.create_job_change_record(
                job_id=job_id,
                project_id=(str(job["project_id"]) if job.get("project_id") else None),
                loop_id=(str(ctx["loop_id"]) if ctx.get("loop_id") else None),
                record_type="loop_record",
                role=role,
                iteration=iteration,
                status=(
                    "blocked_undelivered"
                    if outcome_kind == "blocked_undelivered"
                    else ("failed" if failed else "completed")
                ),
                repo_name=job.get("repo_name"),
                branch_name=job.get("branch_name"),
                delivery_status=delivery_status,
                delivery_ref=_delivery_ref(job, delivery_status),
                delivery_sha=merged_sha,
                completion_notes=_freeze_notes(freeze),
                delivery_notes=[str(note) for note in (merge_notes or [])],
                changes=changes,
                error=(
                    str(error)[:_ERROR_LIMIT]
                    if (failed or outcome_kind == "blocked_undelivered") and error
                    else None
                ),
            )
        )
    except Exception:
        logger.warning(
            "loop job record write failed for %s (non-fatal)",
            job.get("id"),
            exc_info=True,
        )
        return False


async def write_job_record(
    postgres_db: Any,
    job: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    vector_db: Any = None,
    merge_status: str | None = None,
    merged_sha: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Insert the general per-job change record into PostgreSQL.

    ``now`` is accepted temporarily for source compatibility with legacy
    callers, but PostgreSQL owns ``created_at``. Jobs need not have a Git
    repository: this row is execution history, not an agent-visible file.
    """
    del now
    if postgres_db is None:
        return False
    try:
        job_id = str(job.get("id"))
        if merge_status is None:
            row_status = job.get("merge_status")
            merge_status = str(row_status) if row_status else None
        if merge_status is None:
            diff_status = str(job.get("diff_status") or "")
            if diff_status == "accepted":
                merge_status = "cloud-applied"
            elif diff_status == "rejected":
                merge_status = "cloud-rejected"
            elif job.get("repo_name"):
                merge_status = "isolated"
            else:
                merge_status = "none"

        freeze = _parse_freeze_raw(job)
        freeze = freeze if isinstance(freeze, dict) else {}
        note_ids = await fetch_job_knowledge_note_ids(vector_db, job)
        changes = derive_changes(
            job,
            merge_status=merge_status,
            merged_sha=merged_sha,
            knowledge_note_ids=note_ids,
            freeze=freeze,
        )

        role = _sanitize_role(job.get("config_name"))
        project_id = job.get("project_id")
        return bool(
            await postgres_db.create_job_change_record(
                job_id=job_id,
                project_id=str(project_id) if project_id else None,
                loop_id=None,
                record_type="job_record",
                role=role,
                iteration=None,
                status=status,
                repo_name=job.get("repo_name"),
                branch_name=job.get("branch_name"),
                delivery_status=merge_status,
                delivery_ref=_delivery_ref(job, merge_status),
                delivery_sha=merged_sha,
                completion_notes=_freeze_notes(freeze),
                delivery_notes=[],
                changes=changes,
                error=(
                    str(error)[:_ERROR_LIMIT]
                    if status
                    in {
                        "failed",
                        "cancelled",
                        "blocked_undelivered",
                    }
                    and error
                    else None
                ),
            )
        )
    except Exception:
        logger.warning(
            "job record write failed for %s (non-fatal)",
            job.get("id"),
            exc_info=True,
        )
        return False
