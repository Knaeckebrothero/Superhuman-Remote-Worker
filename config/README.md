# Agent Configuration

This directory contains agent configuration files and templates.

## Directory Structure

```
config/
├── defaults.yaml                # Framework defaults (all configs extend this)
├── schema.json                  # JSON Schema for config validation
├── prompt_matrix.yaml           # Base prompt matrix (model family → filename)
├── instruction_matrix.yaml      # Base instruction matrix (model family → filename)
├── settings_matrix.yaml         # Model-family-specific inference params & context limits
├── README.md                    # This file
├── experts/                     # Pre-built agent roles (developer, scholar, critic)
│   └── <expert>/
│       ├── config.yaml              # Agent config (extends defaults)
│       ├── prompt_matrix.yaml       # Expert-level prompt matrix (optional)
│       └── instruction_matrix.yaml  # Expert-level instruction matrix (optional)
├── prompts/                     # Prompt templates (system prompt, phase prompts)
│   ├── systemprompt.txt         # Main system prompt
│   ├── persona.txt              # Agent persona/identity prompt
│   ├── strategic.txt            # Strategic phase system prompt
│   ├── tactical.txt             # Tactical phase system prompt
│   ├── summarization_prompt.txt # Context compaction prompt
│   ├── systemprompt_minimax.txt # MiniMax-optimized system prompt
│   ├── persona_minimax.txt      # MiniMax-optimized persona
│   ├── strategic_minimax.txt    # MiniMax-optimized strategic prompt
│   ├── tactical_minimax.txt     # MiniMax-optimized tactical prompt
│   └── summarization_prompt_minimax.txt  # MiniMax-optimized summarization
└── templates/                   # Instruction templates (non-prompt files)
    ├── instructions.md                  # Default agent instructions
    ├── instructions_minimax.md          # MiniMax-optimized instructions
    ├── strategic_todos_initial.yaml     # Initial todos for job start
    ├── strategic_todos_transition.yaml  # Todos for phase transitions
    ├── strategic_todos_resume.yaml      # Todos for job resume with feedback
    ├── workspace_template.md            # (deprecated; workspace.md removed — unused)
    ├── todo_guide.md                    # Todo crafting guide
    └── phase_retrospective_template.md  # Template for phase retrospectives
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

For configs that need custom prompts or instructions, create a directory:

```
config/
└── my_agent/
    ├── config.yaml              # Agent config (extends defaults)
    ├── prompt_matrix.yaml       # Expert-level prompt matrix (optional)
    ├── instruction_matrix.yaml  # Expert-level instruction matrix (optional)
    ├── instructions.md          # Custom instructions (optional)
    └── strategic.txt            # Custom strategic prompt (optional)
```

### Two Matrix Systems

The agent uses two parallel matrix systems with the same 4-level fallback chain:

**Prompt Matrix** (`prompt_matrix.yaml`) — resolves system prompts:
- Entries: `systemprompt`, `persona`, `strategic`, `tactical`, `summarization`
- File search: expert directory → `config/prompts/`

**Instruction Matrix** (`instruction_matrix.yaml`) — resolves non-prompt templates:
- Entries: `instructions`, `strategic_todos_initial`, `strategic_todos_transition`, `strategic_todos_resume`, `workspace_template`, `todo_guide`
- File search: expert directory → `config/templates/`

Both use the same resolution chain (4 levels):

1. Expert matrix → model-specific key → type
2. Expert matrix → `"default"` key → type
3. Base matrix → model-specific key → type
4. Base matrix → `"default"` key → type

Once the filename is resolved, the loader checks the expert directory first for the file, falling back to the framework directory (`config/prompts/` or `config/templates/`).

### Resolved Config JSONB

On first run, the fully resolved config (agent config + all prompt/instruction content) is frozen into a `resolved_config` JSONB column on the jobs table. On resume, the agent loads from this snapshot instead of resolving from disk. This prevents config drift and makes jobs reproducible.

## Configuration Reference

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Unique identifier (lowercase, underscores allowed) |
| `display_name` | string | Human-readable name |

### Autonomy Level

Controls when the agent pauses for human review at phase boundaries and after job completion.

```yaml
autonomy: partial  # full | review | partial | guided | dependent
```

| Level | After 1st Strategic | After Nth Strategic | After Tactical | After job_complete |
|-------|:---:|:---:|:---:|:---:|
| `full` | - | - | - | auto-complete |
| `review` | - | - | - | freeze |
| `partial` | freeze | - | - | freeze |
| `guided` | freeze | freeze | - | freeze |
| `dependent` | freeze | freeze | freeze | freeze |

- **`full`** — Fully autonomous. Never freezes. On `job_complete`, writes `job_completion.json` directly and sets DB status to `completed`.
- **`review`** — Runs freely through all phases but freezes after `job_complete` for human review before marking as completed.
- **`partial`** (default) — Freezes after the first strategic phase boundary for early feedback, then runs freely. Freezes again at `job_complete`.
- **`guided`** — Freezes at every strategic phase boundary for review. Tactical phases run freely. Freezes at `job_complete`.
- **`dependent`** — Freezes at every phase boundary (strategic and tactical). Maximum human oversight.

When frozen at a phase boundary (`freeze_type: "phase_boundary"`), approving the job sets its status back to `processing` and the agent continues. When frozen at job completion (`freeze_type: "job_complete"`), approving writes `job_completion.json` and sets status to `completed`.

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
    - create_directory
    - delete_directory

  # Task management + completion (src/tools/core/)
  core:
    - next_phase_todos      # Stage todos for next tactical phase
    - todo_complete          # Mark current todo done
    - todo_list              # List current todos
    - todo_rewind            # Roll back failed todo
    - mark_complete          # Signal phase/task completion
    - job_complete           # Signal final completion (strategic only)

  # Research: web, papers, browser, workflows (src/tools/research/)
  research:
    - web_search             # Tavily web search
    - extract_webpage        # Extract content from a URL
    - crawl_website          # Crawl a website following links
    - map_website            # Map a website's link structure
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
    - annotate_source
    - get_annotations
    - tag_source
    - search_library
    - generate_bibliography

  # Database tool categories (src/tools/graph/, sql/, mongodb/)
  # These are injected/stripped automatically by the orchestrator based on
  # which datasources are attached to the job. Usually left empty in config.
  # See docs/datasources.md for details.
  graph: []      # Neo4j: execute_cypher_query, get_database_schema
  sql: []        # PostgreSQL: sql_query, sql_schema, sql_execute
  mongodb: []    # MongoDB: mongo_query, mongo_aggregate, mongo_schema, mongo_insert, mongo_update

  # Shell command execution (src/tools/shell/)
  # Mode controlled by shell.mode: "stateless" (default) or "persistent"
  shell:
    - run_command     # Execute commands, get output (stateless mode, default)
    - shell_read      # Read more output from scrollback
    # Alternative (persistent mode): shell_execute + shell_read

  # Evaluation tools for critic agents (src/tools/evaluation/)
  # Enable in critic config for approve/return capabilities.
  evaluation: []

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

Context limits are model-dependent and set via `settings_matrix.yaml` (see below). The values below are defaults that get overridden per model family:

```yaml
limits:
  message_count_threshold: 300
  # Model-dependent (set in settings_matrix.yaml, NOT here):
  # context_threshold_tokens, model_max_context_tokens,
  # summarization_safe_limit, summarization_chunk_size,
  # message_count_min_tokens

context_management:
  compact_on_archive: true
  keep_recent_tool_results: 150
  keep_recent_messages: 10
```

### Settings Matrix

`settings_matrix.yaml` is the single source of truth for model-family-specific inference parameters and context limits. Keys match `detect_model_family()` output in `src/core/loader.py`.

```yaml
# Resolution: default → family-specific (deep_merge)

default:
  model_max_context_tokens: 128000
  limits:
    model_max_context_tokens: 100000
    context_threshold_tokens: 80000

minimax:
  temperature: 1.0
  top_p: 0.95
  limits:
    model_max_context_tokens: 150000
    context_threshold_tokens: 100000

deepseek:
  model_max_context_tokens: 64000
  limits:
    context_threshold_tokens: 40000
```

Experts can place their own `settings_matrix.yaml` in their directory.

### Verification

Auto-spawn a critic job after `job_complete` to review deliverables:

```yaml
verification:
  enabled: true          # Spawn critic job after job_complete
  critic_config: critic  # Which expert config to use for the reviewer
  max_rounds: 3          # Max feedback round-trips before auto-accepting
```

### Memory Light

Opt-in recall system backed by PostgreSQL (pgvector hybrid search). Stores and retrieves insights across context compactions. See `docs/features/memory_light.md` for full design.

```yaml
memory:
  enabled: true
  budget_tokens: 10000
  max_memories_per_injection: 25
  observer_interval: 5
  embedding_model: qwen3-embedding-8b
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
