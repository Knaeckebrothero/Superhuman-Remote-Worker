"""OKF gardener engine — pure lint + index-generation logic (slice 2, PR1).

No workspace, no DB: these functions operate on already-read note text so they
are fully unit-testable and reusable against any OKF/markdown vault (the
project KB, a `repository` datasource, this repo's own design vault). The thin
`kb_lint` / `kb_index` tools in ``knowledge_tools.py`` read files via the
workspace backend and delegate here.

See knowledge-base/knowledge/features/okf_knowledge_base.md §7 (toolset) and §11 (slice 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Leading '---' line, YAML, closing '---' line, then the body (mirrors
# src/core/skill_format.py — the established frontmatter-parse convention).
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
# OKF/slug id shape — lowercase, digits, hyphens; the shape kb_write generates.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Standard markdown link: [text](target). Images ![...] are excluded by the
# negative lookbehind.
_MD_LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")
# Obsidian wikilink: [[target]], [[target|alias]], [[target#anchor]]. The
# negative lookbehind drops ![[...]] embeds (images/transclusions), mirroring
# the markdown rule above. This vault is an Obsidian vault — wikilinks are the
# dominant link syntax, so a parser that only knew _MD_LINK_RE saw a small
# fraction of the real graph.
_WIKILINK_RE = re.compile(r"(?<!\!)\[\[([^\]]+)\]\]")
# ATX heading anywhere in the body.
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)

_REQUIRED_KEYS = ("id", "type", "description")
# Loud-flag budget for a single note body (§11.1 item 2 — the 131 KB curator
# dumps). Same posture as render_index_md's budget: never silently accepted.
_MAX_NOTE_BYTES = 15 * 1024
# A hash-suffixed sibling slug (foo-a1b2c3) — the collision-suffix shape both
# the legacy random and the deterministic content-hash paths produce.
_FORK_RE = re.compile(r"^(?P<base>.+)-(?P<suffix>[0-9a-f]{6})$")
# OKF reserved filenames — exempt from per-note structural rules (index.md
# carries no frontmatter by spec; log.md is generated change history).
_RESERVED = {"index", "log"}
# Note types that are navigational, not content — never flagged as orphans.
_INDEX_TYPES = {"index", "moc"}

ERROR = "error"
WARNING = "warning"


def parse_note_md(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Split an OKF note into (frontmatter dict, body). Inverse of _render_note_md.

    Returns ``(None, text)`` when there is no frontmatter block. Raises
    ``ValueError`` when a frontmatter block is present but is not valid YAML or
    does not parse to a mapping (callers that lint turn this into a finding).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise ValueError(f"invalid frontmatter YAML: {e}") from e
    if not isinstance(fm, dict):
        raise ValueError("frontmatter must be a mapping")
    return fm, m.group(2)


@dataclass
class Finding:
    """A single lint result anchored to a note."""

    rule: str
    severity: str  # ERROR | WARNING
    path: str
    message: str


@dataclass
class LintReport:
    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    def format_markdown(self) -> str:
        if not self.findings:
            return "**kb_lint:** clean — no findings."
        lines = [
            f"**kb_lint:** {len(self.errors)} error(s), "
            f"{len(self.warnings)} warning(s).",
            "",
        ]
        for f in self.errors + self.warnings:
            tag = "❌" if f.severity == ERROR else "⚠️"
            lines.append(f"- {tag} `{f.path}` [{f.rule}] {f.message}")
        return "\n".join(lines)


_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _basename(path: str) -> str:
    """File basename without the .md extension (== the note slug in flat layout)."""
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def note_title(body: str) -> Optional[str]:
    """The first markdown H1 in a note body, or None (index bullets fall back to id)."""
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else None


def is_reserved(path: str) -> bool:
    """True for OKF reserved filenames (index.md / log.md) — exempt from note rules."""
    return _basename(path) in _RESERVED


def _wikilink_target(raw: str) -> Optional[str]:
    """Slug for one ``[[...]]`` inner text, or None if it names nothing.

    Handles the three Obsidian decorations: ``|alias`` display text,
    ``#anchor`` section refs, and folder-qualified paths. Unlike markdown
    links, wikilinks conventionally omit the ``.md`` extension, so no suffix
    is required — ``_basename`` still strips one when present.
    """
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    if not target or "://" in target:
        return None
    return _basename(target)


def _internal_link_targets(body: str) -> List[str]:
    """Resolvable-or-not internal note slugs referenced by body links.

    Two syntaxes count: ``[...](something.md)`` markdown links and ``[[target]]``
    Obsidian wikilinks (including ``[[target|alias]]`` and ``[[target#anchor]]``).
    External URLs, mailto, bare anchors and ``![[embeds]]`` are ignored. Returns
    target slugs (basename minus ``.md``), markdown links first.
    """
    targets: List[str] = []
    for raw in _MD_LINK_RE.findall(body):
        target = raw.strip()
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        # Drop any in-file anchor (foo.md#section).
        target = target.split("#", 1)[0]
        if target.endswith(".md"):
            targets.append(_basename(target))
    for raw in _WIKILINK_RE.findall(body):
        target = _wikilink_target(raw)
        if target:
            targets.append(target)
    return targets


def frontmatter_link_targets(fm: Optional[Dict[str, Any]]) -> List[str]:
    """Note slugs declared in a note's ``related:`` frontmatter.

    ``related:`` is a relationship declaration, so it is part of the link graph
    even though it lives outside the body — and in an Obsidian vault it carries
    a large share of the edges. Accepts a list or a bare scalar, and the same
    ``[[...]]`` decorations as body wikilinks; bare slugs pass through.
    """
    if not fm:
        return []
    raw_related = fm.get("related")
    if raw_related is None:
        return []
    items = raw_related if isinstance(raw_related, list) else [raw_related]
    targets: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        inner = item.strip()
        m = _WIKILINK_RE.search(inner)
        target = _wikilink_target(m.group(1) if m else inner)
        if target:
            targets.append(target)
    return targets


def lint_kb(notes: List[Dict[str, str]]) -> LintReport:
    """Lint an OKF vault. Each note is ``{"path": str, "text": str}``.

    Deterministic rules only (PR1): structural frontmatter, id integrity, link
    integrity, supersede-chain integrity, orphans, missing titles. The
    embedding-backed near-duplicate rule and the network dead-external-URL
    sweep are deferred to PR2 (they need `find_similar` / network access).
    """
    report = LintReport()

    # First pass: parse every note, collect ids and the link graph.
    parsed: List[Dict[str, Any]] = []
    id_to_paths: Dict[str, List[str]] = {}
    known_slugs: set[str] = set()

    for note in notes:
        path = note["path"]
        slug = _basename(path)
        known_slugs.add(slug)
        reserved = slug in _RESERVED
        fm: Optional[Dict[str, Any]] = None
        body = note["text"]
        parse_error: Optional[str] = None
        try:
            fm, body = parse_note_md(note["text"])
        except ValueError as e:
            parse_error = str(e)
        note_id = fm.get("id") if isinstance(fm, dict) else None
        if note_id:
            id_to_paths.setdefault(str(note_id), []).append(path)
            known_slugs.add(str(note_id))
        parsed.append(
            {
                "path": path,
                "slug": slug,
                "reserved": reserved,
                "fm": fm,
                "body": body,
                "parse_error": parse_error,
                "id": note_id,
                "out_links": _internal_link_targets(body),
            }
        )

    # Inbound-link index (resolvable links only) for orphan detection.
    inbound: Dict[str, int] = {}
    for p in parsed:
        for target in p["out_links"]:
            if target in known_slugs:
                inbound[target] = inbound.get(target, 0) + 1

    # Second pass: per-note rules.
    for p in parsed:
        path, fm, reserved = p["path"], p["fm"], p["reserved"]

        # Dead links apply to every file (a stale index is a real signal).
        for target in p["out_links"]:
            if target not in known_slugs:
                report.findings.append(
                    Finding(
                        "dead-link", ERROR, path, f"link target '{target}' not found"
                    )
                )

        if reserved:
            continue  # index.md / log.md are exempt from note-level structure rules

        # Oversized note — before the parse gate so a 131 KB dump with broken
        # frontmatter still gets flagged for size.
        body_bytes = len(p["body"].encode("utf-8"))
        if body_bytes > _MAX_NOTE_BYTES:
            report.findings.append(
                Finding(
                    "oversized-note",
                    WARNING,
                    path,
                    f"note body is {body_bytes // 1024} KB "
                    f"(budget ~{_MAX_NOTE_BYTES // 1024} KB) — distill or split it",
                )
            )

        if p["parse_error"] is not None:
            report.findings.append(
                Finding("invalid-yaml", ERROR, path, p["parse_error"])
            )
            continue
        if fm is None:
            report.findings.append(
                Finding(
                    "missing-frontmatter", ERROR, path, "note has no YAML frontmatter"
                )
            )
            continue

        for key in _REQUIRED_KEYS:
            if not fm.get(key):
                report.findings.append(
                    Finding(
                        "missing-required-key",
                        ERROR,
                        path,
                        f"missing required key '{key}'",
                    )
                )

        note_id = fm.get("id")
        if note_id and not _ID_RE.match(str(note_id)):
            report.findings.append(
                Finding(
                    "invalid-id", ERROR, path, f"id '{note_id}' is not a valid slug"
                )
            )

        sup = fm.get("superseded_by")
        if sup and str(sup) not in known_slugs:
            report.findings.append(
                Finding(
                    "broken-supersede", ERROR, path, f"superseded_by '{sup}' not found"
                )
            )

        if not _HEADING_RE.search(p["body"]):
            report.findings.append(
                Finding("missing-title", WARNING, path, "body has no markdown heading")
            )

        # Duplicate H1: the same top-level heading text repeated (the run-8
        # double-title defect, docs §11.1 — and any hand-authored equivalent).
        h1s = [t.strip() for t in _H1_RE.findall(p["body"])]
        dupes = sorted({t for t in h1s if h1s.count(t) > 1})
        if dupes:
            report.findings.append(
                Finding(
                    "duplicate-h1",
                    WARNING,
                    path,
                    f"repeated H1 heading(s): {', '.join(dupes)}",
                )
            )

        # Orphan: no resolvable outbound and no inbound links. Navigational
        # notes (index/moc) are never orphans.
        note_type = str(fm.get("type", "")).lower()
        if note_type not in _INDEX_TYPES:
            has_out = any(t in known_slugs for t in p["out_links"])
            has_in = inbound.get(p["slug"], 0) > 0 or (
                note_id and inbound.get(str(note_id), 0) > 0
            )
            if not has_out and not has_in:
                report.findings.append(
                    Finding(
                        "orphan", WARNING, path, "note has no inbound or outbound links"
                    )
                )

    # Slug-forked twins (global): a hash-suffixed sibling next to its base
    # slug, both active — the bare-agent collision signature (§11.1 item 1;
    # gated writers get SUPERSEDE instead, which retires the base). A
    # superseded/archived member is the gate working as designed, not a twin.
    def _is_active(note: Dict[str, Any]) -> bool:
        fm = note["fm"]
        status = str(fm.get("status") or "").lower() if isinstance(fm, dict) else ""
        return status in ("", "active")

    by_slug = {p["slug"]: p for p in parsed}
    for p in parsed:
        if p["reserved"]:
            continue
        m = _FORK_RE.match(p["slug"])
        if not m:
            continue
        base = by_slug.get(m.group("base"))
        if base is None or base["reserved"]:
            continue
        if _is_active(p) and _is_active(base):
            report.findings.append(
                Finding(
                    "slug-forked",
                    WARNING,
                    p["path"],
                    f"hash-suffixed twin of '{m.group('base')}' with both notes "
                    f"active — merge the content or supersede one side",
                )
            )

    # Duplicate ids (global).
    for note_id, paths in id_to_paths.items():
        if len(paths) > 1:
            report.findings.append(
                Finding(
                    "duplicate-id",
                    ERROR,
                    paths[0],
                    f"id '{note_id}' also used by {', '.join(paths[1:])}",
                )
            )

    return report


def external_url_map(notes: List[Dict[str, str]]) -> Dict[str, List[str]]:
    """Map of external http(s) URL → referencing note paths.

    Pure extraction for the dead-external-URL sweep — the reachability check
    (network) lives in the ``kb_lint`` tool wrapper, keeping this engine
    network-free. Only body markdown links count (same parser as dead-link;
    images excluded by the link regex).
    """
    url_map: Dict[str, List[str]] = {}
    for note in notes:
        path = note["path"]
        try:
            _, body = parse_note_md(note["text"])
        except ValueError:
            body = note["text"]
        for raw in _MD_LINK_RE.findall(body):
            target = raw.strip()
            if target.startswith(("http://", "https://")):
                paths = url_map.setdefault(target, [])
                if path not in paths:
                    paths.append(path)
    return {url: sorted(paths) for url, paths in url_map.items()}


def dead_url_findings(
    dead: Dict[str, str], url_map: Dict[str, List[str]]
) -> List[Finding]:
    """WARNING findings for unreachable external URLs, one per referencing note.

    ``dead`` maps URL → failure reason (from the wrapper's probe); ``url_map``
    comes from :func:`external_url_map`.
    """
    findings: List[Finding] = []
    for url, reason in dead.items():
        for path in url_map.get(url, []):
            findings.append(
                Finding(
                    "dead-external-url",
                    WARNING,
                    path,
                    f"external link {url} appears dead ({reason})",
                )
            )
    return findings


def near_duplicate_findings(
    pairs: List[Tuple[str, str, float]], paths: List[str]
) -> List[Finding]:
    """Findings for embedding-near-duplicate note pairs (WARNING).

    ``pairs`` are ``(slug_a, slug_b, similarity)`` triples fetched from the
    pgvector index (``KnowledgeStore.find_near_duplicate_pairs``); ``paths``
    are the files of the vault being linted. Pure formatter — the engine stays
    DB-free: pairs whose slugs aren't both files in this vault are dropped
    (index rows from other roots, or notes not yet dual-written).
    """
    path_by_slug = {_basename(p): p for p in paths}
    findings: List[Finding] = []
    for slug_a, slug_b, similarity in pairs:
        path_a = path_by_slug.get(slug_a)
        if path_a is None or slug_b not in path_by_slug:
            continue
        pct = int(round(similarity * 100))
        findings.append(
            Finding(
                "near-duplicate",
                WARNING,
                path_a,
                f"{pct}% embedding-similar to '{slug_b}' — merge or supersede one side",
            )
        )
    return findings


# =============================================================================
# render_index_md — OKF §6-shaped index.md generation
# =============================================================================

# Markers fence the auto-generated region so human-authored sections outside
# them survive regeneration (Zoottelkeeper's protected-sections pattern).
GEN_START = "<!-- kb_index:generated — do not edit inside these markers -->"
GEN_END = "<!-- /kb_index:generated -->"

# Per-file budget (Claude Code's memory-index budget): keep index files small
# enough to stay in a progressive-disclosure economy.
_MAX_LINES = 200
_MAX_BYTES = 25 * 1024


def render_index_md(
    notes: List[Dict[str, Any]],
    existing: Optional[str] = None,
    max_lines: int = _MAX_LINES,
    max_bytes: int = _MAX_BYTES,
) -> str:
    """Render an OKF §6 index: no frontmatter, `## <type>` groups of
    ``[Title](slug.md) - description`` bullets sorted by title.

    Each note is ``{"id", "type", "description"?, "title"?}`` (title falls back
    to id). Content outside the GEN_START/GEN_END markers in ``existing`` is
    preserved verbatim. Over-budget indexes are truncated with a **loud**
    notice (never silently capped).
    """
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for n in notes:
        by_type.setdefault(str(n.get("type") or "misc"), []).append(n)

    block: List[str] = []
    for note_type in sorted(by_type):
        block.append(f"## {note_type}")
        block.append("")
        for n in sorted(
            by_type[note_type],
            key=lambda x: str(x.get("title") or x.get("id") or "").lower(),
        ):
            nid = str(n.get("id") or "")
            title = str(n.get("title") or nid)
            desc = str(n.get("description") or "").strip()
            bullet = f"- [{title}]({nid}.md)"
            if desc:
                bullet += f" - {desc}"
            block.append(bullet)
        block.append("")

    # Budget enforcement — truncate loudly.
    omitted = 0
    while len(block) > max_lines or len("\n".join(block)) > max_bytes:
        if not block:
            break
        popped = block.pop()
        if popped.startswith("- "):
            omitted += 1
    if omitted:
        block.append(
            f"> (truncated: index exceeds the {max_lines}-line / "
            f"{max_bytes // 1024}KB budget; {omitted} note(s) omitted — narrow the "
            "KB prefix or split the index.)"
        )

    generated = f"{GEN_START}\n" + "\n".join(block).rstrip() + f"\n{GEN_END}"

    if existing and GEN_START in existing and GEN_END in existing:
        before = existing.split(GEN_START, 1)[0]
        after = existing.split(GEN_END, 1)[1]
        return f"{before}{generated}{after}"
    if existing and existing.strip():
        # No markers yet — keep the existing file as a human section below.
        return f"# Index\n\n{generated}\n\n{existing.strip()}\n"
    return f"# Index\n\n{generated}\n"
