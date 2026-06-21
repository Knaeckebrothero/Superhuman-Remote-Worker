"""Bundled skill fixtures must be valid and (where claimed) script-bearing."""

from pathlib import Path

import yaml

_SKILLS = Path(__file__).resolve().parents[1] / "config" / "skills"


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
