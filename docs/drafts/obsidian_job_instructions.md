# Obsidian Tagging Instructions

## Task

You have a folder of markdown documents (`documents/`). Enrich each document with Obsidian-compatible metadata so the collection works as a linked knowledge graph.

## What To Do

For every `.md` file in `documents/` (including subdirectories):

1. **Add YAML frontmatter** at the top with 3-8 relevant tags
2. **Add `[[wiki-links]]`** inline wherever the text references a topic covered by another document in the collection
3. **Add a `## Related` section** at the bottom listing the most relevant linked documents

Do NOT modify existing content, headings, or structure. Only add frontmatter, inline links, and the Related section.

## Tag Conventions

- Lowercase, hyphenated: `context-management`, `citation-engine`, `neo4j`
- Reuse tags across documents — maintain a **tag index** in your workspace to track all tags in use
- Avoid synonyms: pick ONE tag per concept (e.g., decide between `deployment` and `infrastructure`, not both for the same meaning)
- Use broad categories + specific tags: e.g., `tags: [architecture, context-management, token-optimization]`

## Link Conventions

- Link format: `[[filename_without_extension]]` (e.g., `[[context_management]]`, `[[datasources]]`)
- Only link to documents that actually exist in the collection
- Link on first meaningful mention in a section, not every occurrence
- For subdirectories, use relative path: `[[done/masterplan]]`, `[[features/repo_datasource]]`

## Example

A document about the agent's context window handling might look like this after processing:

```markdown
---
tags:
  - architecture
  - context-management
  - token-optimization
  - summarization
---

# Context Management

The agent uses a three-layer defense against [[context_management|context window overflow]]...

When the context compacts, [[working_memory|workspace.md]] is re-injected as a synthetic tool call...

The [[prompts|prompt system]] controls how summarization behaves...

## Related

- [[working_memory]] — Workspace-centric memory model
- [[prompts]] — Prompt architecture and phase directives
- [[model_issues]] — Context-related model failures
```

## Workflow

1. **Inventory**: List all documents and build a filename index
2. **Tag taxonomy**: Skim 10-15 representative documents and define a consistent set of 15-30 tags before tagging individual files
3. **Process documents in batches**: Delegate the actual file editing to `claude_code` (see below). Process 10-15 documents per batch.
4. **Verify**: Spot-check a few documents for consistency after each batch

## Using Claude Code for Batch Processing

**Do NOT edit documents one-by-one with `write_file` or `edit_file`.** That approach is far too slow for 80+ files.

Instead, use the `claude_code` tool to delegate batch editing. Give Claude Code:
- The tag index and link conventions
- A list of 10-15 files to process in one call
- Clear instructions to add frontmatter, inline wiki-links, and Related sections

Example `claude_code` call:
```
Process documents/context_management.md, documents/datasources.md, documents/prompts.md, ...

For each file:
1. Read the file
2. Add YAML frontmatter with 3-8 tags from this taxonomy: [your tag list]
3. Add [[wiki-links]] on first mention of topics covered by other docs
4. Add a ## Related section at the bottom with 3-6 links
5. Write the file back

Only link to files that exist. Do not modify existing content.
```

Claude Code has its own context window and tools — it can read, edit, and write files directly. This is the fastest way to process the collection.

## Deliverables

The edited documents themselves. Every `.md` file in `documents/` should have frontmatter tags, inline wiki-links where appropriate, and a Related section.

Save your final tag index to `output/tag_index.md` as a reference.
