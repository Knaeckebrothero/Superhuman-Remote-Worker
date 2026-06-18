"""Unit tests for DB-backed skill authoring (Slice 1).

Guards the request models + the SKILL.md parse/deny gate + path validation.
Full endpoint integration is verified live on k3d (see the plan's Task 12)
because the auth dependency + asyncpg store are not hermetically mockable here.
Local env may be noisy (Py3.14); CI (Py3.12) is the authoritative gate.
"""

import pytest
from fastapi import HTTPException

from orchestrator.main import (
    SkillCreate,
    SkillUpdate,
    _parse_skill_bundle,
    _validate_skill_frontmatter,
)

GOOD = "---\nname: my-helper\ndescription: Use when X.\n---\n# My Helper\n"


def test_skill_create_minimal_ok():
    s = SkillCreate(files={"SKILL.md": GOOD})
    assert s.icon == "extension" and s.color == "#6B7280" and s.tags == []


def test_skill_create_rejects_bad_color():
    with pytest.raises(Exception):
        SkillCreate(files={"SKILL.md": GOOD}, color="red")


def test_skill_update_excludes_name():
    assert "name" not in SkillUpdate.model_fields


def test_parse_bundle_returns_name_and_description():
    name, desc, files = _parse_skill_bundle({"SKILL.md": GOOD})
    assert name == "my-helper"
    assert desc == "Use when X."
    assert "SKILL.md" in files


def test_parse_bundle_rejects_missing_skill_md():
    with pytest.raises(HTTPException) as ei:
        _parse_skill_bundle({"references/x.md": "y"})
    assert ei.value.status_code == 422


def test_parse_bundle_rejects_path_traversal():
    with pytest.raises(HTTPException) as ei:
        _parse_skill_bundle({"SKILL.md": GOOD, "../evil": "x"})
    assert ei.value.status_code == 422


def test_parse_bundle_rejects_bad_slug():
    bad = "---\nname: Bad Name\ndescription: y\n---\nbody\n"
    with pytest.raises(HTTPException) as ei:
        _parse_skill_bundle({"SKILL.md": bad})
    assert ei.value.status_code == 422


def test_validate_frontmatter_blocks_credentials():
    with pytest.raises(HTTPException) as ei:
        _validate_skill_frontmatter({"connections": {"token": "secret"}})
    assert ei.value.status_code == 422


def test_validate_frontmatter_allows_clean():
    _validate_skill_frontmatter({"name": "x", "description": "y"})
