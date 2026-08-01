"""Per-job change records on the project repo's ``main``.

docs/features/workspace_and_change_records.md (§5, §5.1, §6.5): the invariant
is **every repo-backed job leaves exactly one record on ``main``** when it
reaches a terminal status — jobs that merged nothing and failed jobs included
(a failure is information, and the honest-floor culture depends on it being
recorded). ``main`` is the index of everything the project ever did; the
record is what merges, not the work.

Two entry points share one renderer so the schema can never fork:

* :func:`write_loop_retro` — the loop path's writer, moved here verbatim from
  ``services.project_loops`` (which re-exports it, so the loop's import
  surface is unchanged). Path and content are byte-identical to the
  pre-extraction writer: ``retros/NNN-<role>-<jobid8>.md``, ``type: retro``,
  no ``changes`` block. Pinned by tests/test_loop_merge.py.
* :func:`write_job_record` — the general writer for every other repo-backed
  job, called from the completion path via
  ``services.completion.write_job_change_record``. Path
  ``retros/<YYYYMMDD-HHMMSS>-<role>-<jobid8>.md`` — same ``retros/``
  directory the loop prompts already point agents at, with a timestamp
  prefix so the directory stays lexically ordered — ``type: job_record``,
  extended frontmatter carrying the §5 ``changes:`` block.

Who writes what (§5.1) — the split that must survive every edit here:

* The ORCHESTRATOR stamps what it knows first-hand (branch, merge outcome)
  and what it verified cheaply against its own stores (note ids present in
  ``knowledge_index``) — those entries carry ``verified: true``.
* The AGENT's declared entries (``freeze_data.changes``) pass through with
  ``verified: false`` ALWAYS — recorded as claims, never silently promoted.
  v1 does not fetch external URLs to check PR links; a record that cannot
  distinguish "did it" from "said it did" is worth less than no record.

Best-effort throughout: a record failure must never block completion
handling or a loop advance.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import yaml

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
       ``merged`` and ``curated`` (§6.4's contracted-deliverables merge) are
       both real merges → ``action: merge``. When no merge step ran (every
       non-loop job today), ``action: none`` with the branch recorded.
       First-hand orchestrator knowledge → ``verified: true``.
    2. ``kind: knowledge`` — only when ``knowledge_index`` rows exist for
       this job id; the orchestrator checked its own store → ``verified:
       true``.
    3. Agent-declared entries — see :func:`_agent_declared_changes`
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


async def fetch_job_knowledge_note_ids(
    vector_db: Any, job: dict[str, Any]
) -> list[str]:
    """Note ids this job wrote, verified against ``knowledge_index``.

    Reuses the orchestrator's existing DB path for "which notes did job X
    write" — the ``knowledge_index.job_id`` scan the loop notifier and the
    loop-plan existence check already use (orchestrator/main.py). Scoped to
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
# Idempotence guard
# ---------------------------------------------------------------------------


async def record_exists_for_job(gitea_client: Any, repo_name: str, job_id: str) -> bool:
    """True if ``retros/`` on ``main`` already holds a record for this job.

    Matches on the ``-<jobid8>.md`` suffix, so it catches both naming
    schemes (the loop's ``NNN-`` prefix and the general timestamp prefix) —
    this is the belt-and-braces half of the double-write guard: even if the
    caller's loop-context skip ever misfires, an already-written retro
    blocks a second record. Unknown state (no ``retros/`` yet, listing
    failed) reads as "no record" — the write stays best-effort either way.
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
    gitea_client: Any,
    job: dict[str, Any],
    *,
    ctx: dict[str, Any],
    merge_status: str,
    merged_sha: str | None,
    failed: bool = False,
    error: str | None = None,
    merge_notes: list[str] | None = None,
) -> bool:
    """Record a loop job's outcome as ``retros/NNN-<role>-<jobid8>.md`` on ``main``.

    Orchestrator-written AFTER the merge outcome is known, so the retro
    carries mechanical truth (merge status + SHA) next to the agent's own
    ``freeze_data`` completion notes — critics read this instead of trusting
    KB self-reports from destroyed workspaces (F40). Best-effort: failures
    must never block the loop advance.

    Moved verbatim from ``services.project_loops`` (§6.5 extraction); that
    module re-exports it, and path, frontmatter and body are byte-identical
    to the pre-move writer — no ``changes`` block, ``type: retro``, the
    loop's iteration/role fields. Pinned by tests/test_loop_merge.py.
    ``merge_status`` passes through as given — the §6.4 curated merge's
    ``curated`` renders like any other status — and its ``merge_notes``
    (curation outcome, fallback warnings, missing contracted paths) add an
    optional ``## Merge notes`` section; absent, the shape is unchanged.
    """
    repo_name = job.get("repo_name")
    if not repo_name:
        return False

    job_id = str(job.get("id"))
    role, iteration = loop_role_iteration(job, ctx)

    notes = _freeze_notes(_parse_freeze_raw(job))
    description = (job.get("description") or "").splitlines()[0].strip()
    content = render_job_record(
        record_type="retro",
        iteration=iteration,
        role=role,
        job_id=job_id,
        branch=job.get("branch_name"),
        status="failed" if failed else "completed",
        merge_status=merge_status,
        merge_sha=merged_sha,
        created=datetime.now(UTC).isoformat(timespec="seconds"),
        title=f"# Loop iter {iteration} · {role}",
        description=description,
        notes=notes,
        merge_notes=merge_notes,
        error=error if (failed and error) else None,
    )

    path = loop_retro_path(job, ctx)
    return bool(
        await gitea_client.change_files(
            repo_name,
            "main",
            [
                {
                    "path": path,
                    "content_b64": base64.b64encode(content.encode()).decode(),
                }
            ],
            message=f"retro: iter {iteration:03d} {role} ({job_id[:8]}) — {merge_status}",
        )
    )


async def write_job_record(
    gitea_client: Any,
    job: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    vector_db: Any = None,
    merge_status: str | None = None,
    merged_sha: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Write the general per-job change record to ``retros/`` on ``main`` (§6.5).

    ``retros/<YYYYMMDD-HHMMSS>-<role>-<jobid8>.md`` — the same directory the
    loop prompts already point agents at, the timestamp keeping lexical
    ordering. ``role`` is the job's ``config_name`` (non-loop jobs have no
    loop role).

    Idempotent by job id: if ANY record file for this job is already on
    ``main`` (a loop retro included), nothing is written — the belt half of
    the double-write guard, under the caller's loop-context skip. Jobs with
    no project repo return ``False`` silently and safely. ``merge_status`` /
    ``merged_sha`` default to what the job row carries (no merge step runs
    for non-loop jobs today; §6.4's curated merge can pass real values
    later). Best-effort by contract: every failure is logged and swallowed.
    """
    try:
        repo_name = job.get("repo_name")
        if not repo_name:
            return False
        job_id = str(job.get("id"))

        if await record_exists_for_job(gitea_client, repo_name, job_id):
            logger.debug(
                "job record for %s already on main — skipping duplicate",
                job_id[:8],
            )
            return False

        if merge_status is None:
            row_status = job.get("merge_status")
            merge_status = str(row_status) if row_status else None

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
        stamp = now or datetime.now(UTC)
        desc_lines = (job.get("description") or "").splitlines()
        description = desc_lines[0].strip() if desc_lines else ""
        project_id = job.get("project_id")
        content = render_job_record(
            record_type="job_record",
            role=role,
            job_id=job_id,
            project=str(project_id) if project_id else "~",
            branch=job.get("branch_name"),
            status=status,
            merge_status=merge_status or "~",
            merge_sha=merged_sha,
            created=stamp.isoformat(timespec="seconds"),
            title=f"# {role} · {job_id[:8]}",
            description=description,
            notes=_freeze_notes(freeze),
            changes=changes,
            error=error if status == "failed" else None,
        )

        path = f"retros/{stamp.strftime('%Y%m%d-%H%M%S')}-{role}-{job_id[:8]}.md"
        return bool(
            await gitea_client.change_files(
                repo_name,
                "main",
                [
                    {
                        "path": path,
                        "content_b64": base64.b64encode(content.encode()).decode(),
                    }
                ],
                message=f"job record: {role} ({job_id[:8]}) — {status}",
            )
        )
    except Exception:
        logger.warning(
            "job record write failed for %s (non-fatal)",
            job.get("id"),
            exc_info=True,
        )
        return False
