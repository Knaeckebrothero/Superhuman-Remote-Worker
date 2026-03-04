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
  - "[[memory_light]]"
  - "[[memories_mechanism]]"
  - "[[obsidian]]"
  - "[[working_memory]]"
---

# Project Knowledge Base — What Jobs Share

This document is the single authoritative reference for how projects share state between jobs. It unifies concepts previously scattered across [[projects]] (merge flow, what reaches `main`), [[obsidian]] (note schema, dual representation, sync flow), and [[memory_light]] (retrieval, injection, observer). Those documents remain valid for their respective implementation details; this one captures the overarching design.

## The Core Problem

The project system (Phase 1–4: completed) gives us the infrastructure — repos, branches, merge flow, datasources. But the question it never fully answered was:

**What is the shared artifact that accumulates across jobs?**

For code projects the answer is obvious — jobs merge code changes to a shared repo. But even then, the code repo isn't the jobs repo. And for non-code projects (research, writing, analysis), there's no code to merge. So what's the thing that makes Job N+1 smarter than Job 1?

## The Answer: Knowledge

The shared artifact is **knowledge**. Every job produces knowledge — decisions made, things learned, patterns discovered, questions raised, state recorded. Today this knowledge lives in `workspace.md` (a monolithic blob rewritten each strategic phase) and dies when the job ends. What survives the merge to `main` is just the final `workspace.md` and whatever's in `output/`.

The project knowledge base changes this. Jobs push structured, interlinked **knowledge notes** to the project's jobs repo. These notes are:

1. **Obsidian-compatible markdown files** — human-readable, git-versioned, browsable in Obsidian or the cockpit
2. **Vector-encoded** — embedded for semantic search via pgvector
3. **Graph-synced** — mirrored to Neo4j for relationship traversal and rich queries

This gives us three retrieval channels (files, vectors, graph) that feed into the existing memory injection system. When a new job starts, it inherits the full knowledge base from `main` and can query it through all three channels. The project gets smarter with every job.

## How It Works

```
Job 1 runs
  → writes knowledge notes (decisions, learnings, state, questions)
  → completes, merges to main
  → post-merge: sync knowledge/*.md → Neo4j + vector embeddings
                                              ↓
Job 2 starts
  → clones main → has full knowledge/ directory
  → queries Neo4j graph + vector search + file search
  → "What architectural decisions were made?"
  → "What did we try last week?"
  → "What are the credentials for the staging DB?"
  → writes more notes → merges → graph grows
                                        ↓
Job 3 starts → richer knowledge base → smarter from day one
```

### For Code Projects

The same model, with an additional repo. The project has:
- **Jobs repo** — knowledge lives here (`knowledge/` directory on `main`)
- **Source repos** — code lives there (agents push code changes)

Jobs push code to source repos AND knowledge to the jobs repo. Both accumulate on `main`. The knowledge base records what was built, why, what patterns were followed, what didn't work — context that code alone doesn't capture.

### For Non-Code Projects

The jobs repo IS the content. Research notes, document analysis, writing drafts, data findings — all structured as knowledge notes. The knowledge base is the project's primary deliverable.

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
| `workspace.md` — monolithic blob rewritten each strategic phase | Atomic notes with links, incrementally updated |
| `plan.md` — single flat plan file | Plan notes, roadmap notes, milestone notes — all interlinked |
| `archive/phase_N_retrospective.md` — linear phase history | Retrospective notes linked to the decisions and findings they reference |
| Memory Light PostgreSQL table — parallel memory system | Memories ARE notes in the knowledge base |
| Context lost on compaction | Query Neo4j or search files to reconstruct relevant context |
| Per-job isolation (unless merged) | Project-wide knowledge graph that every job reads and writes |

## Architecture

### Two Representations, One Knowledge Base

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Project Knowledge Base                          │
│                                                                     │
│   ┌──────────────────────┐          ┌──────────────────────┐       │
│   │  Markdown Files      │  sync    │  Neo4j Graph +       │       │
│   │  (jobs repo)         │ ──────►  │  pgvector embeddings │       │
│   │                      │          │  (project-level)     │       │
│   │  • Human-readable    │          │  • Cypher queries     │       │
│   │  • Git-versioned     │          │  • Path traversal     │       │
│   │  • Obsidian-viewable │          │  • Vector search      │       │
│   │  • Diff-friendly     │          │  • Keyword search     │       │
│   └──────────┬───────────┘          └──────────┬───────────┘       │
│              │                                  │                   │
│              │         ┌──────────┐             │                   │
│              └────────►│  Agent   │◄────────────┘                   │
│                        │  Tools   │                                 │
│   ┌──────────┐        │          │        ┌──────────┐             │
│   │ Obsidian │◄───────│ kb_write │───────►│ Cockpit  │             │
│   │ (user)   │        │ kb_query │        │ (user)   │             │
│   └──────────┘        │ kb_search│        └──────────┘             │
│                        └──────────┘                                 │
└─────────────────────────────────────────────────────────────────────┘
```

**Markdown files** are the source of truth — git-versioned, human-editable, Obsidian-compatible. They live in `knowledge/` inside the project's jobs repo and merge to `main` across jobs.

**Neo4j + pgvector** are derived, queryable mirrors. A sync function converts markdown notes (frontmatter + wikilinks) into graph nodes and relationships, and embeds note content for vector search. This gives agents Cypher-powered queries for relationship traversal, contradiction detection, provenance tracing, and semantic similarity — things that are hard to do by scanning files.

**Neither is optional.** Files exist because humans need to read and edit. Neo4j + vectors exist because agents need to query. The sync function bridges them.

### How It Fits the Project Model

The knowledge base is project-level infrastructure, like the jobs repo and the todo system:

```
Project "My-App"
├── Jobs Repo (Gitea, shared across jobs)
│   ├── knowledge/                     ← Obsidian vault (project-scoped, merges to main)
│   │   ├── .obsidian/                 ← Obsidian settings, templates, graph config
│   │   ├── _index.md                  ← Map of Content (auto-generated)
│   │   ├── goals/
│   │   │   └── project-objective.md
│   │   ├── plans/
│   │   │   ├── roadmap.md
│   │   │   └── phase-3-api-design.md
│   │   ├── decisions/
│   │   │   └── chose-jwt-over-oauth.md
│   │   ├── learnings/
│   │   │   └── postgres-jsonb-perf.md
│   │   ├── code/
│   │   │   └── auth-middleware-pattern.md
│   │   ├── sources/
│   │   │   └── requirements-spec.md
│   │   ├── questions/
│   │   │   └── caching-strategy.md
│   │   └── state/
│   │       └── auth-module-status.md
│   ├── output/                        ← Deliverables (existing)
│   └── experts/                       ← Project agent configs (existing)
│
├── Neo4j (project-level, auto-provisioned)
│   ├── (:Note {id, type, title, job_id, confidence, ...})
│   ├── (:Tag {name}), (:Keyword {name})
│   ├── [:REFERENCES], [:SUPPORTS], [:CONTRADICTS], [:DERIVED_FROM]
│   └── Vector index on note content embeddings
│
└── Jobs
    ├── Job 1 → creates notes on branch → merges → syncs to Neo4j + vectors
    ├── Job 2 → queries Neo4j + reads notes → creates more notes → merges
    └── ...
```

### Workspace Layout

```
workspace/job_<uuid>/
├── .git/                              ← jobs repo, on branch job/<short-id>/<slug>
├── knowledge/                         ← Obsidian vault (PROJECT-SCOPED — merges to main)
│   ├── .obsidian/                     ← Obsidian config, templates, graph settings
│   ├── _index.md                      ← Auto-generated Map of Content
│   ├── goals/
│   ├── plans/
│   ├── decisions/
│   ├── learnings/
│   ├── code/
│   ├── sources/
│   ├── questions/
│   └── state/
├── todos.yaml                         ← Task list (JOB-SCOPED, gitignored on main)
├── archive/                           ← Phase artifacts (JOB-SCOPED, gitignored on main)
├── output/                            ← Deliverables (PROJECT-SCOPED)
├── experts/                           ← Project agent configs (PROJECT-SCOPED)
└── repos/                             ← Source/reference repo clones (gitignored)
```

Note: `workspace.md` and `plan.md` are replaced by notes in `knowledge/`. The agent's working memory is distributed across queryable notes rather than concentrated in a single file. A `_index.md` Map of Content is auto-generated to provide an overview and injected as context (replacing workspace.md injection).

## Note Schema

### Frontmatter

```yaml
---
id: 20260201-1423
type: goal | plan | decision | learning | code | source | question | state | retrospective
tags: [requirement, authentication]
keywords: [OAuth, JWT, session]
created: 2026-02-01T14:23:00Z
modified: 2026-02-01T15:30:00Z
job_id: abc-123
phase: 3
confidence: high | medium | low
status: active | resolved | superseded | archived
---
```

Wikilinks (`[[note-name]]`) go in the body text, not frontmatter. This keeps frontmatter machine-parseable and lets links appear in natural context.

### Relationship Types

Derived from wikilinks and explicit frontmatter:

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

### Neo4j Node Model

```cypher
(:Note {
  id: "20260201-1423",
  type: "decision",
  title: "Chose JWT over OAuth",
  status: "active",
  confidence: "high",
  job_id: "abc-123",
  project_id: "proj-456",
  phase: 3,
  created: datetime("2026-02-01T14:23:00Z"),
  modified: datetime("2026-02-01T15:30:00Z"),
  content_hash: "sha256:...",
  embedding: [0.12, -0.34, ...]
})

(:Note {title: "Chose JWT"})-[:REFERENCES]->(:Note {title: "OAuth analysis"})
(:Note {title: "Chose JWT"})-[:ANSWERS]->(:Note {title: "Which auth method?"})
(:Note {title: "Auth middleware"})-[:IMPLEMENTS]->(:Note {title: "Chose JWT"})

(:Note)-[:TAGGED]->(:Tag {name: "authentication"})
(:Note)-[:HAS_KEYWORD]->(:Keyword {name: "JWT"})
```

## Memory Integration — One System, Not Two

This is the key architectural decision: **Memory Light's storage backend IS the knowledge base.** There is no separate `memories` PostgreSQL table running in parallel with a `knowledge/` directory. They are the same system.

### How Memory Channels Map to Knowledge Notes

Memory Light defines 5 extraction channels (observer, todo completion, compaction, phase archive, tool errors). Each becomes a knowledge note:

| Memory Channel | Note Type | How |
|----------------|-----------|-----|
| **Observer** | `learning`, `decision`, `state` | Observer LLM extracts insights → `kb_write()` |
| **Todo completion** | `learning`, `code` | Todo completion notes → `kb_write()` with `type` based on content |
| **Compaction summary** | `state` | Compaction narrative → `kb_write(type="state")` |
| **Phase archive** | `retrospective` | Phase retro → `kb_write(type="retrospective")` |
| **Tool errors** | `learning` | Error-solution pairs → `kb_write(type="learning", tags=["error-solution"])` |

### Retrieval Flow

When the agent needs context, the existing injection hook queries the knowledge base instead of a separate memory table:

```
Agent execute loop
    │
    ├─ Context compaction (existing)
    ├─ Todo injection (existing)
    ├─ _index.md injection (replaces workspace.md injection)
    ├─ ★ Knowledge retrieval (replaces memory injection)
    │   ├─ Dense vector search (pgvector on note embeddings)
    │   ├─ Sparse keyword search (tsvector on note content)
    │   ├─ Graph traversal (Neo4j — related notes within N hops)
    │   ├─ Recency bias (most recently modified notes)
    │   └─ RRF fusion → top-K notes → inject as transient message
    │
    ├─ Instruction file injection (existing)
    └─ LLM call
```

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

Notes are always project-scoped. The `job_id` in frontmatter records provenance (which job created this note) but retrieval queries filter by `project_id`:

- **During a job**: agent reads and writes to `knowledge/` on its branch. File-based tools work immediately. Neo4j/vector queries return the last-synced state from `main` plus any notes the current job has written (synced on write).
- **On merge**: post-merge hook re-syncs the full `knowledge/` directory to Neo4j + vectors. This is the canonical sync point.
- **Next job**: clones `main` with full `knowledge/`, queries return everything from all previous jobs.

This replaces Memory Light's Phase 5 ("cross-job memory via project_id on the memories table") with something richer — structured, interlinked notes instead of flat text blobs.

## Sync Flow

Files → Neo4j + pgvector sync happens at well-defined points:

```
1. Job completes → branch merges to main
                        ↓
2. Post-merge hook: parse knowledge/*.md files
                        ↓
3. For each note:
   - MERGE (:Note {id: frontmatter.id}) SET title, type, content, ...
   - Parse wikilinks → MERGE relationships
   - Parse tags → MERGE (:Tag) relationships
   - Generate embedding if content changed (content_hash comparison)
   - Upsert embedding to pgvector table
                        ↓
4. Neo4j + pgvector now reflect current state of main branch
```

User edits flow the same way:

```
User edits in Obsidian → git commit → push to jobs repo main
                                            ↓
                            Next job startup: sync knowledge/ → Neo4j + pgvector
```

### Sync During Job Execution

During a job, notes written by the agent are immediately available via file tools (`kb_read`, `kb_search`, `kb_list`). For Neo4j and vector availability during execution, two options:

- **Option A (simple)**: Sync to Neo4j/vectors only on merge. During the job, file-based tools are sufficient. Graph queries return the state from `main`.
- **Option B (richer)**: Also sync on write. When `kb_write()` creates a note, it also upserts the Neo4j node and embedding. This means the agent can immediately query relationships with its own newly-written notes.

Recommendation: Start with Option A. Add Option B if agents need to query their own notes via graph traversal during a single job.

## Agent Tools

Knowledge base tools are a **standard tool category** (like `core`, `workspace`, or `coding`), always available:

```python
# === Writing ===

kb_write(
    title="Chose JWT over OAuth",
    type="decision",
    content="After evaluating both...\n\nLinked: [[oauth-analysis]], [[security-requirements]]",
    tags=["authentication", "security"],
    confidence="high"
)
# → Creates knowledge/decisions/chose-jwt-over-oauth.md with frontmatter
# → Auto-sets job_id, phase, created timestamp

kb_update(
    note="chose-jwt-over-oauth",
    append="## Update (Phase 4)\nAfter load testing, JWT validation adds 2ms p99.",
    status="active",
    add_tags=["performance"]
)

# === Reading (file-based, always available) ===

kb_search(query="authentication")       # Full-text search across all notes
kb_read(note="chose-jwt-over-oauth")    # Returns full content with frontmatter
kb_list(type="decision")                # All decisions
kb_list(tag="authentication")           # All notes tagged authentication
kb_list(status="question")              # All open questions
kb_list(job_id="abc-123")              # Everything from a specific job

# === Querying (Neo4j-powered, rich traversal) ===

kb_query(
    query="MATCH (n:Note {title: 'Chose JWT'})-[*1..2]-(related) RETURN related"
)

# Convenience wrappers:
kb_related(note="chose-jwt-over-oauth") # All notes within 2 hops
kb_contradictions()                      # Notes connected by CONTRADICTS edges
kb_provenance(note="auth-middleware")    # Trace back through DERIVED_FROM chains
kb_unanswered()                          # Questions with no ANSWERS relationship
```

### When Agents Write Notes

Knowledge base writing is woven into the existing phase model:

| Phase Event | What Gets Written |
|-------------|-------------------|
| **Job start** | Agent reads existing KB, writes a `state` note with current understanding |
| **Todo completion** | Agent writes `learning` or `code` notes for significant findings |
| **Decision made** | Agent writes a `decision` note with reasoning and links |
| **Question encountered** | Agent writes a `question` note |
| **Phase transition** | Agent writes `retrospective` note, updates `state` and `plan` notes |
| **Job completion** | Agent updates `state` notes, marks resolved `question` notes |
| **Observer extraction** | Observer LLM creates notes from conversation insights (async) |
| **Compaction** | Compaction summary stored as `state` note (free source) |
| **Tool error** | Error-solution pair stored as `learning` note (free source) |

## Context Injection — What Replaces workspace.md

Today, `workspace.md` is injected as a transient message every LLM call. The knowledge base replaces this with two injection layers:

1. **`_index.md` (always injected)** — Auto-generated Map of Content. Lightweight overview of what's in the knowledge base: note counts by type, recent activity, key links. Gives the agent orientation without loading everything.

2. **Retrieved knowledge (dynamically injected)** — Top-K relevant notes selected by the same RRF hybrid search from Memory Light (dense + sparse + recency + graph traversal). Assembled into a transient message. Budget: configurable, default 5,000–10,000 tokens.

The agent can also actively query via `kb_search`, `kb_query`, etc. — the injection is passive recall, the tools are active recall.

## Provisioning

The knowledge base is auto-provisioned when a project is created:

1. **`knowledge/` directory** — Created in the jobs repo on project init. Includes `.obsidian/` config with templates, graph settings, and tag hierarchy. Project-scoped (merges to `main`).
2. **Neo4j namespace** — A project-level namespace within the shared Neo4j instance (label-based: `(:Note {project_id: "abc-123"})`). Auto-provisioned alongside the project.
3. **pgvector table** — Embeddings stored in the existing system PostgreSQL, scoped by `project_id`.
4. **Sync hook** — Registered as a post-merge callback. Parses `knowledge/*.md` and upserts to Neo4j + pgvector.

For default (personal) projects, the knowledge base is still created but may be lighter-weight (no Neo4j sync until the user explicitly upgrades to a full project or the note count exceeds a threshold).

## Migration Path

### Existing Jobs (Pre-Knowledge Base)

Existing jobs keep `workspace.md` (backward compatible). No breaking changes. The knowledge base is opt-in at the project level initially, then becomes the default for new projects.

### workspace.md → Knowledge Notes Conversion

An optional migration tool splits an existing `workspace.md` into atomic notes:

1. Parse sections (headings become note titles)
2. Classify by content (decisions, learnings, state, etc.)
3. Generate frontmatter
4. Extract and convert cross-references to wikilinks
5. Write to `knowledge/` and sync

### Memory Light → Knowledge Base Migration

For projects already using Memory Light (the `memories` PostgreSQL table):

1. Export memories as markdown notes (content → body, keywords → tags, memory_type → note type)
2. Write to `knowledge/` directory
3. Sync to Neo4j
4. Decommission the `memories` table for that project

New projects skip this — they use the knowledge base from day one.

## Open Questions

### Resolved

1. ~~**What do projects share between jobs?**~~
   **Resolved** — The knowledge base. Structured markdown notes that accumulate on `main`, mirrored to Neo4j and pgvector for querying. This is the answer to the core project question.

2. ~~**Is Memory Light a separate system?**~~
   **Resolved** — No. Memory extraction channels (observer, todo completion, compaction, phase archive, tool errors) write to the knowledge base via `kb_write()`. Retrieval queries the same knowledge base. One system.

3. ~~**Vault scope — one per job or per project?**~~
   **Resolved** — One per project. The `knowledge/` directory lives in the project's jobs repo and merges to `main` across jobs.

4. ~~**Graph database choice?**~~
   **Resolved** — Neo4j, as core project infrastructure. Already in the stack, already has tooling. Shared instance with project-label namespacing.

### Open

5. **Sync-on-write vs sync-on-merge** — Should notes be synced to Neo4j/vectors immediately when written, or only on merge? Immediate sync is richer but adds complexity. See "Sync During Job Execution" above.

6. **Automatic linking** — How aggressive should auto-linking be? Too much = noise, too little = missed connections. Likely: agent writes explicit links; a periodic maintenance pass suggests missing connections.

7. **Context injection budget** — How many tokens of retrieved knowledge to inject per LLM call? Memory Light defaults to 5,000. With richer structured notes, this may need tuning. Start with 5,000, adjust based on observed impact.

8. **Teaching the agent to write good notes** — Tool-enforced structure (`kb_write` validates type, requires tags, generates frontmatter) plus an instruction file (`knowledge_guide.md` injected before `kb_write` calls, like `todo_guide.md`).

9. **Obsidian CLI integration** — Optional desktop enhancement. Agent detects CLI availability and uses it for richer queries (backlinks, orphans, deadends). Not required — file tools + Neo4j cover all core functionality headlessly.

## Implementation Plan

### Phase 1: File-Based Knowledge Tools (MVP)

Core note CRUD. Agents can write and read notes. No Neo4j or vector search yet — file-based search only.

1. [ ] Finalize note schema (frontmatter fields, naming conventions, directory structure)
2. [ ] Implement `KnowledgeManager` class (`src/managers/knowledge.py`) — file-based note CRUD
3. [ ] Implement agent tools: `kb_write`, `kb_read`, `kb_search`, `kb_list`, `kb_update`
4. [ ] Register as `knowledge` tool category in `src/tools/registry.py` (always-on)
5. [ ] Create `knowledge_guide.md` instruction file (injected before `kb_write`)
6. [ ] Create note templates per type in `.obsidian/templates/`
7. [ ] Add `knowledge/` directory provisioning to project creation flow
8. [ ] Auto-generate `_index.md` (Map of Content) from note frontmatter
9. [ ] Inject `_index.md` as context (replaces workspace.md injection for KB-enabled projects)

### Phase 2: Memory Channel Integration

Wire the existing Memory Light extraction channels to write knowledge notes instead of (or alongside) the `memories` table.

10. [ ] Adapt observer to call `kb_write()` instead of `memory_manager.store()`
11. [ ] Adapt free sources (todo completion, compaction, phase archive, tool errors) to `kb_write()`
12. [ ] Implement retrieval from file-based search for knowledge injection (replaces memory injection)
13. [ ] Test: run a full job, verify knowledge notes accumulate and appear in injection

### Phase 3: Neo4j Sync + Graph Queries

Add the queryable graph layer for relationship traversal and rich queries.

14. [ ] Implement sync function: parse `knowledge/*.md` → upsert Neo4j nodes + relationships
15. [ ] Register sync as post-merge hook and job-startup step
16. [ ] Implement graph query tools: `kb_query`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`
17. [ ] Add Neo4j namespace provisioning to project creation (shared instance, project labels)

### Phase 4: Vector Search + Hybrid Retrieval

Add vector embeddings and the full RRF hybrid search from Memory Light.

18. [ ] Embed note content via `EmbeddingService` (text-embedding-3-small, same as Memory Light)
19. [ ] Store embeddings in pgvector, scoped by `project_id`
20. [ ] Implement hybrid retrieval: dense (pgvector) + sparse (tsvector) + graph (Neo4j) + recency
21. [ ] RRF fusion for ranking (same algorithm as Memory Light)
22. [ ] Replace file-only retrieval in injection hook with hybrid retrieval

### Phase 5: Obsidian CLI + Polish

23. [ ] CLI availability detection (`obsidian version` probe)
24. [ ] Wrap CLI commands: `backlinks`, `links`, `orphans`, `deadends`, `search`
25. [ ] `.obsidian/` config templates (graph settings, tag hierarchy, workspace layout)
26. [ ] Cockpit UI: knowledge base viewer/browser
27. [ ] workspace.md → knowledge notes conversion tool
28. [ ] Memory Light → knowledge base migration tool
29. [ ] Test with real multi-job project: verify Job N+1 benefits from Job N's knowledge

## What This Obsoletes

Once the knowledge base is fully implemented:

- **`workspace.md`** — Replaced by `_index.md` injection + knowledge notes. Existing projects keep `workspace.md` for backward compatibility.
- **`plan.md`** — Replaced by plan-type knowledge notes (interlinked with goals, decisions, state).
- **`memories` PostgreSQL table** — Replaced by knowledge notes with vector embeddings. The table remains for projects that haven't migrated.
- **Memory Light as a separate feature** — The extraction channels and injection mechanism survive, but they read/write the knowledge base, not a standalone memory store.

The Memory Light doc ([[memory_light]]) remains valid for: observer implementation, extraction prompt, RRF hybrid search algorithm, embedding model selection, injection hook pattern, config keys. The knowledge base doc doesn't re-specify those — it specifies where the data lives and how it flows between jobs.

The Obsidian doc ([[obsidian]]) remains valid for: note schema details, research findings (A-MEM, Obsidian-Assist), Obsidian CLI integration, `.obsidian/` config. The knowledge base doc doesn't re-specify those — it specifies how the knowledge base fits the project model.

The Projects doc ([[projects]]) remains valid for: database schema, API endpoints, merge flow, workspace layout, branch naming, cockpit UI. The knowledge base doc adds `knowledge/` to the "what merges to main" classification and defines the post-merge sync hook.

## References

- [[projects]] — Project model, database schema, merge flow, workspace layout
- [[obsidian]] — Note schema, Obsidian integration, CLI, research findings (A-MEM, Obsidian-Assist)
- [[memory_light]] — Observer, extraction channels, RRF hybrid search, embedding models, injection hook
- [[memories_mechanism]] — Full memory system design (research, scoping model, multi-backend architecture)
- [[working_memory]] — Working memory analysis, workspace.md patterns
- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110)
- [Zep/Graphiti: Temporal Knowledge Graph](https://arxiv.org/abs/2501.13956)
