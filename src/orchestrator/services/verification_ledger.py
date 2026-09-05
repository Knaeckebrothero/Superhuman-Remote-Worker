"""Pure logic for the verification findings ledger.

No I/O. The ledger lives at ``jobs.context.verification_rounds`` on the TARGET
job and is the single source of truth for verification; job status and
``freeze_data`` are projections of it.

Findings are never mutated — the open set is a fold over rounds. A finding is
open unless a later round dispositioned it RESOLVED. DISPUTED records
disagreement without closing, so a fresh critic cannot close a predecessor's
finding by re-judging it.

Design: knowledge-base/knowledge/superpowers/specs/2026-07-27-verification-fail-closed-design.md
Incident: knowledge-base/knowledge/issues/verification_round_reset_spawns_blind_critic.md
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

SEVERITY_ORDER: Dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# Named rather than inlined so it can become a `verification.blocking_severity`
# config key later. Adding that knob is out of scope for this work.
BLOCKING_SEVERITY = "high"

_FINDING_ID_RE = re.compile(r"^F(\d+)$")


def is_blocking(finding: Dict[str, Any]) -> bool:
    """True when a finding is severe enough to gate approval.

    Fails closed: an unrecognised or missing severity blocks. A gate whose
    unknown case passes is the defect this whole design exists to remove.
    """
    severity = str(finding.get("severity", "")).lower()
    if severity not in SEVERITY_ORDER:
        return True
    return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[BLOCKING_SEVERITY]


def _severity_rank(finding: Dict[str, Any]) -> int:
    """Order findings by severity, with unknown ranked ABOVE known.

    Consistent with :func:`is_blocking`, which treats an unrecognised severity
    as blocking: an unreadable severity must never lose a comparison to a
    readable low one.
    """
    severity = str(finding.get("severity", "")).lower()
    if severity not in SEVERITY_ORDER:
        return max(SEVERITY_ORDER.values()) + 1
    return SEVERITY_ORDER[severity]


def fold_open_findings(rounds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compute the currently-open findings by folding all rounds in order.

    Returns copies, each carrying ``opened_round``; a finding left DISPUTED
    additionally carries ``disputed: True`` so the human gate can surface it.

    Malformed entries (non-dict rounds/findings/dispositions, non-list
    ``opened``/``dispositions``) are SKIPPED rather than raising. ``rounds``
    comes from a jsonb column, so nothing at the type level guarantees its
    shape, and this fold runs inside the /complete handler — whose bare
    ``except Exception: log`` would swallow a TypeError and leave the target
    wedged in 'reviewing' forever. Skipping is also the fail-closed direction:
    an unreadable finding is dropped from the OPEN set only if it was
    unreadable to begin with, and a round that opens nothing readable simply
    contributes nothing.
    """
    open_by_id: Dict[str, Dict[str, Any]] = {}

    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue

        opened = rnd.get("opened")
        for finding in opened if isinstance(opened, list) else []:
            if not isinstance(finding, dict):
                continue
            fid = finding.get("id")
            if not fid:
                continue
            entry = dict(finding)
            entry["opened_round"] = rnd.get("round")
            entry["disputed"] = False

            existing = open_by_id.get(fid)
            if existing is not None:
                # An id can only be open twice if two rounds were computed from
                # the same pre-append read (racing duplicate critics — see
                # has_live_verification_critic, which is what prevents this).
                # A plain overwrite silently DROPS the earlier finding, so a
                # colliding low-severity twin could erase a high-severity one
                # and flip a computed 'returned' into 'approved'. Keep the more
                # severe of the two so blocking-ness can only ever be monotone.
                # (A normal reopen after RESOLVED is not a collision: the id is
                # already gone from this dict by then.)
                if _severity_rank(existing) >= _severity_rank(entry):
                    continue
            open_by_id[fid] = entry

        dispositions = rnd.get("dispositions")
        for disp in dispositions if isinstance(dispositions, list) else []:
            if not isinstance(disp, dict):
                continue
            fid = disp.get("id")
            if fid not in open_by_id:
                continue
            kind = str(disp.get("disposition", "")).upper()
            if kind == "RESOLVED":
                del open_by_id[fid]
            elif kind == "DISPUTED":
                open_by_id[fid]["disputed"] = True
                open_by_id[fid]["dispute_reason"] = disp.get("reason", "")

    return list(open_by_id.values())


def next_finding_index(rounds: List[Dict[str, Any]]) -> int:
    """Next free numeric suffix for a server-assigned finding ID.

    Skips malformed entries for the same reason (and from the same stored
    jsonb) as :func:`fold_open_findings`. Raising here would abort the append
    itself, so the critic's verdict would never become durable at all.
    """
    highest = 0
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        opened = rnd.get("opened")
        for finding in opened if isinstance(opened, list) else []:
            if not isinstance(finding, dict):
                continue
            match = _FINDING_ID_RE.match(str(finding.get("id", "")))
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def assign_ids(
    opened: List[Dict[str, Any]], rounds: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Assign server-owned IDs to newly proposed findings.

    Any model-supplied ``id`` is discarded: the critic proposes claims, the
    server owns the namespace, so a critic cannot renumber or silently drop a
    predecessor's finding.
    """
    index = next_finding_index(rounds)
    out: List[Dict[str, Any]] = []
    for finding in opened:
        entry = dict(finding)
        entry["id"] = f"F{index}"
        severity = str(entry.get("severity", "")).lower()
        entry["severity"] = severity if severity in SEVERITY_ORDER else "high"
        out.append(entry)
        index += 1
    return out


def compute_verdict(asserted: str, open_findings: List[Dict[str, Any]]) -> str:
    """Derive the verdict from the open set plus the critic's own assertion.

    The server may compute STRICTER than the model asserted, NEVER laxer.
    Two independent grounds for ``returned``:

    1. The critic asserted ``returned``. Honoured unconditionally — the open
       set cannot override it, because overriding it downward is precisely the
       laxer-than-asserted move the rule forbids.
    2. An open BLOCKING finding. This overrides an asserted ``approved`` — the
       rule that makes the original incident (approving over an open
       high-severity finding) impossible. It must never regress.

    (1) is unconditional rather than gated on ``open_findings`` being
    non-empty, and that distinction is load-bearing rather than theoretical: a
    round may legally supply no NEW findings (the common round-2 shape, once
    :func:`validate_verdict_call` learned to count prior open ones) while
    dispositioning the last open finding ``RESOLVED``. The open set is then
    empty even though the critic explicitly refused to pass the job, and
    computing ``approved`` there ADVANCES it. That shape used to be a hard
    409, so the relaxation is what made it reachable, and the critic brief now
    actively teaches the empty-``findings`` return.

    A ``returned`` with nothing open anywhere is still rejected at the tool
    boundary by :func:`validate_verdict_call` — the critic is asked to name
    the problem — but if one ever reaches here it resolves ``returned``, which
    is the safe direction.
    """
    if str(asserted).lower() == "returned":
        return "returned"
    return "returned" if any(is_blocking(f) for f in open_findings) else "approved"


_VALID_DISPOSITIONS = {"RESOLVED", "STILL_OPEN", "DISPUTED"}


def validate_dispositions(
    dispositions: List[Dict[str, Any]], open_findings: List[Dict[str, Any]]
) -> List[str]:
    """Validate a critic's dispositions against the currently-open findings.

    Returns human-readable errors (empty list = valid). These are surfaced
    verbatim to the model so it can correct itself, so they must name the
    offending finding ID.

    Disposition is required for BLOCKING findings only — non-blocking findings
    are advisory and would otherwise accumulate across rounds forever.
    """
    errors: List[str] = []
    open_ids = {f["id"] for f in open_findings if f.get("id")}
    blocking_ids = {f["id"] for f in open_findings if f.get("id") and is_blocking(f)}
    seen: set[str] = set()

    for disp in dispositions:
        fid = disp.get("id")
        if fid not in open_ids:
            errors.append(
                f"Unknown finding id {fid!r}: there is no open finding with that id."
            )
            continue
        if fid in seen:
            errors.append(f"{fid}: dispositioned more than once in this call.")
            continue
        seen.add(fid)
        kind = str(disp.get("disposition", "")).upper()
        if kind not in _VALID_DISPOSITIONS:
            errors.append(
                f"{fid}: unknown disposition {disp.get('disposition')!r}. "
                f"Use one of: {', '.join(sorted(_VALID_DISPOSITIONS))}."
            )
        elif kind == "RESOLVED" and not str(disp.get("quote", "")).strip():
            errors.append(
                f"{fid}: RESOLVED requires a `quote` from the NEW deliverable "
                f"showing the finding was addressed."
            )
        elif kind == "DISPUTED" and not str(disp.get("reason", "")).strip():
            errors.append(f"{fid}: DISPUTED requires a `reason`.")

    for fid in sorted(blocking_ids - seen):
        errors.append(
            f"{fid}: no disposition supplied. Every open blocking finding must be "
            f"marked RESOLVED, STILL_OPEN, or DISPUTED."
        )

    return errors


def validate_verdict_call(
    asserted: str,
    opened: List[Dict[str, Any]],
    open_before: List[Dict[str, Any]],
) -> List[str]:
    """Reject internally inconsistent verdict calls at the tool boundary.

    A JSON schema cannot express this: ``{"issues": [], "severity": "high"}`` is
    a structurally valid document, and the live incident recorded it as
    "Issues: 0, Severity: high" without complaint.

    A ``returned`` verdict is only inconsistent when there is nothing to return
    ON: no NEW findings in ``opened`` *and* nothing left open by previous
    rounds. Rejecting on empty ``opened`` alone blocked the most common round-2
    shape — no new problems, but a predecessor's F1 still unaddressed — which
    made ``return_job_with_feedback`` uncallable for that critic and pushed it
    toward ``approve_job_verdict`` instead. Wrong pressure direction for a gate whose
    whole point is failing closed.
    """
    if str(asserted).lower() == "returned" and not opened and not open_before:
        return [
            "Cannot return a job with no findings: `opened` is empty and no "
            "findings from previous rounds are open. If the deliverable has a "
            "problem, describe it as a finding in `opened`."
        ]
    return []


def render_prior_findings(
    open_findings: List[Dict[str, Any]], rounds_completed: int = 0
) -> str:
    """Render the open findings block injected into a fresh critic's brief.

    ``rounds_completed`` distinguishes the two ways the open set can be empty.
    Without it, a round-3 critic whose predecessors resolved everything was
    told "This is a first review." — false, and it discards the one signal
    that two other reviewers already went over this deliverable and found
    nothing left standing.
    """
    if not open_findings:
        if rounds_completed <= 0:
            return (
                "No open findings from previous rounds. This is a first review — "
                "evaluate the deliverables against the original requirements."
            )
        plural = "" if rounds_completed == 1 else "s"
        return (
            f"No open findings: {rounds_completed} previous review round{plural} "
            f"already ran and every finding they opened has since been resolved. "
            f"You are reviewer number {rounds_completed + 1}, not the first — "
            f"evaluate the deliverables against the original requirements, and be "
            f"specific about anything your predecessors missed."
        )

    lines = [
        "The following findings were left OPEN by previous review rounds. "
        "You MUST disposition EVERY one of them by id.",
        "",
        "**You may not close a finding by re-judging it.** A finding closes only "
        "if you can quote text from the CURRENT deliverable that addresses it.",
        "",
    ]
    for finding in sorted(open_findings, key=lambda f: f.get("id", "")):
        flag = " *(you previously disputed this)*" if finding.get("disputed") else ""
        lines.append(
            f"- **{finding.get('id')}** [{finding.get('severity')}, opened round "
            f"{finding.get('opened_round')}]{flag}: {finding.get('claim', '')}"
        )
        evidence = str(finding.get("evidence", "")).strip()
        if evidence:
            lines.append(f"  - Evidence when opened: {evidence}")

    lines += [
        "",
        "For each, supply one disposition:",
        "- `RESOLVED` — include `quote`: the text in the CURRENT deliverable that "
        "addresses it.",
        "- `STILL_OPEN` — not addressed.",
        "- `DISPUTED` — include `reason`. **This does not close the finding**; it "
        "flags it for a human.",
    ]
    return "\n".join(lines)


def escalation_status(is_loop_job: bool) -> str:
    """Terminal status for an escalation (no verdict / cap / no progress).

    Project-loop jobs must NEVER land on ``pending_review``: the loop advance
    hook fires only on terminal statuses, so a parked loop job wedges the whole
    loop. They resolve to ``completed`` with the findings recorded in
    ``error_message`` for the retro instead.
    """
    return "completed" if is_loop_job else "pending_review"
