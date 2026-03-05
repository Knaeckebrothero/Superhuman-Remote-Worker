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

The key insight: **the working agent doesn't write knowledge notes.** It works exactly as it does today — writes workspace.md, completes todos, produces output. A specialized **curator subjob** runs in parallel, triggered on every archive phase, continuously updating the knowledge base as the job progresses.

This follows the same pattern as the existing critic verification subjob (`verification.enabled` in config, `create_verification_job()` in `src/api/orchestrator_client.py`). The infrastructure for spawning subjobs already exists. The key difference: the critic runs post-completion, while the curator runs **continuously from the first archive phase**.

```
Job starts
  ↓
Phase 1 (tactical) → archive phase
  ↓
Curator subjob spawned (ONE persistent subjob)
  → reads the job's branch: workspace.md, plan.md, archive/
  → reads the project's existing knowledge base on main
  → extracts knowledge notes from phase 1
  → generates retrieval messages for each note (see below)
  → commits curated changes to branch
  ↓
Phase 2 (tactical) → archive phase
  ↓
Curator receives update (same subjob, new phase data)
  → reads new archive/phase_2_retrospective.md
  → reads updated workspace.md, plan.md
  → extracts incremental knowledge
  → updates/creates notes in knowledge/
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
  → organizes deliverables in output/
  → cleans up the branch for merge
  → commits final curated changes → branch is now PR-ready
  ↓
PR created → human reviews → merge to main
  ↓
Post-merge sync: knowledge/*.md → Neo4j + vector embeddings
  ↓
Next job starts → clones main → full knowledge base available
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

1. **Spawn** — After the first archive phase, the orchestrator spawns the curator subjob on the same branch via `create_curation_job()` (follows the `create_verification_job()` pattern). The curator processes phase 1's archive data and enters `waiting` status.
2. **Incremental updates** — On each subsequent archive phase, the orchestrator calls `resume_job()` on the curator with the new phase data as feedback. The curator reads the latest `archive/phase_N_retrospective.md`, updated `workspace.md`, extracts incremental knowledge, then goes back to `waiting`.
3. **Final pass** — After the critic approves (or after job completion if no critic), the orchestrator resumes the curator with a final signal. It reads `memories`, `output/`, and `freeze_data` for a comprehensive final extraction, cleans the branch, and signals readiness for PR.

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
| `knowledge/` (from main) | Existing project knowledge base | Context: what's already known, what to update vs. create new |
| Job metadata | Description, config, freeze_data, confidence | Context for framing the job's contributions |

### What the Curator Produces

The curator writes to the branch and commits. The result is a clean, reviewable diff:

1. **Knowledge notes** in `knowledge/` — structured markdown with frontmatter, wikilinks to existing notes, proper type/tag classification, and retrieval messages
2. **Updated existing notes** — if the job contradicts or supersedes existing knowledge, the curator updates those notes (status → `superseded`, adds `[[new-note]]` link)
3. **Organized deliverables** in `output/` — curator can rename, restructure, add README files (final pass only)
4. **Cleaned branch** — removes job artifacts that shouldn't be reviewed (tool docs, intermediate files) (final pass only)

Note: items 1–2 happen incrementally during the job (per archive phase). Items 3–4 happen during the final pass after critic approval.

### The Curator as Editorial Filter

Not everything a job produces should reach `main`. The curator makes editorial decisions:

- A research job that went nowhere → curator writes a "we tried X and it didn't work" learning note, doesn't carry forward failed deliverables
- A coding job that built a feature → curator writes `code` and `decision` notes explaining the architecture, links to relevant existing notes
- A job that discovered a contradiction with existing knowledge → curator writes a `learning` note with `CONTRADICTS` relationship, updates the contradicted note's status
- A job with low confidence → curator writes `question` notes instead of `decision` notes

### Chaining: Archive → Curator → Critic → Final Pass → Merge

The natural flow for project jobs:

```
Job starts
    ↓
Phase 1 → archive phase
    ↓
curator.enabled: true → Curator subjob spawned (runs in parallel)
    ↓                          ↓
Phase 2... N (job continues)   Curator processes phases incrementally
    ↓                          ↓
Job completes                  Curator has processed most phases
    ↓
verification.enabled: true → Critic subjob
    ↓ (approved)
Curator receives final signal → final pass (memories, output, cleanup)
    ↓ (branch prepared)
Auto-create PR (or manual trigger)
    ↓
Human reviews PR in Gitea/Cockpit
    ↓
Merge → post-merge sync to Neo4j + pgvector
```

If the critic returns the job with feedback, the job resumes — the curator continues processing new archive phases as they come. The curator's final pass only triggers after critic approval.

Config extension in `defaults.yaml`:

```yaml
curator:
  enabled: false                # Opt-in per config (or per project)
  curator_config: curator       # Which expert config to use
  auto_pr: true                 # Auto-create PR after curation
  autonomy: full                # No human review of curated notes (PR is the quality gate)
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
| `workspace.md` — monolithic blob rewritten each strategic phase | Still written by agents during execution; curator extracts atomic notes post-completion |
| `plan.md` — single flat plan file | Still written by agents; curator promotes goals/milestones to interlinked plan notes |
| `archive/phase_N_retrospective.md` — linear phase history | Still created by agents; curator extracts retrospective notes linked to decisions |
| Memory Light `memories` table — parallel memory system | Kept as staging area during execution; curator promotes valuable entries to knowledge notes |
| Context lost on compaction | Query knowledge_index (pgvector + tsvector) to retrieve relevant project context |
| Per-job isolation (unless merged) | Project-wide knowledge base that accumulates across jobs via curator + merge |

## Architecture

### Core Principle: Repository is Source of Truth, Databases are Indexes

This is the foundational architectural decision. The project repository contains the canonical data. Databases are derived indexes rebuilt from the repo on demand.

```
knowledge/*.md  (git repo — source of truth)
       │
       │  sync on job init + post-merge
       │
       ├──────────► pgvector    (semantic search index)
       ├──────────► Neo4j       (relationship/graph index)
       └──────────► tsvector    (keyword search index)
```

**If you delete the databases, you rebuild them from the repo.** If you delete the repo, the data is gone. This is the same relationship as code and a build artifact — the source is canonical, the artifact is derived.

**Why this matters:**

- **Git gives you history for free.** Who added this note, when, which job — all in the commit log. No audit table needed.
- **Humans can edit directly.** Clone the repo, open in Obsidian, add a note, push. Next sync picks it up.
- **Portable.** Move the project to a different server — clone the repo, re-index, done. No database migration.
- **Debuggable.** `git log knowledge/` tells you exactly what happened and when.
- **The scattered sources problem disappears.** Today, project state lives in PostgreSQL tables, MongoDB logs, git repos, and workspace files. With this decision, there's one authoritative source: the repo. Everything else is a query layer.
- **Recoverable.** Corrupt index? Re-sync from repo. Wrong embeddings? Re-embed from repo. The repo is always there.

This principle applies beyond just knowledge notes. Over time, everything project-scoped — memories, requirements, credentials, configuration — lives as files in the repo and gets indexed into databases for search. The databases serve the agents; the repo serves the project.

### Two Representations, One Knowledge Base

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Project Knowledge Base                          │
│                                                                     │
│   ┌──────────────────────┐          ┌──────────────────────┐       │
│   │  Markdown Files      │  sync    │  Derived Indexes     │       │
│   │  (jobs repo)         │ ──────►  │  (project-level)     │       │
│   │                      │          │                      │       │
│   │  • SOURCE OF TRUTH   │          │  • pgvector (search)  │       │
│   │  • Git-versioned     │          │  • Neo4j (graph)      │       │
│   │  • Human-editable    │          │  • tsvector (keywords) │       │
│   │  • Obsidian-viewable │          │  • Rebuilt from repo   │       │
│   │  • Diff-friendly     │          │  • Never edited directly│      │
│   └──────────┬───────────┘          └──────────┬───────────┘       │
│              │                                  │                   │
│              │         ┌──────────┐             │                   │
│              └────────►│  Agent   │◄────────────┘                   │
│                        │  Tools   │                                 │
│   ┌──────────┐        │          │        ┌──────────┐             │
│   │ Obsidian │◄───────│ kb_write │───────►│ Cockpit  │             │
│   │ (user)   │        │ (files)  │        │ (user)   │             │
│   └──────────┘        │ kb_search│        └──────────┘             │
│                        │ (indexes)│                                 │
│                        └──────────┘                                 │
└─────────────────────────────────────────────────────────────────────┘
```

**Write path:** `kb_write` creates/updates a markdown file in `knowledge/`. That's it. No database call. The file is committed to the branch.

**Read path:** `kb_search` and `kb_query` hit the derived indexes (pgvector, Neo4j, tsvector). These are fast, ranked, and semantic. `kb_read` and `kb_list` can also read files directly for exact lookups.

**Sync path:** On job init and post-merge, a sync function diffs the repo against the indexes and updates only what changed. Git tells us exactly which files changed (`git diff --name-only`), so re-indexing is incremental, not full-scan.

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
├── PostgreSQL — knowledge_index table (Phase 1, auto-provisioned)
│   ├── pgvector embeddings (semantic search)
│   └── tsvector documents (keyword search)
│
├── Neo4j (Phase 3, auto-provisioned)
│   ├── (:Note {id, type, title, job_id, confidence, ...})
│   ├── (:Tag {name}), (:Keyword {name})
│   └── [:REFERENCES], [:SUPPORTS], [:CONTRADICTS], [:DERIVED_FROM]
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

Note: Working agents still write `workspace.md` and `plan.md` during execution — these are unchanged. The curator extracts their content into structured `knowledge/` notes post-completion. Over time, `knowledge/` becomes the project's long-term memory while `workspace.md` remains the agent's working memory during a job. `_index.md` is auto-generated and injected as context alongside (or eventually replacing) workspace.md injection.

## Note Schema

### Frontmatter

```yaml
---
id: 20260201-1423-a7f3       # timestamp + 4-char random suffix to avoid collisions
type: goal | plan | decision | learning | code | source | question | state | retrospective
tags: [requirement, authentication]
keywords: [OAuth, JWT, session]
retrieval_messages:           # curator-generated: when should this note surface?
  - "What authentication approach should I use?"
  - "Why did we pick JWT instead of OAuth?"
  - "Token-based auth trade-offs and session management"
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
  id: "20260201-1423-a7f3",
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
During job execution (both run in parallel):

Working Agent                          Curator Subjob
  Observer LLM → memories table          (spawned after first archive phase)
  Todo completion → memories table
  Compaction summaries → memories table
  Phase archives → memories table    →   Reads archive/phase_N_retrospective.md
  Tool errors → memories table           Reads updated workspace.md, plan.md
                                         Extracts knowledge notes incrementally
                                         Generates retrieval messages per note
                                         Writes structured notes to knowledge/
                    ↓
After approval (Curator final pass):
  Reads memories table for this job
  Reads output/, freeze_data
  Reads existing knowledge base on main
  → Promotes valuable memories to knowledge notes
  → Final extraction from output and deliverables
  → Deduplicates against existing KB
  → Cleans branch for merge
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

Working agents in KB-enabled projects get knowledge injected via the same transient message pattern as Memory Light. Search always hits the derived indexes (built from the repo on job init), never scans files:

```
Agent execute loop
    │
    ├─ Context compaction (existing)
    ├─ Todo injection (existing)
    ├─ _index.md injection (replaces workspace.md injection)
    ├─ ★ Knowledge retrieval (replaces/augments memory injection)
    │   ├─ Dense vector search (pgvector — indexed from repo on job init)
    │   ├─ Sparse keyword search (tsvector — indexed from repo on job init)
    │   ├─ + Graph traversal via Neo4j (Phase 3, indexed from repo)
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

- **Job init**: clone `main` → sync `knowledge/` to indexes (pgvector, tsvector, optionally Neo4j). Indexes now contain all knowledge from all previous jobs.
- **During the job**: working agent queries the indexes. Curator writes new notes as files on the branch.
- **Post-merge**: re-sync indexes from the updated `main`. Incremental — only changed files.
- **Next job**: clones `main`, syncs, has everything.

This replaces Memory Light's Phase 5 ("cross-job memory via project_id on the memories table") with something richer — structured, interlinked notes instead of flat text blobs.

## Sync: Repository → Indexes

The sync function rebuilds derived indexes from the repo. It runs at two points: **job init** (ensures indexes are current before the agent starts) and **post-merge** (updates indexes after new knowledge lands on `main`).

### Incremental Sync via Git

Git already knows what changed. No need for content hashing:

```
1. Get last-synced commit hash from index metadata table
2. git diff --name-only <last-synced>..<current> -- knowledge/
3. For each changed file:
   - Parse frontmatter + body
   - Upsert pgvector: generate embedding, store with note metadata
   - Upsert tsvector: update full-text search index
   - Upsert Neo4j: MERGE node, parse wikilinks → relationships (Phase 3)
4. For each deleted file:
   - Remove from all indexes
5. Store current commit hash as new sync point
```

**Cold start** (no sync point, or index wiped): full scan of `knowledge/`, index everything. This is the "rebuild from repo" path — always available as a recovery mechanism.

**User edits** follow the same path:

```
User edits in Obsidian → git push to jobs repo main
                                    ↓
                    Next job init: incremental sync catches the changes
```

### What Gets Indexed

| Index | What It Stores | What It Enables |
|-------|---------------|-----------------|
| **pgvector** | Note embedding + metadata (id, type, title, tags, project_id) | Semantic search ("find notes about authentication patterns") |
| **tsvector** | Note content as full-text search document | Keyword search ("find notes mentioning JWT") |
| **Neo4j** (Phase 3) | Note nodes + wikilink relationships + tag nodes | Graph traversal ("what depends on this decision?") |

### Schema: `knowledge_index` Table

Extends the existing system PostgreSQL (same database as jobs/memories):

```sql
CREATE TABLE IF NOT EXISTS knowledge_index (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- internal PK
    note_id VARCHAR(50) NOT NULL,            -- frontmatter id (e.g. "20260201-1423-a7f3")
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,                  -- relative path in repo (e.g. "knowledge/decisions/chose-jwt.md")
    title TEXT NOT NULL,
    note_type VARCHAR(50) NOT NULL,           -- goal, plan, decision, learning, code, source, question, state, retrospective
    status VARCHAR(50) DEFAULT 'active',
    confidence VARCHAR(20),
    tags TEXT[] DEFAULT '{}',
    keywords TEXT[] DEFAULT '{}',
    job_id UUID,                              -- which job created this note
    phase INT,
    content TEXT NOT NULL,                    -- full note body (for display in search results)
    retrieval_messages TEXT[] DEFAULT '{}',   -- curator-generated queries for when this note should surface
    embedding vector(1536),                  -- dense embedding for semantic search
    search_doc tsvector,                     -- full-text search document
    created_at TIMESTAMPTZ,                  -- from frontmatter
    modified_at TIMESTAMPTZ,                 -- from frontmatter
    indexed_at TIMESTAMPTZ DEFAULT NOW(),    -- when this row was last synced
    content_hash VARCHAR(64),                -- sha256 of file content (skip re-embedding unchanged files)

    CONSTRAINT uq_knowledge_project_note UNIQUE (project_id, note_id),
    CONSTRAINT uq_knowledge_project_file UNIQUE (project_id, file_path)
);

CREATE INDEX idx_knowledge_project ON knowledge_index(project_id);
CREATE INDEX idx_knowledge_project_type ON knowledge_index(project_id, note_type);
CREATE INDEX idx_knowledge_tags ON knowledge_index USING GIN(tags);
CREATE INDEX idx_knowledge_search ON knowledge_index USING GIN(search_doc);
CREATE INDEX idx_knowledge_embedding ON knowledge_index
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);

-- Sync tracking: which commit was last indexed per project
CREATE TABLE IF NOT EXISTS knowledge_sync_state (
    project_id UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    last_synced_commit VARCHAR(40),           -- git commit hash
    last_synced_at TIMESTAMPTZ DEFAULT NOW(),
    note_count INT DEFAULT 0
);
```

This reuses the same pgvector infrastructure as Memory Light (HNSW index, cosine distance, same embedding model). The `content_hash` column lets us skip re-embedding files that haven't changed even if git reports them as modified (e.g., whitespace changes).

### Search Function

Same RRF hybrid search pattern as Memory Light, querying the `knowledge_index` table instead of `memories`:

```sql
CREATE OR REPLACE FUNCTION knowledge_hybrid_search(
    query_text text,
    query_embedding vector(1536),
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
    content="After evaluating both...\n\nLinked: [[oauth-analysis]], [[security-requirements]]",
    tags=["authentication", "security"],
    confidence="high",
    retrieval_messages=[
        "What authentication approach should I use?",
        "Why did we pick JWT instead of OAuth?",
        "Token-based auth trade-offs and session management"
    ]
)
# → Creates knowledge/decisions/chose-jwt-over-oauth.md with frontmatter
# → Auto-sets job_id, phase, created timestamp
# → retrieval_messages stored in frontmatter (used for search matching)
# → File only — no database call (indexes sync later)

kb_update(
    note="chose-jwt-over-oauth",
    append="## Update (Phase 4)\nAfter load testing, JWT validation adds 2ms p99.",
    status="active",
    add_tags=["performance"]
)

# === Reading (all agents in KB-enabled projects) ===

kb_search(query="authentication")       # Hybrid search via knowledge_index (pgvector + tsvector + RRF)
kb_read(note="chose-jwt-over-oauth")    # Returns full file content with frontmatter
kb_list(type="decision")                # All decisions (queries knowledge_index)
kb_list(tag="authentication")           # All notes tagged authentication
kb_list(status="question")              # All open questions
kb_list(job_id="abc-123")              # Everything from a specific job

# === Graph Querying (Phase 3, Neo4j-powered) ===

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

## Context Injection — What Replaces workspace.md

Today, `workspace.md` is injected as a transient message every LLM call. The knowledge base replaces this with two injection layers:

1. **`_index.md` (always injected)** — Auto-generated Map of Content. Lightweight overview of what's in the knowledge base: note counts by type, recent activity, key links. Gives the agent orientation without loading everything.

2. **Retrieved knowledge (dynamically injected)** — Top-K relevant notes selected by the same RRF hybrid search from Memory Light (dense + sparse + recency + graph traversal). Assembled into a transient message. Budget: configurable, default 5,000–10,000 tokens.

The agent can also actively query via `kb_search`, `kb_query`, etc. — the injection is passive recall, the tools are active recall.

## Provisioning

The knowledge base is auto-provisioned when a project is created:

1. **`knowledge/` directory** — Created in the jobs repo on project init. Includes `.obsidian/` config with templates, graph settings, and subdirectory structure. Project-scoped (merges to `main`).
2. **`knowledge_index` + `knowledge_sync_state` tables** — Created by schema migration (shared across all projects, scoped by `project_id`). Uses existing system PostgreSQL with pgvector extension (already enabled for Memory Light).
3. **Sync on job init** — When a project job starts, the init step syncs `knowledge/` from the cloned repo into the `knowledge_index` table. Incremental via git diff.
4. **Sync post-merge** — After a PR merges to `main`, re-sync to pick up the curated notes.
5. **Neo4j namespace** (Phase 3) — A project-level namespace within the shared Neo4j instance. Deferred until graph queries are needed.

For default (personal) projects, the knowledge base directory is still created but indexing is lazy — only runs if `knowledge/` contains files.

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
2. Write to `knowledge/` directory, commit to repo
3. Run sync to rebuild `knowledge_index` (pgvector + tsvector)
4. The `memories` table remains for job-scoped use during execution; project-scoped retrieval switches to the knowledge base

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

7. ~~**Sync-on-write vs sync-on-merge**~~
   **Resolved** — Sync on job init + post-merge. The repo is the source of truth; databases are rebuilt from it. Sync uses `git diff` for incremental updates. No sync-on-write needed — the curator writes files, they merge to main, next job's init syncs. See "Sync: Repository → Indexes" section.

8. ~~**How does search work without Neo4j/vectors in Phase 1?**~~
   **Resolved** — pgvector + tsvector are Phase 1, not Phase 4. The `knowledge_index` table is synced from the repo on job init. `kb_search` hits `knowledge_hybrid_search()` (RRF over dense + sparse + recency). Neo4j graph queries are Phase 3 — an additional retrieval channel, not a prerequisite.

9. ~~**Automatic linking**~~
   **Resolved** — Curator writes explicit links based on content similarity using existing KB context + `kb_search` results. A periodic maintenance pass fills gaps. Good default — aggressive enough to be useful, not so aggressive that it creates noise.

10. ~~**Context injection budget**~~
    **Resolved** — Use the same defaults as the current Memory Light system: `budget_tokens: 5000`, `max_memories_per_injection: 10`. Tune later based on observed impact.

11. ~~**Curator chain position**~~
    **Resolved** — The curator triggers on archive phase, not post-completion. It runs as a single persistent subjob spawned after the first archive phase, processing knowledge incrementally in parallel with the working agent. The critic still runs post-completion. The curator receives a final signal after critic approval for a last pass (memories, output, cleanup). See "Lifecycle: One Subjob, Many Updates" section.

12. ~~**Curator autonomy level**~~
    **Resolved** — `full` autonomy. The PR review is the quality gate for curated notes. No need for the curator to pause for human approval.

13. ~~**Parallel job knowledge conflicts**~~
    **Resolved** — Handled as git merge conflicts. Two parallel jobs producing contradictory knowledge will surface as merge conflicts when their PRs merge to `main`. These are resolved manually for now. The `CONTRADICTS` relationship type and a future "knowledge health check" job can catch semantic contradictions that slip through file-level merge, but that's a later optimization.

14. ~~**Curator access to target job memories**~~
    **Resolved** — Same pattern as the critic subjob. The curator receives the target job's context (workspace.md content, memories, freeze_data) via formatted instructions, just like the critic receives freeze_data in `create_verification_job()`. The orchestrator passes this context when spawning the curator via `create_curation_job()`.

15. ~~**Obsidian CLI integration**~~
    **Resolved** — Yes, use the Obsidian CLI to enhance the agent's toolset. Agent detects CLI availability (`obsidian version` probe) and wraps CLI commands (backlinks, orphans, deadends, search) as additional tools. This is a Phase 4 enhancement on top of the file tools + database indexes that cover core functionality.

16. ~~**Curator-agent branch coordination**~~
    **Resolved** — The curator gets its own branch in the project's jobs repo (e.g., `job/<curator-id>`), branched off the working agent's branch. The curator writes `knowledge/` notes on its branch. During the final pass, the curator merges its branch into the parent job's branch (or the orchestrator does this automatically). Since the curator writes exclusively to `knowledge/` and the working agent writes to `workspace.md`, `output/`, etc., merge conflicts are extremely unlikely. This works naturally because all subjobs (including critic and curator) now create branches off their parent job's branch in the project's jobs repo — fixed in `POST /api/jobs` and `POST /api/projects/{id}/jobs` (`orchestrator/main.py`).

17. ~~**Archive phase notification mechanism**~~
    **Resolved** — Same pattern as the critic's resume mechanism. The curator enters `waiting` status after processing a phase. When the working agent archives the next phase, the orchestrator calls `resume_job()` on the curator with the new phase data as feedback. This reuses the existing `waiting` → `resume_job(feedback)` infrastructure that the critic already uses for multi-round reviews. The curator processes the phase, then goes back to `waiting` until the next archive or the final signal. The curator merges the parent job's branch into its own branch before processing each phase to pick up the latest workspace.md, archive/, etc.

### Open

18. **Post-merge sync trigger** — Who calls the sync after a PR merges? The orchestrator's merge endpoint (`POST /api/projects/{id}/jobs/{jid}/merge`) already handles the Gitea merge. It should call the sync service after a successful merge. Alternatively, a Gitea webhook on push to `main` could trigger it. TBD.

19. **Retrieval message storage** — Where do the curator's generated retrieval messages live? Options: (a) in the note's frontmatter as a `retrieval_messages` array, (b) as separate embeddings in `knowledge_index` linked to the same note_id, (c) concatenated with the note content before embedding. Option (a) is simplest and keeps everything in the file (repo-as-source-of-truth). Option (b) gives the best search quality but multiplies the embedding count. Option (c) is a middle ground — one embedding that captures both content and retrieval intent. Recommendation: start with (a) for storage + (c) for embedding (concatenate retrieval messages with content before embedding). This keeps the file canonical and gives decent search quality without extra rows. Upgrade to (b) later if recall is insufficient.

## Implementation Plan

### Phase 1: Knowledge Base Infrastructure + Search

Build the foundation: note schema, file CRUD, repo → database sync, and working search. The curator needs something to write to; working agents need something to search. Search works from day one via pgvector + tsvector (reuses Memory Light's infrastructure).

**Note schema + file tools:**
1. [ ] Finalize note schema (frontmatter fields, naming conventions, directory structure)
2. [ ] Implement `KnowledgeManager` class (`src/managers/knowledge.py`) — file-based note CRUD (create, read, list, update notes in `knowledge/`)
3. [ ] Implement agent tools: `kb_write` (creates file), `kb_read` (reads file), `kb_list` (lists by type/tag/status from frontmatter), `kb_update` (appends/modifies file)
4. [ ] Register as `knowledge` tool category in `src/tools/registry.py`
5. [ ] Add `knowledge/` directory provisioning to project creation flow (init with `.obsidian/` config, subdirectories)

**Repo → database sync:**
6. [ ] Add `knowledge_index` and `knowledge_sync_state` tables to schema (pgvector + tsvector, see "Schema" section)
7. [ ] Add `knowledge_hybrid_search()` SQL function (RRF-based, see "Search Function" section)
8. [ ] Implement `KnowledgeSyncService` (`src/services/knowledge_sync.py`) — parses `knowledge/*.md`, extracts frontmatter + body, generates embeddings, upserts to `knowledge_index`
9. [ ] Incremental sync via `git diff --name-only` against `knowledge_sync_state.last_synced_commit`
10. [ ] Cold start path: full scan of `knowledge/` when no sync point exists (also serves as "rebuild indexes" recovery)
11. [ ] Wire sync into project job init (after clone, before agent starts)
12. [ ] Wire sync into post-merge hook (after PR merged to main)

**Search + injection:**
13. [ ] Implement `kb_search` tool — queries `knowledge_hybrid_search()`, returns ranked results with snippets
14. [ ] Auto-generate `_index.md` (Map of Content) from `knowledge_index` table
15. [ ] Inject `_index.md` as context for jobs in KB-enabled projects (supplements workspace.md injection)
16. [ ] Implement knowledge retrieval injection — query `knowledge_hybrid_search()` with current task context, inject top-K as transient message (same pattern as Memory Light)
17. [ ] Test: manually create knowledge notes in a project repo, start a job, verify sync runs and `kb_search` returns ranked results

### Phase 2: Curator Subjob

With the knowledge tools and search working, build the curator that uses them. The curator is a persistent subjob that runs in parallel with the working agent, triggered on every archive phase.

**Config + instructions:**
18. [ ] Create `config/experts/curator/config.yaml` — extends defaults, has `knowledge` + `workspace` + `git` tools, `autonomy: full`
19. [ ] Create `config/experts/curator/instructions.md` — curation guide (what to extract, how to classify, when to update vs. create, editorial judgment rules, retrieval message generation)
20. [ ] Create `config/experts/curator/curation_instructions.md` — instruction file triggered before `kb_write` (note quality standards, linking conventions, retrieval message quality)

**Subjob lifecycle:**
21. [ ] Implement `create_curation_job()` in `src/api/orchestrator_client.py` — follows `create_verification_job()` pattern, passes job context via formatted instructions (same approach as critic's freeze_data)
22. [ ] Add `curator` config section to `defaults.yaml` (`enabled`, `curator_config`, `auto_pr`, `autonomy: full`)
23. [ ] Wire archive phase trigger — after the first archive phase, spawn curator subjob on the same branch (one subjob per job, persistent)
24. [ ] Implement archive phase notification — on each subsequent archive phase, call `resume_job()` on the curator with new phase data as feedback (reuses existing `waiting` → resume infrastructure)
25. [ ] Wire final pass trigger — after critic approves (or after job completion if no critic), send final signal to curator with memories + output + freeze_data

**Curation logic:**
26. [ ] Curator uses `kb_search` to check existing knowledge before writing (dedup, linking)
27. [ ] Curator generates retrieval messages for each note (synthetic queries for when the note should surface)
28. [ ] Curator reads `memories` table for the target job during final pass and promotes valuable entries to knowledge notes
29. [ ] Auto-create PR after curator's final pass completes (if `curator.auto_pr: true`)
30. [ ] Test: run a project job → curator processes archive phases in parallel → critic approves → curator final pass → PR merges → next job's sync picks up notes → `kb_search` finds them

### Phase 3: Neo4j Graph Queries

Add the graph layer for relationship traversal. Extends the sync function to also write Neo4j from the same repo source.

31. [ ] Extend `KnowledgeSyncService` to also upsert Neo4j nodes + relationships (parse wikilinks → edges, tags → tag nodes)
32. [ ] Add Neo4j namespace provisioning to project creation (shared instance, project labels)
33. [ ] Implement graph query tools: `kb_query`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`
34. [ ] Give curator access to graph tools for richer dedup and linking
35. [ ] Optionally add graph traversal as a fourth channel in the RRF search function

### Phase 4: Obsidian CLI + Polish

36. [ ] CLI availability detection (`obsidian version` probe)
37. [ ] Wrap CLI commands: `backlinks`, `links`, `orphans`, `deadends`, `search`
38. [ ] `.obsidian/` config templates (graph settings, tag hierarchy, workspace layout)
39. [ ] Cockpit UI: knowledge base viewer/browser
40. [ ] workspace.md → knowledge notes conversion tool (for existing projects)
41. [ ] Test with real multi-job project: verify Job N+1 benefits from Job N's knowledge

## What Changes and What Stays

The curator model is additive — it doesn't replace existing systems, it builds on them:

**Stays the same:**
- **`workspace.md`** — Working agents still write it during execution. The curator reads it as a source for knowledge extraction. For non-project jobs and backward compatibility, workspace.md injection continues unchanged.
- **`plan.md`** — Working agents still write it. The curator reads it.
- **`memories` PostgreSQL table** — Memory Light keeps running during execution (observer, free sources). The table becomes a staging area that the curator reads during its final pass.
- **Memory Light injection** — Continues injecting job-scoped memories during execution. Knowledge injection adds project-scoped context on top.
- **Critic subjob** — Continues reviewing deliverables post-completion. Curator runs in parallel from the first archive phase; its final pass triggers after critic approval.

**New:**
- **`knowledge/` directory** — Created in project jobs repos. Curated notes accumulate on `main`.
- **`knowledge_index` table** — Derived index in PostgreSQL (pgvector + tsvector), synced from repo on job init and post-merge.
- **`_index.md` injection** — Supplements workspace.md injection for KB-enabled projects.
- **Knowledge retrieval injection** — Project-scoped context injected via hybrid search (pgvector + tsvector + RRF). Works from Phase 1.
- **Curator subjob** (Phase 2) — New expert config, spawned after first archive phase, runs in parallel with working agent, continuously extracts knowledge. Final pass after critic approval.
- **Neo4j sync** (Phase 3) — Post-merge hook syncs notes to graph for relationship traversal.

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
