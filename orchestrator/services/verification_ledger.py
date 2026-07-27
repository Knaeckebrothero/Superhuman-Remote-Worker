"""Pure logic for the verification findings ledger.

No I/O. The ledger lives at ``jobs.context.verification_rounds`` on the TARGET
job and is the single source of truth for verification; job status and
``freeze_data`` are projections of it.

Findings are never mutated — the open set is a fold over rounds. A finding is
open unless a later round dispositioned it RESOLVED. DISPUTED records
disagreement without closing, so a fresh critic cannot close a predecessor's
finding by re-judging it.

Design: docs/superpowers/specs/2026-07-27-verification-fail-closed-design.md
Incident: docs/issues/verification_round_reset_spawns_blind_critic.md
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


def fold_open_findings(rounds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compute the currently-open findings by folding all rounds in order.

    Returns copies, each carrying ``opened_round``; a finding left DISPUTED
    additionally carries ``disputed: True`` so the human gate can surface it.
    """
    open_by_id: Dict[str, Dict[str, Any]] = {}

    for rnd in rounds:
        for finding in rnd.get("opened") or []:
            fid = finding.get("id")
            if not fid:
                continue
            entry = dict(finding)
            entry["opened_round"] = rnd.get("round")
            entry["disputed"] = False
            open_by_id[fid] = entry

        for disp in rnd.get("dispositions") or []:
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
    """Next free numeric suffix for a server-assigned finding ID."""
    highest = 0
    for rnd in rounds:
        for finding in rnd.get("opened") or []:
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

    The server may compute STRICTER than the model asserted, never laxer.
    Two independent grounds for ``returned``:

    1. An open BLOCKING finding. This overrides an asserted ``approved`` — the
       rule that makes the original incident (approving over an open
       high-severity finding) impossible. It must never regress.
    2. An asserted ``returned`` while ANYTHING is still open. Without this, a
       critic that deliberately returned a job over medium/low findings had
       its verdict silently rewritten to ``approved`` and the target advanced.

    An asserted ``returned`` with nothing open at all computes ``approved``:
    there is nothing to return on. That call is rejected upstream by
    :func:`validate_verdict_call`, so it is unreachable in practice, but the
    computation stays total rather than relying on that.
    """
    blocking = any(is_blocking(f) for f in open_findings)
    asserted_returned = str(asserted).lower() == "returned"
    if blocking or (asserted_returned and open_findings):
        return "returned"
    return "approved"


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
    toward ``approve_job`` instead. Wrong pressure direction for a gate whose
    whole point is failing closed.
    """
    if str(asserted).lower() == "returned" and not opened and not open_before:
        return [
            "Cannot return a job with no findings: `opened` is empty and no "
            "findings from previous rounds are open. If the deliverable has a "
            "problem, describe it as a finding in `opened`."
        ]
    return []


def render_prior_findings(open_findings: List[Dict[str, Any]]) -> str:
    """Render the open findings block injected into a fresh critic's brief."""
    if not open_findings:
        return (
            "No open findings from previous rounds. This is a first review — "
            "evaluate the deliverables against the original requirements."
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
