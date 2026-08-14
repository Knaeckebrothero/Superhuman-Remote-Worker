"""Job evidence manifest — declared, bounded disposition material (E4).

officer_supervision_surface §3.3: at completion the orchestrator records a
typed, immutable manifest in ``jobs.context.evidence_manifest`` (JSONB — v1
deliberately mints no new table):

- server-created entries for the completion report and the deliverable-check
  result (deliverable_gate output);
- resolved worker-declared entries: the worker's ``freeze_data.evidence[]``
  declarations (kind, label, media_type, source path) are resolved inside the
  job's own Gitea repo, pinned to the exact completion commit, measured
  (byte_size + sha256), and only then published under an opaque ID.

A raw worker path is never copied into an officer tool call: reads resolve
the opaque ID server-side at the PINNED revision — the model cannot supply a
path, traverse directories, or switch revisions. Binary/screenshot entries
return safe metadata plus the existing job-file viewer representation.

Bounds (Legate-ratified §10 recommendation): 256 KiB paginated text per item,
five images per job, 2 000 diff/change-summary lines; oversize material is
recorded with honest ``availability`` metadata but is not readable — larger
material belongs in a KB report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from services.deliverable_gate import normalize_deliverable_path
from services.kb_git_source import redact_git_error

logger = logging.getLogger(__name__)

CONTEXT_KEY = "evidence_manifest"

#: Allowed kinds v1. completion_report / deliverable_check are SERVER-created;
#: a worker declaring them is rejected (it would forge the server's stamp).
SERVER_KINDS = frozenset({"completion_report", "deliverable_check"})
WORKER_KINDS = frozenset({"test_report", "screenshot", "change_summary"})
ALLOWED_KINDS = SERVER_KINDS | WORKER_KINDS

TEXT_LIMIT_BYTES = 256 * 1024  #: per text item (test_report/change_summary/reports)
MAX_IMAGES_PER_JOB = 5
DIFF_LINE_LIMIT = 2000  #: change_summary entries
BINARY_LIMIT_BYTES = 8 * 1024 * 1024  #: sanity ceiling for screenshots
READ_PAGE_CHARS = 16000  #: pagination window for read_job_evidence
MAX_WORKER_ENTRIES = 20

_TEXT_MEDIA_PREFIXES = ("text/", "application/json", "application/xml")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _opaque_id() -> str:
    return f"ev_{uuid.uuid4().hex[:12]}"


def _is_text_media(media_type: str) -> bool:
    return media_type.startswith(_TEXT_MEDIA_PREFIXES)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_manifest(job: dict[str, Any]) -> dict[str, Any] | None:
    """The recorded manifest from a job row's context, or None."""
    context = job.get("context")
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(context, dict):
        return None
    manifest = context.get(CONTEXT_KEY)
    return manifest if isinstance(manifest, dict) else None


def _safe_repo_path(raw: Any) -> str | None:
    """Canonical repo-relative path, or None when the declaration is unsafe.

    Path-traversal gate: absolute paths, drive letters, backslashes, and any
    ``..`` segment are rejected before Gitea is ever consulted.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > 512:
        return None
    if "\\" in candidate or "\x00" in candidate:
        return None
    if candidate.startswith(("/", "~")) or ":" in candidate.split("/", 1)[0]:
        return None
    normalized = normalize_deliverable_path(candidate)
    if not normalized:
        return None
    if any(segment == ".." for segment in normalized.split("/")):
        return None
    return normalized


async def _resolve_repo_and_head(
    job: dict[str, Any], *, db: Any, gitea: Any
) -> tuple[str | None, str | None, str | None]:
    """(repo_name, ref, pinned head sha) for the job's pushed branch."""
    from services.deliverable_gate import _resolve_repo_ref

    if gitea is None or not getattr(gitea, "is_initialized", False):
        return None, None, None
    repo_name, ref = await _resolve_repo_ref(job, db)
    if not repo_name or not ref:
        return None, None, None
    try:
        sha = await gitea.get_branch_head_sha(repo_name, ref)
    except Exception:  # noqa: BLE001
        sha = None
    return repo_name, ref, sha


def _declared_evidence(result: dict[str, Any], job: dict[str, Any]) -> list[Any]:
    """The worker's evidence[] declaration from the completion payload.

    Preference order: the reported freeze_data (this completion), then the
    persisted job-row freeze_data (resume/replay paths).
    """
    for candidate in (result.get("freeze_data"), job.get("freeze_data")):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                candidate = None
        if isinstance(candidate, dict):
            declared = candidate.get("evidence")
            if isinstance(declared, list) and declared:
                return declared
    return []


def _completion_report_payload(
    result: dict[str, Any], job: dict[str, Any]
) -> dict[str, Any] | None:
    """The worker's completion claim (summary/deliverables/confidence/notes)."""
    for candidate in (result.get("freeze_data"), job.get("freeze_data")):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                candidate = None
        if not isinstance(candidate, dict):
            continue
        if candidate.get("summary") or candidate.get("deliverables"):
            return {
                "summary": candidate.get("summary"),
                "deliverables": candidate.get("deliverables") or [],
                "confidence": candidate.get("confidence"),
                "notes": candidate.get("notes"),
                "reported_at": candidate.get("timestamp"),
            }
    return None


def _inline_entry(
    *,
    kind: str,
    label: str,
    payload: dict[str, Any],
    revision: str | None,
    now: str,
) -> dict[str, Any]:
    content = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    raw = content.encode("utf-8")
    return {
        "id": _opaque_id(),
        "kind": kind,
        "label": label,
        "media_type": "application/json",
        "byte_size": len(raw),
        "sha256": _sha256(raw),
        "source": {"type": "inline", "revision": revision},
        "captured_at": now,
        "producer": "server",
        "availability": "available",
        "inline_content": content,
    }


async def build_evidence_manifest(
    job: dict[str, Any],
    result: dict[str, Any],
    *,
    db: Any,
    gitea: Any,
    gate_stamp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the typed manifest for one completion. Never raises.

    Every entry is measured and pinned before its opaque ID exists; a
    declaration that cannot be resolved is still RECORDED with an honest
    ``availability`` so the officer sees that evidence was claimed but not
    verifiable — silence would hide the gap.
    """
    now = _now_iso()
    job_id = str(job.get("id"))
    repo_name, ref, head_sha = await _resolve_repo_and_head(job, db=db, gitea=gitea)
    entries: list[dict[str, Any]] = []

    # --- server-created: completion report -------------------------------
    report = _completion_report_payload(result, job)
    if report is not None:
        entries.append(
            _inline_entry(
                kind="completion_report",
                label="Worker completion report",
                payload=report,
                revision=head_sha,
                now=now,
            )
        )

    # --- server-created: deliverable-check result ------------------------
    if gate_stamp is None:
        context = job.get("context")
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except (json.JSONDecodeError, ValueError):
                context = {}
        gate_stamp = (
            context.get("deliverable_gate") if isinstance(context, dict) else None
        )
    if isinstance(gate_stamp, dict) and gate_stamp:
        entries.append(
            _inline_entry(
                kind="deliverable_check",
                label="Deliverable-contract gate result",
                payload=gate_stamp,
                revision=gate_stamp.get("commit_sha") or head_sha,
                now=now,
            )
        )

    # --- worker-declared entries ------------------------------------------
    declared = _declared_evidence(result, job)[:MAX_WORKER_ENTRIES]
    image_count = 0
    for declaration in declared:
        if not isinstance(declaration, dict):
            continue
        kind = str(declaration.get("kind") or "").strip()
        label = str(declaration.get("label") or "").strip()[:200] or "(unlabeled)"
        media_type = str(declaration.get("media_type") or "").strip()[:100]
        raw_source = declaration.get("source")
        if isinstance(raw_source, dict):
            raw_source = raw_source.get("path")

        entry: dict[str, Any] = {
            "id": _opaque_id(),
            "kind": kind if kind in ALLOWED_KINDS else "unknown",
            "label": label,
            "media_type": media_type or "application/octet-stream",
            "byte_size": None,
            "sha256": None,
            "source": None,
            "captured_at": now,
            "producer": "worker",
            "availability": "available",
        }

        if kind not in WORKER_KINDS:
            entry["availability"] = "rejected_kind"
            entry["availability_reason"] = (
                f"kind {kind or '(missing)'!r} is not a worker-declarable kind "
                f"(allowed: {', '.join(sorted(WORKER_KINDS))})"
            )
            entries.append(entry)
            continue

        path = _safe_repo_path(raw_source)
        if path is None:
            entry["availability"] = "rejected_path"
            entry["availability_reason"] = (
                "source path is missing, absolute, traversing, or malformed"
            )
            entries.append(entry)
            continue

        if kind == "screenshot":
            image_count += 1
            if image_count > MAX_IMAGES_PER_JOB:
                entry["availability"] = "rejected_image_cap"
                entry["availability_reason"] = (
                    f"more than {MAX_IMAGES_PER_JOB} screenshot entries declared"
                )
                entry["source"] = {"type": "job_repo", "path": path}
                entries.append(entry)
                continue
            if not media_type:
                entry["media_type"] = "image/png"

        if not repo_name or not head_sha:
            entry["availability"] = "unresolved"
            entry["availability_reason"] = (
                "job repository or completion revision unavailable"
            )
            entry["source"] = {"type": "job_repo", "path": path}
            entries.append(entry)
            continue

        content: bytes | None = None
        try:
            content = await gitea.get_file_bytes(repo_name, path, ref=head_sha)
            if content is None:
                # Repository checkouts may place files under repo/ — the same
                # dual spelling the deliverable gate accepts.
                content = await gitea.get_file_bytes(
                    repo_name, f"repo/{path}", ref=head_sha
                )
                if content is not None:
                    path = f"repo/{path}"
        except Exception:  # noqa: BLE001
            logger.warning(
                "evidence: resolve failed for job %s path %r",
                job_id,
                path,
                exc_info=True,
            )
            content = None

        entry["source"] = {
            "type": "job_repo",
            "repo": repo_name,
            "path": path,
            "ref": ref,
            "revision": head_sha,
        }
        if content is None:
            entry["availability"] = "unresolved"
            entry["availability_reason"] = (
                f"declared file not found at the pinned completion revision "
                f"{head_sha[:12]}"
            )
            entries.append(entry)
            continue

        entry["byte_size"] = len(content)
        entry["sha256"] = _sha256(content)

        media = entry["media_type"]
        if kind == "screenshot" or not _is_text_media(media):
            if len(content) > BINARY_LIMIT_BYTES:
                entry["availability"] = "oversize"
                entry["availability_reason"] = (
                    f"{len(content)} B exceeds the {BINARY_LIMIT_BYTES} B binary bound"
                )
        else:
            if len(content) > TEXT_LIMIT_BYTES:
                entry["availability"] = "oversize"
                entry["availability_reason"] = (
                    f"{len(content)} B exceeds the {TEXT_LIMIT_BYTES} B text bound — "
                    "publish a KB report for larger material"
                )
            elif kind == "change_summary":
                line_count = content.count(b"\n") + 1
                if line_count > DIFF_LINE_LIMIT:
                    entry["availability"] = "oversize"
                    entry["availability_reason"] = (
                        f"{line_count} lines exceed the {DIFF_LINE_LIMIT}-line "
                        "change-summary bound"
                    )
        entries.append(entry)

    return {
        "version": 1,
        "recorded_at": now,
        "job_id": job_id,
        "source_revision": head_sha,
        "entries": entries,
    }


def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The manifest with inline payloads stripped (list view)."""
    entries = []
    for entry in manifest.get("entries") or []:
        public = {k: v for k, v in entry.items() if k != "inline_content"}
        entries.append(public)
    return {**manifest, "entries": entries}


def find_entry(manifest: dict[str, Any], evidence_id: str) -> dict[str, Any] | None:
    for entry in manifest.get("entries") or []:
        if entry.get("id") == evidence_id:
            return entry
    return None


def _paginate(text: str, offset: int) -> dict[str, Any]:
    total = len(text)
    offset = max(0, min(offset, total))
    window = text[offset : offset + READ_PAGE_CHARS]
    return {
        "content": window,
        "offset": offset,
        "total_chars": total,
        "truncated": offset + len(window) < total,
    }


async def read_evidence_entry(
    job: dict[str, Any],
    entry: dict[str, Any],
    *,
    offset: int = 0,
    gitea: Any = None,
) -> dict[str, Any]:
    """Resolve one manifest entry for reading. Authorization happens upstream.

    Text entries return a bounded, secret-redacted page; binary/screenshot
    entries return safe metadata plus the job-file viewer pointer. All reads
    resolve at the PINNED revision recorded in the manifest — never a branch
    head — and verify the recorded sha256 before returning content.
    """
    public_entry = {k: v for k, v in entry.items() if k != "inline_content"}
    availability = entry.get("availability")
    if availability not in (None, "available"):
        return {
            "entry": public_entry,
            "note": (
                f"content not readable: availability={availability}"
                + (
                    f" — {entry.get('availability_reason')}"
                    if entry.get("availability_reason")
                    else ""
                )
            ),
        }

    inline = entry.get("inline_content")
    if isinstance(inline, str):
        page = _paginate(redact_git_error(inline), offset)
        return {"entry": public_entry, **page}

    source = entry.get("source") or {}
    repo = source.get("repo")
    path = source.get("path")
    revision = source.get("revision")
    if not (repo and path and revision):
        return {
            "entry": public_entry,
            "note": "content not readable: entry carries no pinned source",
        }
    if gitea is None or not getattr(gitea, "is_initialized", False):
        return {
            "entry": public_entry,
            "note": "content unavailable: Gitea is not reachable right now",
        }
    content = await gitea.get_file_bytes(repo, path, ref=revision)
    if content is None:
        return {
            "entry": public_entry,
            "note": (
                "content unavailable: pinned revision no longer resolvable "
                "(repository pruned or rewritten)"
            ),
        }
    recorded_sha = entry.get("sha256")
    if recorded_sha and _sha256(content) != recorded_sha:
        return {
            "entry": public_entry,
            "note": (
                "content REFUSED: bytes at the pinned revision no longer match "
                "the recorded sha256 — treat this evidence as tampered"
            ),
        }

    media = str(entry.get("media_type") or "")
    if entry.get("kind") == "screenshot" or not _is_text_media(media):
        return {
            "entry": public_entry,
            "view": {"type": "job_repo_file", "path": path, "ref": revision},
        }
    text = content.decode("utf-8", errors="replace")
    page = _paginate(redact_git_error(text), offset)
    return {"entry": public_entry, **page}


__all__ = [
    "ALLOWED_KINDS",
    "BINARY_LIMIT_BYTES",
    "CONTEXT_KEY",
    "DIFF_LINE_LIMIT",
    "MAX_IMAGES_PER_JOB",
    "READ_PAGE_CHARS",
    "SERVER_KINDS",
    "TEXT_LIMIT_BYTES",
    "WORKER_KINDS",
    "build_evidence_manifest",
    "find_entry",
    "parse_manifest",
    "public_manifest",
    "read_evidence_entry",
]
