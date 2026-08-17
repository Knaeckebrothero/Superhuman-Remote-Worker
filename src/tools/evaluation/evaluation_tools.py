"""Evaluation verdict tools for the Universal Agent.

Provides tools for critic/reviewer agents to act on target jobs:
- approve_job_verdict: Record approval verdict for a target job
- return_job_with_feedback: Record feedback verdict for a target job

Journal-before-observe: each tool durably records its round through the
orchestrator (``OrchestratorClient.record_verification_round``, POST
``/api/jobs/{target_job_id}/verification/rounds``) BEFORE it returns to the
model, and the verdict it mirrors into module-level state is the
server-COMPUTED verdict, never the model's assertion. A verdict that cannot
be persisted is reported back to the model as an error, not as success — see
knowledge-base/knowledge/superpowers/plans/2026-07-27-verification-fail-closed.md.

The module-level mirror below is consumed by finalize_job/handle_transition
(src/core/phase.py) after the critic's graph ends; it is a cache of an
already-durable round, not the durability mechanism itself.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict mirror (module-level cache)
# ---------------------------------------------------------------------------
# Populated only after the orchestrator has durably committed the round.
# Consumed by finalize_job in src/core/phase.py after the critic's graph loop
# ends, to build the graph's freeze_data/TransitionResult.
_verdict_data: Dict[str, Dict[str, Any]] = {}


def get_verdict_data(job_id: str) -> Optional[Dict[str, Any]]:
    """Get stored verdict data for a job (used by phase.py).

    Args:
        job_id: The critic job's UUID

    Returns:
        Verdict dict if present, None otherwise
    """
    return _verdict_data.get(job_id)


def clear_verdict_data(job_id: str) -> None:
    """Clear stored verdict data for a job (used by phase.py after processing).

    Args:
        job_id: The critic job's UUID
    """
    if job_id in _verdict_data:
        del _verdict_data[job_id]


# Tool metadata for registry
EVALUATION_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "approve_job_verdict": {
        "module": "evaluation.evaluation_tools",
        "function": "approve_job_verdict",
        "description": "Approve a target job that is pending review",
        "category": "evaluation",
        "short_description": "Approve a pending_review job (transitions to completed).",
        "phases": ["strategic"],
    },
    "return_job_with_feedback": {
        "module": "evaluation.evaluation_tools",
        "function": "return_job_with_feedback",
        "description": "Resume a target job with feedback for the original agent to address",
        "category": "evaluation",
        "short_description": "Return a job to the original agent with issues to fix.",
        "phases": ["strategic"],
    },
}


def create_evaluation_tools(context: ToolContext) -> List[Any]:
    """Create evaluation verdict tools.

    Args:
        context: ToolContext (orchestrator URL from environment)

    Returns:
        List of LangChain tool functions
    """

    async def _submit_verdict(
        asserted: str,
        target_job_id: str,
        narrative: str,
        findings: Optional[List[Dict[str, Any]]],
        dispositions: Optional[List[Dict[str, Any]]],
    ) -> str:
        """Durably record a verdict, then mirror it into the local caches.

        Order matters: nothing is written to ``_verdict_data`` /
        ``_final_phase_data`` until the orchestrator has committed the round.
        """
        from ..core.job import _final_phase_data

        client = getattr(context, "orchestrator_client", None)
        if client is None:
            return (
                "Error: cannot record a verdict — no orchestrator client is "
                "available. The verdict was NOT recorded. Do not proceed as if "
                "the review is complete."
            )

        # Both are only FALLBACKS: the orchestrator prefers the TARGET's own
        # completion freeze, because a critic runs on its own
        # ``subjob/<id>/critic`` branch and its workspace state is a different
        # thing from the target's. Sent anyway so a target whose freeze
        # predates these fields still gets something comparable.
        head_commit = None
        content_tree = None
        try:
            head_commit = context.workspace_manager.get_head_commit()
        except Exception:  # noqa: BLE001 — progress detection is best-effort
            pass
        try:
            content_tree = context.workspace_manager.get_content_tree()
        except Exception:  # noqa: BLE001 — progress detection is best-effort
            pass

        try:
            result = await client.record_verification_round(
                target_job_id=target_job_id,
                critic_job_id=context.job_id,
                asserted_verdict=asserted,
                opened=findings or [],
                dispositions=dispositions or [],
                head_commit=head_commit,
                content_tree=content_tree,
            )
        except Exception as e:  # VerdictRecordingError and anything unexpected
            logger.error(f"Verdict recording failed for {target_job_id}: {e}")
            if getattr(e, "escalated", False):
                # Rejection cap reached — the orchestrator already escalated
                # the target to a human. Telling the model to resubmit here
                # is the livelock; issue a stop order instead.
                return (
                    f"Error: the verdict was NOT recorded and this review has "
                    f"been escalated to a human reviewer. Do NOT resubmit a "
                    f"verdict; wrap up and complete the critic job.\n{e}"
                )
            return (
                f"Error: the verdict was NOT recorded and must be corrected and "
                f"resubmitted.\n{e}"
            )

        # From here on the round IS durably recorded server-side — the only
        # thing that can still go wrong is LOCAL bookkeeping (mirroring it for
        # finalize_job, writing the human-readable report file). That must
        # never raise: an uncaught exception here would hand the model a raw
        # traceback instead of a status string, even though the verdict is
        # already safely persisted (and, per the server's per-critic_job_id
        # dedup guard, does not need — and must not be told — to be
        # resubmitted). The mirror is populated before the best-effort
        # workspace write, so a write_file failure alone can't also cost
        # finalize_job the verdict.
        try:
            verdict = result["verdict"]
            round_num = result["round"]
            open_findings = result["open_findings"]

            _verdict_data[context.job_id] = {
                "_verdict": verdict,
                "_target_job_id": target_job_id,
                "round": round_num,
                "open_findings": open_findings,
            }
            _final_phase_data[context.job_id] = {
                "summary": (
                    f"Verification round {round_num}: {verdict} job {target_job_id}"
                ),
                "deliverables": [f"output/verification_report_round_{round_num}.json"],
                "confidence": 1.0,
                "job_id": context.job_id,
            }

            report_data = {
                "verdict": verdict,
                "asserted_verdict": asserted,
                "target_job_id": target_job_id,
                "narrative": narrative,
                "round": round_num,
                "open_findings": open_findings,
            }
            if context.has_workspace():
                context.workspace_manager.write_file(
                    f"output/verification_report_round_{round_num}.json",
                    json.dumps(report_data, indent=2, ensure_ascii=False),
                )

            open_ids = ", ".join(f["id"] for f in open_findings) or "none"
            divergence = (
                f"\nNOTE: you asserted {asserted!r} but the recorded verdict is "
                f"{verdict!r}, computed from the open findings."
                if verdict != asserted
                else ""
            )
            return (
                f"Verdict recorded (round {round_num}): {verdict.upper()} "
                f"job {target_job_id}.\nOpen findings: {open_ids}.{divergence}\n\n"
                f"Complete your remaining todos to finalize."
            )
        except Exception as e:
            logger.error(
                f"Verdict for {target_job_id} was durably recorded by the "
                f"orchestrator, but local bookkeeping failed afterward: {e}"
            )
            return (
                f"Verdict recorded by the orchestrator for job {target_job_id} "
                f"— this round is durable; do NOT resubmit it. Local "
                f"bookkeeping (workspace report / turn finalization) failed "
                f"afterward and may not be fully reflected in this turn: {e}"
            )

    @tool
    async def approve_job_verdict(
        job_id: str,
        report: str,
        dispositions: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Approve a target job that is pending review.

        Every open blocking finding from previous rounds must be dispositioned.
        A finding closes ONLY with a `quote` from the CURRENT deliverable — you
        cannot close one by judging it not to be a problem. If blocking findings
        remain open, the recorded verdict will be `returned` regardless of this
        call.

        Args:
            job_id: UUID of the target job to approve
            report: Summary of the review findings (2-5 sentences)
            dispositions: [{"id": "F1", "disposition": "RESOLVED", "quote": "..."}]
        """
        return await _submit_verdict("approved", job_id, report, [], dispositions)

    @tool
    async def return_job_with_feedback(
        job_id: str,
        feedback: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        dispositions: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Return a target job to the original agent with feedback.

        `findings` are NEW problems you found this round; the server assigns
        each a stable id. `dispositions` answer findings opened in previous
        rounds. Returning with an empty `findings` list AND no previously-open
        findings is rejected.

        Args:
            job_id: UUID of the target job to return
            feedback: Detailed narrative feedback
            findings: [{"claim": "...", "severity": "high", "evidence": "..."}]
            dispositions: [{"id": "F1", "disposition": "STILL_OPEN"}]
        """
        return await _submit_verdict(
            "returned", job_id, feedback, findings, dispositions
        )

    return [approve_job_verdict, return_job_with_feedback]
