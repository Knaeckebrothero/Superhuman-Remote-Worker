"""Bundled skill fixtures must be valid and (where claimed) script-bearing."""

import re
from pathlib import Path

import yaml

from src.core.loader import render_instruction_content
from src.core.skill_format import parse_skill_md, skill_identity

_SKILLS = Path(__file__).resolve().parents[1] / "config" / "skills"


def _skill_dirs():
    return sorted(d for d in _SKILLS.iterdir() if (d / "SKILL.md").is_file())


def test_every_bundled_skill_parses_and_respects_limits():
    """Format contract for ALL bundled skills (current + future): frontmatter
    parses, name matches the directory, and the two ENFORCED limits hold
    (name <= 64 chars, description <= 1024 chars — per the authoring rubric).
    This is the only coverage the model-invoked skills (brainstorming,
    systematic-debugging, verify-before-done, code-review) get, since they ride
    the catalog rather than the binding mechanism in test_skill_bindings.py."""
    dirs = _skill_dirs()
    assert dirs, "no bundled skills found under config/skills/"
    for d in dirs:
        md = (d / "SKILL.md").read_text(encoding="utf-8")
        fm, body = parse_skill_md(md)
        name, desc = skill_identity(fm)
        assert name == d.name, f"{d.name}: frontmatter name {name!r} != dir name"
        assert 0 < len(name) <= 64, f"{d.name}: name length {len(name)} out of range"
        assert desc and desc.strip(), f"{d.name}: missing/empty description"
        assert len(desc) <= 1024, f"{d.name}: description {len(desc)} > 1024 chars"
        assert body.strip(), f"{d.name}: empty body"


def test_bundled_word_count_skill_is_valid_and_script_bearing():
    root = _SKILLS / "word-count"
    md = (root / "SKILL.md").read_text(encoding="utf-8")

    # Frontmatter parses and carries the two required fields.
    assert md.startswith("---\n")
    frontmatter = yaml.safe_load(md.split("---\n", 2)[1])
    assert frontmatter["name"] == "word-count"
    assert isinstance(frontmatter.get("description"), str)
    assert frontmatter["description"].strip()

    # It is genuinely script-bearing, and the body points at the script.
    assert (root / "scripts" / "wordcount.py").exists()
    assert "scripts/wordcount.py" in md


def test_verify_before_done_uses_shell_checks_only_when_available():
    """A shell-less Scholar must not be instructed to call an absent runner."""
    md = (_SKILLS / "verify-before-done" / "SKILL.md").read_text(encoding="utf-8")

    with_shell = render_instruction_content(
        md,
        ["run_command", "file_exists", "read_file", "get_citation"],
    )
    without_shell = render_instruction_content(
        md,
        ["file_exists", "read_file", "get_citation"],
    )
    with_git = render_instruction_content(
        md,
        ["file_exists", "read_file", "git_diff"],
    )

    assert "`run_command` with the test/build/lint command" in with_shell
    assert "`wc -w`" in with_shell
    assert "This workspace has no command runner" not in with_shell

    assert "run_command" not in without_shell
    assert "`wc -w`" not in without_shell
    assert "This workspace has no command runner" in without_shell
    assert "call `file_exists` once" in without_shell
    assert "call `read_file` once" in without_shell
    assert "repeat an unchanged evidence bundle" in without_shell
    assert "No Git-history check is required" in without_shell
    assert "Git history is optional orientation" in with_git
    assert "use `git_diff` once" in with_git
    assert "instructions.md" in without_shell
    assert "completion_note" in without_shell
    assert "{%" not in without_shell


def test_verify_before_done_reports_when_no_artifact_checker_is_available():
    md = (_SKILLS / "verify-before-done" / "SKILL.md").read_text(encoding="utf-8")

    rendered = render_instruction_content(md, [])

    assert "no deterministic workspace checker is available" in rendered
    assert "file_exists" not in rendered
    assert "read_file" not in rendered
    assert "run_command" not in rendered
    assert "{%" not in rendered


def test_bundled_code_review_skill_is_valid_and_on_topic():
    root = _SKILLS / "code-review"
    md = (root / "SKILL.md").read_text(encoding="utf-8")
    fm, body = parse_skill_md(md)
    name, desc = skill_identity(fm)
    assert name == "code-review"
    # Trigger scopes to reviewing someone else's work and disambiguates from the
    # author's own pre-completion self-check (verify-before-done).
    assert "review" in desc.lower()
    assert "verify-before-done" in desc
    # Body carries the load-bearing review machinery: severity-triaged findings
    # and an explicit verdict (not a vibe-based "looks good").
    assert "Severity" in body and "Verdict" in body
    assert "request-changes" in body


def test_bundled_project_onboarding_skill_is_valid_and_on_topic():
    root = _SKILLS / "project-onboarding"
    md = (root / "SKILL.md").read_text(encoding="utf-8")
    fm, body = parse_skill_md(md)
    name, desc = skill_identity(fm)
    assert name == "project-onboarding"
    # Trigger scopes to orienting in an existing body of work and disambiguates
    # from both verify-before-done (checking your own finished work) and
    # research-guide (investigating a question to produce findings).
    assert "verify-before-done" in desc
    assert "research-guide" in desc
    # Generalized past a codebase: the body must treat a project as a connector
    # bundle, not assume a repo.
    assert "connector" in body.lower()
    assert "datasource" not in body.replace("`datasources.md`", "").lower()
    # Body carries the two load-bearing halves: the source-of-truth/provenance
    # honesty move and an explicit termination rule (orientation must stop).
    assert "source of truth" in body.lower()
    assert "stop" in body.lower()


def test_bundled_app_guide_skill_is_valid_and_indexes_its_references():
    root = _SKILLS / "app-guide"
    md = (root / "SKILL.md").read_text(encoding="utf-8")
    fm, body = parse_skill_md(md)
    name, desc = skill_identity(fm)
    assert name == "app-guide"
    # Trigger scopes to the USER asking about the product and disambiguates
    # from orienting yourself in the user's project content.
    assert "repository orientation" in desc
    assert "application/code questions" in desc
    # The grounding contract is the skill's whole point: retrieve-then-answer
    # from the bundled usage docs, never from priors.
    assert "focused reference" in desc
    assert "priors" in body
    assert "read_product_guide" in desc and "read_product_guide" in body
    assert "skills/app-guide/" in body
    assert "attach the datasource" not in body
    # The body's routing index and the files on disk must agree in both
    # directions: every bundled doc is routable, every routed path exists.
    on_disk = {f"references/{p.name}" for p in (root / "references").glob("*.md")}
    assert on_disk, "app-guide must bundle reference docs"
    indexed = set(re.findall(r"references/[a-z0-9-]+\.md", body))
    assert indexed == on_disk


def test_bundled_tdd_skill_is_valid_and_on_topic():
    root = _SKILLS / "test-driven-development"
    md = (root / "SKILL.md").read_text(encoding="utf-8")
    fm, body = parse_skill_md(md)
    name, desc = skill_identity(fm)
    assert name == "test-driven-development"
    # Trigger self-scopes to code and disambiguates from the two sibling skills
    # whose machinery overlaps (the end gate vs. diagnosing a failure).
    assert "verify-before-done" in desc
    assert "systematic-debugging" in desc
    # Body carries the evidence-backed core (small verified increments, not the
    # coverage ritual) and the load-bearing honesty step: see the test fail first.
    assert "fail" in body.lower()
    assert "refactor" in body.lower()
    # The honest boundary section — TDD is not for every task.
    assert "skip it" in body.lower()
