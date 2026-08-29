"""An expert's prompts may only name tools that expert actually has.

The config half of this is pinned by
``tests/test_config_tool_names_are_registered.py``: a name in a ``tools:`` list
must exist in ``TOOL_REGISTRY``. That says nothing about prose. Ten prompt files
across four experts went on instructing the model to call ``browse_website`` for
months after it was removed from the registry — the tool lists were eventually
cleaned, the prompts were not (see
``knowledge-history/done/expert_prompts_instruct_a_removed_browser_tool.md``).

Being told to call a tool you do not have is not free. At best the instruction
is ignored; at worst the model spends a turn on a call that cannot resolve and
then has to recover. It also silently removes a capability the prompt author
believed they were granting — the designer experts were told to do visual
verification with a tool that had not existed for months.

Two failure shapes are caught:

- a name that is in ``TOOL_REGISTRY`` but not in this expert's merged toolset —
  the prompt promises a capability the config withholds;
- a name from ``_REMOVED_TOOL_NAMES`` — a tool that no longer exists anywhere,
  which the registry check cannot see precisely because it is gone.

Scope is each expert's OWN directory. Framework prompts in ``config/prompts/``
and ``config/templates/`` are shared by every expert, so a name there would have
to be valid for all of them; that is a stricter, separate check.
"""

import re
from pathlib import Path

import pytest

from src.core.loader import load_and_merge_config, resolve_config_path
from src.tools.registry import TOOL_REGISTRY

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# Tools deleted from the registry. Prompts naming these are the exact bug this
# test exists for, and the registry can no longer flag them. Add a name here
# when you remove a tool, so stale prose fails loudly instead of rotting.
_REMOVED_TOOL_NAMES = frozenset(
    {
        "browse_website",
        "download_from_website",
        # U3 WP4: the light reader and the heavy child-job pair — one tool,
        # delegate_agent, replaced all three.
        "spawn_subagent",
        "delegate_work",
        "resume_delegation_child",
    }
)

# Tools the DISPATCHER stamps into a job's config fragment rather than any
# checked-in config: ``approve_job_verdict`` / ``return_job_with_feedback`` via
# ``_critic_config_override`` (src/api/orchestrator_client.py:1880) and
# ``loop_plan`` via the planner loops. ``src/tools/registry.py:160-178``
# explains why these are deliberately NOT marked ``grant: "code"`` — that
# marking would stop ``evaluation: true`` / ``loop: true`` resolving to them.
# So the registry cannot tell us, and the exception has to live here.
#
# Everything else granted at runtime IS marked ``grant: "code"`` (38 tools:
# officer, datasource, repo, product-guide, session-task …) and is skipped
# from the registry metadata rather than by name.
_DISPATCH_STAMPED = frozenset(
    {"approve_job_verdict", "return_job_with_feedback", "loop_plan"}
)


def _is_runtime_granted(name: str) -> bool:
    """True when a tool reaches the agent by code, not by an expert's config."""
    if name in _DISPATCH_STAMPED:
        return True
    return TOOL_REGISTRY.get(name, {}).get("grant") == "code"


# Config, not prose — these are checked by the config-side tests.
_NON_PROSE_FILES = frozenset(
    {
        "config.yaml",
        "model_config_matrix.yaml",
        "prompt_matrix.yaml",
        "instruction_matrix.yaml",
    }
)

_PROSE_SUFFIXES = (".txt", ".md", ".yaml")

# Tool names are snake_case and effectively never appear as ordinary English,
# so a token match against a curated vocabulary is precise enough to act on.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")

_VOCABULARY = frozenset(TOOL_REGISTRY) | _REMOVED_TOOL_NAMES


def _expert_names() -> list[str]:
    return sorted(
        p.parent.name
        for p in _CONFIG_DIR.glob("experts/*/config.yaml")
        if p.parent.name != "__pycache__"
    )


def _prose_files(expert: str) -> list[Path]:
    return sorted(
        p
        for p in (_CONFIG_DIR / "experts" / expert).rglob("*")
        if p.is_file()
        and p.suffix in _PROSE_SUFFIXES
        and p.name not in _NON_PROSE_FILES
    )


def _granted_tools(expert: str) -> set[str]:
    path, _ = resolve_config_path(expert)
    tools = (load_and_merge_config(path) or {}).get("tools") or {}
    return {
        name
        for names in tools.values()
        if isinstance(names, list)
        for name in names
        if isinstance(name, str)
    }


@pytest.mark.parametrize("expert", _expert_names())
def test_expert_prompts_only_name_granted_tools(expert):
    granted = _granted_tools(expert)
    offenders: list[str] = []
    for path in _prose_files(expert):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for token in set(_TOKEN_RE.findall(line)):
                if token not in _VOCABULARY or token in granted:
                    continue
                if _is_runtime_granted(token):
                    continue
                why = (
                    "REMOVED from the registry"
                    if token in _REMOVED_TOOL_NAMES
                    else "exists but is not granted to this expert"
                )
                rel = path.relative_to(_CONFIG_DIR)
                offenders.append(f"{rel}:{lineno} names '{token}' ({why})")

    assert not offenders, (
        f"expert '{expert}' has prompt text naming tools it cannot call — the "
        "model is being instructed to use a capability it does not have:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_vocabulary_and_experts_are_non_empty():
    """Guard the guard: an empty vocabulary or glob makes every case vacuous."""
    assert len(_expert_names()) >= 4
    assert len(_VOCABULARY) > 50
    assert "browse_website" in _VOCABULARY


def test_removed_names_are_really_gone_from_the_registry():
    """If a 'removed' name comes back, drop it from _REMOVED_TOOL_NAMES."""
    resurrected = sorted(_REMOVED_TOOL_NAMES & set(TOOL_REGISTRY))
    assert not resurrected, (
        f"{resurrected} are in TOOL_REGISTRY again — remove them from "
        "_REMOVED_TOOL_NAMES so the registry check governs them instead."
    )


def test_dispatch_stamped_names_still_need_the_local_exception():
    """Shrink _DISPATCH_STAMPED if the registry starts classifying these.

    They are unmarked on purpose today (registry.py:160-178). If someone finds
    a way to mark them without breaking ``evaluation: true`` resolution, this
    fails and the name should move out of the local set.
    """
    now_classified = sorted(
        name
        for name in _DISPATCH_STAMPED
        if TOOL_REGISTRY.get(name, {}).get("grant") == "code"
    )
    assert not now_classified, (
        f"{now_classified} are now marked grant='code' in the registry — drop "
        "them from _DISPATCH_STAMPED so the metadata governs them instead."
    )


def test_lint_would_catch_a_prompt_naming_an_ungranted_tool():
    """Teeth: a config-granted tool absent from an expert must be reported."""
    granted = _granted_tools("designer")
    # A real, config-granted tool that the designer is not given.
    assert "cite_web" in TOOL_REGISTRY
    assert "cite_web" not in granted
    assert not _is_runtime_granted("cite_web")
    # …so were it named in a designer prompt, the check above would flag it.
    assert "cite_web" in _VOCABULARY
