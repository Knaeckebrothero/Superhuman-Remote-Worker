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
import os
from typing import Any

logger = logging.getLogger(__name__)


# Execution roles commit to the project's `main` branch so the artifact
# compounds IN PLACE across iterations; analysis roles coordinate only through
# the KB and run on a throwaway branch that is never merged. The execution slot
# is swappable (developer / default / a future writer), so analysis is the
# closed set and everything else is treated as execution.
# See docs/features/loop_repo_compounding.md.
LOOP_ANALYSIS_ROLES: frozenset[str] = frozenset({"scholar", "critic"})


def is_loop_execution_role(role: str | None) -> bool:
    """True if a loop role produces the project artifact (works on ``main``)."""
    return bool(role) and role not in LOOP_ANALYSIS_ROLES


# Role-specific task blocks. Keyed by expert config name; unknown roles fall to
# _ROLE_BLOCK_DEFAULT so the loop stays domain-agnostic (swap `developer` for a
# `writer` / `default` execution role without code changes). Research-tuned:
# Scholar self-grounds via research, then generates diverse candidates and does
# NOT self-filter; Critic verifies at the GOAL level against the Definition of
# Done (not surface checks) and always selects the next improvement (the loop is
# unconditional — there is no goal-met stop); the executor validates its own work
# before "done".
_ROLE_BLOCKS: dict[str, str] = {
    "scholar": (
        "FIRST ground yourself: you MUST research the target/competitor system "
        "and the domain with your research tools (what it actually does, what "
        "comparable products offer, what users need) and record concrete, named "
        "findings as durable KB notes so later iterations reuse them instead of "
        "re-researching. THEN propose several GENUINELY DISTINCT approaches toward "
        "the goal — not variations on one idea — each anchored in the specifics "
        "you found, not generic boilerplate. Check the KB's tried/rejected record "
        "first so you don't re-propose a dead end. Write each candidate to the KB "
        "as a `plan` note tagged `proposal` (a one-line thesis and why it differs "
        "from the others). Do NOT self-filter — selecting is the Critic's job. "
        "Your output is proposal notes, not repo commits (your working branch is "
        "scratch and is never merged into the project)."
    ),
    "critic": (
        "Select and prioritise among the open proposals AGAINST THE DEFINITION "
        "OF DONE — not your own confidence. Verify any claimed progress at the "
        "GOAL level, not surface checks (do not approve merely because code "
        "compiles or has no leftover TODOs). Write a `decision` note tagged "
        "`verdict`: the single chosen next action, explicit rationale, and how it "
        "will be checked. Then mark every non-selected proposal `superseded` "
        "(ranking is NOT rejection — flip their status) so the tried/rejected "
        "record is real and nobody re-proposes a dead end. The loop is "
        "UNCONDITIONAL — it does not stop on 'done'; if the system already meets "
        "the bar in an area, select the next most valuable improvement instead of "
        "declaring completion. Do NOT modify "
        "the repository — only read, evaluate, and write your verdict to the KB "
        "(your working branch is scratch and is never merged into the project)."
    ),
    "developer": (
        "Implement the Critic's chosen action. VALIDATE YOUR OWN WORK before "
        "declaring done — run it and test it; do not rely on it merely "
        "compiling. You work directly on the project's `main` branch: commit "
        "your work and it is pushed automatically when you finish, becoming the "
        "accumulated project that the next iteration builds on (job-scoped "
        "scratch is kept out of it for you). Record in the KB what you shipped "
        "and any follow-ups."
    ),
}

_ROLE_BLOCK_DEFAULT = (
    "Advance the goal acting as '{role}'. Build on the KB, validate your work "
    "against the Definition of Done before declaring done, and record what you "
    "did and what the next agent should do."
)

# Concise verb phrases for the job *description* (the UI title + task_brief
# "## Description"). The full protocol lives in the kickoff message; this is just
# the scannable one-liner so a glance at the jobs list shows role + iteration +
# goal instead of the identical multi-paragraph preamble.
_ROLE_TASKS: dict[str, str] = {
    "scholar": "research the domain & propose distinct improvements",
    "critic": "select & prioritise the next improvement",
    "developer": "implement the chosen action",
}
_ROLE_TASK_DEFAULT = "advance the goal"


def _goal_snippet(goal: str, limit: int = 80) -> str:
    """First line of the goal, collapsed + truncated for a title."""
    snippet = " ".join(goal.split())  # collapse newlines/runs of whitespace
    return snippet if len(snippet) <= limit else snippet[: limit - 1].rstrip() + "…"


def build_loop_description(loop: dict[str, Any], *, role: str, iteration: int) -> str:
    """Concise job title/task — what the agent accomplishes THIS iteration.

    This becomes ``jobs.description`` (the cockpit row title and the task_brief
    "## Description"). The full loop protocol is delivered separately as the
    kickoff message (``build_loop_kickoff``), so the title stays legible.
    """
    task = _ROLE_TASKS.get(role) or _ROLE_TASK_DEFAULT.format(role=role)
    goal = (loop.get("goal") or "").strip()
    base = f"Loop iter {iteration} · {role.upper()}: {task}"
    return f"{base} — toward: {_goal_snippet(goal)}" if goal else base


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
    """Assemble the loop-aware kickoff *message* for one job.

    Delivered via ``context["kickoff_message"]`` (the "Opening Message" channel),
    NOT as the job description — so the full protocol reaches the agent's
    task_brief "## Kickoff Message" while the cockpit row title stays the concise
    ``build_loop_description`` line.

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
        "and record them in the KB as a `goal` note tagged `definition_of_done` "
        "for later iterations to steer by)"
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
        "DEFINITION OF DONE — the quality bar you STEER TOWARD (the loop keeps "
        f"improving past it; it does not stop when it's 'met'):\n{criteria}",
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
    # lifecycle hooks that would otherwise fight it — a verification critic that
    # resumes the job, and a scholar pre-research that doubles the scholar role.
    # Curation is the exception: it is the inline KB extractor/assembler aux pass
    # (not a competing job rotation), and it is what makes the loop's knowledge
    # compound and converge across cycles, so the loop turns it ON.
    # See docs/features/kb_convergence_ttl_reverification.md.
    config_override: dict[str, Any] = {
        "verification": {"enabled": False},
        "scholar": {"enabled": False},
        "curator": {"enabled": True},
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

    # Per-loop workspace tier override (mirrors `model`). When set, every job
    # the loop spawns boots on this backend — e.g. `vm` gives every role a root
    # VM instead of the default sandbox container. The dispatcher reads
    # config_override.workspace.backend (main._job_needs_vm / _job_needs_sandbox)
    # to decide provisioning; VM sizing falls back to its 8c/16Gi default.
    workspace_backend = loop.get("workspace_backend")
    if workspace_backend:
        config_override["workspace"] = {"backend": workspace_backend}

    # Split the synthesized prompt the way the manual create-job form does:
    # a concise `description` (the cockpit row title + task_brief "## Description")
    # and the full loop protocol as the kickoff message, carried through the
    # "Opening Message" channel (context["kickoff_message"]). Both land together
    # in the agent's task_brief.md; only the description shows as the job title.
    description = build_loop_description(loop, role=role, iteration=iteration)
    kickoff = build_loop_kickoff(loop, role=role, iteration=iteration)
    context = {
        "loop_id": loop_id,
        "loop_role": role,
        "loop_iteration": iteration,
        "kickoff_message": kickoff,
    }

    # Resolve the role NAME to a DB expert_id when DB-backed experts are on, so a
    # custom expert in the rotation pulls its OWN overlay (model, prompts, tools)
    # rather than just a bundled disk config. Mirrors the automations
    # name-resolution path (services/automations.py); falls through to the
    # bundled config_name when nothing matches or the flag is off. The NAME stays
    # in config_name and the UUID only ever goes in expert_id — the guard-safe
    # combo (services/agent_provisioner.py rejects a UUID in config_name).
    expert_id: str | None = None
    owner_id = str(loop["owner_id"]) if loop.get("owner_id") else None
    if os.getenv("EXPERTS_DB_ENABLED", "").lower().strip() in ("true", "1", "yes"):
        from src.core.expert_resolution import pick_expert_by_name

        pids = [project_id] if project_id else []
        try:
            candidates = await db.list_experts_visible(
                user_id=owner_id, project_ids=pids
            )
            matches = [c for c in candidates if c.get("name") == role]
            winner = pick_expert_by_name(matches, owner_id, set(pids))
            if winner:
                expert_id = str(winner["id"])
        except Exception as e:
            logger.warning(
                "loop %s: expert resolution for role %s failed: %s", loop_id, role, e
            )

    job = await db.create_job(
        description=description,
        config_name=role,
        config_override=config_override,
        context=context,
        user_id=owner_id,
        project_id=project_id,
        priority=5,
        expert_id=expert_id,
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
