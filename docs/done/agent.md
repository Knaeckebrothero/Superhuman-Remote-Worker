# Universal Agent Overview

This document provides a high-level overview of the Universal Agent system for team members who want to understand how it works.

## Agent Loop Structure

The agent runs in a simple loop with 4 steps:

```
initialize → process ←→ tools
                ↓
              check → END (or back to process)
```

| Step | What happens |
|------|--------------|
| **Initialize** | Sets up workspace, loads instructions, prepares initial context |
| **Process** | The LLM thinks and decides what to do next (call a tool or respond) |
| **Tools** | Executes the tool the LLM requested (read file, search web, etc.) |
| **Check** | Decides if we're done or should continue. If done → END, otherwise → back to Process |

The agent keeps looping through **Process → Tools → Check** until the task is complete or a limit is reached.

---

## Available Tools

The agent has access to these tools, grouped by category:

### Workspace Tools
| Tool | Description |
|------|-------------|
| `read_file` | Read a file from the workspace |
| `write_file` | Write content to a file |
| `append_file` | Add content to the end of a file |
| `list_files` | List files in a directory |
| `delete_file` | Delete a file |
| `search_files` | Search for text in files |
| `file_exists` | Check if a file exists |
| `get_workspace_summary` | Get statistics about the workspace |

### Todo Tools
| Tool | Description |
|------|-------------|
| `todo_write` | Create or update the task list |
| `todo_complete` | Mark the current task as done |
| `todo_rewind` | Signal that the agent is stuck and needs to replan |
| `archive_and_reset` | Archive completed todos and start fresh |

### Document Tools
| Tool | Description |
|------|-------------|
| `extract_document_text` | Extract text from PDF, Word, or other documents |
| `chunk_document` | Split a document into smaller pieces |
| `get_document_info` | Get metadata about a document |

### Search Tools
| Tool | Description |
|------|-------------|
| `web_search` | Search the web for information |

### Citation Tools
| Tool | Description |
|------|-------------|
| `cite_document` | Create a citation for document content |
| `cite_web` | Create a citation for a web source |
| `list_sources` | List all registered citation sources |
| `get_citation` | Get details of a specific citation |

### Cache Tools (Requirements)
| Tool | Description |
|------|-------------|
| `add_requirement` | Submit a requirement to the database |
| `list_requirements` | Browse existing requirements |
| `get_requirement` | Get details of a specific requirement |

### Graph Tools (Neo4j)
| Tool | Description |
|------|-------------|
| `execute_cypher_query` | Run a database query |
| `get_database_schema` | Get the database structure |
| `find_similar_requirements` | Find existing similar requirements |
| `check_for_duplicates` | Check if a requirement already exists |
| `resolve_business_object` | Match text to a BusinessObject in the graph |
| `resolve_message` | Match text to a Message in the graph |
| `validate_schema_compliance` | Check if data follows the rules |
| `create_requirement_node` | Create a new requirement in the graph |
| `create_fulfillment_relationship` | Link a requirement to what fulfills it |
| `generate_requirement_id` | Generate a unique ID (R-0001, R-0002, etc.) |
| `get_entity_relationships` | Get relationships for an entity |
| `count_graph_statistics` | Get statistics about the graph |

### Completion Tools
| Tool | Description |
|------|-------------|
| `mark_complete` | Signal that a task is complete |
| `job_complete` | Signal that the entire job is finished |

---

## Workspace Structure

Each agent has a workspace folder for storing files and state:

### Creator Agent Workspace
```
workspace/
  archive/                    # Archived todo lists from completed phases
  documents/                  # Source documents being processed
  tools/                      # Auto-generated tool documentation
  instructions.md             # What the agent should do
  plan.md               # The strategic plan (high-level goals)
  workspace.md               # Persistent memory / notes
  workspace_summary.md       # Current workspace state
  document_analysis.md       # Notes from analyzing documents
  extraction_results.md      # Extracted requirements
```

### Validator Agent Workspace
```
workspace/
  archive/                    # Archived todo lists
  analysis/                   # Analysis results
  output/                     # Final outputs
  tools/                      # Auto-generated tool documentation
  instructions.md             # Validation instructions
  plan.md               # Strategic plan
```

---

## What the Agent Sees (Prompt Structure)

On every generation, the LLM receives this information in order:

```
┌─────────────────────────────────────────────────────────┐
│ 1. SYSTEM PROMPT                                        │
│    - Who the agent is and its role                      │
│    - Workspace location and structure                   │
│    - Content of workspace.md (persistent memory)        │
│    - How to work (planning principles)                  │
│    - Citation requirements                              │
│    - What to do when stuck                              │
│    - Task completion protocol                           │
├─────────────────────────────────────────────────────────┤
│ 2. PROTECTED CONTEXT (refreshed every iteration)        │
│    - Strategic plan from plan.md                   │
│    - Current todo list with markers:                    │
│      [x] Completed task                                 │
│      [ ] Pending task ← CURRENT                         │
│      [ ] Future task                                    │
├─────────────────────────────────────────────────────────┤
│ 3. CONVERSATION HISTORY                                 │
│    - Previous tool calls and their results              │
│    - Previous LLM responses                             │
│    (Trimmed when approaching token limits)              │
├─────────────────────────────────────────────────────────┤
│ 4. JOB CONTEXT                                          │
│    - Document path (if processing a document)           │
│    - Task prompt (what to do)                           │
│    - Requirement ID (if validating a requirement)       │
├─────────────────────────────────────────────────────────┤
│ 5. TOOL DESCRIPTIONS                                    │
│    - Short descriptions of all available tools          │
│    - Full docs available in workspace/tools/            │
└─────────────────────────────────────────────────────────┘
```

### Context Management

The agent has an 80,000 token context window. When approaching this limit:
1. Older messages are summarized
2. Recent 5 tool results are kept intact
3. Protected context (plan + todos) is re-injected
4. This prevents the agent from "forgetting" important information

---

## Two Agent Types

The same Universal Agent code runs as two different agent types based on configuration:

| Agent | Role | Polls for | Key Tools |
|-------|------|-----------|-----------|
| **Creator** | Extracts requirements from documents | New jobs | Document, Search, Citation, Cache |
| **Validator** | Validates requirements against the knowledge graph | Pending requirements | Graph, Validation |

---

## Key Limits

| Parameter | Value | Description |
|-----------|-------|-------------|
| Max iterations | 500 | Maximum loop iterations before stopping |
| Context threshold | 80,000 tokens | When to start compacting context |
| Tool retries | 3 | How many times to retry a failed tool |
| Polling interval | 30s (Creator) / 10s (Validator) | How often to check for new work |

---

## Summary

The Universal Agent is a **config-driven, workspace-centric autonomous system** that:
- Loops through thinking → tool use → checking until done
- Uses a workspace folder for persistent storage and memory
- Receives refreshed context (plan + todos) on every iteration
- Can be configured as Creator or Validator with different tools and behaviors
