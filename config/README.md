# Agent Configuration

This directory contains agent configuration files and templates.

## Directory Structure

```
config/
├── defaults.yaml              # Framework defaults (all configs extend this)
├── schema.json                # JSON Schema for config validation
├── README.md                  # This file
├── prompts/                   # Prompt templates
│   ├── strategic.txt          # Strategic phase system prompt
│   ├── tactical.txt           # Tactical phase system prompt
│   ├── systemprompt.txt       # Main system prompt
│   └── summarization_prompt.txt
└── templates/                 # File templates
    ├── strategic_todos_initial.yaml    # Initial todos for job start
    ├── strategic_todos_transition.yaml # Todos for phase transitions
    └── workspace_template.md           # Template for workspace.md
```

## Creating a Custom Agent Config

### Option 1: Single File Config

Create a YAML file that extends defaults:

```yaml
# yaml-language-server: $schema=schema.json
$extends: defaults

agent_id: my_agent
display_name: My Custom Agent
description: Does custom things

tools:
  research:
    - web_search
    - search_papers
  citation:
    - cite_web
```

Save as `config/my_agent.yaml` and run:

```bash
python agent.py --config my_agent
```

### Option 2: Directory Config (with prompt overrides)

For configs that need custom prompts, create a directory:

```
config/
└── my_agent/
    ├── config.yaml           # Agent config (extends defaults)
    ├── instructions.md       # Custom instructions (optional)
    └── strategic.txt         # Custom strategic prompt (optional)
```

The config loader checks the directory first for prompt files, falling back to `config/prompts/` for any not overridden.

## Configuration Reference

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Unique identifier (lowercase, underscores allowed) |
| `display_name` | string | Human-readable name |

### LLM Configuration

```yaml
llm:
  model: openai/gpt-oss-120b
  temperature: 0.0
  reasoning_level: high  # low, medium, high
  base_url: null         # Custom API endpoint
  timeout: 600           # Seconds
  max_retries: 3
```

### Workspace Configuration

```yaml
workspace:
  structure:
    - archive/
    - output/
    - tools/
  instructions_template: instructions.md
  max_read_words: 25000
```

### Tool Categories

Tools are organized into categories. Each category maps to a module under `src/tools/`:

```yaml
tools:
  # File operations (src/tools/workspace/)
  workspace:
    - read_file
    - write_file
    - edit_file
    - list_files
    - delete_file
    - search_files
    - file_exists
    - move_file
    - rename_file
    - copy_file
    - get_workspace_summary
    - get_document_info

  # Task management + completion (src/tools/core/)
  core:
    - next_phase_todos      # Stage todos for next tactical phase
    - todo_complete          # Mark current todo done
    - todo_rewind            # Roll back failed todo
    - mark_complete          # Signal phase/task completion
    - job_complete           # Signal final completion (strategic only)

  # Document processing (src/tools/document/)
  document:
    - chunk_document

  # Research: web, papers, browser, workflows (src/tools/research/)
  research:
    - web_search             # Tavily web search
    - search_papers          # Search arXiv or Semantic Scholar
    - download_paper         # Download PDF (arXiv → Unpaywall → browser fallback)
    - get_paper_info         # Paper metadata via Semantic Scholar
    - browse_website         # AI browser automation (browser-use)
    - download_from_website  # Download files via browser automation
    - research_topic         # Multi-database literature search + download

  # Citation management (src/tools/citation/)
  citation:
    - cite_document
    - cite_web
    - list_sources
    - get_citation
    - list_citations
    - edit_citation

  # Database tool categories (src/tools/graph/, sql/, mongodb/)
  # These are injected/stripped automatically by the orchestrator based on
  # which datasources are attached to the job. Usually left empty in config.
  # See docs/datasources.md for details.
  graph: []      # Neo4j: execute_cypher_query, get_database_schema
  sql: []        # PostgreSQL: sql_query, sql_schema, sql_execute
  mongodb: []    # MongoDB: mongo_query, mongo_aggregate, mongo_schema, mongo_insert, mongo_update

  # Workspace version control (src/tools/git/)
  git:
    - git_log
    - git_show
    - git_diff
    - git_status
    - git_tags
```

Select which tools your agent needs. For example, a research-focused agent:

```yaml
tools:
  research:
    - web_search
    - search_papers
    - download_paper
    - research_topic
  citation:
    - cite_web
    - cite_document
```

See `defaults.yaml` for the full default tool set.

### Research & Browser Configuration

```yaml
research:
  proxy:
    enabled: false       # Enable proxy for paywalled content
    type: socks5         # "http", "socks5", or "none"
    host: localhost       # Proxy host (e.g., SSH tunnel)
    port: 1080            # Proxy port

browser:
  headless: true          # Run browser without GUI
  timeout: 60000          # Navigation timeout (ms)
  use_vision: false       # DOM-based (default) vs screenshot-based navigation
```

Proxy can also be set via environment variables: `RESEARCH_PROXY_TYPE`, `RESEARCH_PROXY_HOST`, `RESEARCH_PROXY_PORT`, `RESEARCH_PROXY_USER`, `RESEARCH_PROXY_PASS`.

Browser LLM is configured separately: `BROWSER_LLM_MODEL` (default: `gpt-4o-mini`), `BROWSER_LLM_API_KEY`, `BROWSER_LLM_BASE_URL`.

### Database Connections

```yaml
connections:
  postgres: true
```

External datasources (Neo4j, MongoDB, additional PostgreSQL) are managed through the datasource connector system. See `docs/datasources.md`.

### Multi-Stage Config Pipeline (Database Tools)

Database tool categories (`graph`, `sql`, `mongodb`) are **not** controlled by the agent config YAML. Instead, they go through a multi-stage pipeline:

```
1. Agent config (config/*.yaml)        → User defines base tools (workspace, research, etc.)
2. Orchestrator datasource override    → System injects/strips database tools based on attached datasources
3. Final resolved config               → What the agent actually receives
```

- If a datasource is attached to the job, the orchestrator **injects** the corresponding tool category (even if the config doesn't list it).
- If no datasource of a type is attached, the orchestrator **strips** the category (even if the config lists it).
- The `read_only` flag on the datasource controls whether write tools are included.

This means the agent config controls non-database tools, while the orchestrator controls database tools based on what's actually connected. See `_build_datasource_tool_override()` in `orchestrator/main.py`.

### Context Management

```yaml
limits:
  context_threshold_tokens: 60000
  message_count_threshold: 200
  model_max_context_tokens: 100000

context_management:
  compact_on_archive: true
  keep_recent_tool_results: 10
  keep_recent_messages: 10
```

## Inheritance

Configs use `$extends: defaults` to inherit from `defaults.yaml`. Deep merge applies:
- Objects (dicts): Recursively merged
- Arrays (lists): Override replaces entirely
- Scalars: Override replaces
- `null` value: Clears the key from result

Example clearing an inherited array:

```yaml
$extends: defaults

tools:
  research: null  # Clears all research tools
```

## Schema Validation

Add the schema comment at the top of your YAML file for IDE autocompletion:

```yaml
# yaml-language-server: $schema=schema.json
```

This works with VS Code + Red Hat YAML extension.

## Running Agents

```bash
# Use defaults
python agent.py

# Use custom config
python agent.py --config my_agent

# Use explicit path
python agent.py --config /path/to/config.yaml

# As API server
python agent.py --config my_agent --port 8001
```
