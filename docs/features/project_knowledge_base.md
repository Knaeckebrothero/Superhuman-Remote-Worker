---
tags:
  - feature
  - architecture
  - projects
  - knowledge-management
  - memory
aliases:
  - project knowledge base
  - shared knowledge
  - project memory
related:
  - "[[projects]]"
  - "[[repo_resolution]]"
  - "[[memory_light]]"
  - "[[memories_mechanism]]"
  - "[[obsidian]]"
  - "[[working_memory]]"
---

# Project Knowledge Base — What Jobs Share

> **⚠️ Architecture Decision (2026-03-06):** This document originally assumed `knowledge/` lives as markdown files in a shared project repo, with databases as derived indexes. That model is superseded. Per [[repo_resolution]], projects no longer have a shared repo — jobs get per-job repos. The new decision: **Neo4j is the source of truth** for the project knowledge base. pgvector serves as a derived search index (write-through on every `kb_write`). Obsidian-compatible markdown files can be exported on demand but are not the canonical store. See the "Architecture" section for the full rationale.

This document is the single authoritative reference for how projects share state between jobs. It unifies concepts previously scattered across [[projects]] (merge flow, what reaches `main`), [[obsidian]] (note schema, dual representation, sync flow), and [[memory_light]] (retrieval, injection, observer). Those documents remain valid for their respective implementation details; this one captures the overarching design.

## The Core Problem

The project system (Phase 1–4: completed) gives us the infrastructure — repos, branches, merge flow, datasources. But the question it never fully answered was:

**What is the shared artifact that accumulates across jobs?**

For code projects the answer is obvious — jobs push code changes to source repos. But the code repo doesn't capture *why* decisions were made, what didn't work, or what patterns to follow. And for non-code projects (research, writing, analysis), there's no code at all. So what's the thing that makes Job N+1 smarter than Job 1?

## The Answer: Knowledge

The shared artifact is **knowledge**. Every job produces knowledge — decisions made, things learned, patterns discovered, questions raised, state recorded. Today this knowledge lives in `workspace.md` (a monolithic blob rewritten each strategic phase) and dies when the job ends.

The project knowledge base changes this. Jobs push structured, interlinked **knowledge notes** to a Neo4j knowledge graph shared across the project. These notes are:

1. **Graph-native** — stored in Neo4j with first-class relationships (REFERENCES, CONTRADICTS, SUPERSEDES, etc.)
2. **Vector-indexed** — embedded in pgvector for semantic hybrid search (RRF over dense + sparse + recency)
3. **Exportable** — dumpable as Obsidian-compatible markdown files for human browsing

This gives us two retrieval channels (graph traversal via Neo4j, ranked search via pgvector) that feed into the existing memory injection system. When a new job starts, the full knowledge base is immediately available. The project gets smarter with every job.

## How It Works — The Curator Subjob

The key insight: **the working agent doesn't write knowledge notes.** It works exactly as it does today — writes workspace.md, completes todos, produces output. A specialized **curator subjob** runs in parallel, triggered on every archive phase, continuously updating the knowledge base as the job progresses.

This follows the same pattern as the existing critic verification subjob (`verification.enabled` in config, `create_verification_job()` in `src/api/orchestrator_client.py`). The infrastructure for spawning subjobs already exists. The key difference: the critic runs post-completion, while the curator runs **continuously from the first archive phase**.

```
Job starts
  ↓
Phase 1 (tactical) → archive phase
  ↓
Curator subjob spawned (ONE persistent subjob)
  → reads the job's branch: workspace.md, plan.md, archive/
  → queries existing knowledge base (kb_search, kb_list)
  → extracts knowledge notes from phase 1
  → generates retrieval messages for each note (see below)
  → kb_write → Neo4j + pgvector (write-through, immediately available)
  ↓
Phase 2 (tactical) → archive phase
  ↓
Curator receives update (same subjob, new phase data)
  → reads new archive/phase_2_retrospective.md
  → reads updated workspace.md, plan.md
  → extracts incremental knowledge
  → kb_write / kb_update → Neo4j + pgvector
  ↓
... (repeats on every archive phase) ...
  ↓
Job completes → status: pending_review
  ↓
Critic subjob (existing)
  → reviews deliverables
  → approves or returns with feedback
  ↓
Curator receives final signal
  → final pass: reads memories, output/, freeze_data
  → kb_write for remaining knowledge (memories promotion, final state)
  ↓
Next job starts → full knowledge base already available in Neo4j
```

### Retrieval Messages

A key innovation: when the curator writes a knowledge note, it also generates a few **retrieval messages** — synthetic queries describing situations where this note should be retrieved. These are stored alongside the note's embedding.

For example, a note about choosing JWT over OAuth might have retrieval messages like:
- "What authentication approach should I use?"
- "Why did we pick JWT instead of OAuth?"
- "Token-based auth trade-offs and session management"

During retrieval, the vector search compares the agent's current message against these retrieval messages (not just the note content). This dramatically improves recall — the curator anticipates *when* the note will be needed, not just *what* it contains. The retrieval messages can be stored as an array field in `knowledge_index` and embedded alongside the main content embedding.

### Why a Subjob, Not Inline

The alternative — teaching every agent to write knowledge notes during execution via `kb_write()` — has problems:

1. **Every agent config needs new tools and instructions.** The developer, scholar, critic — all need to learn the knowledge base schema. That's a lot of prompt engineering across many expert configs.
2. **It competes for context window.** Knowledge writing during execution means more tool calls, more tokens spent on note management instead of the actual task.
3. **Quality varies by agent.** A research agent might write great notes; a coding agent might write terrible ones. The curator is a specialist.
4. **It couples the knowledge schema to the agent loop.** Changing the note format means updating every agent's instructions. With a curator, you update one expert config.

The curator subjob gives us **separation of concerns**: the working agent focuses on the task, the curator focuses on knowledge extraction. One expert config to get right, and every agent type benefits.

### Lifecycle: One Subjob, Many Updates

Unlike the critic (which runs once post-completion), the curator is a single persistent subjob that receives incremental updates via the same `waiting` → `resume_job(feedback)` mechanism the critic uses for multi-round reviews:

1. **Spawn** — After the first archive phase, the orchestrator spawns the curator subjob via `create_curation_job()` (follows the `create_verification_job()` pattern). The curator processes phase 1's archive data and enters `waiting` status.
2. **Incremental updates** — On each subsequent archive phase, the orchestrator calls `resume_job()` on the curator with the new phase data as feedback. The curator reads the latest `archive/phase_N_retrospective.md`, updated `workspace.md`, extracts incremental knowledge via `kb_write`, then goes back to `waiting`.
3. **Final pass** — After the critic approves (or after job completion if no critic), the orchestrator resumes the curator with a final signal. It reads `memories`, `output/`, and `freeze_data` for a comprehensive final extraction via `kb_write`.

This means knowledge extraction happens **asynchronously and in parallel** with the working agent. The curator doesn't block execution. By the time the job completes, most knowledge has already been extracted — the final pass only handles the last phase and deliverables.

### What the Curator Has Access To

The curator runs as a normal job on the same branch, so it has the full execution record:

| Source | What It Contains | How Curator Uses It |
|--------|-----------------|---------------------|
| `workspace.md` | Accumulated decisions, project context, working memory | Primary source for `decision`, `state`, `learning` notes |
| `plan.md` | Strategic plan, phase structure, goals | Source for `plan` and `goal` notes |
| `archive/` | Phase retrospectives, archived todos with completion notes | Source for `retrospective` notes, phase-level learnings |
| `output/` | Deliverables produced by the job | Curator organizes, validates, decides what merges |
| `memories` table | Observer + free source memories (PostgreSQL) | Pre-extracted insights — curator queries via orchestrator API (`GET /api/jobs/{id}/memories`) |
| Neo4j knowledge graph | Existing project knowledge base (via `kb_search`, `kb_list`, `kb_related`) | Context: what's already known, what to update vs. create new |
| Job metadata | Description, config, freeze_data, confidence | Context for framing the job's contributions |

### What the Curator Produces

The curator writes knowledge to Neo4j (via `kb_write` / `kb_update`):

1. **Knowledge notes** — Neo4j Note nodes with typed relationships to existing notes, proper type/tag classification, and retrieval messages
2. **Updated existing notes** — if the job contradicts or supersedes existing knowledge, the curator updates those notes (status → `superseded`, adds `SUPERSEDES` / `CONTRADICTS` relationships)
3. **Organized deliverables** in `output/` — curator can rename, restructure, add README files on the job branch (final pass only)

Note: items 1–2 happen incrementally during the job (per archive phase). Item 3 happens during the final pass after critic approval.

### The Curator as Editorial Filter

Not everything a job produces should become project knowledge. The curator makes editorial decisions:

- A research job that went nowhere → curator writes a "we tried X and it didn't work" learning note, doesn't carry forward failed deliverables
- A coding job that built a feature → curator writes `code` and `decision` notes explaining the architecture, links to relevant existing notes
- A job that discovered a contradiction with existing knowledge → curator writes a `learning` note with `CONTRADICTS` relationship, updates the contradicted note's status
- A job with low confidence → curator writes `question` notes instead of `decision` notes

### Chaining: Archive → Curator → Critic → Final Pass

The natural flow for project jobs:

```
Job starts
    ↓
Phase 1 → archive phase
    ↓
curator.enabled: true → Curator subjob spawned (runs in parallel)
    ↓                          ↓
Phase 2... N (job continues)   Curator processes phases incrementally
    ↓                          ↓ (kb_write → Neo4j + pgvector, write-through)
Job completes                  Curator has processed most phases
    ↓
verification.enabled: true → Critic subjob
    ↓ (approved)
Curator receives final signal → final pass (memories, output)
    ↓
Knowledge is already in Neo4j — immediately available to next job
```

If the critic returns the job with feedback, the job resumes — the curator continues processing new archive phases as they come. The curator's final pass only triggers after critic approval.

Note: since knowledge writes go directly to Neo4j (not to files on a branch), there is no merge step for knowledge. The knowledge is available to other jobs immediately after `kb_write`. The job's branch/PR flow only applies to deliverables in `output/`.

Config extension in `defaults.yaml`:

```yaml
curator:
  enabled: false                # Opt-in per config (or per project)
  curator_config: curator       # Which expert config to use
  autonomy: full                # No human review of curated notes
```

### For Code Projects

The project has:
- **Neo4j knowledge graph** — knowledge lives here (decisions, patterns, learnings)
- **Source repos** — code lives there (agents push code changes)
- **Per-job repos** — workspace and deliverables for individual jobs

Jobs push code to source repos AND knowledge to Neo4j. The knowledge base records what was built, why, what patterns were followed, what didn't work — context that code alone doesn't capture.

### For Non-Code Projects

The knowledge base IS the primary artifact. Research notes, document analysis, findings, decisions — all structured as knowledge notes in Neo4j. Per-job repos hold working files; the knowledge graph holds the accumulated output.

## What Goes in the Knowledge Base

Everything. The knowledge base is the canonical record of a project's life:

| Category | Examples | Note Type |
|----------|---------|-----------|
| **Goals** | Project objectives, success criteria, definition of done | `goal` |
| **Plans** | Roadmap, phase structure, milestones, priorities | `plan` |
| **Decisions** | Architecture choices, technology picks, trade-off analysis | `decision` |
| **Learnings** | What worked, what didn't, debugging insights, performance findings | `learning` |
| **Code** | What was written, why, where, key patterns and conventions | `code` |
| **State** | Current project status, what's done, what's in progress, what's blocked | `state` |
| **Questions** | Open items, unresolved decisions, things to investigate | `question` |
| **Sources** | Documents, URLs, conversations, requirements that informed decisions | `source` |
| **Retrospectives** | Phase reviews — what worked, what didn't | `retrospective` |

### What This Replaces

| Before | After |
|--------|-------|
| `workspace.md` — monolithic blob rewritten each strategic phase | Still written by agents during execution; curator extracts atomic notes post-completion |
| `plan.md` — single flat plan file | Still written by agents; curator promotes goals/milestones to interlinked plan notes |
| `archive/phase_N_retrospective.md` — linear phase history | Still created by agents; curator extracts retrospective notes linked to decisions |
| Memory Light `memories` table — parallel memory system | Kept as staging area during execution; curator promotes valuable entries to knowledge notes |
| Context lost on compaction | Query knowledge_index (pgvector + tsvector) to retrieve relevant project context |
| Per-job isolation (unless merged) | Project-wide knowledge base in Neo4j that accumulates across jobs via `kb_write` |

## Architecture

### Core Principle: Neo4j is Source of Truth, pgvector is a Search Index

This is the foundational architectural decision. **Neo4j owns the canonical knowledge data.** PostgreSQL (pgvector + tsvector) is a derived search index updated via write-through on every `kb_write`. Obsidian-compatible markdown files are a one-way export for human browsing.

```
Neo4j  (source of truth — notes, relationships, tags, keywords)
  │
  │  write-through (on every kb_write, both stores updated)
  │
  ├──────────► pgvector    (semantic search index)
  └──────────► tsvector    (keyword search index)

  │  on-demand export
  │
  └──────────► Obsidian .md files  (human browsing, not canonical)
```

**If you delete pgvector, you rebuild it from Neo4j.** If you delete Neo4j, the data is gone. pgvector is a search acceleration layer, not a source of truth.

**Why Neo4j, not files:**

- **No project repo exists.** Per [[repo_resolution]], projects no longer have a shared repository. Jobs get per-job repos. There is no natural home for `knowledge/*.md` files. Creating a repo just for knowledge files is creating a database with extra steps.
- **A knowledge base is a graph.** Notes reference notes, decisions contradict decisions, learnings derive from sources. Neo4j models this natively — relationships are first-class entities, not JSONB arrays or parsed wikilinks.
- **No sync subsystem needed.** The original design required git-diff-based incremental sync (track commits, parse frontmatter, re-index changed files). With Neo4j as source of truth and write-through to pgvector, both stores are updated atomically on every write. No batch sync, no drift, no recovery logic.
- **Graph queries are immediate.** "What depends on this decision?" is a single Cypher traversal, not a Phase 3 enhancement. Graph capabilities are available from day one.
- **Already in the stack.** Neo4j runs in docker-compose, has a driver (`Neo4jDB` in `src/database/neo4j_db.py`), and the agent already has graph tools. Promoting it from external datasource to system database is a small step.

**Why keep pgvector:**

- **Hybrid search is proven.** The Memory Light RRF pattern (dense + sparse + recency) works well. Replicating this in Neo4j is possible but adds complexity.
- **Embedding infrastructure exists.** `EmbeddingService`, `knowledge_hybrid_search()` SQL function, HNSW indexes — all reusable from Memory Light.
- **Separation of concerns.** Neo4j is the source of truth for structure and relationships. pgvector is optimized for ranked retrieval. Each does what it's best at.

**Human access via Obsidian export:**

- A `kb_export(project_id)` function walks the Neo4j graph, writes `.md` files with frontmatter and `[[wikilinks]]` derived from graph relationships.
- One-way, on-demand. Not bidirectional sync.
- Users can browse in Obsidian or any markdown viewer. Edits go through agent tools, the cockpit UI, or a future API.
- The export can also serve as a backup/portability mechanism — dump to files, import on another instance.

### Two Stores, One Knowledge Base

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Project Knowledge Base                          │
│                                                                     │
│   ┌──────────────────────┐          ┌──────────────────────┐       │
│   │  Neo4j               │  write-  │  pgvector + tsvector  │       │
│   │  (source of truth)   │ through  │  (search index)       │       │
│   │                      │ ──────►  │                       │       │
│   │  • Canonical store   │          │  • Semantic search     │       │
│   │  • Graph structure   │          │  • Keyword search      │       │
│   │  • Relationships     │          │  • RRF hybrid ranking  │       │
│   │  • Tags, keywords    │          │  • Rebuilt from Neo4j  │       │
│   │  • Full note content │          │  • Never edited directly│      │
│   └──────────┬───────────┘          └──────────┬───────────┘       │
│              │                                  │                   │
│              │         ┌──────────┐             │                   │
│              └────────►│  Agent   │◄────────────┘                   │
│                        │  Tools   │                                 │
│   ┌──────────┐        │          │        ┌──────────┐             │
│   │ Obsidian │◄───────│ kb_write │───────►│ Cockpit  │             │
│   │ (export) │        │ (Neo4j)  │        │ (user)   │             │
│   └──────────┘        │ kb_search│        └──────────┘             │
│                        │ (pgvec) │                                  │
│                        └──────────┘                                 │
└─────────────────────────────────────────────────────────────────────┘
```

**Write path:** `kb_write` creates/updates a note node in Neo4j (source of truth) AND upserts the corresponding row in `knowledge_index` (pgvector + tsvector). Both writes happen in the same tool call — write-through, not eventual sync.

**Search path:** `kb_search` hits `knowledge_hybrid_search()` in pgvector (RRF over dense + sparse + recency). Fast, ranked, semantic. Returns note summaries with IDs.

**Graph path:** `kb_related`, `kb_contradictions`, `kb_provenance` hit Neo4j directly for relationship traversal. "What depends on this decision?" is a Cypher query, not a text search.

**Read path:** `kb_read` and `kb_list` query Neo4j directly. Full note content, metadata, and relationships.

**Export path:** `kb_export` walks the Neo4j graph, writes Obsidian-compatible `.md` files with frontmatter and `[[wikilinks]]`. One-way, on-demand.

### How It Fits the Project Model

The knowledge base is project-level infrastructure that lives in databases, not in job repos:

```
Project "My-App"
│
├── Neo4j (source of truth, auto-provisioned, project-label namespacing)
│   ├── (:Note {id, type, title, content, job_id, confidence, project_id, ...})
│   ├── (:Tag {name, project_id}), (:Keyword {name, project_id})
│   └── [:REFERENCES], [:SUPPORTS], [:CONTRADICTS], [:DERIVED_FROM], ...
│
├── PostgreSQL — knowledge_index table (search index, auto-provisioned)
│   ├── pgvector embeddings (semantic search)
│   └── tsvector documents (keyword search)
│
├── Per-Job Repos (Gitea, one per root job — see repo_resolution)
│   ├── workspace.md, plan.md, todos.yaml  ← job-scoped working memory
│   ├── archive/                            ← phase artifacts
│   └── output/                             ← deliverables
│
└── Jobs
    ├── Job 1 → kb_write() → Neo4j + pgvector (write-through)
    ├── Job 2 → kb_search() + kb_related() → reads from pgvector + Neo4j
    └── ...
```

Note: The job workspace is unchanged — agents still write `workspace.md` and `plan.md` during execution. The knowledge base lives entirely in databases (Neo4j + pgvector), not in the job repo. The curator extracts knowledge from job artifacts and writes to Neo4j via `kb_write`. Over time, the knowledge base becomes the project's long-term memory while `workspace.md` remains the agent's working memory during a single job.

## Note Schema

### Note Addressing

Notes are identified by a human-readable **slug** derived from the title (e.g., `chose-jwt-over-oauth`). The slug is unique per project (enforced by the Neo4j constraint on `project_id` + `id`). Agents use slugs in all tool calls (`kb_read(note="chose-jwt-over-oauth")`). The slug is generated by `kb_write` from the title: lowercase, hyphens for spaces, strip special characters, truncate to 80 chars. If a collision occurs, a short random suffix is appended.

### Neo4j Node Model (Canonical)

The canonical representation of a knowledge note is a Neo4j node. All note properties live here. The `content` property holds the full markdown body of the note.

```cypher
(:Note {
  id: "chose-jwt-over-oauth",      // slug derived from title (unique per project)
  type: "decision",                 // goal|plan|decision|learning|code|source|question|state|retrospective
  title: "Chose JWT over OAuth",
  content: "After evaluating both approaches...",  // full markdown body
  status: "active",                 // active|resolved|superseded|archived
  confidence: "high",               // high|medium|low
  job_id: "abc-123",               // which job created this note
  project_id: "proj-456",          // project scope
  phase: 3,
  created: datetime("2026-02-01T14:23:00Z"),
  modified: datetime("2026-02-01T15:30:00Z"),
  retrieval_messages: ["What auth approach?", "Why JWT over OAuth?"]
})

// Relationships — first-class edges, linked by id (titles shown for readability)
(:Note {id: "chose-jwt-over-oauth"})-[:REFERENCES]->(:Note {id: "oauth-analysis"})
(:Note {id: "chose-jwt-over-oauth"})-[:ANSWERS]->(:Note {id: "which-auth-method"})
(:Note {id: "auth-middleware-pattern"})-[:IMPLEMENTS]->(:Note {id: "chose-jwt-over-oauth"})

// Tags and keywords as connected nodes
(:Note)-[:TAGGED]->(:Tag {name: "authentication", project_id: "proj-456"})
(:Note)-[:HAS_KEYWORD]->(:Keyword {name: "JWT", project_id: "proj-456"})
```

### Relationship Types

Relationships are first-class Neo4j edges, created explicitly by `kb_write` and `kb_update` (not parsed from wikilinks):

| Relationship | Meaning | Neo4j Edge |
|--------------|---------|------------|
| `REFERENCES` | Note mentions another concept | `[:REFERENCES]` |
| `DERIVED_FROM` | Learning/decision extracted from source | `[:DERIVED_FROM]` |
| `SUPPORTS` | Evidence for a claim or decision | `[:SUPPORTS]` |
| `CONTRADICTS` | Conflicting information | `[:CONTRADICTS]` |
| `ANSWERS` | Decision resolves question | `[:ANSWERS]` |
| `DEPENDS_ON` | Prerequisite relationship | `[:DEPENDS_ON]` |
| `SUPERSEDES` | New decision replaces an old one | `[:SUPERSEDES]` |
| `IMPLEMENTS` | Code note implements a decision or plan | `[:IMPLEMENTS]` |

### Obsidian Export Format

When `kb_export` dumps the knowledge base, each note becomes a `.md` file with YAML frontmatter. Relationships are rendered as `[[wikilinks]]` in the body. This format is for human browsing only — the canonical data lives in Neo4j.

```yaml
---
id: chose-jwt-over-oauth
type: decision
tags: [requirement, authentication]
keywords: [OAuth, JWT, session]
confidence: high
status: active
job_id: abc-123
phase: 3
created: 2026-02-01T14:23:00Z
modified: 2026-02-01T15:30:00Z
---

# Chose JWT over OAuth

After evaluating both approaches...

**References:** [[oauth-analysis]], [[security-requirements]]
**Answers:** [[which-auth-method]]
```

## Memory Integration — Two Stages, One Knowledge Base

Memory Light and the knowledge base are not parallel systems — they are two stages of the same pipeline. Memory Light captures raw insights during execution (PostgreSQL `memories` table). The curator promotes them to structured knowledge notes post-completion.

### The Pipeline

```
During job execution (both run in parallel):

Working Agent                          Curator Subjob
  Observer LLM → memories table          (spawned after first archive phase)
  Todo completion → memories table
  Compaction summaries → memories table
  Phase archives → memories table    →   Reads archive/phase_N_retrospective.md
  Tool errors → memories table           Reads updated workspace.md, plan.md
                                         Extracts knowledge notes incrementally
                                         Generates retrieval messages per note
                                         kb_write → Neo4j + pgvector (write-through)
                    ↓
After approval (Curator final pass):
  Reads memories table for this job
  Reads output/, freeze_data
  Queries existing KB via kb_search, kb_list
  → Promotes valuable memories to knowledge notes (kb_write)
  → Final extraction from output and deliverables
  → Deduplicates against existing KB
```

This means Memory Light keeps working exactly as implemented — no changes to the observer, free sources, or injection hook. The `memories` table is a staging area that the curator reads during its final pass. The phase-by-phase curation means most knowledge is already extracted before the job even completes.

### How Memory Channels Feed the Curator

| Memory Channel | What Curator Gets | Typical Output |
|----------------|-------------------|----------------|
| **Observer** | Pre-extracted insights with keywords and importance | `learning`, `decision` notes |
| **Todo completion** | Structured outcome summaries | `learning`, `code` notes |
| **Compaction summary** | Narrative of discarded conversation | `state` notes (what happened) |
| **Phase archive** | Retrospective with all todo outcomes | `retrospective` notes |
| **Tool errors** | Error-solution pairs | `learning` notes tagged `error-solution` |

The curator doesn't blindly promote every memory. It has the full picture (memories + workspace + archive + existing KB) and makes editorial decisions: merge similar memories into one note, discard noise, link related findings, update existing notes instead of creating duplicates.

### Retrieval Flow

Working agents in KB-enabled projects get knowledge injected via the same transient message pattern as Memory Light. Search hits pgvector (always current via write-through):

```
Agent execute loop
    │
    ├─ Context compaction (existing)
    ├─ Todo injection (existing)
    ├─ Knowledge summary injection (note counts, recent activity)
    ├─ ★ Knowledge retrieval (replaces/augments memory injection)
    │   ├─ Dense vector search (pgvector — always current via write-through)
    │   ├─ Sparse keyword search (tsvector — always current via write-through)
    │   └─ RRF fusion → top-K notes → inject as transient message
    │
    ├─ Memory Light injection (still runs for job-scoped memories)
    ├─ Instruction file injection (existing)
    └─ LLM call
```

Note: Memory Light injection continues during job execution for job-scoped memories (the working agent's own observations). Knowledge injection provides project-scoped context from all previous jobs. Both can coexist — Memory Light gives "what I've learned this session", knowledge gives "what the project knows".

The injected block looks like:

```
--- Project Knowledge ---

[1] (decision, high confidence, phase 2)
Chose JWT over OAuth for authentication. JWT is stateless, no session store
needed. Trade-off: token revocation requires a blacklist. See [[oauth-analysis]].

[2] (learning, phase 3)
JSONB queries on the requirements table need a GIN index for >10k rows.
Without it, full-table scan takes 800ms. With GIN: 12ms.

[3] (state, current)
Auth module: implemented. Needs integration tests. Blocked on staging DB
credentials — see [[staging-db-access]].

[4] (learning, error-solution, phase 1)
TimeoutError on Neo4j connection after pod restart: increase
max_connection_lifetime from 1h to 30min (connections go stale).

--- End Knowledge (4 notes, ~1,200 tokens) ---
```

### Scoping

Notes are always project-scoped. The `job_id` property on each Neo4j Note node records provenance (which job created this note) but retrieval queries filter by `project_id`:

- **During the job**: working agent queries pgvector (search) and Neo4j (graph). Curator writes new notes via `kb_write` (write-through to both stores).
- **Next job**: immediately sees all knowledge from previous jobs — no sync step needed, both stores are already current.

This replaces Memory Light's Phase 5 ("cross-job memory via project_id on the memories table") with something richer — structured, interlinked notes in a graph instead of flat text blobs.

## Write-Through: Neo4j → pgvector

There is no batch sync step. Every `kb_write` and `kb_update` call writes to both stores atomically (from the agent's perspective):

```
kb_write("Chose JWT over OAuth", type="decision", ...)
  │
  ├─► Neo4j: MERGE (n:Note {id: $id, project_id: $pid}) SET n += {...}
  │           CREATE (n)-[:TAGGED]->(:Tag {name: "auth"})
  │           CREATE (n)-[:REFERENCES]->(m) WHERE m.id = $ref_id
  │
  └─► pgvector: INSERT INTO knowledge_index (...) ON CONFLICT DO UPDATE
  │             (embedding generated via EmbeddingService)
  │
  Done. Both stores are consistent.
```

**No sync tracking needed.** No `knowledge_sync_state` table, no commit hashing, no git diff. The write path keeps both stores in sync by construction.

**Recovery:** If pgvector gets corrupted or out of sync, a `rebuild_search_index(project_id)` function reads all notes from Neo4j and re-inserts them into `knowledge_index`. This is the cold-start / recovery path — run once, takes seconds for typical knowledge bases.

### Neo4j Schema Initialization

On project creation, the system ensures Neo4j constraints and indexes exist:

```cypher
// Unique constraint per project
CREATE CONSTRAINT note_id_unique IF NOT EXISTS
FOR (n:Note) REQUIRE (n.project_id, n.id) IS UNIQUE;

// Indexes for common lookups
CREATE INDEX note_project IF NOT EXISTS FOR (n:Note) ON (n.project_id);
CREATE INDEX note_type IF NOT EXISTS FOR (n:Note) ON (n.project_id, n.type);
CREATE INDEX note_status IF NOT EXISTS FOR (n:Note) ON (n.project_id, n.status);
CREATE INDEX tag_project IF NOT EXISTS FOR (t:Tag) ON (t.project_id);
CREATE INDEX keyword_project IF NOT EXISTS FOR (k:Keyword) ON (k.project_id);
```

Project isolation uses `project_id` properties on all nodes. All queries filter by `project_id`. No multi-tenancy concerns — simple label + property filtering.

### Schema: `knowledge_index` Table (Search Index)

Derived search index in PostgreSQL (same database as jobs/memories). Updated via write-through, never edited directly:

```sql
CREATE TABLE IF NOT EXISTS knowledge_index (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    note_id VARCHAR(100) NOT NULL,           -- Neo4j note id / slug (e.g. "chose-jwt-over-oauth")
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    note_type VARCHAR(50) NOT NULL,           -- goal, plan, decision, learning, code, source, question, state, retrospective
    status VARCHAR(50) DEFAULT 'active',
    confidence VARCHAR(20),
    tags TEXT[] DEFAULT '{}',
    keywords TEXT[] DEFAULT '{}',
    job_id UUID,                              -- which job created this note
    phase INT,
    content TEXT NOT NULL,                    -- full note body (for search result display)
    retrieval_messages TEXT[] DEFAULT '{}',   -- curator-generated queries for when this note should surface
    embedding vector,                         -- dense embedding (dimension set by EMBEDDING_DIMENSIONS env var, default 1536)
    search_doc tsvector,                     -- full-text search document
    created_at TIMESTAMPTZ,
    modified_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ DEFAULT NOW(),    -- when this row was last written
    content_hash VARCHAR(64),                -- sha256 of content (skip re-embedding on metadata-only updates)

    CONSTRAINT uq_knowledge_project_note UNIQUE (project_id, note_id)
);

CREATE INDEX idx_knowledge_project ON knowledge_index(project_id);
CREATE INDEX idx_knowledge_project_type ON knowledge_index(project_id, note_type);
CREATE INDEX idx_knowledge_tags ON knowledge_index USING GIN(tags);
CREATE INDEX idx_knowledge_search ON knowledge_index USING GIN(search_doc);
CREATE INDEX idx_knowledge_embedding ON knowledge_index
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);
```

This reuses the same pgvector infrastructure as Memory Light (HNSW index, cosine distance, same embedding model). The vector dimension is configurable via `EMBEDDING_DIMENSIONS` env var (default: 1536 for `text-embedding-3-small`). The `content_hash` column lets us skip re-embedding when only metadata changes (status, tags) without content changes.

### Search Function

Same RRF hybrid search pattern as Memory Light, querying `knowledge_index` scoped by `project_id`:

```sql
CREATE OR REPLACE FUNCTION knowledge_hybrid_search(
    query_text text,
    query_embedding vector,
    project_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50
) RETURNS SETOF knowledge_index LANGUAGE sql AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> query_embedding) AS rank_ix
    FROM knowledge_index WHERE project_id = project_id_param AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery(query_text)) DESC) AS rank_ix
    FROM knowledge_index WHERE project_id = project_id_param AND search_doc @@ websearch_to_tsquery(query_text)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY modified_at DESC) AS rank_ix
    FROM knowledge_index WHERE project_id = project_id_param
    ORDER BY rank_ix LIMIT match_count
)
SELECT ki.* FROM dense
FULL OUTER JOIN sparse ON dense.id = sparse.id
FULL OUTER JOIN recent ON COALESCE(dense.id, sparse.id) = recent.id
JOIN knowledge_index ki ON COALESCE(dense.id, sparse.id, recent.id) = ki.id
ORDER BY
    COALESCE(1.0 / (rrf_k + dense.rank_ix), 0.0) * dense_weight +
    COALESCE(1.0 / (rrf_k + sparse.rank_ix), 0.0) * sparse_weight +
    COALESCE(1.0 / (rrf_k + recent.rank_ix), 0.0) * recency_weight
    DESC
LIMIT match_count
$$;
```

## Agent Tools

Knowledge base tools are a tool category registered in `src/tools/registry.py`. The **curator** gets all tools (read + write). **Working agents** get read tools only by default — they benefit from knowledge injection and can actively search, but writing is the curator's job.

```python
# === Writing (curator + opt-in for working agents) ===

kb_write(
    title="Chose JWT over OAuth",
    type="decision",
    content="After evaluating both...",
    tags=["authentication", "security"],
    confidence="high",
    links=[                                # explicit relationships to other notes
        {"target": "oauth-analysis", "type": "REFERENCES"},
        {"target": "which-auth-method", "type": "ANSWERS"},
    ],
    retrieval_messages=[
        "What authentication approach should I use?",
        "Why did we pick JWT instead of OAuth?",
        "Token-based auth trade-offs and session management"
    ]
)
# → Creates Note node in Neo4j (source of truth)
# → Upserts row in knowledge_index (pgvector + tsvector search index)
# → Auto-sets job_id, phase, created timestamp
# → Write-through: both stores updated in the same call

kb_update(
    note="chose-jwt-over-oauth",
    append="## Update (Phase 4)\nAfter load testing, JWT validation adds 2ms p99.",
    status="active",
    add_tags=["performance"],
    add_links=[{"target": "load-test-results", "type": "REFERENCES"}]
)
# → Updates Neo4j node + pgvector row (write-through)

# === Reading (all agents in KB-enabled projects) ===

kb_search(query="authentication")       # Hybrid search via knowledge_index (pgvector + tsvector + RRF)
kb_read(note="chose-jwt-over-oauth")    # Returns full note from Neo4j (content + metadata + relationships)
kb_list(type="decision")                # All decisions (queries Neo4j)
kb_list(tag="authentication")           # All notes tagged authentication
kb_list(status="question")              # All open questions
kb_list(job_id="abc-123")              # Everything from a specific job

# === Graph Querying (Neo4j-powered, available from Phase 1) ===

kb_related(note="chose-jwt-over-oauth") # All notes within 2 hops
kb_contradictions()                      # Notes connected by CONTRADICTS edges
kb_provenance(note="auth-middleware")    # Trace back through DERIVED_FROM chains
kb_unanswered()                          # Questions with no ANSWERS relationship

# === Export (on-demand, one-way) ===

kb_export(path="/tmp/obsidian-vault")    # Dumps all notes as Obsidian-compatible .md files
```

### When Knowledge Notes Get Written

With the curator model, knowledge note creation is concentrated in the curator subjob, running in parallel with the working agent:

**During the job (working agent — unchanged):**
The agent writes `workspace.md`, `plan.md`, todo completion notes, and `archive/` retrospectives as it does today. Memory Light's free sources (todo completion, compaction, phase archive, tool errors) store memories in the PostgreSQL `memories` table as they do today. No new tools or behavior required from the working agent.

**During the job (curator — incremental, per archive phase):**
After each archive phase, the curator reads the new retrospective, updated workspace.md, and updated plan.md. It extracts knowledge incrementally — decisions, learnings, state changes from that phase. This means knowledge is being built *while the job is still running*.

**After approval (curator — final pass):**
The curator reads memories, output/, and freeze_data to produce final knowledge notes:

| Source | Curator Output |
|--------|---------------|
| `workspace.md` sections on decisions | `decision` notes with reasoning, links to related notes |
| `workspace.md` sections on discoveries | `learning` notes with tags and keywords |
| `plan.md` goals and milestones | `goal` and `plan` notes (if not already in KB) |
| `archive/phase_N_retrospective.md` | `retrospective` notes linked to decisions and findings |
| Todo completion notes (notable ones) | `learning` or `code` notes for significant outcomes |
| `memories` table entries for this job | Promoted to typed notes where valuable, deduplicated against existing KB |
| Unresolved items from workspace.md | `question` notes for the next job to pick up |
| Overall job outcome | `state` note summarizing what changed in the project |

**Optional: agents with kb_write (future):**
Nothing prevents giving the knowledge tools to working agents too. A researcher might want to write `source` notes as it discovers references. But this is additive — the curator is the baseline that ensures every job contributes knowledge regardless of whether the working agent uses KB tools.

## Context Injection — What Supplements workspace.md

Today, `workspace.md` is injected as a transient message every LLM call. The knowledge base supplements this with two additional injection layers:

1. **Knowledge summary (always injected)** — Dynamically generated from Neo4j: note counts by type, recent activity, key notes. Gives the agent orientation without loading everything. Lightweight (~200–500 tokens).

2. **Retrieved knowledge (dynamically injected)** — Top-K relevant notes selected by RRF hybrid search (dense + sparse + recency via pgvector). Assembled into a transient message. Budget: configurable, default 5,000–10,000 tokens.

The agent can also actively query via `kb_search`, `kb_related`, etc. — the injection is passive recall, the tools are active recall.

## Provisioning

The knowledge base is auto-provisioned as part of system initialization:

1. **Neo4j schema** — Constraints and indexes (see "Neo4j Schema Initialization") are created during `orchestrator/init.py`, same as PostgreSQL schema migrations. They're global (not per-project) since `project_id` properties handle isolation.
2. **`knowledge_index` table** — Created by PostgreSQL schema migration (shared across all projects, scoped by `project_id`). Uses existing system PostgreSQL with pgvector extension (already enabled for Memory Light).
3. **System Neo4j connection** — Environment variables `NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` configure the system Neo4j instance. This is a system database like PostgreSQL — required for the platform to function. Configured in `.env` alongside `DATABASE_URL`.

No per-project provisioning needed. The first `kb_write` for a project simply creates nodes with that `project_id`.

## Migration Path

### Existing Jobs (Pre-Knowledge Base)

Existing jobs keep `workspace.md` (backward compatible). No breaking changes. The knowledge base is opt-in at the project level initially, then becomes the default for new projects.

### workspace.md → Knowledge Notes Conversion

An optional migration tool splits an existing `workspace.md` into atomic notes:

1. Parse sections (headings become note titles)
2. Classify by content (decisions, learnings, state, etc.)
3. Call `kb_write` for each extracted note (writes to Neo4j + pgvector)
4. Relationships inferred from cross-references between sections

### Memory Light → Knowledge Base Migration

For projects already using Memory Light (the `memories` PostgreSQL table):

1. Read memories for a project's jobs from the `memories` table
2. Call `kb_write` for each valuable memory (content → body, keywords → tags, memory_type → note type)
3. The `memories` table remains for job-scoped use during execution; project-scoped retrieval switches to the knowledge base

New projects skip this — they use the knowledge base from day one.

## Open Questions

### Resolved

1. ~~**What do projects share between jobs?**~~
   **Resolved** — The knowledge base. Notes stored in Neo4j (source of truth), indexed in pgvector for search. No shared repo needed — the database is the shared artifact.

2. ~~**Is Memory Light a separate system?**~~
   **Resolved** — It's a stage, not a separate system. Memory Light captures raw insights during execution (PostgreSQL `memories` table). The curator subjob promotes them to structured knowledge notes post-completion. Memory Light is the capture stage; the curator is the promotion stage. See "Memory Integration" section.

3. ~~**Vault scope — one per job or per project?**~~
   **Resolved** — One per project. Knowledge lives in Neo4j scoped by `project_id`. No files or repos involved.

4. ~~**Graph database choice?**~~
   **Resolved** — Neo4j, as core project infrastructure. Already in the stack, already has tooling. Shared instance with project-label namespacing.

5. ~~**How do agents write good knowledge notes?**~~
   **Resolved** — They don't have to. The curator subjob is a specialist that reads the job's full output (workspace.md, archive, memories, output) and produces structured notes. Only the curator needs knowledge tools and schema understanding. Working agents are unchanged. See "The Curator Subjob" section.

6. ~~**Who decides what becomes project knowledge?**~~
   **Resolved** — The curator. It makes editorial decisions: what to promote, what to discard, what to update. Failed experiments become "we tried X" learning notes. Successful work becomes decisions, code, and state notes. The curator writes to Neo4j via `kb_write`.

7. ~~**Sync-on-write vs sync-on-merge**~~
   **Resolved** — Write-through. Every `kb_write` and `kb_update` writes to both Neo4j (source of truth) and pgvector (search index) in the same call. No batch sync, no git tracking, no drift. See "Write-Through: Neo4j → pgvector" section.

8. ~~**How does search work without Neo4j/vectors in Phase 1?**~~
   **Resolved** — Both Neo4j and pgvector are Phase 1. Neo4j is the source of truth; pgvector provides hybrid search via `knowledge_hybrid_search()` (RRF over dense + sparse + recency). Graph queries (`kb_related`, etc.) hit Neo4j directly. All available from day one.

9. ~~**Automatic linking**~~
   **Resolved** — Curator writes explicit links based on content similarity using existing KB context + `kb_search` results. A periodic maintenance pass fills gaps. Good default — aggressive enough to be useful, not so aggressive that it creates noise.

10. ~~**Context injection budget**~~
    **Resolved** — Use the same defaults as the current Memory Light system: `budget_tokens: 5000`, `max_memories_per_injection: 10`. Tune later based on observed impact.

11. ~~**Curator chain position**~~
    **Resolved** — The curator triggers on archive phase, not post-completion. It runs as a single persistent subjob spawned after the first archive phase, processing knowledge incrementally in parallel with the working agent. The critic still runs post-completion. The curator receives a final signal after critic approval for a last pass (memories, output, cleanup). See "Lifecycle: One Subjob, Many Updates" section.

12. ~~**Curator autonomy level**~~
    **Resolved** — `full` autonomy. No need for the curator to pause for human approval. See open question #23 on knowledge quality gates.

13. ~~**Parallel job knowledge conflicts**~~
    **Resolved** — With Neo4j as source of truth, there are no file-level merge conflicts. Parallel jobs write to the same Neo4j instance — last write wins for node properties. Semantic contradictions are handled via the `CONTRADICTS` relationship type: the curator (or a future "knowledge health check" job) can detect and flag contradictory notes. Since notes are append-mostly (new notes far outnumber updates to existing ones), write conflicts are rare in practice.

14. ~~**Curator access to target job memories**~~
    **Resolved** — Same pattern as the critic subjob. The curator receives the target job's context (workspace.md content, memories, freeze_data) via formatted instructions, just like the critic receives freeze_data in `create_verification_job()`. The orchestrator passes this context when spawning the curator via `create_curation_job()`.

15. ~~**Obsidian CLI integration**~~
    **Resolved** — Deferred. With Neo4j as source of truth, Obsidian CLI is less critical — backlinks, orphans, and dead ends are graph queries (`kb_related`, `kb_unanswered`). Obsidian export (`kb_export`) generates browsable files on demand. CLI integration remains a Phase 4 nice-to-have for users who prefer the Obsidian UI.

16. ~~**Curator-agent branch coordination**~~
    **Resolved** — With Neo4j as source of truth, branch coordination is no longer relevant for knowledge writes. The curator writes to Neo4j via `kb_write` — no files on a branch, no merge step for knowledge. The curator still runs as a subjob on its own branch (per [[repo_resolution]]) for its own workspace.md/plan.md, but its knowledge output goes directly to the database, not to files that need merging.

17. ~~**Archive phase notification mechanism**~~
    **Resolved** — Same pattern as the critic's resume mechanism. The curator enters `waiting` status after processing a phase. When the working agent archives the next phase, the orchestrator calls `resume_job()` on the curator with the new phase data as feedback. This reuses the existing `waiting` → `resume_job(feedback)` infrastructure that the critic already uses for multi-round reviews. The curator processes the phase, then goes back to `waiting` until the next archive or the final signal. The curator merges the parent job's branch into its own branch before processing each phase to pick up the latest workspace.md, archive/, etc.

18. ~~**Post-merge sync trigger**~~
    **Resolved** — No longer applicable. With write-through to Neo4j + pgvector, there is no post-merge sync step. Knowledge is written directly to databases on every `kb_write`, not via file commits.

19. ~~**Retrieval message storage**~~
    **Resolved** — Retrieval messages are stored as a list property on the Neo4j Note node (`retrieval_messages`) and as a `TEXT[]` column in `knowledge_index`. For embedding, retrieval messages are concatenated with the note content before generating the embedding vector — one embedding that captures both content and retrieval intent. Upgrade to separate per-retrieval-message embeddings later if recall is insufficient.

20. ~~**Source of truth: files vs database**~~
    **Resolved (2026-03-06)** — Neo4j is the source of truth. Per [[repo_resolution]], projects no longer have a shared repository, so there is no natural home for `knowledge/*.md` files. Neo4j models the knowledge graph natively (notes as nodes, relationships as edges). pgvector serves as a derived search index updated via write-through. Obsidian-compatible files can be exported on demand via `kb_export` for human browsing. See "Architecture" section.

21. ~~**System Neo4j connection management**~~
    **Resolved** — Neo4j becomes a full system database, same tier as PostgreSQL. New env vars `NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (separate from `DEFAULT_DS_NEO4J_*` external datasource vars). `KnowledgeGraphDB` service class wraps `Neo4jDB` for knowledge operations. Connection on orchestrator startup, schema init in `orchestrator/init.py`. Reuse `Neo4jDB` driver/session management, wrap with knowledge-specific methods (create note, read note, graph traversal, etc.).

22. ~~**Neo4j availability**~~
    **Resolved** — Neo4j is a required system database. If it's down, the system is degraded — same as if PostgreSQL were down. No graceful fallback. KB tools return errors, agents continue working without knowledge injection (same as a job without KB enabled). Neo4j uptime is an infrastructure concern, not an application concern.

23. ~~**Knowledge quality gate**~~
    **Resolved** — No gate for MVP. The curator is a specialist with full autonomy. Bad notes can be updated, superseded, or deleted via the cockpit knowledge management UI. Add `pending_review` status gating later if quality issues emerge in practice.

24. ~~**Write-through atomicity**~~
    **Resolved** — Neo4j first (source of truth), PostgreSQL second (search index). If the pgvector upsert fails after a successful Neo4j write, the note exists but isn't searchable — a safe failure mode. `rebuild_search_index(project_id)` is the recovery path. All write-through failures are logged for monitoring.

25. ~~**Embedding dimension portability**~~
    **Resolved** — Make the vector dimension configurable via an environment variable `EMBEDDING_DIMENSIONS` (default: `1536` for `text-embedding-3-small`). The schema migration reads this variable to set the column type. If the user changes `EMBEDDING_MODEL`, they must also update `EMBEDDING_DIMENSIONS` and rebuild the search index. The `rebuild_search_index` function handles re-embedding with the new model.

26. ~~**Curator access to parent job files**~~
    **Resolved** — This is the general "persistent subjob branch synchronization" problem, shared with the critic. Persistent subjobs (curator, critic) that enter `waiting` status need to: (a) squash-merge their branch into the parent's `main` when entering `waiting` (so their output is visible to the parent), (b) pull the parent's `main` into their branch when resumed (so they see the latest workspace.md, archive/, etc.). The orchestrator handles this automatically on `resume_job()` — merge parent's `main` into the subjob branch before waking the subjob. For the curator specifically: reads from git (workspace.md, archive/), writes to Neo4j (knowledge). This split is fine — the curator just needs current files to extract from.

27. ~~**Note deletion and lifecycle**~~
    **Resolved** — Agent tools only perform status transitions (`active` → `superseded`, `archived`, etc.). No `kb_delete` tool. Hard deletion is available via the cockpit's project knowledge management UI (a dedicated tab showing all notes with filters, bulk actions, and delete). When a note is superseded/archived, it's excluded from context injection but still findable via `kb_list(status="superseded")` and graph traversal. The `ON DELETE CASCADE` on `knowledge_index.project_id` handles PostgreSQL cleanup on project deletion.

28. ~~**Project deletion cleanup**~~
    **Resolved** — The orchestrator's `delete_project()` path must explicitly delete all Neo4j nodes with the project's `project_id` (a single Cypher: `MATCH (n {project_id: $pid}) DETACH DELETE n`). PostgreSQL `knowledge_index` rows are cascade-deleted via the FK. Both cleanups happen in the same `delete_project()` call.

### Open

29. **Cockpit knowledge management UI** — The project detail view needs a new "Knowledge" tab showing all notes for the project. Features: filter by type/status/tag, view note content and relationships, edit status, hard delete, trigger `kb_export`. This is a Phase 3 deliverable but the API endpoints (list notes, update status, delete note, export) should be designed in Phase 1 to ensure the data model supports them.

30. **Knowledge base backup and restore** — Neo4j is the source of truth but has no equivalent of `pg_dump`. For backup/portability: `kb_export` produces Obsidian files, but re-importing requires parsing them back into Neo4j + pgvector. Should there be a `kb_import(path)` tool that reads `.md` files with frontmatter and creates notes? Or is a Neo4j database dump (via `neo4j-admin dump`) sufficient? The export/import path is more portable; the dump is more complete (preserves internal IDs and relationships exactly). Recommendation: support both — `kb_export`/`kb_import` for portability, `neo4j-admin dump` for full backup.

## Implementation Plan

### Phase 1: Knowledge Base Infrastructure (Neo4j + pgvector + Tools)

Build the full foundation: system Neo4j connection, Neo4j schema, pgvector search index, write-through logic, agent tools, and context injection. Everything works from day one — graph queries and search are both Phase 1.

**System Neo4j setup:**
1. [x] Add system Neo4j env vars (`NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`) and `EMBEDDING_DIMENSIONS` (default: 1536) to `.env.example`
2. [x] Implement `KnowledgeGraphDB` service class (`src/services/knowledge_graph.py`) — wraps `Neo4jDB` for system-level knowledge operations (create note, read note, list notes, create relationship, graph traversal queries)
3. [x] Add Neo4j schema initialization to `orchestrator/init.py` — create constraints and indexes (see "Neo4j Schema Initialization" section)
4. [x] Add `knowledge_index` table + `knowledge_hybrid_search()` function to PostgreSQL schema (see "Schema" section)

**Agent tools (write-through):**
5. [x] Implement `kb_write` tool — creates Note node in Neo4j + upserts `knowledge_index` row (embedding via `EmbeddingService`). Creates relationship edges for `links` parameter. Creates/merges Tag and Keyword nodes.
6. [x] Implement `kb_update` tool — updates Neo4j node properties + pgvector row (write-through). Supports append to content, status change, add tags/links.
7. [x] Implement `kb_read` tool — reads full note from Neo4j (content + metadata + relationships)
8. [x] Implement `kb_list` tool — queries Neo4j by type, tag, status, job_id
9. [x] Implement `kb_search` tool — queries `knowledge_hybrid_search()` in pgvector (RRF over dense + sparse + recency)
10. [x] Implement graph query tools: `kb_related` (2-hop traversal), `kb_contradictions` (CONTRADICTS edges), `kb_provenance` (DERIVED_FROM chains), `kb_unanswered` (questions without ANSWERS edges)
11. [x] Register as `knowledge` tool category in `src/tools/registry.py`

**Context injection:**
12. [x] Auto-generate knowledge summary (note counts by type, recent notes) — `KnowledgeStore.get_summary()` provides summary data, `assemble_knowledge_block()` formats for injection
13. [x] Implement knowledge retrieval injection — query `knowledge_hybrid_search()` with current task context, inject top-K as transient message (`src/core/knowledge_injection.py`, wired into `src/graph.py`)
14. [x] `rebuild_search_index(project_id)` — `KnowledgeStore.rebuild_from_notes()` reads all notes from Neo4j, re-inserts into pgvector (cold start / recovery). Handles Neo4j DateTime conversion.

**Export:**
15. [x] Implement `kb_export(project_id, path)` — reads all notes and relationships from Neo4j, writes Obsidian-compatible `.md` files with frontmatter and `[[wikilinks]]`

**Test:**
16. [x] Test: use `kb_write` to create notes → verify Neo4j nodes + pgvector rows exist → `kb_search` returns ranked results → `kb_related` traverses graph → `kb_export` generates valid Obsidian files

### Phase 2: Curator Subjob

With the knowledge tools and search working, build the curator that uses them. The curator is a persistent subjob that runs in parallel with the working agent, triggered on every archive phase.

**Config + instructions:**
17. [x] Create `config/experts/curator/config.yaml` — extends defaults, has `knowledge` + `workspace` + `git` tools, `autonomy: full`
18. [x] Create `config/experts/curator/instructions.md` — curation guide (what to extract, how to classify, when to update vs. create, editorial judgment rules, retrieval message generation)
19. [x] Create `config/experts/curator/curation_instructions.md` — instruction file triggered before `kb_write` (note quality standards, linking conventions, retrieval message quality)

**Subjob lifecycle:**
20. [x] Implement `create_curation_job()` in `src/api/orchestrator_client.py` — follows `create_verification_job()` pattern, passes job context via formatted instructions (same approach as critic's freeze_data)
21. [x] Add `curator` config section to `defaults.yaml` (`enabled`, `curator_config`, `autonomy: full`)
22. [x] Wire archive phase trigger — after the first archive phase, spawn curator subjob (one subjob per job, persistent)
23. [x] Implement archive phase notification — on each subsequent archive phase, call `resume_job()` on the curator with new phase data as feedback (reuses existing `waiting` → resume infrastructure)
24. [x] Wire final pass trigger — after critic approves (or after job completion if no critic), send final signal to curator with memories + output + freeze_data

**Curation logic:**
25. [x] Curator uses `kb_search` to check existing knowledge before writing (dedup, linking)
26. [x] Curator generates retrieval messages for each note (synthetic queries for when the note should surface)
27. [x] Curator reads `memories` table for the target job during final pass and promotes valuable entries to knowledge notes
28. [x] Test: run a project job → curator processes archive phases in parallel → critic approves → curator final pass → next job can `kb_search` and find the curated notes

### Phase 3: Obsidian Export + Polish

29. [x] Cockpit UI: knowledge base viewer/browser (reads from Neo4j via orchestrator API)
30. [x] workspace.md → knowledge notes conversion tool (for existing projects)
31. [x] Memory Light → knowledge notes migration tool
32. [x] Test with real multi-job project: verify Job N+1 benefits from Job N's knowledge

## What Changes and What Stays

The curator model is additive — it doesn't replace existing systems, it builds on them:

**Stays the same:**
- **`workspace.md`** — Working agents still write it during execution. The curator reads it as a source for knowledge extraction. For non-project jobs and backward compatibility, workspace.md injection continues unchanged.
- **`plan.md`** — Working agents still write it. The curator reads it.
- **`memories` PostgreSQL table** — Memory Light keeps running during execution (observer, free sources). The table becomes a staging area that the curator reads during its final pass.
- **Memory Light injection** — Continues injecting job-scoped memories during execution. Knowledge injection adds project-scoped context on top.
- **Critic subjob** — Continues reviewing deliverables post-completion. Curator runs in parallel from the first archive phase; its final pass triggers after critic approval.

**New:**
- **System Neo4j** — Neo4j promoted from external datasource to required system database (same tier as PostgreSQL). New env vars: `NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`. Initialized alongside PostgreSQL in `orchestrator/init.py`.
- **Neo4j knowledge graph** — Notes, tags, keywords as nodes; relationships as edges. Source of truth for all project knowledge. Scoped by `project_id`.
- **`knowledge_index` table** — Derived search index in PostgreSQL (pgvector + tsvector), updated via write-through on every `kb_write`.
- **Knowledge agent tools** — `kb_write`, `kb_read`, `kb_search`, `kb_list`, `kb_update`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`, `kb_export`.
- **Knowledge retrieval injection** — Project-scoped context injected via hybrid search (pgvector + tsvector + RRF). Works from Phase 1.
- **Curator subjob** (Phase 2) — New expert config, spawned after first archive phase, runs in parallel with working agent, continuously extracts knowledge. Final pass after critic approval.
- **Obsidian export** — `kb_export` dumps Neo4j graph as `.md` files with frontmatter and wikilinks. One-way, on-demand.

**Related docs:**
- [[memory_light]] — Remains valid for: observer, extraction channels, RRF hybrid search, embedding models, injection hook. Memory Light is the capture stage; the curator is the promotion stage.
- [[obsidian]] — Remains valid for: Obsidian export format, research findings. The note schema now lives in Neo4j; Obsidian files are an export format, not the source of truth.
- [[projects]] — Remains valid for: database schema, API, workspace layout, cockpit UI. This doc adds Neo4j knowledge graph as project-level infrastructure.
- [[repo_resolution]] — The decision that projects don't have shared repos is what drove the move from files-as-source-of-truth to Neo4j-as-source-of-truth.

## References

- [[projects]] — Project model, database schema, merge flow, workspace layout
- [[obsidian]] — Note schema, Obsidian integration, CLI, research findings (A-MEM, Obsidian-Assist)
- [[memory_light]] — Observer, extraction channels, RRF hybrid search, embedding models, injection hook
- [[memories_mechanism]] — Full memory system design (research, scoping model, multi-backend architecture)
- [[working_memory]] — Working memory analysis, workspace.md patterns
- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110)
- [Zep/Graphiti: Temporal Knowledge Graph](https://arxiv.org/abs/2501.13956)
