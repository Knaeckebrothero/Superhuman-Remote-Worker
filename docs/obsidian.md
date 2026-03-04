---
tags:
  - knowledge-management
  - agent-architecture
  - planning
  - project-infrastructure
aliases:
  - Zettelkasten workspace
  - knowledge graph workspace
  - Obsidian integration
  - project knowledge base
related:
  - "[[project_knowledge_base]]"
  - "[[memories_mechanism]]"
  - "[[context_management]]"
  - "[[working_memory]]"
  - "[[vectorization]]"
  - "[[projects]]"
---

> **Note:** This document covers note schema details, research findings, and Obsidian CLI integration. For the unified design of how the knowledge base fits the project model, how it replaces Memory Light as the storage backend, and the full implementation plan, see [[project_knowledge_base]].

# Project Knowledge Base — Obsidian Integration & Note Schema

This document captures the design for the project-wide knowledge base — a core piece of infrastructure (like PostgreSQL or the todo system) that every project gets automatically. Agents write structured, interlinked notes throughout execution. Notes accumulate across jobs into a living knowledge graph that both humans (via Obsidian) and agents (via Neo4j + file tools) can query.

## Vision

The knowledge base is **not** an optional datasource or plugin. It is standard infrastructure that every project has, every agent writes to, and every agent reads from. It replaces `workspace.md` as the primary memory mechanism.

1. **Always present** — Every project gets a knowledge base on creation, like it gets a jobs repo and a todo system
2. **Always used** — Agents document what they did, what they learned, what decisions were made, what questions remain
3. **Cross-job accumulation** — Notes merge to `main` after each job, building a living project history
4. **Dual representation** — Obsidian-compatible markdown files (human-readable, git-versioned) mirrored to Neo4j (queryable, traversable)
5. **Bidirectional collaboration** — Users add notes in Obsidian, agents pick them up; agents write notes, users review them

### What goes in the knowledge base

Everything. The knowledge base is the canonical record of a project's life:

| Category | Examples |
|----------|---------|
| **Goals** | Project objectives, success criteria, definition of done |
| **Plans** | Roadmap, phase structure, milestones, priorities |
| **Approaches** | Architecture decisions, technology choices, trade-off analysis |
| **Learnings** | What worked, what didn't, debugging insights, performance findings |
| **Code** | What was written, why, where, key patterns and conventions |
| **State** | Current project status, what's done, what's in progress, what's blocked |
| **Questions** | Open items, unresolved decisions, things to investigate |
| **Sources** | Documents, URLs, conversations, requirements that informed decisions |

### What this replaces

| Before | After |
|--------|-------|
| `workspace.md` — monolithic blob rewritten each strategic phase | Atomic notes with links, incrementally updated |
| `plan.md` — single flat plan file | Plan notes, roadmap notes, milestone notes — all interlinked |
| `archive/phase_N_retrospective.md` — linear phase history | Retrospective notes linked to the decisions and findings they reference |
| Context lost on compaction | Query Neo4j or search files to reconstruct relevant context |
| Per-job isolation (unless merged) | Project-wide knowledge graph that every job reads and writes |

## Research Findings

### A-MEM Paper (Feb 2025)

[A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110) - Academic research on Zettelkasten-style memory for LLM agents:

- Notes have structured attributes: context, keywords, tags
- **Dynamic linking** - when new memories arrive, the system finds connections to existing notes
- **Memory evolution** - existing notes get updated when new related information arrives
- **Results**: Outperformed existing SOTA baselines across 6 foundation models

### Obsidian-Assist Project

[Obsidian-Assist](https://github.com/ya0002/obsidian-assist) - Uses Obsidian vault as LLM backend:

- Graph-based context retrieval: traverse neighbors, follow paths between notes
- Combines semantic search + graph traversal for context building
- User controls the knowledge graph structure

### Multi-Modal Retrieval Consensus

Research consensus is that combining retrieval methods beats any single approach:

| Retrieval Type | What it finds |
|----------------|---------------|
| Vector search | Semantically similar concepts |
| Graph traversal | Relationships between data |
| Keyword search | Exact matches |

**Key insight**: Making explicit connections between data works better than storing raw chunks.

### Obsidian CLI (Feb 2026)

[Obsidian CLI](https://help.obsidian.md/cli) — Official command-line interface released in Obsidian 1.12 (Feb 2026). 100+ commands providing full programmatic access to vaults, eliminating the need for custom file parsing and graph indexing.

**Key capabilities for agent integration:**

| Category | Commands | What it replaces |
|----------|----------|-----------------|
| File ops | `create`, `read`, `append`, `prepend`, `move`, `delete` | Custom markdown file I/O |
| Link queries | `backlinks`, `links`, `unresolved`, `orphans`, `deadends` | Custom wikilink parsing + graph DB |
| Properties | `property:set`, `property:read`, `property:remove` | Custom YAML frontmatter parsing |
| Search | `search`, `search:context` | Custom full-text indexing |
| Tags | `tags`, `tag` | Custom tag extraction |
| Tasks | `tasks`, `task` | Custom todo parsing |
| Templates | `templates`, `template:insert` | Custom note scaffolding |
| Developer | `eval`, `dev:cdp` | N/A (new capability) |

**Requirements:**
- Obsidian 1.12+ installed with CLI enabled (Settings → General → CLI)
- Obsidian app must be running (auto-launches if needed)
- Linux: symlink at `/usr/local/bin/obsidian`
- Early Access requires Catalyst license ($25), planned free for all users

**Caveat:** Requires Obsidian desktop running — not available in headless/server environments. A fallback path using direct file parsing is still needed for CI and remote agent execution.

### Additional Resources

- [Zettelkasten AI System](https://github.com/joshylchen/zettelkasten) - AI-powered knowledge management with MCP integration
- [Agentic AI with Knowledge Graph](https://loysbelleguie.medium.com/an-implement-of-an-agentic-ai-framework-powered-by-a-knowledge-graph-2-n-24319275291f) - Knowledge graphs as persistent agent memory
- [AI Agent Knowledge Base Architecture](https://www.infoworld.com/article/4091400/anatomy-of-an-ai-agent-knowledge-base.html) - Multi-modal retrieval strategies

## Architecture

### Core Principle: Two Representations, One Knowledge Base

The knowledge base has two synchronized representations, each serving a different audience:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Project Knowledge Base                          │
│                                                                     │
│   ┌──────────────────────┐          ┌──────────────────────┐       │
│   │  Markdown Files      │  sync    │  Neo4j Graph         │       │
│   │  (jobs repo)         │ ──────►  │  (project-level)     │       │
│   │                      │          │                      │       │
│   │  • Human-readable    │          │  • Cypher queries     │       │
│   │  • Git-versioned     │          │  • Path traversal     │       │
│   │  • Obsidian-viewable │          │  • Aggregation        │       │
│   │  • Diff-friendly     │          │  • Vector search      │       │
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

**Neo4j** is a derived, queryable mirror. A sync function converts markdown notes (frontmatter + wikilinks) into graph nodes and relationships. This gives agents Cypher-powered queries for relationship traversal, contradiction detection, provenance tracing, and aggregation — things that are hard to do by scanning files.

**Neither is optional.** Files exist because humans need to read and edit. Neo4j exists because agents need to query. The sync function bridges them.

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
│   │   └── questions/
│   │       └── caching-strategy.md
│   ├── output/                        ← Deliverables (existing)
│   └── experts/                       ← Project agent configs (existing)
│
├── Neo4j (project-level, auto-provisioned)
│   ├── (:Note {id, type, title, job_id, confidence, ...})
│   ├── (:Tag {name}), (:Keyword {name})
│   ├── [:REFERENCES], [:SUPPORTS], [:CONTRADICTS], [:DERIVED_FROM]
│   └── Vector index on note content embeddings (optional)
│
└── Jobs
    ├── Job 1 → creates notes on branch → merges → syncs to Neo4j
    ├── Job 2 → queries Neo4j + reads notes → creates more notes → merges
    └── ...
```

### Sync Flow

Files → Neo4j sync happens at well-defined points, not continuously:

```
1. Job completes → branch merges to main
                        ↓
2. Post-merge hook: parse knowledge/*.md files
                        ↓
3. For each note:
   - MERGE (:Note {id: frontmatter.id}) SET title, type, content, ...
   - Parse wikilinks → MERGE relationships
   - Parse tags → MERGE (:Tag) relationships
   - Update embeddings if content changed
                        ↓
4. Neo4j now reflects current state of main branch
```

User edits flow the same way:

```
User edits in Obsidian → git commit → push to jobs repo main
                                            ↓
                            Next job startup: sync knowledge/ → Neo4j
```

### Comparison with Current System

| Aspect | Current | Knowledge Base |
|--------|---------|----------------|
| Memory mechanism | `workspace.md` blob | Atomic interlinked notes |
| Storage | Single file in jobs repo | `knowledge/` directory + Neo4j graph |
| Querying | Read entire file | Cypher queries, file search, tag filtering |
| Cross-job continuity | Rewrite workspace.md each phase | Notes accumulate, link, and evolve |
| Human access | Read a long markdown file | Obsidian vault with graph view |
| Agent access | Injected as system message | Query tools (graph + file) |
| Scope | Per-job (merged on completion) | Project-wide (always available) |
| Provisioning | Automatic | Automatic (like jobs repo) |

## Note Schema

### Frontmatter Structure

```yaml
---
id: 20260201-1423
type: goal | plan | decision | learning | code | source | question | state
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

### Note Types

Expanded to cover everything a project needs to track:

| Type | Purpose | Example |
|------|---------|---------|
| **Goal** | Project objective or success criterion | "Support 1000 concurrent users" |
| **Plan** | Roadmap, phase plan, milestone | "Phase 3: API layer and auth" |
| **Decision** | Conclusion with reasoning and trade-offs | "Chose JWT because..." → links to [[oauth-analysis]] |
| **Learning** | Insight gained during execution | "JSONB queries need GIN index for >10k rows" |
| **Code** | What was written, where, why, key patterns | "Auth middleware pattern" → links to [[chose-jwt]] |
| **Source** | Document, URL, conversation, requirement | Reference to input spec |
| **Question** | Open item to resolve | "Which caching strategy?" |
| **State** | Current status of a component or area | "Auth: implemented, needs integration tests" |
| **Retrospective** | Phase review — what worked, what didn't | "Phase 2 retro" → links to learnings |

### Relationship Types

| Relationship | Meaning | Neo4j edge |
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

Each markdown note becomes a graph node. Relationships are derived from wikilinks and frontmatter:

```cypher
// Note node (1:1 with a markdown file)
(:Note {
  id: "20260201-1423",
  type: "decision",
  title: "Chose JWT over OAuth",
  status: "active",
  confidence: "high",
  job_id: "abc-123",
  phase: 3,
  created: datetime("2026-02-01T14:23:00Z"),
  modified: datetime("2026-02-01T15:30:00Z"),
  content_hash: "sha256:...",       // detect changes for re-sync
  embedding: [0.12, -0.34, ...]     // optional: vector for semantic search
})

// Relationships from wikilinks + frontmatter
(:Note {title: "Chose JWT"})-[:REFERENCES]->(:Note {title: "OAuth analysis"})
(:Note {title: "Chose JWT"})-[:ANSWERS]->(:Note {title: "Which auth method?"})
(:Note {title: "Auth middleware"})-[:IMPLEMENTS]->(:Note {title: "Chose JWT"})

// Tags as nodes for efficient filtering
(:Note)-[:TAGGED]->(:Tag {name: "authentication"})
(:Note)-[:HAS_KEYWORD]->(:Keyword {name: "JWT"})
```

## Benefits

### For Humans

- **Scannable** — Atomic notes easy to review vs reading a monolithic workspace.md
- **Navigable** — Follow links in Obsidian to understand how decisions connect
- **Editable** — Add your own notes, corrections, or context; agents pick them up on next job
- **Visualizable** — Obsidian graph view shows project structure at a glance
- **Searchable** — Tags, keywords, full-text search, and frontmatter filtering
- **Auditable** — Every note has `job_id` and `phase` — trace any finding back to the job that produced it

### For the Agent

- **Persistent memory** — Survives context compaction; query Neo4j or search files to recall anything
- **Context building** — Traverse related notes to reconstruct relevant context for current task
- **Memory evolution** — Update existing notes as understanding deepens, mark old decisions as superseded
- **Cross-job continuity** — Job 2 reads everything Job 1 learned without re-discovering it
- **Structured output** — Clear units for reasoning about, not one giant file to parse
- **Rich queries** — "What decisions depend on this assumption?" is a Cypher query, not a regex

## Integration with Current System

### Provisioning

The knowledge base is auto-provisioned when a project is created, like the jobs repo:

1. **`knowledge/` directory** — Created in the jobs repo on project init. Includes `.obsidian/` config with templates, graph settings, and tag hierarchy. Project-scoped (merges to `main`).
2. **Neo4j graph** — A project-level Neo4j database (or namespace within a shared instance). Auto-provisioned alongside the project. Not treated as an optional datasource — it's standard infrastructure.
3. **Sync function** — Registered as a post-merge hook. Parses `knowledge/*.md` and upserts to Neo4j.

For default (personal) projects, the knowledge base is still created but may be lighter-weight (no Neo4j until the user explicitly upgrades to a full project).

### Workspace Structure

```
workspace/job_<uuid>/
├── .git/                              ← jobs repo, on branch job/<short-id>/<slug>
├── knowledge/                         ← Obsidian vault (PROJECT-SCOPED — merges to main)
│   ├── .obsidian/                     ← Obsidian config, templates, graph settings
│   ├── _index.md                      ← Auto-generated Map of Content
│   ├── goals/
│   │   └── project-objective.md
│   ├── plans/
│   │   ├── roadmap.md
│   │   └── phase-3-api-design.md
│   ├── decisions/
│   │   └── chose-jwt-over-oauth.md
│   ├── learnings/
│   │   └── postgres-jsonb-perf.md
│   ├── code/
│   │   └── auth-middleware-pattern.md
│   ├── sources/
│   │   └── requirements-spec.md
│   ├── questions/
│   │   └── caching-strategy.md
│   └── state/
│       └── auth-module-status.md
├── todos.yaml                         ← Task list (JOB-SCOPED)
├── archive/                           ← Phase artifacts (JOB-SCOPED)
├── output/                            ← Deliverables (PROJECT-SCOPED)
└── repos/                             ← Source/reference repo clones (.gitignored)
```

Note: `workspace.md` and `plan.md` are replaced by notes in `knowledge/`. The agent's working memory is now distributed across queryable notes rather than concentrated in a single file. A `_index.md` Map of Content is auto-generated to provide an overview.

### Agent Tools

Knowledge base tools are a **standard tool category** (like `core`, `workspace`, or `coding`), always available to every agent. They are not opt-in.

**Tool category: `knowledge`**

```python
# === Writing ===

# Create a note — agent documents a finding, decision, learning, etc.
kb_write(
    title="Chose JWT over OAuth",
    type="decision",
    content="After evaluating both...\n\nLinked: [[oauth-analysis]], [[security-requirements]]",
    tags=["authentication", "security"],
    confidence="high"
)
# → Creates knowledge/decisions/chose-jwt-over-oauth.md with frontmatter
# → Auto-sets job_id, phase, created timestamp

# Update an existing note — add context, change status, add links
kb_update(
    note="chose-jwt-over-oauth",
    append="## Update (Phase 4)\nAfter load testing, JWT validation adds 2ms p99.",
    status="active",                    # or: superseded, resolved, archived
    add_tags=["performance"]
)

# === Reading (file-based, always available) ===

# Search notes by content
kb_search(query="authentication")       # Full-text search across all notes

# Read a specific note
kb_read(note="chose-jwt-over-oauth")    # Returns full content with frontmatter

# List notes by type, tag, status, or job
kb_list(type="decision")                # All decisions
kb_list(tag="authentication")           # All notes tagged authentication
kb_list(status="question")              # All open questions
kb_list(job_id="abc-123")              # Everything from a specific job

# === Querying (Neo4j-powered, rich traversal) ===

# Find related notes via graph traversal
kb_query(
    query="MATCH (n:Note {title: 'Chose JWT'})-[*1..2]-(related) RETURN related"
)

# Or use convenience wrappers:
kb_related(note="chose-jwt-over-oauth") # All notes within 2 hops
kb_contradictions()                      # Notes connected by CONTRADICTS edges
kb_provenance(note="auth-middleware")    # Trace back through DERIVED_FROM chains
kb_unanswered()                          # Questions with no ANSWERS relationship
kb_roadmap()                             # Goals → plans → state, ordered by dependency

# === Obsidian CLI (when desktop available, bonus capabilities) ===
# Automatically used when detected — provides richer results:
# obsidian backlinks, obsidian orphans, obsidian deadends, etc.
```

### When agents write notes

Knowledge base writing is woven into the existing phase model:

| Phase event | What gets written |
|-------------|-------------------|
| **Job start** | Agent reads existing knowledge base, writes a `state` note with current understanding |
| **Todo completion** | Agent writes `learning` or `code` notes for significant findings |
| **Decision made** | Agent writes a `decision` note with reasoning and links |
| **Question encountered** | Agent writes a `question` note |
| **Phase transition (strategic)** | Agent writes `retrospective` note, updates `state` and `plan` notes |
| **Job completion** | Agent updates `state` notes, marks resolved `question` notes |

This replaces the current pattern of rewriting `workspace.md` each strategic phase. Instead, knowledge accumulates incrementally.

### Sync Strategy

**Files as source of truth**, Neo4j as derived queryable index:

1. **Agent writes** a markdown note to `knowledge/` → immediately available via file-based tools (`kb_search`, `kb_read`, `kb_list`)
2. **Sync to Neo4j** happens at defined points (post-merge, job startup) → then available via graph tools (`kb_query`, `kb_related`)
3. **During a job**, the agent can also write directly to Neo4j for temporary working relationships, but canonical state is always the files
4. **Obsidian CLI** provides a third query path when desktop is available — bonus, not required

## Open Questions

### Resolved

1. ~~**Vault scope** — One vault per job, or single vault with all jobs?~~
   **Resolved** — One vault per project. The `knowledge/` directory lives in the project's jobs repo and merges to `main` across jobs. Each job sees the full project knowledge base. Cross-project knowledge is not in scope (projects are isolated).

2. ~~**Graph database choice** — Neo4j, or lighter alternative?~~
   **Resolved** — Neo4j, as core project infrastructure (not a datasource). Already in the stack, already has tooling. Auto-provisioned per project. For default/personal projects, can defer Neo4j provisioning until explicitly needed.

### Open

3. **Automatic linking** — How aggressive should auto-linking be?
   - Too much = noise, too little = missed connections
   - Option: agent writes explicit links; a periodic maintenance job suggests missing connections
   - Obsidian CLI's `unresolved` and `orphans` commands can help detect gaps

4. **Migration path** — How to handle existing `workspace.md` jobs?
   - Existing jobs keep `workspace.md` (backward compatible)
   - New jobs on projects with knowledge base enabled use the new system
   - Optional: conversion tool to split a `workspace.md` into atomic notes for bootstrapping

5. **Teaching the agent** — How to ensure agents write good notes?
   - Tool-enforced structure: `kb_write` validates type, requires tags, generates frontmatter (immediate)
   - Instruction file: `knowledge_guide.md` injected before `kb_write` calls (like `todo_guide.md`)
   - Templates: pre-built note templates per type in `.obsidian/templates/`
   - Fine-tuning on Obsidian corpora (expensive, future)

6. **Neo4j provisioning model** — Shared instance with project namespacing, or dedicated per project?
   - Shared instance is simpler to operate (single Neo4j container)
   - Namespace via labels: `(:Note {project_id: "abc-123"})`
   - Dedicated per project is cleaner isolation but heavier
   - Recommendation: shared instance with project labels, option to attach dedicated instance

7. **Context injection** — What replaces workspace.md injection in the system prompt?
   - Option A: inject a summary MOC (`_index.md`) — lightweight, always current
   - Option B: inject recent notes (last N by modified date) — more context but larger
   - Option C: inject nothing static; agent queries knowledge base as needed — cleanest but slower startup
   - Likely: A + C. Inject the MOC for orientation, agent queries for detail

8. **Obsidian CLI as optional enhancement** — Available on desktop only
   - Agent should detect CLI availability (`obsidian version`) and use it for richer queries when present
   - Not required — file tools + Neo4j cover all core functionality headlessly
   - Catalyst license ($25) required during Early Access, planned free later

## Implementation Plan

### Phase 1: File-based knowledge tools (MVP)

Core note CRUD — agents can write and read notes, no Neo4j required yet.

1. [ ] Finalize note schema (frontmatter fields, naming conventions, directory structure)
2. [ ] Implement `KnowledgeManager` class (`src/managers/knowledge.py`) — file-based note CRUD
3. [ ] Implement agent tools: `kb_write`, `kb_read`, `kb_search`, `kb_list`, `kb_update`
4. [ ] Register as `knowledge` tool category in `src/tools/registry.py` (always-on, not opt-in)
5. [ ] Create `knowledge_guide.md` instruction file (injected before `kb_write`, like `todo_guide.md`)
6. [ ] Create note templates per type in `.obsidian/templates/`
7. [ ] Add `knowledge/` directory provisioning to project creation flow
8. [ ] Update `.gitignore` patterns — `knowledge/` is project-scoped (merges to main)
9. [ ] Auto-generate `_index.md` (Map of Content) from note frontmatter
10. [ ] Inject `_index.md` as context (replaces workspace.md injection)

### Phase 2: Neo4j sync and graph queries

Add the queryable graph layer for relationship traversal and rich queries.

11. [ ] Implement sync function: parse `knowledge/*.md` → upsert Neo4j nodes + relationships
12. [ ] Register sync as post-merge hook and job-startup step
13. [ ] Implement graph query tools: `kb_query`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`
14. [ ] Add Neo4j auto-provisioning to project creation (shared instance with project labels)
15. [ ] Add vector embeddings for semantic search (optional, pgvector or Neo4j vector index)

### Phase 3: Obsidian CLI integration (desktop enhancement)

Optional layer — richer queries when Obsidian desktop is available.

16. [ ] Implement CLI availability detection (`obsidian version` probe)
17. [ ] Wrap CLI commands: `backlinks`, `links`, `orphans`, `deadends`, `search`, `property:set`
18. [ ] Use CLI results to augment file-based and graph tools when available
19. [ ] Add `.obsidian/` config templates (graph settings, tag hierarchy, workspace layout)

### Phase 4: Migration and polish

20. [ ] Build `workspace.md` → atomic notes conversion tool (for existing projects)
21. [ ] Update strategic phase model — retrospectives become notes, plan.md becomes plan notes
22. [ ] Integrate with Memory Light — memories become notes in the knowledge base
23. [ ] Cockpit UI: knowledge base viewer/browser (alternative to Obsidian for users without it)
24. [ ] Test with real multi-job project and iterate

## References

- [Zettelkasten Method](https://zettelkasten.de/posts/overview/)
- [Obsidian Help](https://help.obsidian.md/)
- [Obsidian CLI Documentation](https://help.obsidian.md/cli)
- [Obsidian 1.12 Changelog](https://obsidian.md/changelog/2026-02-27-desktop-v1.12.4/)
- [A-MEM Paper](https://arxiv.org/abs/2502.12110)
- [Obsidian-Assist](https://github.com/ya0002/obsidian-assist)

## Related

- [[memories_mechanism]] — Associative memory system with observer and vector storage → may merge into knowledge base (Phase 4)
- [[context_management]] — Context window management and compaction → knowledge base reduces compaction pressure
- [[working_memory]] — Working memory implementation → `_index.md` injection replaces workspace.md injection
- [[vectorization]] — Vector embedding approaches → used for semantic search in Neo4j or pgvector
- [[projects]] — Project model and multi-job architecture → knowledge base is project-level infrastructure
