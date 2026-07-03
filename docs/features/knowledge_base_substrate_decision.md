---
tags:
  - data-management
  - knowledge-management
  - context-management
  - agent-architecture
status: open
created: 2026-07-02
related:
  - "[[database_architecture]]"
  - "[[agent_memory_overhaul]]"
  - "[[no_workspace_agent_mode]]"
  - "[[okf_knowledge_base]]"
---

# Knowledge Base Substrate: OKF vs Postgres vs Neo4j — Findings & Decision

**Status:** OPEN — the architecture below is proposed; one roadmap question gates it (see §8).
**Date:** 2026-07-02
**Origin:** Design discussion triggered by Google's launch of the Open Knowledge Format
(OKF) and Karpathy's "LLM Wiki" pattern. Captures the reasoning, the two empirical
investigations we ran against the dev cluster, and an external literature check.

---

## TL;DR

- **OKF is not a competitor to our KB — it's a serialization/interchange format.** It
  standardizes the *on-disk* shape of an LLM-maintained markdown wiki (Karpathy's
  pattern). It deliberately says nothing about retrieval, freshness, or curation — the
  hard layer we already built (RecallStore).
- **Three roles must stay separate:** *source of truth*, *retrieval substrate*,
  *interchange*. Our old `docs/` Obsidian vault tried to be all three and drifted; that's
  the root failure, not "markdown bad."
- **Our three scalability pains (granularity, org-scale, "no `WHERE NOT IN`") are one
  pain:** the filesystem has no relational semantics — no write-time integrity, no
  transactions, no non-text indexing. That argues for *a database*, not necessarily a
  *graph* database.
- **Empirically, our Neo4j is not doing graph work.** Static analysis: of ~40 Cypher
  statements, only 2 are genuinely graph-shaped, both shallow bounded traversals (≤3 and
  ≤10 hops); zero `gds.*`/`shortestPath`. Dynamic analysis: 0 Neo4j datasources on the
  cluster, 0 `cypher_*` tool calls in the audited jobs; agents read the KB via
  `search_knowledge`/`kb_write` (retrieval), never via traversal.
- **The literature says graph queries *can* help and agents *can* write them — with
  asterisks.** Frontier text-to-Cypher accuracy ~60% (2024) → ~77% (2025-26), worst on
  multi-hop. Graph wins ~15% of queries (multi-hop / global-sensemaking / temporal);
  vector wins the rest. Agents **default to search and won't traverse without
  scaffolding**. Our highest-value graph slice (temporal supersede) is already done
  relationally in RecallStore.
- **Proposed:** Postgres as the default KB substrate; OKF as the export/interchange skin
  (already ~half-built via `get_all_notes_for_export`); Neo4j as an opt-in tier for a
  genuine graph workload.
- **Open decision (§8):** is there a multi-hop / global-sensemaking / citation-graph query
  class on the near roadmap frequent enough to justify building the router +
  text-to-Cypher scaffolding that agents need to actually exploit a graph? If yes, invest
  in Neo4j *and the scaffolding*. If no, ship Postgres + OKF and let Neo4j go dormant.

---

## 1. Trigger & question

Google Cloud published **Open Knowledge Format (OKF) v0.1** on 2026-06-12 — a
vendor-neutral spec that freezes the "LLM wiki" pattern (Karpathy's gist, 2026-04-04) into
a portable standard. We had independently run this pattern for ~1 year (our `docs/` vault),
and it was the template for the product KB. The question: **now that the pattern is being
standardized, what should the project-level knowledge base actually be built on —
markdown/OKF files, a relational DB, or a graph DB?**

### What OKF actually is (so we don't strawman it)

A directory of markdown files, **one file = one concept**, file path = identity. YAML
frontmatter with exactly **one required field: `type`**; reserved optionals
`title/description/resource/tags/timestamp`. Concepts cross-link with **standard markdown
links**, so the directory is an emergent graph. Optional `index.md` (progressive
disclosure) and `log.md` (change history). The whole spec "fits on a page." Its bet is
**interop** ("every team that adopts it makes every other team's agents smarter"), not
technical novelty — and codifying the constraints that keep a vault from drifting.

---

## 2. The three roles a KB plays (don't conflate them)

| Role | Question it answers | Best medium |
|---|---|---|
| **Source of truth** | Where do writes land? Where does integrity live? | A database (constraints, transactions) |
| **Retrieval substrate** | What does the LLM query at run time? | Vector + relational index (RecallStore) |
| **Interchange** | How does knowledge leave the system (human read, git history, customer handoff, foreign agent)? | Files — **OKF** |

The `docs/` vault (and any pure-markdown KB) tries to be all three at once. That's why it
hurt at scale. Separating them is the key move of this whole analysis.

---

## 3. Why a filesystem / markdown vault breaks at scale

The three pains we hit are all the same underlying gap — **the filesystem has no
relational semantics.** Raw size is *not* the problem (git holds the Linux kernel, ripgrep
scans a million files sub-second, GitHub is planet-scale markdown-with-links). What a
filesystem genuinely can't give:

1. **Write-time integrity.** It can't *prevent* a duplicate concept — only let you grep for
   it afterward ("two docs, same name"). A `UNIQUE` constraint refuses the bad write.
2. **Transactions.** Karpathy's "touch 15 files in one pass" is sold as a strength, but for
   an **interruptible agent** (we freeze / hard-interrupt / pkill mid-turn) a 15-file
   update that dies at file 7 is a torn write with no rollback. A DB transaction makes it
   atomic-or-nothing.
3. **Non-text indexing / set algebra.** The `done/`-folder relevance rot is the tell:
   "exclude everything superseded" is `WHERE status != 'superseded'`, indexed. On a
   filesystem it's a hand-rolled `grep -vf exclude.txt` the LLM must reconstruct correctly
   *every query* — and won't.

**Conclusion:** the pains argue for *a database*. They do **not** by themselves argue for a
*graph* database — dedup, anti-joins, superseded-filtering, uniqueness are all vanilla SQL.

---

## 4. Lived evidence: our own `docs/` Obsidian vault

Snapshot (2026-07-02): **417 markdown files** — `features/` 119, `issues/` 70, `done/` 85,
root 87. It is an **Obsidian vault**: `[[wikilinks]]` in **225** files vs standard
`[](path.md)` links in only **60**; a 25-tag taxonomy; `aliases:`/`related:` frontmatter;
`features → issues → done` as the lifecycle spine.

What a year of running the pattern actually demonstrated:

- **We made the two choices OKF forbids — rightly, for solo use.** `[[wikilinks]]` (Obsidian
  lock-in, don't render on GitHub) over portable markdown links; and **no `type` field** —
  we encode type as *folder + a flat tag folksonomy*. Both are locally optimal, globally
  non-portable. Fine while one human + one tool read it; debt the moment knowledge must
  leave the tool.
- **The taxonomy rotted anyway.** `tag_taxonomy.md` says "based on analysis of 48 markdown
  documents" and "full counts to be computed after tagging all documents." We're at 417;
  ~half (203/417) have no frontmatter. **Having an LLM *author* the docs did not keep the
  taxonomy coherent** — nobody ran the ingest+lint loop. The artifact shape doesn't
  self-maintain. (OKF doesn't fix this either; it's a format, not a maintenance loop. The
  loop is what RecallStore's write-gate + Assembler actually built.)
- **The flat index blew its budget.** The sibling agent-memory index (`MEMORY.md`) is
  ~41 KB against a 24 KB limit — no progressive disclosure. OKF's hierarchical
  `index.md`-per-directory is the direct fix, and the one piece of OKF worth stealing
  regardless of the substrate choice.

---

## 5. Empirical investigation — is our Neo4j actually doing graph work?

Method: don't argue from intuition. The ground truth is the **shape of the Cypher we
actually issue**, from two angles.

There are **two distinct Neo4j usages** and they must not be conflated:
1. **System-managed project KB** (`src/services/knowledge_graph.py`) — orchestrator writes
   `Note`/`Tag`/`Keyword` nodes + links via its own connection.
2. **Agent-facing graph datasource** (`cypher_query`/`cypher_execute` tools,
   `src/tools/graph/neo4j.py`) — arbitrary Cypher, only when a `neo4j` datasource is
   attached.

### 5.1 Static — the Cypher the system emits

Schema: nodes `Note`, `Tag`, `Keyword`; relationships `TAGGED`, `HAS_KEYWORD`,
`CONTRADICTS`, `DERIVED_FROM`, `ANSWERS`, plus generic note→note links.

Of ~40 Cypher statements, **exactly two are genuinely graph-shaped:**

| Function | Location | Query | Verdict |
|---|---|---|---|
| `get_related` | `knowledge_graph.py:531` | `MATCH path = (start)-[*1..max_hops]-(related:Note)`, **`max_hops` capped at 3** ("avoid expensive traversals") | bounded neighborhood — CTE-able |
| `get_provenance` | `knowledge_graph.py:582` | `-[:DERIVED_FROM*1..min(max_depth,10)]->` | bounded single-edge chain — CTE-able |

Everything else is relational wearing Cypher syntax:
- `MATCH (n:Note {project_id, id})` — point lookup by PK.
- `(n)-[:TAGGED]->(t:Tag)` / `(n)-[:HAS_KEYWORD]->(k)` — single join to a junction table.
- `(n)-[r]->(m:Note)` and reverse — one-hop links / backlinks (self-join).
- `(a)-[:CONTRADICTS]->(b)` — one hop.
- **`get_unanswered` (`:605`)** — `WHERE NOT ()-[:ANSWERS]->(q)`. This is literally the
  "`WHERE NOT IN`" we said filesystems lack — i.e. `WHERE NOT EXISTS (...)`, textbook SQL.

**Tell-tale absence:** zero `shortestPath`, zero `gds.*` (no graph-data-science — pagerank,
community, centrality), zero unbounded traversal. The two real traversals are shallow,
bounded, single-edge-type — the exact sweet spot for a Postgres `WITH RECURSIVE` CTE over a
`note_links(src, dst, rel_type)` table.

Agent-facing graph tools = `cypher_query`, `cypher_execute`, `get_database_schema`; they
require a `neo4j` datasource in `ToolContext` (`src/tools/registry.py:414`).

**Bonus:** `get_all_notes_for_export` (`knowledge_graph.py:621`) already dumps the KB to
Obsidian markdown. **The OKF-export skin is ~half-built** — re-point it (wikilinks →
markdown links, add a `type:` field — notes are already typed, e.g. `state`, `question`),
write into the project repo.

### 5.2 Dynamic — the audit trail (dev cluster, real user projects)

- **Datasources:** 9 total — 7 `webdav`, 2 `repository`, **0 `neo4j`**. The agent graph
  tools can't instantiate without a `neo4j` datasource → **they were never loaded for any
  job on this cluster.** Categorical: the capability was never switched on.
- **Audit trail (`search_audit`, per-job):**
  - *Positive control* — job `5ac90405` (scholar), query `todo_complete` → **20 hits** with
    full tool-call detail. The trail is searchable; a zero means zero.
  - *Target* — `cypher` in `5ac90405` (scholar) → **0**; `cypher` in `e1ede3c0`
    (developer) → **0**; `MATCH (` in `833d52b5` (scholar, different project) → **0**. No
    Cypher tool call or raw Cypher argument across 3 jobs / 2 experts / 2 projects.
  - *How agents actually use the KB* — the same trail shows heavy KB use ("REVIEW PROJECT
    KNOWLEDGE: search the knowledge base…", "persist as KB `state` note", "KB archaeology")
    via **`search_knowledge` / `kb_write`** — retrieval, never traversal.

**Caveats (honest):** `search_audit` is per-job (no global filter), so the audit search is
a *sample* (3 of 262 jobs) — but the load-bearing evidence is categorical (0 datasources →
tool cannot load). And this is one cluster (dev, but with the real user projects, so
representative). If prod ever attaches a `neo4j` datasource, revisit.

**Verdict:** both angles agree — **Neo4j is running as infrastructure that nothing
exercises as a graph.** The KB is a *retrieval* workload wearing a graph engine.

---

## 6. External evidence — can / do agents benefit from graph queries?

Motivation: our empty trail is confounded — it can't distinguish "agents don't benefit"
from "our system never gave them the capability or incentive." Checked the outside
literature (2025-26).

**1. Can agents author graph queries? Yes, imperfectly — worst where graphs pay off.**
CypherBench (real-scale KGs): 2024 frontier ~**60-61%** execution accuracy (Claude 3.5
Sonnet 61.6%, GPT-4o 60.2%), open-source ~42%, sub-10B <20%. Near-perfect on simple
single-entity queries; **worst on complex 7-node multi-hop**, with *semantic* failures
(wrong graph match, reversed edges), not syntax. Newer work (multi-agent Text-to-Cypher,
Nov 2025) reports ~**77%**. Capable and improving, still ~1-in-4 wrong on the multi-hop
queries that justify a graph.

**2. Do they benefit? Yes — on a minority query class.** Graph wins **multi-hop &
global-sensemaking**; vector wins single-hop / detail lookups. Sharpest data point: KRAGEN
(1.6M-edge biomedical KG) **94.2% vs 49.9%** on multi-hop medical reasoning. Production
consensus routing split: **~80% vector / ~15% graph / ~5% agentic** — graph earns its keep
on ~15% of traffic; recommended architecture is hybrid-with-a-router. (Routing splits are
practitioner-blog figures — directional, not gospel; KRAGEN + arXiv are firmer.)

**3. Do they reach for it spontaneously? No — it's harness-determined.** Agents default to
vector/lexical search even when structured tools exist; "Is Grep All You Need?" (May 2026)
is built on the thesis that scaffolding — tool exposure, prompting, routing —
*fundamentally decides* whether agents fall back to grep or use richer retrieval. So our
null result is genuinely confounded, **and** the fix isn't "turn Neo4j on" — it's "build
the router + text-to-Cypher path," because the capability must be *driven*.

**4. The slice that already pays — and we already have it.** The best-documented graph-
*memory* win is **temporal**: Zep/Graphiti (bi-temporal KG) beats Mem0 **63.8% vs 49.0% on
LongMemEval**, its edge being "model the state change when a fact updates." That is exactly
the **bi-temporal supersede** logic RecallStore shipped in Phase 4 — relationally, without
Neo4j. (Graphiti pays with ~600k tokens/conversation and retrieval that only works after
background graph-building settles.)

---

## 7. Synthesis — proposed architecture (three tiers, one knob)

Not "markdown vs graph." Keep a DB as source of truth + retrieval; make OKF the interchange
skin; reserve the graph for a genuine graph workload.

- **Default — Postgres-backed KB ⇄ OKF export to the project repo.** Port
  `Note`/`Tag`/`Keyword`/link to `notes` + `note_tags` + `note_links` with two recursive
  CTEs (covers `get_related` + `get_provenance`). Native uniqueness, constraints,
  transactions, anti-joins — kills the §3 pains. `search_knowledge`/`kb_write` tool
  signatures don't change (backend swap, not UX change). OKF/git gives versioning,
  human-readable audit, portability, customer handoff — re-point
  `get_all_notes_for_export`.
- **Lite (opt-in) — pure OKF, md + git, no DB.** Zero-infra / air-gapped / thesis-demo /
  human-authored, where a human is still the primary reader. Agent navigates files
  Karpathy-style; honest about the ceiling.
- **Graph (opt-in, helm/flag) — Neo4j**, for a genuinely edge-heavy workload — multi-hop
  relationship reasoning, global sensemaking, and/or the **citation network at scale**
  (`claim → supported-by → source → cites → source`), which *is* genuinely graph-shaped.
  If enabled, invest in the **whole path** (populated graph + query router + text-to-Cypher
  ~75-80% correct), because agents won't traverse spontaneously.

Steal-regardless: **hierarchical `index.md` / progressive disclosure** over a single flat
index (fixes the §4 index-budget problem in both `docs/` and the KB).

---

## 8. The open decision (this gates §7)

> **Is there a multi-hop / global-sensemaking / citation-graph query class on the near
> roadmap, frequent enough to justify building the routing + text-to-Cypher scaffolding
> agents need to actually exploit a graph?**

> **Follow-up (2026-07-03):** [[okf_knowledge_base]] proposes answering this **NO for the
> KB workload** (Neo4j dormant there; citation network stays the open case) — and flips
> this doc's default tier to files-canonical (OKF repo as source of truth, Postgres as a
> disposable git-watermarked index), because KB-as-*datasource* (org wikis, human-edited
> vaults) forces file-first anyway.

- **If YES** — keep Neo4j central as the opt-in tier *and* build the scaffolding; the
  literature says it'll pay on that query class. The citation network is the leading
  candidate.
- **If NO** — ship Postgres + OKF-export; let Neo4j go dormant (it's currently a live
  dependency serving zero graph queries; removable from the default deployment, keep the
  helm switch).

Everything upstream of this question is settled by the evidence; only this remains.

---

## 9. Concrete next steps (when unblocked)

1. Answer §8 (roadmap owner).
2. Sketch the Postgres schema: `notes`, `note_tags`, `note_links(src, dst, rel_type)` +
   the two `WITH RECURSIVE` CTEs replacing `get_related` / `get_provenance`.
3. Re-point `get_all_notes_for_export` to emit OKF (markdown links + `type:` frontmatter)
   into the project repo; add hierarchical `index.md`.
4. Decide Neo4j's fate in the default helm values (dormant vs removed-with-switch).

---

## Appendix A — references

- Google Cloud, "How the Open Knowledge Format can improve data sharing" — OKF v0.1 spec
  (`GoogleCloudPlatform/knowledge-catalog`).
- Karpathy, "LLM Wiki" gist (2026-04-04).
- CypherBench — arXiv 2412.18702 (text-to-Cypher accuracy on real-scale KGs).
- Multi-Agent GraphRAG / Text-to-Cypher — arXiv 2511.08274 (~77% linear-pass).
- "Is Grep All You Need? How Agent Harnesses Reshape Agentic Search" — arXiv 2605.15184.
- GraphRAG vs Vector RAG head-to-head (TianPan, 2026-04-19); KRAGEN multi-hop (SingleStore).
- Mem0 vs Zep/Graphiti, LongMemEval (Vectorize, 2026).

**Internal code references:** `src/services/knowledge_graph.py` (`get_related:531`,
`get_provenance:582`, `get_unanswered:605`, `get_all_notes_for_export:621`);
`src/tools/graph/neo4j.py`; `src/tools/registry.py:414`.

---

## Appendix B — Postgres schema & CTE sketch (draft for §9.2)

> Draft to replace the `Note`/`Tag`/`Keyword` Neo4j model with a relational one. **Not yet
> tested**; types/indexes to be tuned. Placeholders shown as `:name` (illustrative — real
> binding is psycopg `%(name)s` / `$1`). One self-referential `note_links` table carries
> note→note edges; junction tables carry tags/keywords. pgvector (5433) already handles
> semantic KB search — this is the relational/traversal half, queried alongside it.

### Mapping: Neo4j → Postgres

| Neo4j | Postgres |
|---|---|
| `(:Note {id, project_id, …})` | row in `notes`, PK `(project_id, id)` |
| `(:Tag)` / `(:Keyword)` + `TAGGED` / `HAS_KEYWORD` | junction rows in `note_tags` / `note_keywords` |
| `(a)-[:CONTRADICTS\|DERIVED_FROM\|ANSWERS]->(b)`, generic links | rows in `note_links(src, dst, rel_type)` |
| `get_related` (`-[*1..3]-`) | recursive CTE (undirected, depth-capped) |
| `get_provenance` (`-[:DERIVED_FROM*1..10]->`) | recursive CTE (directed, single `rel_type`) |
| `get_unanswered` (`WHERE NOT ()-[:ANSWERS]->`) | `WHERE NOT EXISTS (…)` |
| "two docs, same name" (no constraint) | `PRIMARY KEY (project_id, id)` — refused at write |
| `done/` relevance rot ("exclude superseded") | `WHERE status <> 'superseded'`, indexed |

### DDL

```sql
CREATE TABLE notes (
    project_id  UUID        NOT NULL,
    id          TEXT        NOT NULL,                   -- human slug; identity within project
    type        TEXT        NOT NULL,                   -- OKF's one required field: 'state'|'question'|'decision'|...
    title       TEXT,
    content     TEXT,
    status      TEXT        NOT NULL DEFAULT 'active',  -- 'active'|'superseded'|...
    job_id      UUID,                                   -- provenance
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- optional bi-temporal (mirrors RecallStore Phase 4 / Graphiti's temporal edge):
    valid_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to    TIMESTAMPTZ,                            -- NULL = currently valid
    PRIMARY KEY (project_id, id)                        -- the uniqueness the filesystem couldn't enforce
);

CREATE TABLE note_tags (
    project_id UUID NOT NULL,
    note_id    TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (project_id, note_id, tag),
    FOREIGN KEY (project_id, note_id) REFERENCES notes (project_id, id) ON DELETE CASCADE
);

CREATE TABLE note_keywords (
    project_id UUID NOT NULL,
    note_id    TEXT NOT NULL,
    keyword    TEXT NOT NULL,
    PRIMARY KEY (project_id, note_id, keyword),
    FOREIGN KEY (project_id, note_id) REFERENCES notes (project_id, id) ON DELETE CASCADE
);

CREATE TABLE note_links (
    project_id UUID NOT NULL,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    rel_type   TEXT NOT NULL,                           -- 'CONTRADICTS'|'DERIVED_FROM'|'ANSWERS'|'RELATED'|...
    created    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, src, dst, rel_type),
    FOREIGN KEY (project_id, src) REFERENCES notes (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, dst) REFERENCES notes (project_id, id) ON DELETE CASCADE
    -- referential integrity: a link to a non-existent note is refused at write time
);

CREATE INDEX ON notes         (project_id, status);
CREATE INDEX ON notes         (project_id, type, status);
CREATE INDEX ON note_tags     (project_id, tag);
CREATE INDEX ON note_keywords (project_id, keyword);
CREATE INDEX ON note_links    (project_id, dst, rel_type);   -- backlinks
CREATE INDEX ON note_links    (project_id, src, rel_type);   -- forward traversal
```

### CTE 1 — replaces `get_related` (undirected neighborhood, depth ≤ `:max_hops`, app-cap 3)

```sql
WITH RECURSIVE neighborhood AS (
    SELECT n.id AS note_id, 0 AS distance,
           ARRAY[]::text[] AS rel_path,
           ARRAY[n.id]     AS visited                        -- cycle guard
    FROM notes n
    WHERE n.project_id = :pid AND n.id = :nid
  UNION ALL
    SELECT e.note_id, nb.distance + 1,
           nb.rel_path || e.rel_type,
           nb.visited  || e.note_id
    FROM neighborhood nb
    JOIN (                                                   -- undirected: expand both directions
        SELECT src AS from_id, dst AS note_id, rel_type FROM note_links WHERE project_id = :pid
        UNION ALL
        SELECT dst AS from_id, src AS note_id, rel_type FROM note_links WHERE project_id = :pid
    ) e ON e.from_id = nb.note_id
    WHERE nb.distance < :max_hops
      AND NOT (e.note_id = ANY(nb.visited))
)
SELECT id, title, type, status, distance, rel_path
FROM (
    SELECT DISTINCT ON (r.note_id)                           -- keep shortest distance per note
           r.note_id AS id, n.title, n.type, n.status, r.distance, r.rel_path, n.modified
    FROM neighborhood r
    JOIN notes n ON n.project_id = :pid AND n.id = r.note_id
    WHERE r.note_id <> :nid
    ORDER BY r.note_id, r.distance
) sub
ORDER BY distance, modified DESC
LIMIT :limit;
```

### CTE 2 — replaces `get_provenance` (directed `DERIVED_FROM` chain, depth ≤ `:max_depth`, app-cap 10)

```sql
WITH RECURSIVE provenance AS (
    SELECT :nid::text AS note_id, 0 AS depth, ARRAY[:nid]::text[] AS visited
  UNION ALL
    SELECT l.dst, p.depth + 1, p.visited || l.dst
    FROM provenance p
    JOIN note_links l
      ON l.project_id = :pid AND l.src = p.note_id AND l.rel_type = 'DERIVED_FROM'
    WHERE p.depth < :max_depth
      AND NOT (l.dst = ANY(p.visited))                       -- cycle guard
)
SELECT DISTINCT ON (p.note_id) p.note_id AS id, n.title, n.type, p.depth
FROM provenance p
JOIN notes n ON n.project_id = :pid AND n.id = p.note_id
WHERE p.note_id <> :nid
ORDER BY p.note_id, p.depth;   -- app: re-sort by depth for output
```

### Everything else was never graph — trivial SQL

```sql
-- get_unanswered  (the "WHERE NOT IN" we thought a filesystem couldn't do)
SELECT q.id, q.title, q.content, q.created, q.job_id
FROM notes q
WHERE q.project_id = :pid AND q.type = 'question' AND q.status = 'active'
  AND NOT EXISTS (SELECT 1 FROM note_links l
                  WHERE l.project_id = :pid AND l.rel_type = 'ANSWERS'
                    AND (l.dst = q.id OR l.src = q.id));

-- get_contradictions
SELECT a.id, a.title, b.id, b.title
FROM note_links l
JOIN notes a ON a.project_id = :pid AND a.id = l.src
JOIN notes b ON b.project_id = :pid AND b.id = l.dst
WHERE l.project_id = :pid AND l.rel_type = 'CONTRADICTS'
  AND a.status = 'active' AND b.status = 'active';

-- notes by tag  /  backlinks  /  exclude-superseded
SELECT n.* FROM notes n
JOIN note_tags t ON (t.project_id, t.note_id) = (n.project_id, n.id)
WHERE n.project_id = :pid AND t.tag = :tag;
SELECT src FROM note_links WHERE project_id = :pid AND dst = :nid;   -- backlinks
-- any KB read simply appends:  AND status <> 'superseded'           -- the done/ fix
```

### Caveats
- The `visited`-array cycle guard is portable but O(depth) per row; for deep/dense graphs
  consider the SQL-standard `CYCLE` clause (PG14+).
- Bi-temporal columns are optional for v1 — include only if we want note-level supersede
  parity with RecallStore rather than a coarse `status` flag.
- These two CTEs are the *entire* graph surface. If profiling ever shows them hot on deep
  graphs, that's the signal to promote the Neo4j opt-in tier (§8) — not a reason to start
  there.
