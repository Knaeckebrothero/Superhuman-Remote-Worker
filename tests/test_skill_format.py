import pytest

from src.core.skill_format import (
    SkillFormatError,
    pack_skill_zip,
    parse_skill_md,
    set_skill_name,
    skill_identity,
    unpack_skill_zip,
    validate_skill_files,
    validate_skill_path,
)

SAMPLE = (
    "---\n"
    "name: pdf-filler\n"
    "description: Use when filling PDF forms from structured data.\n"
    "---\n"
    "\n"
    "# PDF Filler\n"
    "\n"
    "Run `scripts/fill.py`.\n"
)


def test_parse_splits_frontmatter_and_body():
    fm, body = parse_skill_md(SAMPLE)
    assert fm["name"] == "pdf-filler"
    assert fm["description"].startswith("Use when")
    assert body.startswith("\n# PDF Filler")


def test_parse_rejects_missing_frontmatter():
    with pytest.raises(SkillFormatError):
        parse_skill_md("# no frontmatter here\n")


def test_parse_rejects_non_mapping_frontmatter():
    with pytest.raises(SkillFormatError):
        parse_skill_md("---\n- just\n- a\n- list\n---\nbody\n")


def test_identity_extracts_name_and_description():
    fm, _ = parse_skill_md(SAMPLE)
    name, desc = skill_identity(fm)
    assert name == "pdf-filler"
    assert desc.startswith("Use when")


@pytest.mark.parametrize("bad", ["Bad Name", "1leading", "UPPER", "has space", ""])
def test_identity_rejects_bad_slug(bad):
    with pytest.raises(SkillFormatError):
        skill_identity({"name": bad, "description": "x"})


@pytest.mark.parametrize(
    "bad", ["/abs", "../escape", "a/../b", "a//b", "back\\slash", "", "trail/"]
)
def test_validate_path_rejects_unsafe(bad):
    with pytest.raises(SkillFormatError):
        validate_skill_path(bad)


def test_validate_path_accepts_nested():
    assert validate_skill_path("references/guide.md") == "references/guide.md"


def test_validate_files_requires_skill_md():
    with pytest.raises(SkillFormatError):
        validate_skill_files({"references/x.md": "y"})


def test_zip_round_trip_is_lossless():
    files = {
        "SKILL.md": SAMPLE,
        "references/g.md": "# Guide\n",
        "scripts/f.py": "print(1)\n",
    }
    data = pack_skill_zip("pdf-filler", files)
    assert unpack_skill_zip(data) == files


def test_zip_pack_is_deterministic():
    files = {"SKILL.md": SAMPLE, "references/g.md": "# Guide\n"}
    assert pack_skill_zip("pdf-filler", files) == pack_skill_zip("pdf-filler", files)


def test_unpack_strips_single_top_dir():
    files = {"SKILL.md": SAMPLE}
    data = pack_skill_zip("pdf-filler", files)  # writes 'pdf-filler/SKILL.md'
    assert unpack_skill_zip(data) == files  # top dir stripped


def test_set_skill_name_rewrites_frontmatter_only():
    out = set_skill_name(SAMPLE, "pdf-filler-copy")
    fm, _ = parse_skill_md(out)
    assert fm["name"] == "pdf-filler-copy"
    assert "# PDF Filler" in out  # body untouched
