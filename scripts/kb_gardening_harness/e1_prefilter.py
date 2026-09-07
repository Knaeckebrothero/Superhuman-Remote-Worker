"""E1 — deterministic prefilter (no LLM) over the Better Resavio dump.

Ground truth classes:
  KEEP    = live-slice active notes + every note linked from a live-active note (anchor)
  RETIRED = status superseded/archived (a past human/agent retirement decision)
  UNKNOWN = everything else (active nursery material)
Per rule we report: selected, KEEP violations (must be 0 for an automatic lane),
RETIRED agreement (precision proxy), UNKNOWN volume (what it would newly retire).
"""

from __future__ import annotations
import sys
import pathlib
import re
import hashlib
import collections
import json
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus import load
from embed import embeddings

NURSERY = {"learning", "retrospective", "state"}
MOVING = {"state", "goal", "plan", "question"}
PROTECTED_TYPES = {"charter", "feature", "issue", "idea", "report"}

notes = load()
slugs, emb = embeddings(notes)
idx = {s: i for i, s in enumerate(slugs)}

inbound = collections.defaultdict(set)  # target -> set(source)
for n in notes.values():
    for l in n["links"]:
        if l in notes:
            inbound[l].add(n["slug"])
active = {s for s, n in notes.items() if n["status"] == "active"}
inbound_active = {
    s: {src for src in srcs if src in active} for s, srcs in inbound.items()
}

live_active = {
    s for s, n in notes.items() if n["slice"] == "live" and n["status"] == "active"
}
anchor = set()
for s in live_active:
    anchor |= {l for l in notes[s]["links"] if l in notes}
KEEP = live_active | anchor
RETIRED = {s for s, n in notes.items() if n["status"] in ("superseded", "archived")}
UNKNOWN = set(notes) - KEEP - RETIRED
print(
    f"classes: KEEP={len(KEEP)} RETIRED={len(RETIRED)} UNKNOWN={len(UNKNOWN)} (overlap keep&retired={len(KEEP & RETIRED)})"
)


def report(name, selected, detail=""):
    sel = set(selected)
    v = sel & KEEP
    r = sel & RETIRED
    u = sel & UNKNOWN
    by_type = collections.Counter(notes[s]["type"] for s in u)
    print(
        f"{name:34s} selected={len(sel):5d}  KEEP-violations={len(v):4d}  agrees-with-RETIRED={len(r):4d} ({len(r) / max(1, len(RETIRED)):.0%} of retired)  new-UNKNOWN={len(u):4d} {dict(by_type.most_common(4))} {detail}"
    )
    return sel


def norm_body(b):
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", b.lower())).strip()


# R0 exact duplicate bodies → retire all but the oldest (oldest = has 'created' earliest, else lexical slug)
groups = collections.defaultdict(list)
for s, n in notes.items():
    groups[hashlib.sha1(norm_body(n["body"]).encode()).hexdigest()].append(s)
r0 = []
for g in groups.values():
    if len(g) > 1:
        g.sort(key=lambda s: (str(notes[s]["fm"].get("created") or "9"), s))
        r0 += g[1:]
report(
    "R0 exact-dup (keep oldest)",
    r0,
    f"groups={sum(1 for g in groups.values() if len(g) > 1)}",
)

# R1 near-dup at thresholds; retire the member with fewer active inbound links, tie → newer
sims = emb @ emb.T
np.fill_diagonal(sims, 0)


def near_dup(thr):
    ii, jj = np.where(np.triu(sims, 1) >= thr)
    losers = set()
    for i, j in zip(ii, jj):
        a, b = slugs[i], slugs[j]
        ka = (
            len(inbound_active.get(a, ())),
            -len(notes[a]["links"]),
            notes[a]["fm"].get("created") is None,
            a,
        )
        kb = (
            len(inbound_active.get(b, ())),
            -len(notes[b]["links"]),
            notes[b]["fm"].get("created") is None,
            b,
        )
        # loser = fewer inbound active links; tie: fewer outbound; tie: missing created; tie: lexical newer
        loser = a if ka < kb else b
        losers.add(loser)
    return losers


for thr in (0.99, 0.97, 0.95, 0.90):
    report(f"R1 near-dup >= {thr} (MiniLM proxy)", near_dup(thr))


# R2 same normalised title within moving-target types → keep the one with most inbound-active, retire others
def norm_title(t):
    t = t.lower()
    t = re.sub(r"[#`*_]", " ", t)
    t = re.sub(r"\b(iter(ation)?|phase|cycle|job|v)\s*-?\s*\d+\b", " ", t)
    t = re.sub(r"\d{4}-\d{2}-\d{2}(t\d+)?", " ", t)
    t = re.sub(r"\d+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


tg = collections.defaultdict(list)
for s, n in notes.items():
    if n["type"] in MOVING:
        tg[(n["type"], norm_title(n["title"]))].append(s)
r2 = []
for g in tg.values():
    if len(g) > 1:
        g.sort(
            key=lambda s: (
                -len(inbound_active.get(s, ())),
                str(notes[s]["fm"].get("created") or "0"),
            ),
            reverse=False,
        )
        # keep the best-linked (first after sort by -inbound asc? fix: sort desc by inbound)
        g.sort(
            key=lambda s: (
                len(inbound_active.get(s, ())),
                str(notes[s]["fm"].get("created") or "0"),
            ),
            reverse=True,
        )
        r2 += g[1:]
report(
    "R2 same-title moving-target twins",
    r2,
    f"groups={sum(1 for g in tg.values() if len(g) > 1)}",
)

# R3 orphan nursery: nursery type, zero inbound links from ACTIVE notes, not protected type
r3 = [
    s
    for s, n in notes.items()
    if n["type"] in NURSERY
    and not inbound_active.get(s)
    and n["type"] not in PROTECTED_TYPES
]
report("R3 orphan nursery (no active inbound)", r3)
# R3' stricter: zero inbound from ANY note
r3b = [s for s in r3 if not inbound.get(s)]
report("R3' orphan nursery (no inbound at all)", r3b)

# Union of the automatic lanes proposed in the design: R0 + R1@0.97 + R2
auto = set(r0) | near_dup(0.97) | set(r2)
report("AUTO = R0 ∪ R1@0.97 ∪ R2", auto)
auto_strict = auto - KEEP
print(
    f"AUTO after G5 protection (drop anchor/live): {len(auto_strict)} of which already retired {len(auto_strict & RETIRED)}, newly retired {len(auto_strict & UNKNOWN)} = {len(auto_strict & UNKNOWN) / len(UNKNOWN):.0%} of UNKNOWN"
)

# what share of currently-active history notes would be left?
hist_active = {
    s for s, n in notes.items() if n["slice"] == "history" and n["status"] == "active"
}
print(
    f"history active notes: {len(hist_active)}; AUTO would retire {len(auto_strict & hist_active)} ({len(auto_strict & hist_active) / len(hist_active):.0%}); R3 would retire {len(set(r3) & hist_active)} ({len(set(r3) & hist_active) / len(hist_active):.0%})"
)
json.dump(
    {"auto": sorted(auto_strict), "r3": sorted(set(r3) - KEEP)},
    open(pathlib.Path(__file__).parent / "e1_selection.json", "w"),
)
