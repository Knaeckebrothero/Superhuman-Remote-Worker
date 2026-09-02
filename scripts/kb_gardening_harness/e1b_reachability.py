"""E1b — GC-style reachability: roots = active durable notes; nursery notes not
reachable from any root (following links from active notes only) are candidates."""

from __future__ import annotations
import sys
import pathlib
import collections
import json

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus import load

notes = load()
DURABLE = {
    "decision",
    "goal",
    "plan",
    "charter",
    "feature",
    "issue",
    "idea",
    "code",
    "question",
    "source",
}
NURSERY = {"learning", "retrospective", "state"}
active = {s for s, n in notes.items() if n["status"] == "active"}
live_active = {
    s for s, n in notes.items() if n["slice"] == "live" and n["status"] == "active"
}
anchor = set()
for s in live_active:
    anchor |= {l for l in notes[s]["links"] if l in notes}
KEEP = live_active | anchor
RETIRED = {s for s, n in notes.items() if n["status"] in ("superseded", "archived")}
UNKNOWN = set(notes) - KEEP - RETIRED
hist_active = {
    s for s, n in notes.items() if n["slice"] == "history" and n["status"] == "active"
}


def reach(roots, max_depth):
    seen = set(roots)
    frontier = set(roots)
    d = 0
    while frontier and d < max_depth:
        nxt = set()
        for s in frontier:
            for l in notes[s]["links"]:
                if l in notes and l in active and l not in seen:
                    seen.add(l)
                    nxt.add(l)
        frontier = nxt
        d += 1
    return seen


roots = {s for s in active if notes[s]["type"] in DURABLE}
print(f"roots (active durable) = {len(roots)}; active total = {len(active)}")
for depth in (1, 2, 3, 99):
    r = reach(roots, depth)
    cand = {s for s in active if notes[s]["type"] in NURSERY and s not in r}
    v = cand & KEEP
    u = cand & UNKNOWN
    bt = collections.Counter(notes[s]["type"] for s in u)
    print(
        f"R4 unreachable from roots within {depth:2d} hops: candidates={len(cand):4d} KEEP-violations={len(v):3d} new-UNKNOWN={len(u):4d} ({len(u) / len(hist_active):.0%} of active history) {dict(bt)}"
    )
r = reach(roots, 99)
cand = {s for s in active if notes[s]["type"] in NURSERY and s not in r} - KEEP
json.dump(
    sorted(cand), open(pathlib.Path(__file__).parent / "e1b_r4_selection.json", "w")
)
# What links INTO the reachable nursery? how deep are chains?
depths = {}
seen = set(roots)
frontier = set(roots)
d = 0
while frontier:
    nxt = set()
    for s in frontier:
        for l in notes[s]["links"]:
            if l in notes and l in active and l not in seen:
                seen.add(l)
                nxt.add(l)
                depths[l] = d + 1
    frontier = nxt
    d += 1
print(
    "depth histogram of reachable nursery notes:",
    sorted(
        collections.Counter(
            depths[s] for s in seen if s in depths and notes[s]["type"] in NURSERY
        ).items()
    ),
)
