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
  domain:
    - web_search
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

Tools are organized into categories:

```yaml
tools:
  workspace:
    - read_file
    - write_file
    - list_files
    # ...
  todo:
    - next_phase_todos
    - todo_complete
    - todo_rewind
  domain:
    - web_search
    - cite_web
    # ...
  completion:
    - mark_complete
    - job_complete
```

**Domain tools** are agent-specific and should be customized. See `defaults.yaml` for all available workspace, todo, and completion tools.

### Database Connections

```yaml
connections:
  postgres: true
  neo4j: false
```

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
  domain: null  # Clears all domain tools
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
