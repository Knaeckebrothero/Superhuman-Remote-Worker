"""Project self-improvement loop — kickoff assembly + job materialization.

Mirrors ``services/automations.py``: translate a ``project_loops`` control row
into a real job via ``db.create_job()``. The caller — the loop router on start,
and the ``_advance_project_loop`` completion hook on each step — is responsible
for repo provisioning and the dispatch nudge, exactly like the automation
run-now path.

Loop jobs run **bare**: the per-job lifecycle hooks (verification critic,
scholar pre-research, curator) are disabled via ``config_override`` so the
loop's explicit Scholar→Critic→Execution rotation IS the cycle and the
auto-hooks don't fight it. State is shared between iterations through the
project knowledge base (a blackboard), not this row.

Design: docs/features/project_self_improvement_loop.md.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Role-specific task blocks. Keyed by expert config name; unknown roles fall to
# _ROLE_BLOCK_DEFAULT so the loop stays domain-agnostic (swap `developer` for a
# `writer` / `default` execution role without code changes). Research-tuned:
# Scholar generates diverse candidates and does NOT self-filter; Critic verifies
# at the GOAL level against the Definition of Done (not surface checks) and owns
# the goal-met stop signal; the executor validates its own work before "done".
_ROLE_BLOCKS: dict[str, str] = {
    "scholar": (
        "Propose several GENUINELY DISTINCT approaches toward the goal — not "
        "variations on one idea. Check the KB's tried/rejected record first so "
        "you don't re-propose a dead end. Write each candidate to the KB as a "
        "`proposal` note with a one-line thesis and why it differs from the "
        "others. Do NOT self-filter — selecting is the Critic's job."
    ),
    "critic": (
        "Select and prioritise among the open proposals AGAINST THE DEFINITION "
        "OF DONE — not your own confidence. Verify any claimed progress at the "
        "GOAL level, not surface checks (do not approve merely because code "
        "compiles or has no leftover TODOs). Write a `verdict` note: the single "
        "chosen next action, explicit rationale, and how it will be checked. If "
        "the Definition of Done is genuinely and fully met, state that "
        "explicitly and why — that is the goal-met stop signal."
    ),
    "developer": (
        "Implement the Critic's chosen action. VALIDATE YOUR OWN WORK before "
        "declaring done — run it and test it; do not rely on it merely "
        "compiling. Commit your work to the project's attached repository so the "
        "next iteration builds on it. Record in the KB what you shipped and any "
        "follow-ups."
    ),
}

_ROLE_BLOCK_DEFAULT = (
    "Advance the goal acting as '{role}'. Build on the KB, validate your work "
    "against the Definition of Done before declaring done, and record what you "
    "did and what the next agent should do."
)


def _format_budget(remaining: int | None, run_until: Any) -> str:
    """One-line budget summary for the kickoff (termination awareness)."""
    bits: list[str] = []
    if remaining is not None:
        bits.append(f"{remaining} iteration(s) left")
    if run_until is not None:
        until = (
            run_until.isoformat() if hasattr(run_until, "isoformat") else str(run_until)
        )
        bits.append(f"runs until {until}")
    return ("Budget: " + " / ".join(bits) + ".") if bits else "Budget: bounded."


def build_loop_kickoff(loop: dict[str, Any], *, role: str, iteration: int) -> str:
    """Assemble the loop-aware kickoff prompt for one job.

    Part system-preamble, part project goal + Definition of Done, part
    role-specific task, part optional user steering. Research-tuned: anchors
    "done" to external acceptance criteria, makes the agent restate the goal,
    states the remaining budget (termination awareness), and tells it to read
    the tried/rejected record before proposing (repetition guard).
    """
    goal = (loop.get("goal") or "").strip() or (
        "(no explicit goal set — make useful, self-directed progress and record "
        "in the KB what you chose to pursue and why)"
    )
    criteria = (loop.get("acceptance_criteria") or "").strip() or (
        "(no explicit acceptance criteria — infer reasonable ones from the goal "
        "and record them in the KB as a `definition_of_done` note for later "
        "iterations to check against)"
    )
    budget_line = _format_budget(
        loop.get("remaining_iterations"), loop.get("run_until")
    )
    role_block = _ROLE_BLOCKS.get(role) or _ROLE_BLOCK_DEFAULT.format(role=role)
    user_prompt = (loop.get("user_prompt") or "").strip()

    parts = [
        "You are ONE step in a CONTINUOUS, UNATTENDED improvement loop on this "
        "project. Other agents run before and after you. You coordinate ONLY "
        "through the project knowledge base — it is your shared memory with them. "
        "READ IT FIRST; WRITE BACK what matters before you finish.",
        f"PROJECT GOAL:\n{goal}",
        f"DEFINITION OF DONE (what 'finished' actually means):\n{criteria}",
        f"LOOP STATUS: iteration {iteration}. {budget_line} Do NOT try to finish "
        "the whole goal in one job — make ONE solid, verifiable increment and "
        "hand off through the KB.",
        "BEFORE you act: restate the goal in one line, then check the KB for "
        "(a) what's already done, (b) what's been TRIED AND REJECTED (do not "
        "re-propose it), and (c) the current open backlog.",
        f"YOUR ROLE THIS ITERATION — {role.upper()}:\n{role_block}",
        "WHEN DONE: write to the KB what you did, what you learned, and what the "
        "next agent should do. If you closed or abandoned an approach, record it "
        "as tried/rejected so nobody repeats it.",
    ]
    if user_prompt:
        parts.append(f"ADDITIONAL STEERING FROM THE USER:\n{user_prompt}")
    return "\n\n".join(parts)


async def create_loop_job(
    db: Any,
    loop: dict[str, Any],
    *,
    role: str,
    iteration: int,
) -> dict[str, Any]:
    """Materialize ONE bare loop job for the given role + iteration.

    DB-only (job row + context + datasource links), mirroring
    ``create_job_from_automation``. The caller provisions the Gitea repo and
    nudges the dispatcher afterwards.

    Stamps ``context.loop_id`` (the join key for ``list_project_loop_jobs`` and
    the ``_advance_project_loop`` hook) plus the role + iteration. Attaches the
    project's linked datasources explicitly (option A — mirrors the cockpit
    picker) so a repository datasource gives the execution role code continuity
    across iterations; resolution stays explicit-only.
    """
    loop_id = str(loop["id"])
    project_id = str(loop["project_id"]) if loop.get("project_id") else None

    # Bare config: the loop is the orchestration, so disable the per-job
    # lifecycle hooks that would otherwise fight it (a verification critic that
    # resumes the job; a scholar pre-research that doubles the scholar role).
    config_override: dict[str, Any] = {
        "verification": {"enabled": False},
        "scholar": {"enabled": False},
        "curator": {"enabled": False},
        "autonomy": "full",
        # The loop coordinates ONLY through the project knowledge base + shared
        # memory, so a step that loses its embedding-backed stores must pause for
        # re-dispatch rather than run blind (see
        # docs/done/embedding_key_missing_silently_disables_memory_and_kb.md).
        "memory": {"required": True},
    }
    model = loop.get("model")
    if model:
        config_override["llm"] = {"model": model}

    kickoff = build_loop_kickoff(loop, role=role, iteration=iteration)
    context = {
        "loop_id": loop_id,
        "loop_role": role,
        "loop_iteration": iteration,
    }

    job = await db.create_job(
        description=kickoff,
        config_name=role,
        config_override=config_override,
        context=context,
        user_id=str(loop["owner_id"]) if loop.get("owner_id") else None,
        project_id=project_id,
        priority=5,
    )
    job_id = str(job["id"])

    # Option A: attach the project's linked datasources explicitly.
    if project_id:
        try:
            for ds in await db.list_project_datasources(project_id):
                ds_id = ds.get("id")
                if not ds_id:
                    continue
                try:
                    await db.link_datasource_to_job(job_id, str(ds_id))
                except Exception as e:
                    logger.warning(
                        "loop %s: linking datasource %s → job %s failed: %s",
                        loop_id,
                        ds_id,
                        job_id,
                        e,
                    )
        except Exception as e:
            logger.warning(
                "loop %s: listing project datasources failed: %s", loop_id, e
            )

    logger.info(
        "Project loop %s → %s job %s (iteration %s)",
        loop_id,
        role,
        job_id,
        iteration,
    )
    return job
