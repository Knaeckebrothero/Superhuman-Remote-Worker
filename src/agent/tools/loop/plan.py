"""``loop_plan`` — the checkpoint Critic's campaign-filing tool.

Files a structured campaign plan with the orchestrator
(``POST /api/jobs/{job_id}/loop-plan``). The orchestrator validates at call
time — schema, caps, budget reserve, disposition rules — so a malformed plan
comes back as an actionable error message while the Critic can still fix it.
The accepted plan is stored on the job and APPLIED when this job completes:
the loop expands its execution slot into the plan's stage queue instead of
rotating a single job. knowledge-base/knowledge/features/loop_campaign_scheduling.md.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from agent.tools.context import ToolContext

from shared.tool_catalog.definitions import (
    LOOP_PLAN_TOOLS_METADATA as LOOP_PLAN_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)


def create_loop_plan_tools(context: ToolContext) -> List[Any]:
    """Create the loop_plan tool with injected context."""

    @tool
    async def loop_plan(
        initiative_note_id: str = "",
        stages: Optional[List[str]] = None,
        title: str = "",
        acceptance: Optional[List[str]] = None,
        disposition_outcome: Optional[str] = None,
        disposition_notes: Optional[str] = None,
    ) -> str:
        """File this loop's next campaign plan with the orchestrator.

        The plan is validated immediately (you get an actionable error if it
        is rejected — fix and re-file) and applied when this job completes:
        instead of one follow-up job, the loop runs your scheduled stages in
        order, then returns control to the next critic checkpoint, which
        judges the campaign against your pre-registered acceptance evidence.

        Dispose-only filing: to close a campaign awaiting review WITHOUT
        opening a new one, call with ONLY disposition_outcome ("ship" or
        "kill") and disposition_notes — omit initiative_note_id and stages.
        The loop then returns to plain rotation. A KB note is NOT a
        disposition; only this tool delivers the verdict to the engine.

        Args:
            initiative_note_id: KB note id of the chosen initiative (must
                exist in the project knowledge base — kb_write it first if
                your verdict note isn't saved yet). Omit for a dispose-only
                filing.
            stages: Roles to run in order, e.g. ["developer", "developer",
                "product-qa"]. Each entry is one job. Budget: each stage
                costs one loop iteration. Omit for a dispose-only filing.
            title: Short human-readable campaign title (shown in the cockpit
                and notifications).
            acceptance: Commands/checks the CLOSING critic will run to judge
                the campaign (e.g. a pytest invocation, a grep). Max 10.
            disposition_outcome: Verdict on the PREVIOUS campaign if one is
                awaiting review: "ship" (evidence passes, close it),
                "extend" (unfinished but alive — your stages continue the
                SAME initiative), or "kill" (dead end, record why).
            disposition_notes: Short rationale for the disposition.

        Returns:
            Acceptance confirmation with the normalized plan, or the
            validator's rejection reason to act on.
        """
        job_id = context.job_id
        if not job_id:
            return "Error: no job_id available in this session."
        dispose_only = (
            not (initiative_note_id or "").strip()
            and not stages
            and bool(disposition_outcome)
        )
        if not dispose_only and not stages:
            return (
                "Error: stages must list at least one role — or, to close the "
                "reviewed campaign without opening a new one, call with only "
                "disposition_outcome (ship|kill)."
            )

        if dispose_only:
            plan: Dict[str, Any] = {
                "disposition": {
                    "outcome": disposition_outcome.strip().lower(),
                    "notes": (disposition_notes or "").strip(),
                }
            }
        else:
            plan = {
                "initiative": {
                    "kb_note_id": (initiative_note_id or "").strip(),
                    "title": (title or "").strip(),
                },
                "stages": [{"role": str(r).strip()} for r in stages],
                "acceptance": [str(a) for a in (acceptance or [])],
            }
            if disposition_outcome:
                plan["disposition"] = {
                    "outcome": disposition_outcome.strip().lower(),
                    "notes": (disposition_notes or "").strip(),
                }

        orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
        api_url = f"{orchestrator_url}/api/jobs/{job_id}/loop-plan"
        headers: Dict[str, str] = {}
        internal_key = os.getenv("MCP_INTERNAL_KEY", "")
        if internal_key:
            headers["X-Internal-Key"] = internal_key

        try:
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                resp = await client.post(api_url, json={"plan": plan})
        except httpx.RequestError as e:
            logger.warning("loop_plan: orchestrator unreachable: %s", e)
            return (
                f"Error: could not reach the orchestrator ({e}). The plan was "
                "NOT filed — retry, or record your selection as a KB verdict "
                "note so the loop can fall back to standard scheduling."
            )

        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            return (
                f"Plan REJECTED (HTTP {resp.status_code}): {str(detail)[:800]}\n"
                "Fix the plan and call loop_plan again — nothing was stored."
            )

        data = resp.json()
        accepted = data.get("plan", plan)
        disposition = accepted.get("disposition")
        if not accepted.get("initiative"):
            return (
                "Dispose-only plan ACCEPTED: the reviewed campaign will be "
                f"disposed ({(disposition or {}).get('outcome', '?')}) when "
                "this job completes, and the loop returns to plain rotation. "
                "Re-filing loop_plan replaces this filing."
            )
        n = len(accepted.get("stages", []))
        roles = ", ".join(s.get("role", "?") for s in accepted.get("stages", []))
        lines = [
            f"Plan ACCEPTED: {n}-stage campaign ({roles}) toward "
            f"'{accepted.get('initiative', {}).get('kb_note_id', '?')}'.",
            "It will be applied when this job completes — finish your verdict "
            "note and complete normally. Re-filing loop_plan replaces this plan.",
        ]
        if disposition:
            lines.append(f"Previous campaign disposed: {disposition.get('outcome')}.")
        return "\n".join(lines)

    return [loop_plan]
