"""Regression guard: every shipped prompt must survive the real render pipeline.

The loop job `bf9183cf` hard-failed because `config/experts/product-qa/tactical.txt`
carried a literal `{py,sh,md}` and the assembler rendered the phase component with
`str.format`, which read it as a replacement field and raised `KeyError: 'py,sh,md'`
at phase render. The fix (src/core/loader.py::render_placeholders) substitutes only
an explicit allow-list of tokens and leaves every other brace literal.

This module sweeps ALL bundled expert phase prompts and base system prompts through
the actual two stages — Jinja2 (`render_instruction_content`) then
`render_placeholders` — and asserts none raise and none render empty. It catches a
new prompt shipping with a Jinja syntax error or a stray brace pattern, and pairs
with `test_prompt_matrix.py::TestRenderPlaceholders` (which proves a literal brace
survives end-to-end and would crash if anyone reverts the assembler to str.format).
"""

import re
from pathlib import Path

import pytest

from src.core.loader import render_instruction_content, render_placeholders

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every placeholder the assembler owns, filled with sentinels for the sweep.
_KNOWN = {
    "phase_number": "1",
    "agent_display_name": "SENTINEL_NAME",
    "expert_identity": "SENTINEL_IDENTITY",
    "available_skills": "SENTINEL_SKILLS",
    "prompt_content": "SENTINEL_CONTENT",
}

# Files that flow through the get_phase_system_prompt render sites: expert phase
# prompts + persona, and the bundled base/phase/persona templates. Summarization,
# guardrail runtime messages, and todo templates use other render paths and their
# own kwargs, so they are out of scope here.
_PROMPT_GLOBS = [
    "config/experts/*/strategic.txt",
    "config/experts/*/tactical.txt",
    "config/experts/*/persona.txt",
    "config/prompts/systemprompt*.txt",
    "config/prompts/strategic*.txt",
    "config/prompts/tactical*.txt",
    "config/prompts/persona*.txt",
]


def _shipped_prompts() -> list[Path]:
    found: list[Path] = []
    for pattern in _PROMPT_GLOBS:
        found.extend(sorted(_REPO_ROOT.glob(pattern)))
    return found


def _has_tool_names(content: str) -> list[str]:
    """Tool names referenced by ``has_tool("x")`` so the true Jinja branch renders."""
    return re.findall(r'has_tool\(\s*["\']([^"\']+)["\']\s*\)', content)


@pytest.mark.parametrize(
    "prompt_path",
    _shipped_prompts(),
    ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_shipped_prompt_survives_render_pipeline(prompt_path: Path):
    """Jinja2 → render_placeholders over each shipped prompt: no raise, non-empty.

    Rendered with tools ON (has_tool branches taken) and OFF, since either side of
    a conditional may hold a stray brace.
    """
    content = prompt_path.read_text()
    tools = _has_tool_names(content)
    for tool_names in (tools, []):
        jinja_rendered = render_instruction_content(content, tool_names)
        # Must not raise regardless of which tokens are literal prose vs. ours.
        rendered = render_placeholders(jinja_rendered, **_KNOWN)
        assert rendered.strip(), f"{prompt_path} rendered empty (tools={tool_names!r})"


def test_sweep_covers_the_known_offenders():
    """The three files that started this must be in the swept set (guards the glob)."""
    swept = {str(p.relative_to(_REPO_ROOT)) for p in _shipped_prompts()}
    for f in (
        "config/experts/product-qa/tactical.txt",
        "config/experts/bughunter/tactical.txt",
        "config/experts/designer/tactical.txt",
    ):
        assert f in swept, f"{f} not covered by the brace-safety sweep"
