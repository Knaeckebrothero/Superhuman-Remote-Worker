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


def compute_verdict(open_findings: List[Dict[str, Any]]) -> str:
    """Derive the verdict from the open set. Never trusts a model assertion."""
    return "returned" if any(is_blocking(f) for f in open_findings) else "approved"
