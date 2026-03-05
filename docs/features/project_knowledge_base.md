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

## How It Works — The Curator Subjob

The key insight: **the working agent doesn't write knowledge notes.** It works exactly as it does today — writes workspace.md, completes todos, produces output. A specialized **curator subjob** runs after completion to extract knowledge and prepare the merge.

This follows the same pattern as the existing critic verification subjob (`verification.enabled` in config, `create_verification_job()` in `src/api/orchestrator_client.py`). The infrastructure for spawning post-completion subjobs already exists.

```
Job runs (unchanged)
  → writes workspace.md, plan.md, todos, output/
  → completes → status: pending_review
                    ↓
Critic subjob (existing)
  → reviews deliverables
  → approves or returns with feedback
                    ↓
Curator subjob (NEW)
  → reads the job's branch: workspace.md, plan.md, archive/, output/
  → reads the job's memories from PostgreSQL
  → reads the project's existing knowledge base on main
  → extracts knowledge notes (decisions, learnings, state, questions)
  → organizes deliverables in output/
  → cleans up the branch for merge (removes noise, structures content)
  → commits curated changes → branch is now PR-ready
                    ↓
PR created → human reviews → merge to main
                    ↓
Post-merge sync: knowledge/*.md → Neo4j + vector embeddings
                    ↓
Next job starts → clones main → full knowledge base available
```

### Why a Subjob, Not Inline

The alternative — teaching every agent to write knowledge notes during execution via `kb_write()` — has problems:

1. **Every agent config needs new tools and instructions.** The developer, scholar, critic — all need to learn the knowledge base schema. That's a lot of prompt engineering across many expert configs.
2. **It competes for context window.** Knowledge writing during execution means more tool calls, more tokens spent on note management instead of the actual task.
3. **Quality varies by agent.** A research agent might write great notes; a coding agent might write terrible ones. The curator is a specialist.
4. **It couples the knowledge schema to the agent loop.** Changing the note format means updating every agent's instructions. With a curator, you update one expert config.

The curator subjob gives us **separation of concerns**: the working agent focuses on the task, the curator focuses on knowledge extraction. One expert config to get right, and every agent type benefits.

### What the Curator Has Access To

The curator runs as a normal job on the same branch, so it has the full execution record:

| Source | What It Contains | How Curator Uses It |
|--------|-----------------|---------------------|
| `workspace.md` | Accumulated decisions, project context, working memory | Primary source for `decision`, `state`, `learning` notes |
| `plan.md` | Strategic plan, phase structure, goals | Source for `plan` and `goal` notes |
| `archive/` | Phase retrospectives, archived todos with completion notes | Source for `retrospective` notes, phase-level learnings |
| `output/` | Deliverables produced by the job | Curator organizes, validates, decides what merges |
| `memories` table | Observer + free source memories (PostgreSQL) | Pre-extracted insights — curator can promote to structured notes |
| `knowledge/` (from main) | Existing project knowledge base | Context: what's already known, what to update vs. create new |
| Job metadata | Description, config, freeze_data, confidence | Context for framing the job's contributions |

### What the Curator Produces

The curator writes to the branch and commits. The result is a clean, reviewable diff:

1. **Knowledge notes** in `knowledge/` — structured markdown with frontmatter, wikilinks to existing notes, proper type/tag classification
2. **Updated existing notes** — if the job contradicts or supersedes existing knowledge, the curator updates those notes (status → `superseded`, adds `[[new-note]]` link)
3. **Organized deliverables** in `output/` — curator can rename, restructure, add README files
4. **Cleaned branch** — removes job artifacts that shouldn't be reviewed (tool docs, intermediate files)

### The Curator as Editorial Filter

Not everything a job produces should reach `main`. The curator makes editorial decisions:

- A research job that went nowhere → curator writes a "we tried X and it didn't work" learning note, doesn't carry forward failed deliverables
- A coding job that built a feature → curator writes `code` and `decision` notes explaining the architecture, links to relevant existing notes
- A job that discovered a contradiction with existing knowledge → curator writes a `learning` note with `CONTRADICTS` relationship, updates the contradicted note's status
- A job with low confidence → curator writes `question` notes instead of `decision` notes

### Chaining: Critic → Curator → Merge

The natural flow for project jobs:

```
Job completes
    ↓
verification.enabled: true → Critic subjob
    ↓ (approved)
curator.enabled: true → Curator subjob
    ↓ (branch prepared)
Auto-create PR (or manual trigger)
    ↓
Human reviews PR in Gitea/Cockpit
    ↓
Merge → post-merge sync to Neo4j + pgvector
```

If the critic returns the job with feedback, the curator never runs — the job resumes first. The curator only processes approved work.

Config extension in `defaults.yaml`:

```yaml
curator:
  enabled: false                # Opt-in per config (or per project)
  curator_config: curator       # Which expert config to use
  auto_pr: true                 # Auto-create PR after curation
```

### For Code Projects

The same model, with an additional repo. The project has:
- **Jobs repo** — knowledge lives here (`knowledge/` directory on `main`)
- **Source repos** — code lives there (agents push code changes)

Jobs push code to source repos AND knowledge to the jobs repo. Both accumulate on `main`. The knowledge base records what was built, why, what patterns were followed, what didn't work — context that code alone doesn't capture. The curator handles the jobs repo side; source repo merges are separate (existing merge flow).

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

## Memory Integration — Two Stages, One Knowledge Base

Memory Light and the knowledge base are not parallel systems — they are two stages of the same pipeline. Memory Light captures raw insights during execution (PostgreSQL `memories` table). The curator promotes them to structured knowledge notes post-completion.

### The Pipeline

```
During job execution (Memory Light — unchanged):
  Observer LLM → memories table
  Todo completion → memories table
  Compaction summaries → memories table
  Phase archives → memories table
  Tool errors → memories table
                    ↓
After approval (Curator subjob):
  Reads memories table for this job
  Reads workspace.md, archive/, output/
  Reads existing knowledge base on main
  → Promotes valuable memories to knowledge notes
  → Extracts additional insights from workspace.md/archive
  → Deduplicates against existing KB
  → Writes structured notes to knowledge/
```

This means Memory Light keeps working exactly as implemented — no changes to the observer, free sources, or injection hook. The `memories` table is a staging area. The curator is the promotion step.

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

Working agents in KB-enabled projects get knowledge injected via the same transient message pattern as Memory Light:

```
Agent execute loop
    │
    ├─ Context compaction (existing)
    ├─ Todo injection (existing)
    ├─ _index.md injection (replaces workspace.md injection)
    ├─ ★ Knowledge retrieval (replaces/augments memory injection)
    │   ├─ File-based search on knowledge/ (Phase 1 — MVP)
    │   ├─ + Graph traversal via Neo4j (Phase 2)
    │   ├─ + Dense vector search via pgvector (Phase 3)
    │   ├─ + Sparse keyword search via tsvector (Phase 3)
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

### When Knowledge Notes Get Written

With the curator model, knowledge note creation is concentrated in the curator subjob, not scattered across the working agent's execution:

**During the job (working agent — unchanged):**
The agent writes `workspace.md`, `plan.md`, todo completion notes, and `archive/` retrospectives as it does today. Memory Light's free sources (todo completion, compaction, phase archive, tool errors) store memories in the PostgreSQL `memories` table as they do today. No new tools or behavior required from the working agent.

**After approval (curator subjob):**
The curator reads all of the above and produces structured knowledge notes:

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
   **Resolved** — It's a stage, not a separate system. Memory Light captures raw insights during execution (PostgreSQL `memories` table). The curator subjob promotes them to structured knowledge notes post-completion. Memory Light is the capture stage; the curator is the promotion stage. See "Memory Integration" section.

3. ~~**Vault scope — one per job or per project?**~~
   **Resolved** — One per project. The `knowledge/` directory lives in the project's jobs repo and merges to `main` across jobs.

4. ~~**Graph database choice?**~~
   **Resolved** — Neo4j, as core project infrastructure. Already in the stack, already has tooling. Shared instance with project-label namespacing.

5. ~~**How do agents write good knowledge notes?**~~
   **Resolved** — They don't have to. The curator subjob is a specialist that reads the job's full output (workspace.md, archive, memories, output) and produces structured notes. Only the curator needs knowledge tools and schema understanding. Working agents are unchanged. See "The Curator Subjob" section.

6. ~~**Who decides what merges to main?**~~
   **Resolved** — The curator. It makes editorial decisions: what to promote, what to discard, what to update. Failed experiments become "we tried X" learning notes. Successful work becomes decisions, code, and state notes. The curator prepares a clean PR that humans can review.

### Open

7. **Sync-on-write vs sync-on-merge** — Should notes be synced to Neo4j/vectors immediately when written, or only on merge? With the curator model, sync-on-merge is natural — the curator writes notes, they merge, then sync. Sync-on-write is only needed if working agents also write notes during execution (future).

8. **Automatic linking** — How aggressive should auto-linking be? The curator can use existing KB context + Neo4j graph to suggest links. Likely: curator writes explicit links based on content similarity; a periodic maintenance pass fills gaps.

9. **Context injection budget** — How many tokens of retrieved knowledge to inject per LLM call? Memory Light defaults to 5,000. With richer structured notes, this may need tuning. Start with 5,000, adjust based on observed impact.

10. **Curator chain position** — Should the curator run before or after the critic? Current design: critic first (approves/returns), curator second (only processes approved work). Alternative: curator first (extracts knowledge), critic reviews both deliverables AND knowledge notes. The first option is simpler; the second ensures knowledge quality.

11. **Obsidian CLI integration** — Optional desktop enhancement. Agent detects CLI availability and uses it for richer queries (backlinks, orphans, deadends). Not required — file tools + Neo4j cover all core functionality headlessly.

## Implementation Plan

### Phase 1: Knowledge Base Infrastructure

Build the foundation: note schema, `KnowledgeManager`, tools, provisioning. This is prerequisite to everything — the curator needs something to write to, working agents need something to read from.

1. [ ] Finalize note schema (frontmatter fields, naming conventions, directory structure)
2. [ ] Implement `KnowledgeManager` class (`src/managers/knowledge.py`) — file-based note CRUD (create, read, search, list, update notes in `knowledge/`)
3. [ ] Implement agent tools: `kb_write`, `kb_read`, `kb_search`, `kb_list`, `kb_update`
4. [ ] Register as `knowledge` tool category in `src/tools/registry.py`
5. [ ] Add `knowledge/` directory provisioning to project creation flow (init with `.obsidian/` config, subdirectory structure, `.gitignore` for Obsidian cache)
6. [ ] Auto-generate `_index.md` (Map of Content) from note frontmatter — callable utility, not just post-curation
7. [ ] Inject `_index.md` as context for jobs in KB-enabled projects (supplements or replaces workspace.md injection)
8. [ ] Test: manually create knowledge notes in a project repo, verify next job sees them via `kb_search` and `_index.md` injection

### Phase 2: Curator Subjob

With the knowledge tools working, build the curator that uses them.

9. [ ] Create `config/experts/curator/config.yaml` — extends defaults, has `knowledge` + `workspace` + `git` tools
10. [ ] Create `config/experts/curator/instructions.md` — curation guide (what to extract, how to classify, when to update vs. create, editorial judgment rules)
11. [ ] Create `config/experts/curator/curation_instructions.md` — instruction file triggered before `kb_write` (note quality standards, linking conventions)
12. [ ] Implement `create_curation_job()` in `src/api/orchestrator_client.py` — follows `create_verification_job()` pattern, passes job context (workspace.md content, memories, freeze_data) via formatted instructions
13. [ ] Add `curator` config section to `defaults.yaml` (`enabled`, `curator_config`, `auto_pr`)
14. [ ] Wire curation trigger in the post-approval flow — after critic approves (or after job completes if no critic), spawn curator subjob on the same branch
15. [ ] Curator reads `memories` table for the target job via MCP/API and promotes valuable entries to knowledge notes
16. [ ] Auto-create PR after curator completes (if `curator.auto_pr: true`)
17. [ ] Test: run a project job → critic approves → curator extracts notes → PR contains clean `knowledge/` diff

### Phase 3: Neo4j Sync + Graph Queries

Add the queryable graph layer. The sync runs post-merge; graph tools are available to both curators and working agents.

18. [ ] Implement sync function: parse `knowledge/*.md` → upsert Neo4j nodes + relationships
19. [ ] Register sync as post-merge hook and job-startup step
20. [ ] Implement graph query tools: `kb_query`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`
21. [ ] Add Neo4j namespace provisioning to project creation (shared instance, project labels)
22. [ ] Give curator access to graph tools for better dedup and linking against existing knowledge

### Phase 4: Vector Search + Hybrid Retrieval

Add vector embeddings and the full RRF hybrid search from Memory Light.

23. [ ] Embed note content via `EmbeddingService` (text-embedding-3-small, same as Memory Light)
24. [ ] Store embeddings in pgvector, scoped by `project_id`
25. [ ] Implement hybrid retrieval: dense (pgvector) + sparse (tsvector) + graph (Neo4j) + recency
26. [ ] RRF fusion for ranking (same algorithm as Memory Light)
27. [ ] Replace `_index.md`-only injection with hybrid knowledge retrieval for working agents

### Phase 5: Obsidian CLI + Polish

28. [ ] CLI availability detection (`obsidian version` probe)
29. [ ] Wrap CLI commands: `backlinks`, `links`, `orphans`, `deadends`, `search`
30. [ ] `.obsidian/` config templates (graph settings, tag hierarchy, workspace layout)
31. [ ] Cockpit UI: knowledge base viewer/browser
32. [ ] workspace.md → knowledge notes conversion tool (for existing projects)
33. [ ] Test with real multi-job project: verify Job N+1 benefits from Job N's knowledge

## What Changes and What Stays

The curator model is additive — it doesn't replace existing systems, it builds on them:

**Stays the same:**
- **`workspace.md`** — Working agents still write it during execution. The curator reads it as a source for knowledge extraction. For non-project jobs and backward compatibility, workspace.md injection continues unchanged.
- **`plan.md`** — Working agents still write it. The curator reads it.
- **`memories` PostgreSQL table** — Memory Light keeps running during execution (observer, free sources). The table becomes a staging area that the curator reads post-completion.
- **Memory Light injection** — Continues injecting job-scoped memories during execution. Knowledge injection adds project-scoped context on top.
- **Critic subjob** — Continues reviewing deliverables. Curator runs after the critic.

**New:**
- **`knowledge/` directory** — Created in project jobs repos. Curated notes accumulate on `main`.
- **`_index.md` injection** — Replaces workspace.md injection for KB-enabled projects (or supplements it).
- **Curator subjob** — New expert config, spawned post-approval, writes knowledge notes.
- **Neo4j sync** — Post-merge hook syncs notes to graph (Phase 2).
- **Knowledge retrieval** — Project-scoped context injection from the knowledge base (Phase 3).

**Related docs:**
- [[memory_light]] — Remains valid for: observer, extraction channels, RRF hybrid search, embedding models, injection hook. Memory Light is the capture stage; the curator is the promotion stage.
- [[obsidian]] — Remains valid for: note schema, research findings, Obsidian CLI, `.obsidian/` config.
- [[projects]] — Remains valid for: database schema, API, merge flow, workspace layout, cockpit UI. This doc adds `knowledge/` to "what merges to main" and defines the curator subjob in the post-completion chain.

## References

- [[projects]] — Project model, database schema, merge flow, workspace layout
- [[obsidian]] — Note schema, Obsidian integration, CLI, research findings (A-MEM, Obsidian-Assist)
- [[memory_light]] — Observer, extraction channels, RRF hybrid search, embedding models, injection hook
- [[memories_mechanism]] — Full memory system design (research, scoping model, multi-backend architecture)
- [[working_memory]] — Working memory analysis, workspace.md patterns
- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110)
- [Zep/Graphiti: Temporal Knowledge Graph](https://arxiv.org/abs/2501.13956)
