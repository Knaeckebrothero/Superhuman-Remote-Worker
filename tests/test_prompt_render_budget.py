"""Security audit 2026-08-27 follow-up: the render sandbox is bounded, not just
escape-proof.

``ce07bd4e9`` put every prompt/instruction render behind
``ImmutableSandboxedEnvironment``, which blocks code execution. It does not
block *allocation*: Jinja caps ``range()`` at ``MAX_RANGE`` but string
repetition is unbounded, so

    {% set a = 'x' * 100000 %}{% set b = a * 100000 %}{{ b|length }}

asks for ~10 GB in a few milliseconds — an uncaught ``MemoryError`` where an
address-space limit exists, an OOM kill of the agent pod where it does not.
``render_instruction_content`` caught only ``SecurityError``, so a bomb escaped
as an uncaught exception either way.

Reachability: the five gated prompt keys (persona/instructions/strategic/
tactical/summarization) are brace-fenced before they render, but **bound skill
bodies and instruction files are Jinja-rendered with no fence**
(``src/agent.py`` ``_deploy_instruction_files``, ``src/api/persistent_session.py``).

The fix is a render budget at the single render site: amplifying operators are
intercepted and refused *before* they allocate, the emitted output is capped,
the render has a wall-clock deadline, and ``MemoryError`` / ``RecursionError`` /
``OverflowError`` are converted into the same fail-closed refusal. Every bomb
below is executed in a child process under ``RLIMIT_AS`` so a regression cannot
take out the machine running the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.core.loader import (
    MAX_RENDERED_PROMPT_CHARS,
    PROMPT_RENDER_TIME_BUDGET_SECONDS,
    PromptRenderBudgetError,
    PromptRenderSecurityError,
    load_agent_config_from_dict,
    render_instruction_content,
    serialize_resolved_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Headroom the bomb child allows itself ON TOP of the address space it already
# holds after importing the agent tree. A render that respects the 1 MiB output
# cap needs a rounding error of this; a regression dies as a MemoryError in a
# throwaway process instead of swapping the developer's machine to death.
# Relative, not absolute: the import's own VA footprint varies by interpreter
# and allocator, and an absolute ceiling below it fails every case instantly.
_AS_HEADROOM_BYTES = 512 * 1024**2

# Every bomb shape from the finding. ``origin`` is asserted in the refusal, so
# the case name doubles as the attribution label.
_BOMBS = {
    # The confirmed finding: two bounded repetitions multiply into ~10 GB.
    "multiplication": "{% set a = 'x' * 100000 %}{% set b = a * 100000 %}{{ b|length }}",
    # A single oversized repetition, emitted.
    "repetition": "{{ 'a' * 100000000 }}",
    # A generator global that needs no operator at all.
    "lipsum": "{{ lipsum(20000) }}",
    # Emits nothing, so only a check *inside* the loop can ever see it.
    "nested_for": (
        "{% for a in range(100000) %}"
        "{% for b in range(100000) %}{% endfor %}"
        "{% endfor %}done"
    ),
    # printf width: 2 GB out of a thirteen-character template.
    "percent_width": "{{ '%2000000000d' % 1 }}",
    # An integer wide enough to exhaust memory on its own.
    "exponent": "{{ 10 ** 100000000 }}",
}

# Runs in the child. Imports FIRST, then clamps the address space, so a big
# virtual-memory reservation during import can never make this flaky. Each
# result is printed and flushed on its own line, so a hang (the pre-fix
# behaviour of ``nested_for``) still leaves the completed cases readable.
_BOMB_RUNNER = r"""
import json, os, resource, sys, time

sys.path.insert(0, os.environ["SRW_REPO_ROOT"])
from src.core.loader import render_instruction_content  # noqa: E402

with open("/proc/self/status") as status:
    held = next(
        int(line.split()[1]) * 1024
        for line in status
        if line.startswith("VmSize:")
    )
limit = held + int(os.environ["SRW_AS_HEADROOM"])
resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

for case, source in json.loads(os.environ["SRW_BOMBS"]).items():
    started = time.monotonic()
    try:
        rendered = render_instruction_content(
            source, ["run_command"], origin="bomb " + case
        )
        result = {"outcome": "rendered", "chars": len(rendered), "error": ""}
    except BaseException as exc:  # noqa: BLE001 — the point is what escaped
        result = {
            "outcome": type(exc).__name__,
            "chars": 0,
            "error": str(exc)[:400],
        }
    result["case"] = case
    result["seconds"] = round(time.monotonic() - started, 3)
    print("SRW_RESULT " + json.dumps(result), flush=True)
"""


@pytest.fixture(scope="module")
def bomb_results() -> dict:
    """Render every bomb in one child process under ``RLIMIT_AS``."""
    env = {
        **os.environ,
        "SRW_REPO_ROOT": str(_REPO_ROOT),
        "SRW_AS_HEADROOM": str(_AS_HEADROOM_BYTES),
        "SRW_BOMBS": json.dumps(_BOMBS),
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _BOMB_RUNNER],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        stdout = proc.stdout
    except subprocess.TimeoutExpired as expired:
        # A bomb that hangs forever is a failure, not an error — keep whatever
        # completed so the per-case assertions name the offender.
        stdout = expired.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
    results = {}
    for line in stdout.splitlines():
        if line.startswith("SRW_RESULT "):
            payload = json.loads(line[len("SRW_RESULT ") :])
            results[payload["case"]] = payload
    return results


@pytest.mark.parametrize("case", sorted(_BOMBS))
def test_template_bomb_fails_closed_with_the_origin_named(bomb_results, case):
    """Every bomb is refused as the fail-closed budget error, attributably, and
    without returning any partial prompt."""
    assert case in bomb_results, (
        f"{case!r} never returned — the render did not terminate "
        f"(got: {sorted(bomb_results)})"
    )
    result = bomb_results[case]
    assert result["outcome"] == "PromptRenderBudgetError", (
        f"{case!r} escaped the budget as {result['outcome']}: {result['error']}"
    )
    assert f"bomb {case}" in result["error"], (
        f"{case!r} refusal does not name its origin: {result['error']}"
    )
    # No partial prompt: a truncated render must never be handed back as if it
    # were the complete one.
    assert result["chars"] == 0


def test_no_bomb_escapes_as_a_memory_error(bomb_results):
    """The whole point of the budget: the process must never be the thing that
    notices. ``MemoryError`` only fires where an address-space limit exists —
    under a container memory limit the kernel OOM-kills instead, with nothing
    to catch."""
    escaped = {
        case: r["outcome"]
        for case, r in bomb_results.items()
        if r["outcome"] in ("MemoryError", "OverflowError", "RecursionError")
    }
    assert not escaped, f"raw resource exceptions escaped the render: {escaped}"


def test_budget_error_is_a_security_error_subclass():
    """Callers that already fail closed on ``PromptRenderSecurityError`` keep
    failing closed on a bomb without being touched."""
    assert issubclass(PromptRenderBudgetError, PromptRenderSecurityError)


def test_budget_constants_leave_real_prompts_far_below_the_cap():
    assert MAX_RENDERED_PROMPT_CHARS == 1024 * 1024
    assert PROMPT_RENDER_TIME_BUDGET_SECONDS >= 1.0


# ── a large-but-legitimate prompt is untouched ───────────────────────────────


def test_large_legitimate_prompt_renders_byte_identically():
    """~100 KB of real prompt with live conditionals: the budget must be
    invisible to it, byte for byte."""
    filler = "Follow the workspace conventions in plan.md.\n" * 2200
    content = (
        "# Instructions\n"
        + filler
        + '{% if has_tool("run_command") %}You have a shell.\n{% endif %}'
        + '{% if has_tool("kb_write") %}You may write knowledge.\n{% endif %}'
        + "Tools: {{ tools|join(', ') }}\n"
    )
    assert 90_000 < len(content) < MAX_RENDERED_PROMPT_CHARS

    expected = (
        "# Instructions\n"
        + filler
        + "You have a shell.\n"
        + "Tools: run_command, read_file\n"
    )
    assert (
        render_instruction_content(
            content, ["run_command", "read_file"], origin="legit"
        )
        == expected
    )


def test_ordinary_arithmetic_still_works_through_the_intercepted_operators():
    """Interception is transparent: ``+``/``*``/``%``/``**`` below the cap
    delegate to the ordinary operator."""
    out = render_instruction_content(
        "{{ 2 + 3 }}|{{ 'ab' * 3 }}|{{ 7 % 3 }}|{{ 2 ** 10 }}|"
        "{% for i in range(4) %}{{ i }}{% endfor %}|{{ range(3)|length }}",
        [],
        origin="arithmetic",
    )
    assert out == "5|ababab|1|1024|0123|3"


# ── the skill path: no fence, by provenance, and the budget covers it ────────


def test_bound_skill_bodies_are_disk_provenance_only():
    """Why bound skills are NOT brace-fenced like the five gated prompt keys.

    A bound skill's body is frozen from disk — the expert's own
    ``skills/<name>/SKILL.md`` or the bundled ``config/skills/<name>`` — never
    from the DB skills table, so it is not user-authored content and fencing it
    would only break the ``{% if has_tool(...) %}`` blocks the bundled skills
    genuinely use. A skill name that exists only as a DB row freezes to
    nothing, and ``filter_bound_skills`` then removes it from the catalog too,
    so a user-authored body reaches neither delivery channel.
    """
    from src.core.skill_resolution import filter_bound_skills

    config = load_agent_config_from_dict(
        {
            "agent_id": "t",
            "display_name": "T",
            "instruction_files": [
                {"trigger": "phase_start:tactical", "skill": "verify-before-done"},
                {"trigger": "phase_start:tactical", "skill": "not-on-disk"},
            ],
        }
    )
    blob = serialize_resolved_config(config)

    bundled = (
        _REPO_ROOT / "config" / "skills" / "verify-before-done" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert blob["instructions"]["verify-before-done"] == bundled
    assert "{% if has_tool(" in bundled, (
        "the bundled skill this test relies on stopped using Jinja — pick "
        "another, the point is that fencing would break it"
    )
    assert "not-on-disk" not in blob["instructions"]

    blob["skills"] = {
        "menu": [{"name": "not-on-disk"}],
        "files": {"not-on-disk": {"SKILL.md": _BOMBS["multiplication"]}},
    }
    filter_bound_skills(blob)
    assert blob["skills"]["files"] == {}
    assert blob["skills"]["menu"] == []


def test_catalog_skill_files_are_delivered_verbatim_never_rendered():
    """The other half of the same argument: DB catalog skills are written to the
    workspace as-is, so their bodies never reach the renderer at all."""
    from src.core.skill_resolution import skill_files_to_workspace

    bomb = _BOMBS["multiplication"]
    mapped = skill_files_to_workspace({"user-skill": {"SKILL.md": bomb}})
    assert mapped == {"skills/user-skill/SKILL.md": bomb}


def test_a_bound_skill_body_that_is_a_bomb_is_refused_not_rendered():
    """Provenance is the argument for not fencing; the budget is the guarantee
    that does not depend on it. Whatever a bound skill body turns out to be, the
    render refuses it fail-closed and names the binding."""
    with pytest.raises(PromptRenderBudgetError) as excinfo:
        render_instruction_content(
            _BOMBS["multiplication"],
            ["run_command"],
            origin="bound skill 'evil'",
        )
    assert "bound skill 'evil'" in str(excinfo.value)


# ── the poisoned fragment never becomes durable ──────────────────────────────


def _db_for_merge(metadata, update_result="UPDATE 1"):
    """PostgresDB with a mocked pool (mirrors tests/test_postgres_advisory_lock.py)."""
    from unittest.mock import AsyncMock, MagicMock

    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"metadata": metadata})
    conn.execute = AsyncMock(side_effect=[None, update_result])
    txn_cm = AsyncMock()
    txn_cm.__aenter__.return_value = None
    txn_cm.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=txn_cm)

    pool_cm = AsyncMock()
    pool_cm.__aenter__.return_value = conn
    pool_cm.__aexit__.return_value = False
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=pool_cm)
    db._pool = pool
    return db, conn


@pytest.mark.asyncio
async def test_thread_config_merge_strips_loader_owned_keys_before_persisting():
    """``resolve_config`` cleans authored layers on the READ path, so a poisoned
    ``_db_prompt_keys`` renders nothing unfenced today — but persisting it
    leaves an unfenced prompt at rest, one missed strip away from the renderer.
    The write path strips too."""
    db, conn = _db_for_merge({"config_override": {"llm": {"model": "old"}}})

    ok = await db.merge_thread_config_override(
        "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        {
            "llm": {"model": "new"},
            "_db_prompt_keys": [],
            "_persona_source": None,
            "_resolved_prompts": {"tactical": "{{ ''.__class__ }}"},
            "extra": {
                "shell": {"mode": "persistent"},
                "_db_prompt_keys": [],
            },
        },
    )
    assert ok is True

    written = json.loads(conn.execute.call_args_list[1].args[1])
    assert written == {
        "llm": {"model": "new"},
        "extra": {"shell": {"mode": "persistent"}},
    }
