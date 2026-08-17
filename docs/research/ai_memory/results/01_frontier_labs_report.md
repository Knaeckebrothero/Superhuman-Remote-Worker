# Brief 01 — How Frontier Labs & Commercial Memory Products Implement AI Memory
## Synthesized findings report

**Provenance:** Deep-research workflow run on **2026-06-04** (run id `wf_f5bcd7eb-b33`,
task `wwr8t72ra`) against `../01_frontier_labs_and_products.md`.
**Run stats:** 5 search angles · 23 sources fetched · 114 claims extracted →
25 verified → **17 confirmed / 8 killed** → 11 synthesized findings · 105 agents · ~3M tokens.
**Raw output (full findings, refuted claims, sources, caveats):** `01_frontier_labs_raw.json`

> ⚠️ **Two load-bearing caveats.**
> 1. **Coverage came in narrower than the brief.** Primary-sourced verification landed on
>    **Anthropic memory tool, ChatGPT, Claude Code, Cursor, Windsurf, Mem0**. **No surviving
>    verified claims** for Gemini, Copilot, Meta/Perplexity/Grok, Devin/Replit/GitHub Copilot,
>    or dedicated products **Zep/Graphiti, Letta, Cognee, LangMem, Supermemory, Memobase,
>    MemoryOS**. Letta's 4 claims and *every* benchmark number failed adversarial verification —
>    "unverified," not "disproven" (the verify phase had many agent failures this round).
> 2. **No performance/benchmark numbers are citable from this run** (Mem0 LOCOMO self-score,
>    LongMemEval, Memora all failed verification). This is a read on *architecture & design
>    patterns*, not a leaderboard.

---

## 1. Per-system breakdown

| System | Storage | Who decides the write | Retrieval | Injection | Forgetting | Doc vs inferred |
|---|---|---|---|---|---|---|
| **Claude memory tool** (API) | Client-side files in `/memories` (you back it with anything) | **Model** (tool calls: view/create/str_replace/…) | Model reads specific files on demand (file-level, no intra-file search) | Small auto-injected "**view memory first**" protocol; details read JIT | Model deletes; pairs w/ context editing | Documented |
| **ChatGPT** | "Saved memories" store + chat-history index | **Hybrid**: auto *and* explicit "remember this" | Saved memories + implicit recall over past chats | Injected into system context | User edit/delete; toggles | Saved=doc; chat-history retrieval=**inferred** |
| **Claude Code** | `CLAUDE.md` (human) + agentic `MEMORY.md`+topic files (model) | `CLAUDE.md`=you; auto-memory=**model** | `CLAUDE.md` full at launch; **MEMORY.md capped 200 lines/25KB**; topic files **on demand** | Bounded index always-on + JIT reads | Manual edit | Documented |
| **Cursor** | Version-controlled `.cursor/rules/*.mdc` | **Human** (you author rules) | 4 activation types (Always / Intelligent / glob / @-mention) | Rule body **prepended at start of context** when triggered | Edit/delete files | Doc (assembly order partly inferred) |
| **Windsurf Cascade** | Auto-memories (local) + version-controlled Rules/`AGENTS.md` | Auto-memory=**model** (+ "create a memory of…"); Rules=human | Cascade auto-retrieves memories it judges relevant | Mixed | Docs **steer you to Rules** b/c auto-memory persists false info | Documented |
| **Mem0** | Dense **vector** base + **Mem0ᵍ graph** variant (Neo4j triplets) | **Backend memory LLM** picks ADD/UPDATE/DELETE/NOOP per fact | Top-s (s=10) on query | Retrieved slice on query — **not all** | LLM-chosen DELETE/UPDATE | Documented (arXiv) |
| **— Ours —** | pgvector (dense+sparse+recency RRF) + Neo4j typed KB | **Background observer LLM** (behind the agent's back) | Hybrid RRF top-k + KB top-5 | **Full slice into *every* call** | TTL + assembler | — |

---

## 2. Deep-dives (the four most instructive)

**Claude Code auto-memory — most directly relevant.** Two systems: human `CLAUDE.md` (loaded
*in full*) and an agentic `MEMORY.md`+topic-files store the model writes itself. The load-bearing
detail: **only the first 200 lines / 25 KB of `MEMORY.md` load at session start; topic files are
read on demand.** Sharpest documented instance of "small always-injected index + just-in-time
detail," and the cleanest answer to *"am I an outlier auto-injecting everything?"* — yes.
(This is the same pattern as the `MEMORY.md` index this project maintains.)

**Anthropic memory tool — agentic + context-editing as a pair.** Model-directed, client-side file
ops. When enabled, Anthropic *auto-injects* a fixed protocol ("ALWAYS VIEW YOUR MEMORY DIRECTORY
FIRST… ASSUME INTERRUPTION: your context may reset"). Designed to pair with **context editing**
(clear oldest/stale tool results, with an automatic warning to *save essentials to memory before
the clear*) and compaction — memory is the durable layer beneath a bounded, self-pruning window.

**ChatGPT — the canonical hybrid.** Two genuinely different mechanisms: discrete **saved memories**
(written both automatically *and* on explicit "remember this") and an implicit **chat-history
reference** layer that draws on past chats even when nothing was saved. Saved-memory behavior is
documented; the retrieval/injection internals of the implicit layer are reverse-engineered
(Willison, Embrace The Red), not disclosed.

**Mem0 — our closest architectural twin.** Two-phase extract→update pipeline where an LLM picks
ADD/UPDATE/DELETE/NOOP per fact via a tool call (model-*directed write*, but on the
automatic-pipeline side — a backend LLM, not the user-facing agent). Storage is dual: dense-vector
base + **Mem0ᵍ graph (Neo4j entity/relation triplets)** — *the same vector+graph split we run.*
The difference that matters: Mem0 retrieves **s=10 on query**; we inject everything every turn.
Our retrieval (dense+sparse+recency RRF) is actually *more* sophisticated than Mem0's plain similarity.

---

## 3. Patterns & takeaways

**Converging:**
1. **Model-directed writes are the default** — the model (or a backend memory LLM) decides what to persist, via tools/files.
2. **Always-inject only a small bounded slice; pull the rest just-in-time.** *Nobody verified injects their full store every call.* Claude Code caps at 200 lines; Mem0 fetches s=10; the memory tool reads files on demand.
3. **Prefer explicit, version-controlled rules over auto-memory when reuse must be reliable** — *both* Windsurf and Cursor say this in their own docs.
4. **Pair durable memory with context editing/compaction** so the working window stays bounded.

**Contested / common complaints:** auto-memories are widely reported as unreliable, workspace-scoped,
and capable of **persisting false information** (Windsurf's own docs warn this); ChatGPT's automatic
profiling draws **over-remembering / privacy** criticism. This is *why* vendors push explicit rules
and user controls.

---

## 4. What to adopt — ROI-ranked, mapped to our stack

**① Bound the always-injected slice + add an on-demand recall tool.** *(highest ROI — fixes the one place we're an outlier)*
The precise lesson isn't "stop injecting memory always-on" — even Letta (MemGPT), the one system
built around compiling memory into context *every step*, keeps those blocks **small, bounded, and
model-editable** (widely understood, but its claims didn't survive this verification round — treat as
a flag, not a fact). The norm is: **inject a small bounded index, expose the full RecallStore + KB as
a recall tool the agent calls when it needs detail.** Today we push the full RRF top-k + KB top-5 into
*every* call — biggest token-cost and signal-to-noise win available.

**② Context-editing with save-before-clear for long jobs.** Adopt the Anthropic pattern: clear stale
tool results from the window but persist essentials to the KB/memory first. Fits our multi-phase jobs
and complements existing compaction.

**③ Add an explicit "remember this" write path** alongside the background observer. ChatGPT, Cascade,
and Claude Code *all* run human/agent-directed writes *and* auto-extraction. We only have the
behind-the-back observer.

**④ Lean into the KB over auto-memories for must-reuse facts.** Per Windsurf/Cursor's own guidance,
explicit durable notes beat auto-extracted memories when reliability matters. We already have the typed
Neo4j KB — promote it as the authoritative tier; treat RecallStore as the softer, decaying tier.

**Already ahead:** hybrid dense+sparse+recency RRF (more than Mem0's plain similarity) and a typed graph
mirrored to vectors (more elaborate than most single-store products). *Caveat: "more sophisticated" ≠
"better outcomes" until measured — that's brief 05.*

**Scorecard:** *Aligned* with the Mem0/automatic-pipeline + hybrid-graph camp on storage & extraction ·
*Ahead* on retrieval sophistication · *Behind* on model-directed control and context editing ·
*Outlier* on full-every-turn injection.

---

## 5. Open questions (from the run)
- **Measured token-cost + recall/precision delta** of full-inject vs. a bounded-index + on-demand-recall design for *our* workload — only instrumentation can settle it (→ brief 05).
- How do **Letta/MemGPT** (memory blocks compiled into context every step), **Zep/Graphiti** (temporal KG), and **Cognee** handle inject-everything vs on-demand? Letta is the closest analogue to our design but didn't survive verification — re-research.
- **ChatGPT's / Gemini's actual retrieval+injection internals** for the implicit chat-history layer (semantic? recency? full-scan? token budget?) — undocumented, only reverse-engineering exists.
- Does any system use a **typed knowledge graph as the primary always-on memory** (vs a vector store), and how do they bound graph-context injection? No verified source showed a graph used as an always-fully-injected memory.

## 6. Killed / unverified this round (do NOT cite)
- Mem0 LOCOMO self-benchmark (Mem0=66.88 / Mem0ᵍ=68.44 vs OpenAI=52.90) — vendor self-benchmark, 0-0.
- Letta: memory-blocks structure, agentic writing, inject-every-step, sleep-time compute — all 0-0 (unverified, not disproven).
- LongMemEval definition + ~30% multi-session accuracy drop — 0-0.
- Memora benchmark definition — 0-0.

## 7. Citations (primary)
- Anthropic memory tool — https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Anthropic context editing — https://platform.claude.com/docs/en/build-with-claude/context-editing
- Claude Code memory — https://code.claude.com/docs/en/memory
- ChatGPT memory — https://help.openai.com/en/articles/8983136-what-is-memory · https://openai.com/index/memory-and-new-controls-for-chatgpt/
- Cursor rules — https://cursor.com/docs/rules
- Windsurf Cascade memories — https://docs.windsurf.com/windsurf/cascade/memories
- Mem0 — https://arxiv.org/pdf/2504.19413
