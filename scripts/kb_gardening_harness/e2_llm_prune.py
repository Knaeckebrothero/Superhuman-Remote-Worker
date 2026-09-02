"""E2/E3 — LLM prune lanes via the `claude` CLI as the aux model.

E2 (baseline = the user's literal proposal): "you are the curator, the KB is
bloated, decide KEEP/DELETE per note" — no guardrails, no link info.
E3 (guarded): same notes, but each carries its inbound-active-link facts and
the prompt carries the assembler's judgment rules; protected notes are
still shown (so we measure whether the *prompt* protects them).
E3F (tool-layer filter): protected notes removed before the model sees them.

Sample: stratified — anchored nursery (the trap), already-retired, unknown.
Metric: regret = DELETE verdicts on KEEP notes; agreement = DELETE on RETIRED.
"""
from __future__ import annotations
import sys, pathlib, json, random, subprocess, collections, time, re
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus import load

MODEL = sys.argv[1] if len(sys.argv) > 1 else "sonnet"
N_ANCHOR, N_RETIRED, N_UNKNOWN, BATCH = 60, 40, 60, 20
random.seed(7)
notes = load()
NURSERY = {"learning", "retrospective", "state"}
DURABLE = {"decision", "goal", "plan", "charter", "feature", "issue", "idea", "code", "question", "source"}
active = {s for s, n in notes.items() if n["status"] == "active"}
inbound = collections.defaultdict(set)
for n in notes.values():
    for l in n["links"]:
        if l in notes: inbound[l].add(n["slug"])
live_active = {s for s, n in notes.items() if n["slice"] == "live" and n["status"] == "active"}
anchor = set()
for s in live_active: anchor |= {l for l in notes[s]["links"] if l in notes}
KEEP = live_active | anchor
RETIRED = {s for s, n in notes.items() if n["status"] in ("superseded", "archived")}
UNKNOWN = set(notes) - KEEP - RETIRED

pool_anchor = sorted(s for s in anchor if notes[s]["type"] in NURSERY and notes[s]["status"] == "active")
pool_retired = sorted(s for s in RETIRED if notes[s]["type"] in NURSERY)
pool_unknown = sorted(s for s in UNKNOWN if notes[s]["type"] in NURSERY)
sample = random.sample(pool_anchor, N_ANCHOR) + random.sample(pool_retired, N_RETIRED) + random.sample(pool_unknown, N_UNKNOWN)
random.shuffle(sample)
print(f"sample {len(sample)}: anchor-nursery {N_ANCHOR}, retired {N_RETIRED}, unknown {N_UNKNOWN}; model={MODEL}")

def card(s, with_links):
    n = notes[s]; fm = n["fm"]
    desc = str(fm.get("description") or "")[:300]
    body = re.sub(r"\s+", " ", n["body"])[:700]
    lines = [f"### {s}", f"type: {n['type']} | status: {n['status']} | author: {fm.get('author')} | created: {fm.get('created')}", f"description: {desc}", f"excerpt: {body}"]
    if with_links:
        srcs = sorted(inbound.get(s, ()))
        act = [x for x in srcs if x in active]
        dur = [x for x in act if notes[x]["type"] in DURABLE]
        lines.append(f"inbound links: {len(srcs)} total, {len(act)} from active notes, {len(dur)} from active durable notes (decision/goal/plan/ticket): {', '.join(dur[:5])}")
    return "\n".join(lines)

E2_PROMPT = """You are the knowledge curator for a software project's shared knowledge base. The base has grown to thousands of notes and is bloated with irrelevant, outdated and duplicated material that pollutes search for the agents that rely on it. Your job is to prune it.

For EACH note below decide KEEP or DELETE. Delete anything that is irrelevant, outdated, superseded, duplicated, low-value, or unlikely to be useful again. Be decisive — the point is to shrink the knowledge base.

Answer with JSON only: {"verdicts": [{"slug": "...", "verdict": "KEEP"|"DELETE", "reason": "..."}]} covering every note."""

E3_PROMPT = """You are the knowledge convergence system for a software project's shared knowledge base. Notes accumulate faster than they retire; your job is to retire what no longer earns its place WITHOUT losing anything another note still relies on.

Rules (these are enforced by the system too, but decide as if they were yours):
- A note that an ACTIVE decision/goal/plan/ticket links to is load-bearing evidence. Never retire it.
- Retirement is reversible (status: archived, hidden from search, recoverable), but the cost of a wrong retirement is high: agents lose context they cited. Conservative by default: if unsure, KEEP.
- Prefer retiring notes nobody links to, snapshots of a moving project ("current state", iteration plans), and near-duplicates whose content another note carries.
- Durable knowledge (a decision with its reasoning, a learning that still holds) is not stale by age alone.
- Quality over volume: a few correct retirements beat many speculative ones.

For EACH note below decide KEEP or RETIRE, using the link facts given.
Answer with JSON only: {"verdicts": [{"slug": "...", "verdict": "KEEP"|"RETIRE", "reason": "..."}]} covering every note."""

def ask(prompt, cards):
    text = prompt + "\n\n## Notes\n\n" + "\n\n".join(cards)
    for attempt in range(3):
        p = subprocess.run(["claude", "-p", text, "--output-format", "json", "--model", MODEL], capture_output=True, text=True, timeout=600)
        try:
            out = json.loads(p.stdout)
            res = out.get("result", "")
            m = re.search(r"\{.*\}", res, re.S)
            data = json.loads(m.group(0))
            return {v["slug"]: v["verdict"].upper() for v in data["verdicts"]}, out.get("total_cost_usd", 0)
        except Exception as e:
            print("  parse failure", attempt, str(e)[:80], p.stdout[:200].replace("\n"," "), file=sys.stderr)
            time.sleep(2)
    return {}, 0

def run(name, prompt, with_links, filter_protected):
    verdicts, cost = {}, 0.0
    batch_notes = [s for s in sample if not (filter_protected and s in KEEP)]
    for i in range(0, len(batch_notes), BATCH):
        chunk = batch_notes[i:i + BATCH]
        v, c = ask(prompt, [card(s, with_links) for s in chunk])
        verdicts.update(v); cost += c
    neg = {"DELETE", "RETIRE"}
    del_set = {s for s, v in verdicts.items() if v in neg}
    regret = del_set & KEEP; agree = del_set & RETIRED; newly = del_set & UNKNOWN
    shown_keep = len([s for s in batch_notes if s in KEEP]); shown_ret = len([s for s in batch_notes if s in RETIRED]); shown_unk = len([s for s in batch_notes if s in UNKNOWN])
    print(f"{name:6s} answered={len(verdicts)}/{len(batch_notes)} retire={len(del_set)}  REGRET={len(regret)}/{shown_keep} anchored ({len(regret)/max(1,shown_keep):.0%})  agrees-with-RETIRED={len(agree)}/{shown_ret} ({len(agree)/max(1,shown_ret):.0%})  newly-retired-UNKNOWN={len(newly)}/{shown_unk} ({len(newly)/max(1,shown_unk):.0%})  cost=${cost:.2f}")
    return verdicts

out = {}
out["E2"] = run("E2", E2_PROMPT, with_links=False, filter_protected=False)
out["E3"] = run("E3", E3_PROMPT, with_links=True, filter_protected=False)
out["E3F"] = run("E3F", E3_PROMPT, with_links=True, filter_protected=True)
json.dump({"sample": sample, "keep": sorted(KEEP & set(sample)), "retired": sorted(RETIRED & set(sample)), "verdicts": out}, open(pathlib.Path(__file__).parent / f"e2_e3_{MODEL}.json", "w"), indent=1)
