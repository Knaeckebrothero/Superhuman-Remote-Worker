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

import base64
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# Every loop role works on its own `job/<id>` branch; on completion the
# orchestrator squash-merges its net contribution onto `main` (one commit per
# job, branch kept as the audit log). Execution roles are the ones whose merge
# is EXPECTED to be non-empty — an `empty` merge from them is the F29-family
# lost-work signal; analysis roles coordinate through the KB, so `empty` is
# normal for them (until the KB is file-based). The execution slot is swappable
# (developer / default / a future writer), so analysis is the closed set and
# everything else is treated as execution.
# See docs/features/loop_repo_compounding_v2.md.
#
# product-qa audits the SHIPPED product (missing UI, broken setup, integration
# gaps) and files issue candidates as KB notes — it never touches `repo/`, so an
# `empty` merge is normal, not lost work. It coordinates through the KB exactly
# like scholar/critic. Wiring per docs/features/loop_parallel_stages.md (Phase 0).
LOOP_ANALYSIS_ROLES: frozenset[str] = frozenset({"scholar", "critic", "product-qa"})


def is_loop_execution_role(role: str | None) -> bool:
    """True if a loop role produces the project artifact (merge must land)."""
    return bool(role) and role not in LOOP_ANALYSIS_ROLES


def normalize_stage(entry: Any) -> list[str]:
    """Normalize one ``role_sequence`` entry to its list of concurrent roles.

    The loop grammar (docs/features/loop_parallel_stages.md, Phase 1) lets an
    entry be either a single role name (``"scholar"`` — a one-job stage) or a
    list of role names (``["scholar", "product-qa"]`` — a fan-out stage whose
    jobs run concurrently and barrier before the loop rotates). This collapses
    both to a list so the spawn/advance code has one shape to handle. Raises
    ValueError on a malformed entry so bad grammar fails at the boundary.
    """
    if isinstance(entry, str):
        role = entry.strip()
        if not role:
            raise ValueError("role_sequence entry is an empty string")
        return [role]
    if isinstance(entry, (list, tuple)):
        roles = [r.strip() for r in entry if isinstance(r, str) and r.strip()]
        if not roles:
            raise ValueError(f"role_sequence stage {entry!r} has no valid roles")
        # De-dupe within a stage (a role twice in one fan-out = one job) while
        # preserving order — two jobs of the same role would collide on branch
        # name and race the same KB slot.
        seen: set[str] = set()
        deduped = [r for r in roles if not (r in seen or seen.add(r))]
        return deduped
    raise ValueError(f"role_sequence entry must be a string or list, got {type(entry)}")


def validate_role_sequence(role_sequence: list[Any]) -> None:
    """Validate loop grammar; raise ValueError on the first problem.

    Enforces: non-empty sequence; every entry normalizes to ≥1 role; a fan-out
    (multi-role) stage contains ONLY analysis roles. The last rule keeps the
    single-writer-per-artifact invariant — two execution roles committing to
    ``main`` concurrently would race the squash-merge — and matches the design:
    parallel stages are additive *producers* (scholar ∥ product-qa) feeding a
    single downstream consumer, never concurrent executors.
    """
    if not role_sequence:
        raise ValueError("role_sequence must be non-empty")
    for entry in role_sequence:
        roles = normalize_stage(entry)
        if len(roles) > 1:
            execution = [r for r in roles if is_loop_execution_role(r)]
            if execution:
                raise ValueError(
                    "parallel role_sequence stages may contain analysis roles "
                    f"only (got execution role(s) {execution} in stage {roles}); "
                    "a fan-out stage is additive producers feeding one consumer, "
                    "not concurrent executors racing the merge."
                )


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
        "re-researching. Fan independent research threads out to subagents "
        "(multiple spawn_subagent calls in one turn) instead of reading every "
        "source yourself — keep your own context for synthesis. THEN propose "
        "several GENUINELY DISTINCT approaches toward "
        "the goal — not variations on one idea — each anchored in the specifics "
        "you found, not generic boilerplate. Check the KB's tried/rejected record "
        "first so you don't re-propose a dead end. Write each candidate to the KB "
        "as a `plan` note tagged `proposal` (a one-line thesis and why it differs "
        "from the others). Do NOT self-filter — selecting is the Critic's job. "
        "Your output is proposal notes, not repo commits. You work on your own "
        "branch; anything you deliberately leave in the working tree is "
        "squash-merged into the project when you finish, so keep it clean — "
        "research scratch belongs in the KB, not the repo."
    ),
    "critic": (
        "Select and prioritise among ALL open work candidates AGAINST THE "
        "DEFINITION OF DONE — not your own confidence. Two kinds compete on ONE "
        "rubric: Scholar's new-feature `proposal` notes (build something new) and "
        "Product-QA's `qa-finding` issue notes (fix or integrate what exists). "
        "Choosing a fix over a feature is a first-class outcome — a shipped module "
        "no user can reach, a broken setup, or a missing product surface can "
        "outweigh yet another new backend slice. Judge every candidate on "
        "user-visible value, product-stability risk of ignoring it, leverage of "
        "already-shipped work, implementation size, and evidence quality. Verify "
        "any claimed progress at the "
        "GOAL level, not surface checks (do not approve merely because code "
        "compiles or has no leftover TODOs). Fan independent verification "
        "streams out to subagents (multiple spawn_subagent calls in one turn) "
        "and keep your own context for judging — the verdict stays yours. "
        "Write a `decision` note tagged "
        "`verdict`: the single chosen next action, explicit rationale, and how it "
        "will be checked. Then mark every non-selected candidate of BOTH kinds "
        "`superseded` "
        "(ranking is NOT rejection — flip their status) so the tried/rejected "
        "record is real and nobody re-proposes a dead end. The loop is "
        "UNCONDITIONAL — it does not stop on 'done'; if the system already meets "
        "the bar in an area, select the next most valuable improvement instead of "
        "declaring completion. Do NOT modify "
        "the repository — only read, evaluate, and write your verdict to the KB. "
        "You work on your own branch; anything you deliberately leave in the "
        "working tree is squash-merged into the project when you finish, so "
        "leave it untouched. Check `retros/` on the repo for the mechanical "
        "record of what previous jobs actually landed (merge status per "
        "iteration) — trust it over KB self-reports."
    ),
    "developer": (
        "Implement the Critic's chosen action. VALIDATE YOUR OWN WORK before "
        "declaring done — run it and test it; do not rely on it merely "
        "compiling. Fan the UNDERSTANDING out, not the writing: use subagents "
        "(multiple spawn_subagent calls in one turn) to explore unfamiliar code "
        "areas and look things up (docs, APIs, errors) in parallel, but write "
        "every production change yourself — subagent-driven coding fragments the "
        "one thing that needs a coherent head. You work on your own branch of "
        "the project repository; "
        "your commits push there automatically, and when you finish the "
        "orchestrator squash-merges your net contribution onto `main` — that "
        "merge IS the project the next iteration builds on (job-scoped scratch "
        "is kept out of it for you; partial work is safe on your branch). "
        "Record in the KB what you shipped and any follow-ups."
    ),
    "product-qa": (
        "Audit the CURRENT application as a product a real user must operate — "
        "not as a codebase. You are Scholar's counterpart: Scholar looks OUTWARD "
        "for new opportunities; you look INWARD for what is broken, missing, or "
        "unusable in what already exists. Exercise the product: run setup from a "
        "fresh checkout, launch any UI/CLI/API/demo path, check that shipped "
        "modules are reachable in one workflow, look for regressions, setup "
        "failures, integration gaps, and documentation holes. A tested backend "
        "module that no user can reach IS a product gap — missing product "
        "surfaces (no UI, no demo path, no persistence) are legitimate HIGH "
        "findings even when every unit test passes; the evidence for an absence "
        "is the audit trail (paths searched, commands run, expected-vs-observed), "
        "not a reproduction. First check the KB for findings already filed (UPDATE "
        "them, don't re-file duplicates) and `retros/` on the repo for what prior "
        "iterations actually landed. Then file 3-7 issue candidates MAXIMUM — "
        "high-leverage over a long bug list — each as a `plan` note tagged "
        "`qa-finding` carrying: severity, confidence, target user/workflow, "
        "evidence, smallest useful remediation, acceptance criteria, and an "
        "explicit Critic-selection argument (why this should or should not beat a "
        "new Scholar feature). Do NOT fix anything and do NOT propose new features "
        "— repairing is the Developer's job, new ideas are Scholar's lane; writing "
        "a repro script or audit transcript is fine but it is not the handoff, the "
        "KB notes are. If the product is genuinely stable and usable, SAY SO in "
        "one summary note and recommend the Critic select a Scholar proposal — "
        "being fair to Scholar is part of the role, not a failure to find work. "
        "Your output is issue-candidate notes, not repo commits. You work on your "
        "own branch; an empty merge is expected — your findings live in the KB."
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
    "product-qa": "audit the current product & file issue candidates",
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
    seq_index: int | None = None,
    remaining_iterations: int | None = None,
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

    When ``seq_index`` is given (always, when spawned through a stage), the
    stage index and post-advance ``remaining_iterations`` are ALSO stamped into
    context (``loop_seq_index`` / ``loop_remaining``). These are spawn-time
    truth the torn-advance sweeper reads back directly to reconstruct the loop's
    counters — robust to variable-width parallel stages, where the old
    ``(iteration-1) % len(roles)`` modulo no longer maps job-count to stage.
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
    # Spawn-time counter stamps for the torn-advance heal (see docstring). Gated
    # on seq_index so legacy single-job callers stay stamp-free and fall through
    # to the sweeper's modulo derivation; loop_remaining is stamped alongside
    # (None is authoritative — a deadline-only loop genuinely has no budget).
    if seq_index is not None:
        context["loop_seq_index"] = int(seq_index)
        context["loop_remaining"] = remaining_iterations

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


async def merge_loop_job_branch(
    gitea_client: Any, job: dict[str, Any]
) -> tuple[str, str | None]:
    """Squash-merge a completed loop job's branch into ``main``.

    One clean commit per job lands on ``main``; the ``job/<id>`` branch is
    KEPT as the per-iteration audit log. The returned status is literal —
    the loop's artifact-integrity signal (replaces the v1 SHA-compare no-op
    guard, which inferred what this step *knows*):

    - ``merged``       — squash commit created; second element is its SHA.
    - ``empty``        — branch has no commits vs ``main``: the job landed
                         nothing (F29-family signal for execution roles;
                         expected for analysis roles while the KB is a DB).
    - ``merge-failed`` — compare/PR/merge errored. Sequential loops are
                         conflict-free by construction, so this is loud-flag
                         territory, never silent.
    - ``skipped``      — nothing to merge structurally: no repo, or a v1-era
                         job that worked directly on ``main`` (its push
                         already landed) completing after the v2 deploy.

    Design: docs/features/loop_repo_compounding_v2.md.
    """
    repo_name = job.get("repo_name")
    branch = job.get("branch_name")
    if not repo_name or not branch or branch == "main":
        return "skipped", None

    compare = await gitea_client.get_compare(repo_name, "main", branch)
    if compare is None:
        return "merge-failed", None
    if not compare.get("total_commits"):
        return "empty", None

    job_id = str(job.get("id"))
    title = (job.get("description") or "").splitlines()[0].strip()[:200] or (
        f"Loop job {job_id[:8]}"
    )
    pr = await gitea_client.create_pr(
        repo_name,
        title=title,
        head=branch,
        base="main",
        body=f"job: {job_id}\nbranch: {branch}",
    )
    if not pr:
        return "merge-failed", None
    merged = await gitea_client.merge_pr(
        repo_name,
        pr["number"],
        merge_strategy="squash",
        # The branch is the audit log — never deleted on merge.
        delete_branch_after_merge=False,
    )
    if not merged:
        return "merge-failed", None
    sha = await gitea_client.get_branch_head_sha(repo_name, "main")
    return "merged", sha


_RETRO_NOTES_LIMIT = 6000


async def write_loop_retro(
    gitea_client: Any,
    job: dict[str, Any],
    *,
    ctx: dict[str, Any],
    merge_status: str,
    merged_sha: str | None,
    failed: bool = False,
    error: str | None = None,
) -> bool:
    """Record a loop job's outcome as ``retros/NNN-<role>-<jobid8>.md`` on ``main``.

    Orchestrator-written AFTER the merge outcome is known, so the retro
    carries mechanical truth (merge status + SHA) next to the agent's own
    ``freeze_data`` completion notes — critics read this instead of trusting
    KB self-reports from destroyed workspaces (F40). Best-effort: failures
    must never block the loop advance.
    """
    repo_name = job.get("repo_name")
    if not repo_name:
        return False

    job_id = str(job.get("id"))
    role = ctx.get("loop_role") or job.get("config_name") or "unknown"
    try:
        iteration = int(ctx.get("loop_iteration") or 0)
    except (TypeError, ValueError):
        iteration = 0

    freeze = job.get("freeze_data")
    if isinstance(freeze, str):
        try:
            freeze = json.loads(freeze)
        except (json.JSONDecodeError, ValueError):
            freeze = {"notes": freeze}
    notes = (freeze or {}).get("notes") or "(none recorded)"
    if len(notes) > _RETRO_NOTES_LIMIT:
        notes = notes[:_RETRO_NOTES_LIMIT] + "\n\n[truncated]"

    description = (job.get("description") or "").splitlines()[0].strip()
    lines = [
        "---",
        "type: retro",
        f"iteration: {iteration}",
        f"role: {role}",
        f"job: {job_id}",
        f"branch: {job.get('branch_name') or '~'}",
        f"status: {'failed' if failed else 'completed'}",
        f"merge_status: {merge_status}",
        f"merge_sha: {merged_sha or '~'}",
        f"created: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "---",
        "",
        f"# Loop iter {iteration} · {role}",
        "",
        description,
        "",
        "## Agent completion notes",
        "",
        notes,
    ]
    if failed and error:
        lines += ["", "## Error", "", str(error)[:2000]]
    content = "\n".join(lines) + "\n"

    path = f"retros/{iteration:03d}-{role}-{job_id[:8]}.md"
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
