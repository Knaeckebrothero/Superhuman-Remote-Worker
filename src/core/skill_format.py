"""SKILL.md open-standard (agentskills.io) parsing + packaging for SRW skills.

A skill is a directory: a required SKILL.md (YAML frontmatter + markdown body)
plus optional reference/script/asset files. The SKILL.md is the canonical
artifact — we store its bytes verbatim and only PARSE it to denormalize
name/description onto the skills row. This module is pure (no DB, no FastAPI):
parse, validate paths, and pack/unpack the native zip used for import/export.

Design: docs/features/agent_skills.md (Slice 1).
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import yaml

SKILL_MD = "SKILL.md"
# Skill name slug — same shape as expert names (see ExpertCreate in main.py).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# Leading '---' line, YAML, closing '---' line, then the body.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
# Fixed timestamp so import->export is byte-reproducible (zip stores mtime).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class SkillFormatError(ValueError):
    """Raised when a skill bundle or SKILL.md is malformed (maps to HTTP 422)."""


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter dict, body str)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillFormatError(
            "SKILL.md must start with a '---' YAML frontmatter block"
        )
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillFormatError(f"SKILL.md frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        raise SkillFormatError("SKILL.md frontmatter must be a mapping")
    return fm, m.group(2)


def skill_identity(frontmatter: dict[str, Any]) -> tuple[str, str]:
    """Extract and validate (name, description) from frontmatter."""
    name = str(frontmatter.get("name", "")).strip()
    if not _NAME_RE.match(name):
        raise SkillFormatError(
            f"SKILL.md 'name' must match ^[a-z][a-z0-9_-]*$ (got {name!r})"
        )
    description = str(frontmatter.get("description", "") or "").strip()
    return name, description


def validate_skill_path(path: str) -> str:
    """Return a safe relative path, or raise. Rejects abs/traversal/empty/backslash."""
    if not path or path.strip() != path or path.endswith("/"):
        raise SkillFormatError(f"illegal file path: {path!r}")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise SkillFormatError(f"illegal file path: {path!r}")
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise SkillFormatError(f"illegal file path: {path!r}")
    return path


def validate_skill_files(files: dict[str, str]) -> None:
    """Require a root SKILL.md and safe paths everywhere."""
    if SKILL_MD not in files:
        raise SkillFormatError("a skill must contain a SKILL.md at its root")
    for path in files:
        validate_skill_path(path)


def pack_skill_zip(name: str, files: dict[str, str]) -> bytes:
    """Pack files into a deterministic zip under a top-level <name>/ dir."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{name}/{path}", date_time=_ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[path])
    return buf.getvalue()


def unpack_skill_zip(data: bytes) -> dict[str, str]:
    """Unpack a skill zip into a path->content map rooted at the skill dir."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SkillFormatError(f"not a valid zip archive: {e}") from e
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise SkillFormatError("zip archive is empty")
    tops = {n.split("/", 1)[0] for n in names}
    strip = len(tops) == 1 and all("/" in n for n in names)
    prefix = f"{next(iter(tops))}/" if strip else ""
    files: dict[str, str] = {}
    for n in names:
        rel = n[len(prefix) :] if prefix and n.startswith(prefix) else n
        validate_skill_path(rel)
        try:
            files[rel] = zf.read(n).decode("utf-8")
        except UnicodeDecodeError as e:
            raise SkillFormatError(f"{rel}: only UTF-8 text files are supported") from e
    validate_skill_files(files)
    return files


def set_skill_name(text: str, new_name: str) -> str:
    """Rewrite frontmatter 'name:' to new_name, preserving the body. For forks."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillFormatError(
            "SKILL.md must start with a '---' YAML frontmatter block"
        )
    block, body = m.group(1), m.group(2)
    new_block, n = re.subn(r"(?m)^name:.*$", f"name: {new_name}", block, count=1)
    if n == 0:
        new_block = f"name: {new_name}\n{block}"
    return f"---\n{new_block}\n---\n{body}"
