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
from datetime import datetime
from typing import Any

# ``write_loop_retro`` moved to ``services.job_records`` (the §6.5
# generalisation of the per-job record — one renderer for loop retros and
# general job records). The ``as`` re-export keeps this module the loop
# path's import surface (main.py, tests) with the loop's path, frontmatter
# and body byte-identical to the pre-move writer.
from services.job_records import write_loop_retro as write_loop_retro

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

# Ceiling on how long a born-parked loop member may wait for a model-cooldown
# reset. A pathological upstream reset_at must not park a loop for a year; an
# early wake self-heals (the member re-hits the remaining cooldown → in-job
# pause if ≤12h, else fail-fast → the next member re-parks on a fresh reset).
# docs/issues/loop_advances_into_active_model_cooldown.md
LOOP_COOLDOWN_PARK_CAP_SECONDS: int = 14 * 24 * 3600


def extract_cooldown_reset_at(
    job: dict[str, Any] | None, result: dict[str, Any] | None
) -> float | None:
    """Absolute epoch reset time if this member failed on a model cooldown, else None.

    Prefers the in-flight completion result's error dict; falls back to the
    row's persisted ``error_details`` (heal/resume re-drives call the advance
    with ``result={}``, and asyncpg hands JSONB back as raw JSON strings).
    """
    for cand in ((result or {}).get("error"), (job or {}).get("error_details")):
        if isinstance(cand, str):
            try:
                cand = json.loads(cand)
            except (ValueError, TypeError):
                continue
        if not isinstance(cand, dict) or cand.get("classification") != "cooldown":
            continue
        try:
            return float(cand["reset_at"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def is_loop_execution_role(role: str | None) -> bool:
    """True if a loop role produces the project artifact (merge must land)."""
    return bool(role) and role not in LOOP_ANALYSIS_ROLES


def job_loop_id(job: dict[str, Any]) -> str | None:
    """Return the project-loop id a job belongs to, or None if it isn't a loop job.

    Reads ``context.loop_id`` (the stamp ``create_loop_job`` writes), tolerating a
    JSON-string context. This is the same signal ``_advance_project_loop`` keys
    off; exposed so the completion handler can ask "is this a loop job?" before
    applying human-in-the-loop gates (e.g. Mode-A diff review) that would wedge an
    unattended loop — its advance hook fires only on a terminal status.
    """
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            return None
    loop_id = ctx.get("loop_id") if isinstance(ctx, dict) else None
    return str(loop_id) if loop_id else None


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


# --- Campaign scheduling (docs/features/loop_campaign_scheduling.md, P0) ---
#
# Guardrail defaults for planner-mode loops. Per-loop overrides live in the
# loop row's `campaign_caps` and are validated against the hard ceilings at
# loop start — the ceilings are the non-negotiable runaway floor.
LOOP_CAMPAIGN_DEFAULT_CAPS: dict[str, int] = {
    "max_stages": 5,  # stages one plan may schedule
    "max_extensions": 2,  # times one campaign may be extended
    "abort_failures": 2,  # consecutive member failures before early abort
}
LOOP_CAMPAIGN_CAPS_CEILING: dict[str, int] = {
    "max_stages": 10,
    "max_extensions": 5,
    "abort_failures": 5,
}
# A plan may never spend the loop's whole remaining budget — room must remain
# for the closing analysis stage + critic checkpoint after the campaign.
LOOP_CAMPAIGN_BUDGET_RESERVE = 2
# Bounded archive of disposed campaigns on the loop row (newest last).
LOOP_CAMPAIGN_HISTORY_LIMIT = 20

_PLAN_OUTCOMES = frozenset({"ship", "extend", "kill"})


def resolve_campaign_caps(loop: dict[str, Any]) -> dict[str, int]:
    """Effective campaign guardrails for a loop: defaults + per-loop overrides.

    Overrides come pre-validated from loop start (``validate_campaign_caps``),
    but re-clamp against the ceilings anyway — the caps gate spawning, so a
    hand-edited row must not be able to exceed the runaway floor.
    """
    caps = dict(LOOP_CAMPAIGN_DEFAULT_CAPS)
    overrides = loop.get("campaign_caps") or {}
    if isinstance(overrides, dict):
        for key in caps:
            val = overrides.get(key)
            if isinstance(val, int) and val >= 1:
                caps[key] = min(val, LOOP_CAMPAIGN_CAPS_CEILING[key])
    return caps


def validate_campaign_caps(overrides: dict[str, Any]) -> dict[str, int]:
    """Validate per-loop cap overrides at loop start; raise ValueError.

    Explicit rejection beats silent clamping at the API boundary: a caller who
    asks for max_stages=50 should learn the ceiling, not silently get 10.
    """
    if not isinstance(overrides, dict):
        raise ValueError("campaign_caps must be an object")
    unknown = set(overrides) - set(LOOP_CAMPAIGN_DEFAULT_CAPS)
    if unknown:
        raise ValueError(
            f"unknown campaign_caps key(s) {sorted(unknown)}; "
            f"allowed: {sorted(LOOP_CAMPAIGN_DEFAULT_CAPS)}"
        )
    out: dict[str, int] = {}
    for key, val in overrides.items():
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"campaign_caps.{key} must be an integer >= 1")
        ceiling = LOOP_CAMPAIGN_CAPS_CEILING[key]
        if val > ceiling:
            raise ValueError(
                f"campaign_caps.{key}={val} exceeds the hard ceiling {ceiling}"
            )
        out[key] = val
    return out


def planner_slots(role_sequence: list[Any]) -> tuple[int, int]:
    """Locate a planner loop's (critic_slot, execution_slot) in the template.

    Planner grammar on top of ``validate_role_sequence``: the critic must
    appear exactly once, as a single-role stage (the checkpoint — a fan-out
    member could never own plan-filing), and the stage after it (cyclically)
    must be single-role — that stage is the execution slot a filed plan
    expands into a campaign. Raises ValueError with the first problem.
    """
    critic_slots = [
        i
        for i, entry in enumerate(role_sequence)
        if normalize_stage(entry) == ["critic"]
    ]
    for i, entry in enumerate(role_sequence):
        roles = normalize_stage(entry)
        if "critic" in roles and len(roles) > 1:
            raise ValueError(
                "campaign scheduling requires the critic to be a single-role "
                f"checkpoint stage, not a fan-out member (stage {i}: {roles})"
            )
    if len(critic_slots) != 1:
        raise ValueError(
            "campaign scheduling requires exactly one 'critic' stage in "
            f"role_sequence (found {len(critic_slots)})"
        )
    if len(role_sequence) < 2:
        raise ValueError(
            "campaign scheduling requires at least one non-critic stage — the "
            "execution slot after the critic checkpoint"
        )
    critic_slot = critic_slots[0]
    execution_slot = (critic_slot + 1) % len(role_sequence)
    if len(normalize_stage(role_sequence[execution_slot])) > 1:
        raise ValueError(
            "campaign scheduling requires the stage after the critic (the "
            "execution slot a plan expands) to be single-role, got fan-out "
            f"stage {role_sequence[execution_slot]!r}"
        )
    return critic_slot, execution_slot


def validate_loop_plan(plan: Any, loop: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize a Critic-filed campaign plan; raise ValueError.

    Shared by the intake endpoint (so the agent gets actionable feedback while
    it can still fix the plan) and the advance path (re-validated at apply time
    — never trust stored input). Roster laxity matches ``role_sequence``: any
    non-empty role string is accepted, the same contract rotation mode has for
    its entries (unknown roles fall to the default role block downstream).

    Returns the normalized plan:
    ``{initiative: {kb_note_id, title}, stages: [{role}...],
    acceptance: [str...], disposition: {outcome, notes} | None}``.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    caps = resolve_campaign_caps(loop)

    campaign = loop.get("campaign") or None
    pending_review = bool(campaign) and campaign.get("status") in ("review", "aborted")

    def _normalize_disposition(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("plan.disposition must be an object")
        outcome = str(raw.get("outcome") or "").strip()
        if outcome not in _PLAN_OUTCOMES:
            raise ValueError(
                f"plan.disposition.outcome must be one of {sorted(_PLAN_OUTCOMES)}"
            )
        if not pending_review:
            raise ValueError(
                "plan.disposition given but no campaign is awaiting review — "
                "omit disposition"
            )
        return {
            "outcome": outcome,
            "notes": str(raw.get("notes") or "").strip()[:2000],
        }

    # Dispose-only filing: close the reviewed campaign without opening a new
    # one. Without this shape, a critic whose verdict is ship/kill with no
    # successor campaign has no legal way to deliver it — the observed failure
    # mode is the verdict landing in a KB note the engine cannot read while
    # the campaign stays parked in review.
    if (
        plan.get("disposition") is not None
        and plan.get("initiative") is None
        and not plan.get("stages")
    ):
        disposition = _normalize_disposition(plan.get("disposition"))
        if disposition["outcome"] == "extend":
            raise ValueError(
                "extend requires stages — an extension continues the same "
                "initiative; include plan.initiative (same kb_note_id) and "
                "plan.stages, or dispose with ship|kill"
            )
        return {
            "initiative": None,
            "stages": [],
            "acceptance": [],
            "disposition": disposition,
        }

    initiative = plan.get("initiative")
    if not isinstance(initiative, dict):
        raise ValueError("plan.initiative must be an object with kb_note_id")
    kb_note_id = str(initiative.get("kb_note_id") or "").strip()
    if not kb_note_id:
        raise ValueError("plan.initiative.kb_note_id is required")
    if len(kb_note_id) > 100:
        raise ValueError("plan.initiative.kb_note_id exceeds 100 chars")
    title = str(initiative.get("title") or "").strip()[:300]

    raw_stages = plan.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValueError("plan.stages must be a non-empty list")
    if len(raw_stages) > caps["max_stages"]:
        raise ValueError(
            f"plan.stages has {len(raw_stages)} stages; this loop's cap is "
            f"{caps['max_stages']}"
        )
    stages: list[dict[str, str]] = []
    for i, entry in enumerate(raw_stages):
        role = entry.get("role") if isinstance(entry, dict) else entry
        role = str(role or "").strip()
        if not role:
            raise ValueError(f"plan.stages[{i}] has no role")
        if len(role) > 100:
            raise ValueError(f"plan.stages[{i}].role exceeds 100 chars")
        stages.append({"role": role})

    remaining = loop.get("remaining_iterations")
    if remaining is not None:
        affordable = int(remaining) - LOOP_CAMPAIGN_BUDGET_RESERVE
        if len(stages) > affordable:
            raise ValueError(
                f"plan costs {len(stages)} iterations but the loop can only "
                f"afford {max(0, affordable)} (remaining {remaining} minus a "
                f"reserve of {LOOP_CAMPAIGN_BUDGET_RESERVE} for the closing "
                "analysis + critic stages) — file a shorter plan"
            )

    raw_acceptance = plan.get("acceptance") or []
    if not isinstance(raw_acceptance, list):
        raise ValueError("plan.acceptance must be a list of strings")
    if len(raw_acceptance) > 10:
        raise ValueError("plan.acceptance is capped at 10 entries")
    acceptance = [str(a).strip()[:2000] for a in raw_acceptance if str(a).strip()]

    raw_disposition = plan.get("disposition")
    disposition: dict[str, Any] | None = None
    if raw_disposition is not None:
        disposition = _normalize_disposition(raw_disposition)
    elif pending_review:
        raise ValueError(
            f"campaign '{campaign.get('title') or campaign.get('id')}' is in "
            f"status {campaign.get('status')} and must be disposed first — "
            "include plan.disposition with outcome ship|extend|kill"
        )

    if disposition and disposition["outcome"] == "extend":
        if int(campaign.get("extensions_used") or 0) >= caps["max_extensions"]:
            raise ValueError(
                "campaign has already used its "
                f"{caps['max_extensions']} extension(s) — outcome must be "
                "ship or kill"
            )
        if kb_note_id != str(campaign.get("initiative_note_id") or ""):
            raise ValueError(
                "extend must continue the same initiative "
                f"({campaign.get('initiative_note_id')!r}); to switch "
                "initiatives, dispose with ship or kill"
            )

    return {
        "initiative": {"kb_note_id": kb_note_id, "title": title},
        "stages": stages,
        "acceptance": acceptance,
        "disposition": disposition,
    }


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
        "research scratch belongs in the KB, not the repo. Default to foraging "
        "widely rather than waiting to be told what to look at — file what you "
        "find as `idea` notes; that is how the backlog grows."
    ),
    "critic": (
        "You are the OVERSEER: select the next ticket to work, judged AGAINST "
        "THE DEFINITION OF DONE — not your own confidence. Your candidate set "
        "is the PROJECT BACKLOG pool shown above (the `feature`/`issue`/`idea` "
        "tickets) — it is given to you, do not go searching for one. Every "
        "ticket type competes on ONE rubric: choosing a fix over a feature is "
        "a first-class outcome — a shipped module no user can reach, a broken "
        "setup, or a missing product surface can outweigh yet another new "
        "backend slice. Judge every ticket on user-visible value, "
        "product-stability risk of ignoring it, leverage of already-shipped "
        "work, implementation size, and evidence quality. Verify any claimed "
        "progress at the "
        "GOAL level, not surface checks (do not approve merely because code "
        "compiles or has no leftover TODOs). Fan independent verification "
        "streams out to subagents (multiple spawn_subagent calls in one turn) "
        "and keep your own context for judging — the verdict stays yours. If "
        "the pool is empty, fall back to self-directed selection from the "
        "PROJECT GOAL — but your pick must always be a real ticket note_id, "
        "so kb_write the action yourself as a `feature`/`issue`/`idea` "
        "ticket FIRST, then select it. "
        "Write a `decision` note tagged "
        "`verdict`: your rationale for the pick and how it will be checked — "
        "this note records WHY you chose it; it is NOT the ticket itself and "
        "never stands in for one (on a campaign-scheduled loop, the "
        "campaign's initiative is the CHOSEN TICKET's own note_id — see "
        "your planner duties below, when this loop runs campaign "
        "scheduling). Leave every non-selected "
        "ticket exactly as it is: do NOT flip it to `superseded` just because "
        "you didn't pick it this turn "
        "— the backlog pool exists so ideas stay queued instead of "
        "evaporating; reserve `superseded` (kb_update, with `superseded_by`) "
        "for a ticket that turns out to be a genuine duplicate of another "
        "one. The loop is "
        "UNCONDITIONAL — it does not stop on 'done'; if the system already meets "
        "the bar in an area, select the next most valuable ticket instead of "
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
        "not a reproduction. First check the KB (and the backlog pool above) for "
        "findings already filed — UPDATE them (kb_update), don't re-file "
        "duplicates — and `retros/` on the repo for what prior iterations "
        "actually landed. Then file 3-7 defects MAXIMUM — high-leverage over a "
        "long bug list — each as its OWN `issue` note (kb_write, type=`issue`) "
        "carrying: severity, confidence, target user/workflow, evidence, "
        "smallest useful remediation, acceptance criteria, and a priority "
        "(high/normal/low — weigh user-visible value, risk of leaving it "
        "unfixed, leverage on already-shipped work) — presented as "
        "evidence, not an argument against Scholar. An issue that exists only "
        "in your report is invisible to the next iteration: the `issue` note "
        "IS the handoff, and it is what puts the defect in the project "
        "backlog pool — a repro script or audit transcript is a fine "
        "attachment, never a substitute. Do NOT fix anything and do NOT "
        "propose new features "
        "— repairing is the Developer's job, new ideas are Scholar's lane. "
        "If the product is genuinely stable and usable, SAY SO "
        "plainly in one summary note — 'no blocking issues found' is a valid "
        "outcome, not a failure to find work. You and Scholar are peers "
        "feeding the same backlog pool; you do not rank against each "
        "other — the Critic reads the pool and decides what to do next. "
        "Your output is `issue` tickets, not repo commits. You work on your "
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


def _planner_critic_block(loop: dict[str, Any], budget_line: str) -> str:
    """Planner duties appended to a checkpoint critic's kickoff.

    docs/features/loop_campaign_scheduling.md (P1). Covers: the loop_plan verb
    + budget arithmetic, the disposition duty when a campaign awaits review,
    why campaigns exist (multi-job investments beat horizon-1 selection), and
    the no-fictional-stakeholders rule the Better Resavio audit earned.
    """
    caps = resolve_campaign_caps(loop)
    campaign = loop.get("campaign") or {}
    lines = [
        "PLANNER DUTIES (this loop runs campaign scheduling):",
        "- You are the loop's PLANNER as well as its judge. After selecting "
        "the next ticket, you MAY file a campaign plan with the "
        f"`loop_plan` tool: 1..{caps['max_stages']} execution stages (each "
        "stage = one job = one iteration of budget) toward ONE initiative — "
        "the SELECTED TICKET's own note_id (from the backlog pool above, or "
        "one you kb_write'd yourself if the pool was empty). Your `decision`/"
        "verdict note is a separate rationale record, never the initiative "
        "itself — plus the acceptance evidence the closing critic will check. "
        "If you "
        "file no plan, the loop falls back to a single follow-up job — but "
        "filing nothing NEVER discharges a pending disposition duty.",
        f"- Budget arithmetic: {budget_line} A k-stage campaign costs k "
        "iterations — spend accordingly.",
        "- A multi-job campaign is how high-value work that cannot be proven "
        "in one job (a UI, an integration, a migration) beats yet another "
        "small provable increment: early stages may be honest scaffolding; "
        "the campaign is judged at its END against the acceptance evidence, "
        "not per-job.",
        "- Constraints must be REAL: the only human stakeholder is the "
        "operator. If something seems to need a human (legal review, budget, "
        "third-party access), file a KB note tagged `user-question` and "
        "proceed on the best assumption — NEVER park or defer work on a "
        "trigger no real person will ever pull.",
    ]
    if campaign.get("status") in ("review", "aborted"):
        acceptance = campaign.get("acceptance") or []
        checks = (
            "\n".join(f"    {c}" for c in acceptance)
            if acceptance
            else "    (none pre-registered — judge against the KB verdict "
            "that selected it)"
        )
        lines.insert(
            1,
            f"- DISPOSITION DUTY FIRST: campaign "
            f"'{campaign.get('title') or campaign.get('initiative_note_id')}' "
            f"({campaign.get('stages_done', '?')} of "
            f"{len(campaign.get('stages') or [])} stages, status "
            f"{campaign.get('status')}) awaits your verdict. Run its "
            f"pre-registered acceptance checks:\n{checks}\n  Then dispose via "
            "loop_plan's disposition fields: ship (evidence passes), extend "
            "(alive but unfinished — your stages continue the SAME "
            "initiative), or kill (dead end — record why in the KB). A plan "
            "without a disposition will be rejected while this campaign is "
            "pending. If you have no new campaign to open, file ONLY the "
            'disposition: `{"disposition": {"outcome": "ship|kill", "notes": '
            '"..."}}` — a KB note is NOT a disposition, and filing nothing '
            "leaves this campaign parked.",
        )
    return "\n".join(lines)


def _campaign_member_block(campaign: dict[str, Any], stage_index: int) -> str:
    """Campaign context appended to a campaign member's kickoff.

    The verification-repricing contract: mid-campaign scaffolding is licensed,
    dishonest retros are not — the campaign is judged at its end against the
    pre-registered evidence, so truthfulness (not per-job provability) is what
    keeps the loop's shared reality intact.
    """
    stages = campaign.get("stages") or []
    acceptance = campaign.get("acceptance") or []
    checks = (
        "\n".join(f"    {c}" for c in acceptance)
        if acceptance
        else "    (none pre-registered)"
    )
    return (
        f"CAMPAIGN CONTEXT: you are stage {stage_index + 1} of {len(stages)} "
        f"of campaign '{campaign.get('title') or campaign.get('initiative_note_id')}' "
        f"(initiative KB note: {campaign.get('initiative_note_id')}).\n"
        "- Prior stages' actual state: check retros/ (newest first) and the "
        "repo — build on what landed, don't restart.\n"
        "- The campaign is judged at its END against this pre-registered "
        f"acceptance evidence:\n{checks}\n"
        "- You do NOT have to reach a fully provable state THIS job — "
        "mid-campaign scaffolding is fine — but your retro MUST be honest "
        "about actual state: never claim working what isn't. The closing "
        "critic runs the checks above; honesty is the contract that keeps "
        "the loop sane."
    )


def build_loop_kickoff(
    loop: dict[str, Any],
    *,
    role: str,
    iteration: int,
    extra_context: dict[str, Any] | None = None,
    backlog_block: str | None = None,
) -> str:
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

    Planner-scheduled loops add a block: the checkpoint critic gets its
    planner duties (loop_plan / disposition / budget arithmetic), a campaign
    member gets its campaign context (stage N of M + the honesty contract).
    ``extra_context`` is the spawn-time context stamp dict — a present
    ``loop_campaign_id`` marks a campaign member.

    ``backlog_block`` is the pre-rendered work pool (see
    ``services/project_backlog.render_backlog_block``). It is injected
    VERBATIM: this function stays pure and does no I/O, so the caller fetches
    it. Passing None (start-up paths, tests) simply omits the section.
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
    ]

    # The work pool, handed over rather than searched for. Placed before the
    # role block so selection duty reads it in order.
    if backlog_block:
        parts.append(backlog_block)
        backlog_note = (
            "The open backlog is listed above — it is given to you, do not go "
            "searching for it."
        )
    else:
        # Fix round 1, Finding 1: backlog_block=None in production means the
        # fetch failed (a KB outage) -- project_id is NOT NULL and vector_db is
        # assigned unconditionally, so this is the only realistic route. Saying
        # "listed above" here would assert a block that isn't there AND forbid
        # the agent from doing anything about the gap -- worse than the old
        # fiction it replaced. Tell the truth instead.
        backlog_note = (
            "The project backlog could not be read this turn (KB unavailable) "
            "— proceed without it rather than hunting for a substitute; note "
            "the gap in your write-back."
        )

    parts += [
        "BEFORE you act: restate the goal in one line, then check the KB for "
        "(a) what's already done and (b) what's been TRIED AND REJECTED (do "
        f"not re-propose it). {backlog_note}",
        f"YOUR ROLE THIS ITERATION — {role.upper()}:\n{role_block}",
        "WHEN DONE: write to the KB what you did, what you learned, and what the "
        "next agent should do. If you closed or abandoned an approach, record it "
        "as tried/rejected so nobody repeats it. File any new work you spotted "
        "but did not do as a `feature`, `issue` or `idea` note (kb_write) — that "
        "is the project backlog, and it is the only place future iterations will "
        "look for it.",
    ]

    # Campaign-scheduled loops (docs/features/loop_campaign_scheduling.md):
    # campaign context for members, planner duties for the checkpoint critic.
    if (loop.get("scheduling") or "standard") == "campaign":
        stamps = extra_context or {}
        if stamps.get("loop_campaign_id") is not None:
            campaign = loop.get("campaign") or {}
            try:
                stage_index = int(stamps.get("loop_campaign_index") or 0)
            except (TypeError, ValueError):
                stage_index = 0
            parts.append(_campaign_member_block(campaign, stage_index))
        elif role == "critic":
            parts.append(_planner_critic_block(loop, budget_line))

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
    disable_memory_assembler: bool = False,
    extra_context: dict[str, Any] | None = None,
    backlog_block: str | None = None,
    park_until: datetime | None = None,
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

    ``disable_memory_assembler`` turns off the RecallStore TTL-curation
    assembler for this job (set by the caller for members of a *fan-out* stage).
    The assembler is the one memory writer that does a read-modify-write over the
    project-scoped shared store (it retires/re-TTLs existing memories); running N
    of them concurrently over one project would race that curation slot. It is
    disabled via the existing ``auxiliary.tasks.assemble_memories.enabled`` flag
    (the same lever ``session_base.yaml`` uses) rather than by editing the
    ``memory.pipeline.writers`` list — a scalar deep-merge that leaves the
    append-only extractors, the KB curator, and the writers list untouched. See
    docs/features/loop_parallel_stages.md and [[project_kb_convergence_f13]].

    ``backlog_block`` is threaded straight through to ``build_loop_kickoff`` —
    this function does no fetching itself, it just carries the caller's
    pre-rendered pool string (or None) into the kickoff message.
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

    # Fan-out members: silence the TTL-curation assembler so N concurrent
    # analysis roles don't race the shared project RecallStore's read-modify-
    # write curation slot (see docstring). Scalar override on the existing aux
    # gate — no list surgery, extractors/curator keep running.
    if disable_memory_assembler:
        config_override["auxiliary"] = {
            "tasks": {"assemble_memories": {"enabled": False}}
        }

    # Split the synthesized prompt the way the manual create-job form does:
    # a concise `description` (the cockpit row title + task_brief "## Description")
    # and the full loop protocol as the kickoff message, carried through the
    # "Opening Message" channel (context["kickoff_message"]). Both land together
    # in the agent's task_brief.md; only the description shows as the job title.
    # Campaign loops: the checkpoint critic gets the campaign-filing tool via
    # an additive category override (tools.loop doesn't exist in any bundled
    # config, so the deep-merge adds it without touching the expert's other
    # tool lists). Campaign members — even critic-flavored sub-critics — and
    # standard loops never get it; the intake endpoint re-gates server-side
    # (defense in depth, mirroring phase-restricted tools).
    is_campaign_member = bool((extra_context or {}).get("loop_campaign_id"))
    if (
        (loop.get("scheduling") or "standard") == "campaign"
        and role == "critic"
        and not is_campaign_member
    ):
        config_override["tools"] = {"loop": ["loop_plan"]}

    description = build_loop_description(loop, role=role, iteration=iteration)
    kickoff = build_loop_kickoff(
        loop,
        role=role,
        iteration=iteration,
        extra_context=extra_context,
        backlog_block=backlog_block,
    )
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

    # Born parked (docs/issues/loop_advances_into_active_model_cooldown.md): the
    # previous turn failed on a model cooldown that outlives the pause budget, so
    # this member is created paused-with-freeze instead of dispatched —
    # freeze_data non-NULL hides it from the dispatcher and the existing
    # llm_outage sweeper wakes it at next_retry_at. context.llm_outage carries NO
    # first_failed_at: evaluate_llm_outage would read creation time as a
    # multi-day elapsed outage and ceiling-fail the member AT WAKE; absent, it
    # defaults to wake-time now → elapsed≈0 → survives (completion.py).
    status = "created"
    freeze_data: dict[str, Any] | None = None
    if park_until is not None:
        park_iso = park_until.isoformat()
        status = "paused"
        freeze_data = {
            "freeze_type": "llm_unavailable",
            "classification": "cooldown",
            "next_retry_at": park_iso,
            "attempt": 0,
            "model": loop.get("model"),
            "origin": "loop_cooldown_park",
            "error_summary": (
                f"Created parked: model '{loop.get('model') or 'pinned model'}' "
                f"in quota cooldown until {park_iso} (inherited from the "
                f"previous loop turn)"
            )[:500],
        }
        context["llm_outage"] = {"attempt": 0, "next_retry_at": park_iso}

    # Campaign member stamps (loop_campaign_id / loop_campaign_index) and any
    # other spawn-time truth the caller needs read back by the advance/heal.
    # Reserved keys above win — extra context can never shadow the loop join
    # key, the counter stamps, or the born-parked llm_outage state.
    if extra_context:
        for key, value in extra_context.items():
            context.setdefault(key, value)

    # Resolve the role NAME to a DB expert_id when DB-backed experts are on, so a
    # custom expert in the rotation pulls its OWN overlay (model, prompts, tools)
    # rather than just a bundled disk config. Mirrors the automations
    # name-resolution path (services/automations.py); falls through to the
    # bundled config_name when nothing matches or the flag is off. A DB winner
    # resolves on worker_base; keeping the role slug in config_name as well would
    # accidentally merge the bundled role and the DB expert into one profile.
    expert_id: str | None = None
    config_name = role
    owner_id = str(loop["owner_id"]) if loop.get("owner_id") else None
    if os.getenv("EXPERTS_DB_ENABLED", "true").lower().strip() in (
        "true",
        "1",
        "yes",
    ):
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
                config_name = "worker_base"
        except Exception as e:
            logger.warning(
                "loop %s: expert resolution for role %s failed: %s", loop_id, role, e
            )

    job = await db.create_job(
        description=description,
        config_name=config_name,
        config_override=config_override,
        context=context,
        user_id=owner_id,
        project_id=project_id,
        priority=5,
        expert_id=expert_id,
        status=status,
        freeze_data=freeze_data,
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


def _contracted_file_deliverables(job: dict[str, Any]) -> list[str]:
    """The job's FILE deliverables (canonical, ``kb:`` entries dropped).

    Same manifest source and normalization as the seal-side gate
    (``services.deliverable_gate``): ``context.required_deliverables``,
    ``repo/`` prefix and ``./`` stripped. ``kb:`` entries are store-backed,
    never files — a contract of only those curates nothing.
    """
    from services.deliverable_gate import (
        KB_DELIVERABLE_PREFIX,
        parse_required_deliverables,
    )

    manifest = parse_required_deliverables(job.get("context"))
    return [p for p in manifest if not p.startswith(KB_DELIVERABLE_PREFIX)]


async def merge_loop_job_contribution(
    gitea_client: Any,
    job: dict[str, Any],
    *,
    ctx: dict[str, Any] | None = None,
) -> tuple[str, str | None, list[str]]:
    """Land a completed loop job's contribution on ``main`` — curated when
    the job carries a file-deliverable contract, full squash-merge otherwise.

    The §6.4 change (docs/features/workspace_and_change_records.md): a job
    with ``required_deliverables`` naming at least one FILE gets a **curated
    merge** — the contracted files are copied from the branch head onto
    ``main`` as ONE commit and *nothing else merges*. The branch becomes a
    genuine scratchpad: stray ``.venv``s and scratch files stop accumulating
    into permanent shared history. The audit PR is still created, then
    closed UNMERGED with a comment naming the retro record and the curated
    commit. Jobs with no contract (or a ``kb:``-only one) take the exact
    ``merge_loop_job_branch`` path — behaviour byte-identical to today.

    Returns ``(merge_status, merged_sha, merge_notes)``. ``merge_status``
    adds ``curated`` to the existing vocabulary (``merged`` / ``empty`` /
    ``merge-failed`` / ``skipped``); for ``curated`` the SHA is the curated
    commit on ``main``. ``merge_notes`` are orchestrator observations for
    the retro's ``## Merge notes`` section: what was curated (and at which
    branch spelling), contracted paths missing from the branch, and any
    fallback warning.

    Fallback rules — never lose work to a curation bug:

    * Mechanical failure mid-curation (unreadable tree/blob, refused commit,
      any unexpected error) → today's full squash-merge, with a warning
      note. Only reachable BEFORE the curated commit exists; afterwards a
      full merge would re-land the whole branch on top of it.
    * NONE of the contracted files on the branch → full merge + warning (an
      empty curation would silently discard whatever work exists; policing
      missing deliverables is the deliverable gate's job, not the merge's).
    * Some missing → curate what exists, list the missing paths.
    """
    files_contract = _contracted_file_deliverables(job)
    if not files_contract:
        status, sha = await merge_loop_job_branch(gitea_client, job)
        return status, sha, []

    # Structural guards — same vocabulary and same order as the full merge.
    repo_name = job.get("repo_name")
    branch = job.get("branch_name")
    if not repo_name or not branch or branch == "main":
        return "skipped", None, []
    compare = await gitea_client.get_compare(repo_name, "main", branch)
    if compare is None:
        return "merge-failed", None, []
    if not compare.get("total_commits"):
        return "empty", None, []

    from services.job_records import loop_retro_path, loop_role_iteration

    job_id = str(job.get("id"))
    role, _ = loop_role_iteration(job, ctx or {})

    async def _fallback(reason: str) -> tuple[str, str | None, list[str]]:
        logger.warning(
            "curated merge for job %s fell back to full squash-merge: %s",
            job_id[:8],
            reason,
        )
        status, sha = await merge_loop_job_branch(gitea_client, job)
        return (
            status,
            sha,
            [f"curated merge FELL BACK to full squash-merge: {reason}"],
        )

    # ------------------------------------------------------------------
    # Curation phase — every failure here falls back to the full merge.
    # ------------------------------------------------------------------
    try:
        branch_tree = await gitea_client.list_tree(repo_name, branch)
        if branch_tree is None:
            return await _fallback(f"branch tree unreadable at {repo_name}@{branch}")
        branch_paths = {
            str(entry.get("path"))
            for entry in branch_tree
            if entry.get("type") == "blob" and entry.get("path")
        }

        # Variant resolution (F14 both-spellings rule, mirroring
        # src/core/deliverables.deliverable_path_variants): the canonical
        # path first, then the ``repo/``-prefixed checkout spelling. The
        # blob is written back to the SAME path it was found at.
        resolved: list[tuple[str, str]] = []  # (canonical, path on branch)
        missing: list[str] = []
        for canonical in files_contract:
            for variant in (canonical, f"repo/{canonical}"):
                if variant in branch_paths:
                    resolved.append((canonical, variant))
                    break
            else:
                missing.append(canonical)

        if not resolved:
            return await _fallback(
                f"none of the {len(files_contract)} contracted file "
                f"deliverable(s) exist on the branch — an empty curation "
                f"would discard whatever work exists"
            )

        main_tree = await gitea_client.list_tree(repo_name, "main")
        if main_tree is None:
            return await _fallback(f"main tree unreadable at {repo_name}")
        main_paths = {
            str(entry.get("path"))
            for entry in main_tree
            if entry.get("type") == "blob" and entry.get("path")
        }

        files: list[dict[str, str]] = []
        for canonical, path in resolved:
            blob = await gitea_client.get_file_bytes(repo_name, path, ref=branch)
            if blob is None:
                return await _fallback(f"blob unreadable at {path}@{branch}")
            files.append(
                {
                    "path": path,
                    "content_b64": base64.b64encode(blob).decode(),
                    # ``create`` on an existing path is a Gitea 422; pick per
                    # file against main's tree.
                    "operation": "update" if path in main_paths else "create",
                }
            )
    except Exception as e:  # noqa: BLE001 — a curation bug must not lose work
        return await _fallback(f"unexpected curation error: {e!r}")

    message = f"curated merge: {role} ({job_id[:8]}) — {len(files)} deliverable(s)"
    try:
        committed = await gitea_client.change_files(
            repo_name, "main", files, message=message
        )
    except Exception as e:  # noqa: BLE001
        return await _fallback(f"curated commit raised: {e!r}")
    if not committed:
        return await _fallback("curated commit refused by change_files")

    # ------------------------------------------------------------------
    # The curated commit is on ``main`` — the point of no return. From here
    # every failure is a note, NEVER a fallback (a full merge now would
    # re-land the whole branch on top of the curated files).
    # ------------------------------------------------------------------
    notes: list[str] = []
    try:
        curated_sha = await gitea_client.get_branch_head_sha(repo_name, "main")
    except Exception:  # noqa: BLE001 — sha is provenance, not load-bearing
        curated_sha = None
    sha8 = (curated_sha or "")[:8] or "?"
    notes.append(
        f"curated merge: {len(resolved)}/{len(files_contract)} contracted "
        f"deliverable(s) copied to main @ {sha8}"
    )
    for canonical, path in resolved:
        notes.append(
            f"merged {canonical}"
            + (f" (branch spelling: {path})" if path != canonical else "")
        )
    for canonical in missing:
        notes.append(
            f"contracted deliverable NOT on the branch (not curated): {canonical}"
        )

    # Audit-PR ceremony: the numbered PR documents the branch's full diff,
    # then closes unmerged — the branch stays behind as the scratchpad's
    # audit log. Strictly best-effort.
    try:
        record_path = loop_retro_path(job, ctx or {})
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
        if pr:
            await gitea_client.comment_on_pr(
                repo_name,
                pr["number"],
                (
                    f"Curated merge (§6.4): {len(resolved)} contracted "
                    f"deliverable(s) landed on `main` as `{curated_sha or '?'}`.\n"
                    f"Record: `{record_path}`\n\n"
                    f"This PR is the branch's audit trail and is closed "
                    f"WITHOUT merging — the branch is a scratchpad; only "
                    f"contracted deliverables merge."
                ),
            )
            closed = await gitea_client.close_pr(repo_name, pr["number"])
            if not closed:
                notes.append(
                    f"audit PR #{pr['number']} could not be closed "
                    f"(left open, unmerged)"
                )
        else:
            notes.append("audit PR could not be created (curated commit landed)")
    except Exception as e:  # noqa: BLE001 — ceremony must not flip the outcome
        notes.append(f"audit PR ceremony failed: {e!r}")

    logger.info(
        "curated merge for job %s: %d/%d deliverable(s) → main (%s)",
        job_id[:8],
        len(resolved),
        len(files_contract),
        sha8,
    )
    return "curated", curated_sha, notes
