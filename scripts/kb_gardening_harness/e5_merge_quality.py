"""E5 — merge quality: does an LLM synthesis of near-duplicate notes preserve
their claims? (kb_gardening §7 E5; the T2 MERGE verdict's safety.)

Clusters = union-find over MiniLM pairs in [0.85, 0.97) among active nursery
notes. Per cluster: (1) synthesise one note; (2) extract atomic claims per
member; (3) judge each claim entailed by the synthesis. Coverage = entailed /
claims. Uses `claude -p` as the model.
"""

from __future__ import annotations

import json
import pathlib
import random
import re
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus import load  # noqa: E402
from embed import embeddings  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "sonnet"
N_CLUSTERS = int(sys.argv[2]) if len(sys.argv) > 2 else 8
LO, HI = 0.85, 0.97
random.seed(11)
notes = load()
slugs, emb = embeddings(notes)
NURSERY = {"learning", "retrospective", "state"}
idx = {s: i for i, s in enumerate(slugs)}
sims = emb @ emb.T
np.fill_diagonal(sims, 0)
ii, jj = np.where((np.triu(sims, 1) >= LO) & (np.triu(sims, 1) < HI))
parent = {}


def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


for i, j in zip(ii, jj):
    a, b = slugs[i], slugs[j]
    if notes[a]["type"] in NURSERY and notes[b]["type"] in NURSERY and notes[a]["status"] == "active" and notes[b]["status"] == "active":
        parent[find(a)] = find(b)
clusters = {}
for s in parent:
    clusters.setdefault(find(s), []).append(s)
cands = [sorted(c) for c in clusters.values() if 2 <= len(c) <= 4 and sum(notes[s]["size"] for s in c) < 30000]
random.shuffle(cands)
sample = cands[:N_CLUSTERS]
print(f"clusters in band: {len(clusters)}, eligible (2-4 members, <30KB): {len(cands)}, sampled {len(sample)}; model={MODEL}", flush=True)


def ask(prompt):
    for attempt in range(3):
        p = subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--model", MODEL], capture_output=True, text=True, timeout=900)
        try:
            out = json.loads(p.stdout)
            res = out.get("result", "")
            m = re.search(r"\{.*\}", res, re.S)
            return json.loads(m.group(0)), float(out.get("total_cost_usd") or 0)
        except Exception as e:  # noqa: BLE001
            print("  parse failure", attempt, str(e)[:60], flush=True)
            time.sleep(3)
    return None, 0.0


def body(s, cap=6000):
    return re.sub(r"\n{3,}", "\n\n", notes[s]["body"])[:cap]


results = []
cost = 0.0
for k, cluster in enumerate(sample, 1):
    members = "\n\n".join(f"=== NOTE {s} (type {notes[s]['type']}) ===\n{body(s)}" for s in cluster)
    merge_prompt = (
        "You are consolidating a project knowledge base. Merge the notes below into ONE synthesis note in markdown. "
        "Preserve EVERY distinct factual claim, number, identifier, command, file path and decision from every member; "
        "drop only exact repetition. Keep it well structured; end with a 'Sources' line naming the member note ids.\n"
        'Answer with JSON only: {"synthesis": "<markdown>"}\n\n' + members
    )
    merged, c = ask(merge_prompt)
    cost += c
    if not merged:
        print(f"[{k}] merge failed", flush=True)
        continue
    synthesis = str(merged.get("synthesis") or "")
    claims_prompt = (
        "Extract the atomic factual claims from each note below (max 8 per note; concrete facts, numbers, decisions, "
        "identifiers — not vague summaries).\n"
        'Answer with JSON only: {"claims": [{"note": "<id>", "claim": "..."}]}\n\n' + members
    )
    claims, c = ask(claims_prompt)
    cost += c
    if not claims:
        print(f"[{k}] claims failed", flush=True)
        continue
    claim_list = [x for x in claims.get("claims", []) if isinstance(x, dict) and x.get("claim")]
    numbered = "\n".join(f"{i + 1}. [{x['note']}] {x['claim']}" for i, x in enumerate(claim_list))
    judge_prompt = (
        "Below is a synthesis note and a numbered list of claims from its source notes. For each claim decide whether the "
        "synthesis ENTAILS it (the fact is present, possibly reworded, with the same numbers/identifiers).\n"
        'Answer with JSON only: {"entailed": [true/false, ... one per claim in order]}\n\n=== SYNTHESIS ===\n'
        + synthesis + "\n\n=== CLAIMS ===\n" + numbered
    )
    judged, c = ask(judge_prompt)
    cost += c
    if not judged:
        print(f"[{k}] judge failed", flush=True)
        continue
    flags = [bool(x) for x in judged.get("entailed", [])][: len(claim_list)]
    cov = sum(flags) / max(1, len(flags))
    ratio = len(synthesis) / max(1, sum(len(body(s)) for s in cluster))
    lost = [claim_list[i]["claim"][:100] for i, f in enumerate(flags) if not f][:3]
    results.append({"cluster": cluster, "members": len(cluster), "claims": len(flags), "coverage": cov, "compression": ratio, "lost_examples": lost})
    print(f"[{k}] members={len(cluster)} claims={len(flags)} coverage={cov:.0%} compression={ratio:.2f} lost={lost[:1]}", flush=True)

if results:
    covs = [r["coverage"] for r in results]
    print(f"\nE5 summary: clusters={len(results)} mean coverage={np.mean(covs):.0%} min={min(covs):.0%} share>=0.95={sum(c >= 0.95 for c in covs)}/{len(covs)} mean compression={np.mean([r['compression'] for r in results]):.2f} cost=${cost:.2f}", flush=True)
json.dump(results, open(pathlib.Path(__file__).parent / f"e5_{MODEL}.json", "w"), indent=1)
