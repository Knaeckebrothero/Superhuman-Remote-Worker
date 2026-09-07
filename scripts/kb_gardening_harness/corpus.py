"""Corpus loader for the Better Resavio KB dump (host-side experiments).

Parses OKF frontmatter with the repo's own parser so the harness sees exactly
what the reindexer would see.
"""

from __future__ import annotations
import collections
import pathlib

from shared.runtime.knowledge.gardener import (
    parse_note_md,
    _internal_link_targets,
    frontmatter_link_targets,
)  # type: ignore

ROOT = pathlib.Path(__file__).resolve().parents[2] / "BetterResavio-KB"


def load(slices=("history", "live")):
    notes = {}
    for sl in slices:
        for p in sorted((ROOT / sl / "knowledge").glob("*.md")):
            text = p.read_text(errors="replace")
            try:
                fm, body = parse_note_md(text)
            except ValueError:
                fm, body = None, text
            fm = fm or {}
            slug = p.stem
            t = str(fm.get("type") or "learning").split(" ")[0].strip("()")
            st = str(fm.get("status") or "active").split(" ")[0]
            links = set()
            try:
                links |= set(_internal_link_targets(body))
                links |= set(frontmatter_link_targets(fm))
            except Exception:
                pass
            notes[slug] = dict(
                slug=slug,
                slice=sl,
                type=t,
                status=st,
                fm=fm,
                body=body,
                links=sorted(links),
                size=len(text),
                title=(body.lstrip().splitlines() or [""])[0][:120],
            )
    return notes


if __name__ == "__main__":
    notes = load()
    by = lambda k: collections.Counter(n[k] for n in notes.values())
    print(
        "notes",
        len(notes),
        "history",
        sum(n["slice"] == "history" for n in notes.values()),
        "live",
        sum(n["slice"] == "live" for n in notes.values()),
    )
    print("type", by("type").most_common(12))
    print("status", by("status").most_common(8))
    inbound = collections.Counter()
    for n in notes.values():
        for l in n["links"]:
            if l in notes:
                inbound[l] += 1
    orphans = [s for s in notes if inbound[s] == 0]
    print(
        "links_total",
        sum(len(n["links"]) for n in notes.values()),
        "resolvable",
        sum(inbound.values()),
        "orphans",
        len(orphans),
        f"({len(orphans) / len(notes):.0%})",
    )
    # must-keep anchor: notes linked from live decision/goal/plan notes
    anchor = set()
    for n in notes.values():
        if n["slice"] == "live" and n["status"] == "active":
            anchor |= {l for l in n["links"] if l in notes}
    print(
        "linked_from_live_active",
        len(anchor),
        "of which in history",
        sum(notes[s]["slice"] == "history" for s in anchor),
    )
    # type x status crosstab for history
    ct = collections.Counter(
        (n["type"], n["status"]) for n in notes.values() if n["slice"] == "history"
    )
    for (t, s), c in sorted(ct.items(), key=lambda x: -x[1])[:15]:
        print(f"  {t:14s} {s:11s} {c}")
    sizes = sorted(n["size"] for n in notes.values())
    print(
        "size p50",
        sizes[len(sizes) // 2],
        "p90",
        sizes[int(len(sizes) * 0.9)],
        "max",
        sizes[-1],
    )
    # author mix
    print(
        "author",
        collections.Counter(
            str(n["fm"].get("author")) for n in notes.values()
        ).most_common(8),
    )
    print(
        "with created",
        sum(1 for n in notes.values() if n["fm"].get("created")),
        "with job",
        sum(1 for n in notes.values() if n["fm"].get("job")),
    )
