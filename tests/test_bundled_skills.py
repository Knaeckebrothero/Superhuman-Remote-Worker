"""Bundled skill fixtures must be valid and (where claimed) script-bearing."""

from pathlib import Path

import yaml

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
