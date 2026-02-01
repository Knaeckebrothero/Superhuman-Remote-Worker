# Obsidian-Style Knowledge Workspace

This document captures the design discussion for integrating Obsidian-compatible, Zettelkasten-style note-taking into the agent's workspace.

## Vision

The agent should adopt a knowledge-graph-native approach to note-taking:

1. **Human-readable workspace** - Notes the user can open in Obsidian and collaborate on
2. **Agent-optimized structure** - Atomic notes with links enable better retrieval during context pressure
3. **Bidirectional collaboration** - User and agent work on the same knowledge base
4. **Persistent knowledge** - Cross-job accumulation instead of per-job isolation

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

### Additional Resources

- [Zettelkasten AI System](https://github.com/joshylchen/zettelkasten) - AI-powered knowledge management with MCP integration
- [Agentic AI with Knowledge Graph](https://loysbelleguie.medium.com/an-implement-of-an-agentic-ai-framework-powered-by-a-knowledge-graph-2-n-24319275291f) - Knowledge graphs as persistent agent memory
- [AI Agent Knowledge Base Architecture](https://www.infoworld.com/article/4091400/anatomy-of-an-ai-agent-knowledge-base.html) - Multi-modal retrieval strategies

## Architecture

### Layered Design

```
┌─────────────────────────────────────────────────────┐
│  Obsidian (User Interface)                          │
│  - View/edit notes                                  │
│  - Graph visualization                              │
│  - Manual linking & tagging                         │
└─────────────────────────────────────────────────────┘
                         ↕ sync
┌─────────────────────────────────────────────────────┐
│  Markdown Files (workspace/)                        │
│  - Wikilinks: [[concept]]                           │
│  - Tags: #requirement #source/document              │
│  - Frontmatter: relationships, metadata             │
└─────────────────────────────────────────────────────┘
                         ↕ parse/generate
┌─────────────────────────────────────────────────────┐
│  Graph Database                                     │
│  - Nodes: Note, Concept, Source, Claim, Question    │
│  - Edges: REFERENCES, SUPPORTS, CONTRADICTS         │
│  - Properties: timestamps, confidence, job_id       │
└─────────────────────────────────────────────────────┘
                         ↕ query
┌─────────────────────────────────────────────────────┐
│  Agent Tools                                        │
│  - query_knowledge(pattern)                         │
│  - find_related(concept)                            │
│  - trace_provenance(claim)                          │
│  - detect_contradictions()                          │
└─────────────────────────────────────────────────────┘
```

### Current vs Proposed Workspace

| Current | Knowledge-graph native |
|---------|------------------------|
| Writes `workspace.md` as blob | Creates atomic notes per concept |
| Linear phase archive | Graph of interconnected findings |
| Context lost on compaction | Query graph for relevant context |
| Per-job isolation | Cross-job knowledge accumulation |

## Note Schema

### Frontmatter Structure

```yaml
---
id: 20260201-1423
type: concept | claim | source | question | decision
tags: [requirement, authentication]
keywords: [OAuth, JWT, session]
links: [[user-management]], [[security-model]]
created: 2026-02-01T14:23:00Z
modified: 2026-02-01T15:30:00Z
job_id: abc-123
confidence: high | medium | low
---
```

### Note Types

| Type | Purpose | Example |
|------|---------|---------|
| **Concept** | An idea or term | `[[distributed consensus]]` |
| **Claim** | A statement with provenance | "System must handle 1000 RPS" |
| **Source** | Document, URL, conversation | Reference to input document |
| **Question** | Open item to resolve | "Which auth method to use?" |
| **Decision** | Conclusion with reasoning | "Using JWT because..." |

### Relationship Types

| Relationship | Meaning |
|--------------|---------|
| `REFERENCES` | Note mentions another concept |
| `DERIVED_FROM` | Claim extracted from source |
| `SUPPORTS` | Evidence for a claim |
| `CONTRADICTS` | Conflicting information |
| `ANSWERS` | Decision resolves question |
| `DEPENDS_ON` | Prerequisite relationship |

## Benefits

### For Humans

- **Scannable** - Atomic notes easy to review
- **Navigable** - Follow links to understand relationships
- **Editable** - Add your own notes, agent picks them up
- **Visualizable** - Obsidian graph view shows structure
- **Searchable** - Tags and keywords for filtering

### For the Agent

- **Retrieval** - Query graph when context compacts instead of losing connections
- **Context building** - Traverse related notes to reconstruct relevant context
- **Memory evolution** - Update existing notes as understanding deepens
- **Cross-job learning** - Access knowledge from previous jobs
- **Structured output** - Clear units for reasoning about

## Integration with Current System

### Workspace Structure (Proposed)

```
workspace/job_<uuid>/
├── index.md              # MOC (Map of Content) for this job
├── notes/                # Atomic notes
│   ├── 20260201-1423-oauth-vs-jwt.md
│   ├── 20260201-1435-user-requirements.md
│   └── ...
├── sources/              # Source documents and references
│   └── spec-document.md
├── questions/            # Open questions
│   └── auth-method.md
├── decisions/            # Decisions made
│   └── chose-jwt.md
├── archive/              # Phase artifacts (existing)
└── todos.yaml            # Task list (existing)
```

### Agent Tools (Proposed)

```python
# Create atomic note with automatic linking
create_note(
    title="OAuth vs JWT",
    content="...",
    type="concept",
    tags=["authentication"],
    links=["user-management", "security"]
)

# Query knowledge graph
find_related("authentication")  # Returns linked notes
trace_provenance("must handle 1000 RPS")  # Find source
detect_contradictions()  # Find conflicting claims

# Memory evolution
update_note(
    note_id="20260201-1423",
    add_context="After further analysis...",
    add_links=["new-finding"]
)
```

### Sync Strategy Options

1. **Files as source of truth** - Parse markdown into graph on read
2. **Graph as source of truth** - Generate markdown from graph
3. **Bidirectional sync** - Track changes in both, merge conflicts

Recommendation: **Files as source of truth** for simplicity and Obsidian compatibility. Graph is a derived index for querying.

## Open Questions

1. **Vault scope** - One vault per job, or single vault with all jobs?
   - Single vault enables cross-job graph view and search
   - Per-job keeps things isolated but loses connections

2. **Graph database choice** - Neo4j, or lighter alternative?
   - Could start with in-memory graph, persist to JSON
   - Neo4j for production scale

3. **Automatic linking** - How aggressive should auto-linking be?
   - Too much = noise
   - Too little = missed connections

4. **Migration path** - How to handle existing `workspace.md` approach?
   - Gradual: new notes in Obsidian style, workspace.md as legacy
   - Convert: tool to split workspace.md into atomic notes

5. **Fine-tuning vs prompting** - How to teach the agent the style?
   - System prompt conventions (immediate)
   - Tool-enforced structure (medium effort)
   - Fine-tuning on Obsidian corpora (expensive, deepest integration)

## Next Steps

1. [ ] Design note schema in detail
2. [ ] Implement `create_note` tool with frontmatter generation
3. [ ] Implement basic graph indexing (parse wikilinks from files)
4. [ ] Add `find_related` query tool
5. [ ] Update workspace template to use Obsidian structure
6. [ ] Test with a real job and iterate

## References

- [Zettelkasten Method](https://zettelkasten.de/posts/overview/)
- [Obsidian Help](https://help.obsidian.md/)
- [A-MEM Paper](https://arxiv.org/abs/2502.12110)
- [Obsidian-Assist](https://github.com/ya0002/obsidian-assist)
