# Agent Configuration

This directory contains agent configuration files and templates.

## Directory Structure

```
config/
├── expert_base.yaml             # The ONE shared root every expert resolves on (every role)
├── overlays/                    # Role overlays — each `$extends: expert_base`
│   ├── worker.yaml              #   public name `worker_base`  (job / phase-loop experts)
│   ├── session.yaml             #   public name `session_base` (interactive / persistent experts)
│   └── subagent.yaml            #   public name `subagent_base` (roster entries; declares `$ignore_keys`)
├── schema.json                  # JSON Schema for config validation
├── model_config_matrix.yaml     # Per model family: prompt/instruction filenames, inference params, context limits
├── README.md                    # This file
├── experts/                     # Bundled roles and application-default seed bundles
│   └── <expert>/
│       ├── config.yaml              # Expert overlay (`$extends: worker_base` or `session_base`)
│       └── model_config_matrix.yaml # Expert-level matrix override (optional)
├── subagents/                   # Subagent library — small experts a roster references by name (see subagents/README.md)
│   └── <name>/
│       ├── config.yaml              # `$extends: expert_base`, `tags: [subagent]`, read-only tools, `llm: {model: inherit}`
│       └── persona.txt              # Prompt files next to the config, like an expert's
├── prompts/                     # Prompt templates (system prompt, phase prompts)
│   ├── systemprompt.txt         # Main system prompt
│   ├── persona.txt              # Agent persona/identity prompt
│   ├── strategic.txt            # Strategic phase prompt — legacy prompt_mode only; the live
│   ├── tactical.txt             # Tactical phase prompt      guidance is skills/{strategic,tactical}-phase/SKILL.md
│   ├── summarization_prompt.txt # Context compaction prompt
│   ├── systemprompt_minimax.txt # MiniMax M2.7-optimized system prompt
│   ├── persona_minimax.txt      # MiniMax M2.7-optimized persona
│   ├── strategic_minimax.txt    # MiniMax M2.7-optimized strategic prompt
│   ├── tactical_minimax.txt     # MiniMax M2.7-optimized tactical prompt
│   ├── summarization_prompt_minimax.txt  # MiniMax M2.7-optimized summarization
│   ├── systemprompt_minimax_m3.txt        # MiniMax M3 system prompt (1M ctx, multimodal)
│   ├── persona_minimax_m3.txt             # MiniMax M3 persona
│   ├── strategic_minimax_m3.txt           # MiniMax M3 strategic prompt
│   ├── tactical_minimax_m3.txt            # MiniMax M3 tactical prompt
│   ├── summarization_prompt_minimax_m3.txt # MiniMax M3 summarization
│   ├── systemprompt_glm.txt                # GLM-5.2 worker system prompt
│   ├── systemprompt_interactive_glm.txt    # GLM-5.2 persistent-chat system prompt
│   └── persona_glm.txt                     # GLM-5.2 persona
└── templates/                   # Instruction templates (non-prompt files)
    ├── instructions.md                  # Default agent instructions
    ├── instructions_minimax.md          # MiniMax M2.7-optimized instructions
    ├── instructions_minimax_m3.md       # MiniMax M3 instructions (+ multimodal / long-context guidance)
    ├── strategic_todos_initial.yaml     # Initial todos for job start
    ├── strategic_todos_transition.yaml  # Todos for phase transitions
    ├── strategic_todos_resume.yaml      # Todos for job resume with feedback
    ├── workspace_template.md            # (deprecated; workspace.md removed — unused)
    ├── todo_guide.md                    # Todo crafting guide
    └── phase_retrospective_template.md  # Template for phase retrospectives
```

## Roles, the shared root and the overlays

Every expert resolves on the same chain, most specific last:

```
expert_base  <-  overlays/<role>  <-  expert ($extends chain)  <-  model family (matrix)  <-  job / thread / roster override
```

- `expert_base.yaml` carries everything every role shares (llm, tools, limits,
  memory, auxiliary, browser, shell, ...). It is never loaded on its own by a
  runtime.
- `overlays/<role>.yaml` adds the role's own keys and the role's values of
  shared keys. The worker overlay owns the phase loop (`instruction_files`,
  `phase_settings`, `delegation`, `autonomy`, `verification`, `scholar`,
  `curator`, `communication`, the `core` tool group); the session overlay owns
  the canvas grant, the session-only application groups and the session memory
  writers; the subagent overlay is a read-only tool floor with memory and
  background tasks off.
- The overlays' **public names** are `worker_base`, `session_base` and
  `subagent_base`. They are what `$extends`, `--config`, `config_name` and the
  experts API use; `default`/`defaults`, `persistent_default`/`persistent_defaults`
  and the file spelling `overlays/<role>` are accepted aliases. A path such as
  `config/worker_base.yaml` still loads (it lands on the overlay).

**Role re-rooting.** A config is normally loaded on the root its own chain
names. When it is resolved *for* a role — a job resolves for `worker`, a
session for `session`, a roster entry for `subagent` — the loader
(`load_and_merge_config(path, role=...)`) replaces the link that ends the chain
with that role's overlay. So a session expert dispatched as a job gains the
worker keys underneath it, and a worker expert used in a session sits on the
session overlay; the expert's own values always win ("expert wins").

**Ignored keys.** A role overlay may declare `$ignore_keys`, a list of dotted
paths its role never reads. They are pruned from the merged config after every
merge, again after the job/thread override layers, and after a roster
override, so no later layer can re-introduce them. A key that does not apply
to a role is dropped silently — never an error. Today only the subagent
overlay declares any (`workspace.backend/remote/mounts/structure/instructions_template/initial_files/git_versioning`,
`autonomy`, `verification`, `scholar`, `curator`, `phase_settings`,
`delegation`, `communication`, `officer`, `headless`); the worker and session
overlays declare none, so a re-rooted expert keeps everything it authored.

Never read a base file directly — an overlay alone is only the role's residue.
Use `load_role_base(role)` (the merged `expert_base` + overlay) from
`src/core/loader.py`.

## Creating a Custom Agent Config

### Option 1: Single File Config

Create a worker YAML file that extends the worker role base:

```yaml
# yaml-language-server: $schema=schema.json
$extends: worker_base

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

Persistent/session experts use `$extends: session_base` instead; a shared
"small expert" meant for rosters uses `$extends: subagent_base`. The legacy
names `default`, `defaults`, `persistent_default`, and `persistent_defaults`
remain accepted as compatibility aliases, but new configs should use the
explicit public root names. Whichever root an expert names, it can be used in
every role (see "Roles, the shared root and the overlays" above).

### Option 2: Directory Config (with prompt overrides)

For configs that need custom prompts or instructions, create a directory:

```
config/
└── my_agent/
    ├── config.yaml              # Expert overlay (extends a mode base)
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
    - request_replan         # End the phase early and re-plan, keeping all work
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
    - research_topic         # Multi-database literature search + download
    # NOTE: browse_website / download_from_website were removed from the
    # registry — the agent drives the browser itself via the browser_direct
    # group below. Names listed here must exist in TOOL_REGISTRY; an unknown
    # name fails the whole batch load (tests/test_config_tool_names_are_registered.py).

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

  # Version control (src/tools/git/) — reads the job's own repo by default and
  # an attached repository datasource with repo="<clone-dir>".
  #
  # ONLY BOUND WHEN THE AGENT HAS NO SHELL TOOLS. If `shell` above is
  # non-empty, ToolsConfig.__post_init__ (src/core/loader.py) drops this whole
  # group: a shell can run git against any repository in the workspace, and
  # granting both gives the agent two ways to ask one question — the weaker of
  # which silently answers about a different repo. Shell-having agents should
  # be told to run `git ...` (and `git -C repos/<name> ...`) instead.
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

See `expert_base.yaml` (the shared groups) and `overlays/worker.yaml` /
`overlays/session.yaml` (the role-owned groups) for the conservative inherited
tool surfaces. Privileged and orchestration-oriented groups such as shell,
delegation, automations, and loops are opt-in at the expert layer.

### Research & Browser Configuration

```yaml
research:
  proxy:
    enabled: false       # Enable proxy for paywalled content
    type: socks5         # "http", "socks5", or "none"
    host: localhost       # Proxy host (e.g., SSH tunnel)
    port: 1080            # Proxy port

browser:
  snapshot:
    include_screenshot: auto  # "auto" (if model is multimodal) | true | false
    max_dom_chars: 40000      # Truncate DOM text beyond this
  security:
    allowed_domains: []       # Empty = allow all domains
    blocked_domains: []
    blocked_schemes: ["file", "javascript", "data"]
```

The browser itself runs on the workspace (`browser-exec` daemon) — the agent
pod never executes Chromium. See the public
[workspace architecture](../docs/architecture.md#workspace-tiers).

Proxy can also be set via environment variables: `RESEARCH_PROXY_TYPE`, `RESEARCH_PROXY_HOST`, `RESEARCH_PROXY_PORT`, `RESEARCH_PROXY_USER`, `RESEARCH_PROXY_PASS`.

### Database Connections

```yaml
connections:
  postgres: true
```

External datasources (Neo4j, MongoDB, and additional PostgreSQL instances) are
managed through the datasource connector system and resolved at dispatch.

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
  # message_count_min_tokens
  # (summarization budgets are not config leaves — they are computed at call
  # time from the auxiliary model's window, see src/core/summarizer.py)

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

minimax:              # MiniMax M2.7 — 204K context, text-only
  temperature: 1.0
  top_p: 0.95
  limits:
    model_max_context_tokens: 150000
    context_threshold_tokens: 100000

minimax-m3:           # MiniMax M3 — 1M context (MSA), native multimodal; distinct family from minimax
  temperature: 1.0
  top_p: 0.95
  multimodal: true
  model_max_context_tokens: 1000000
  limits:
    model_max_context_tokens: 200000
    context_threshold_tokens: 150000

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

Opt-in recall system backed by PostgreSQL and pgvector hybrid search. It stores
and retrieves project-scoped insights across context compactions; see
[state, knowledge, and recovery](../docs/architecture.md#state-knowledge-and-recovery).

```yaml
memory:
  enabled: true
  budget_tokens: 10000
  max_memories_per_injection: 25
  observer_interval: 5
  embedding_model: qwen3-embedding-8b
```

## Inheritance

Configs use `$extends: worker_base`, `$extends: session_base` or
`$extends: subagent_base` to inherit a role base (`expert_base` + that role's
overlay), or `$extends: <expert>` to build on another expert's chain. Deep
merge applies at every link:
- Objects (dicts): Recursively merged
- Arrays (lists): Override replaces entirely
- Scalars: Override replaces
- `null` value: Clears the key from result

Example clearing an inherited array:

```yaml
$extends: worker_base

tools:
  research: null  # Clears all research tools
```

`null` clears a key for *that* merge only — a later layer (a job override)
re-adds it. Keys a role must never see are declared with `$ignore_keys` on the
role overlay instead (see above); they are pruned after every layer.

## Schema Validation

Add the schema comment at the top of your YAML file for IDE autocompletion:

```yaml
# yaml-language-server: $schema=schema.json
```

This works with VS Code + Red Hat YAML extension.

## Running Agents

```bash
# Use the worker framework base directly (normally a named expert is selected)
python agent.py

# Use custom config
python agent.py --config my_agent

# Use explicit path
python agent.py --config /path/to/config.yaml

# As API server
python agent.py --config my_agent --port 8001
```
