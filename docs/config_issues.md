# Config Audit: defaults.json Value Usage

Audit of all config values in `src/config/defaults.json` to determine whether each is actually parsed and used at runtime.

## Broken

| Field | Issue |
|---|---|
| `workspace.max_read_size` | `WorkspaceConfig` dataclass in `loader.py` has no `max_read_size` field. The value is silently dropped during parsing. Workspace tools always fall back to the hardcoded default of 25,000. **Changing this in config has zero effect.** |

**Fix**: Add `max_read_size` to `WorkspaceConfig` dataclass, parse it in `load_agent_config()`, and wire it through to `WorkspaceManager` / `ToolContext`.

## Dead Config (Parsed but Never Read)

These values are loaded into dataclasses but no runtime code ever accesses them.

| Field | Issue |
|---|---|
| `todo.archive_on_reset` | `TodoManager` always archives unconditionally; this flag is never checked. |
| `todo.archive_path` | `TodoManager.archive()` hardcodes `archive/` as the path; never reads from config. |
| `todo.max_items` (schema only) | Parsed into `TodoConfig` but never accessed. Min/max enforcement comes from `phase_settings.min_todos`/`max_todos` instead. |
| `description` | Parsed into `AgentConfig.description` but never accessed by any code. |
| `phase_settings.archive_on_transition` | Parsed but never accessed; archiving is unconditional in the `archive_phase` graph node. |

**Fix**: Either wire these into the runtime code or remove them from the config/schema to avoid confusion.

## Aspirational (Not Even Parsed)

These three entire sections are not in `known_fields` in `loader.py`. They end up in `AgentConfig.extra` and are passed to `ToolContext.config`, but no tool or manager ever reads them.

### `document_processing` (5 fields)

- `chunking_strategy`
- `max_chunk_size`
- `chunk_overlap`
- `extraction_mode`
- `min_confidence_threshold`

No code consumes these values. Document chunking does not consult the config.

### `research` (3 fields)

- `web_search_enabled`
- `graph_search_enabled`
- `research_depth`

No code reads these flags. Tool availability is controlled by the `tools.domain` array, not by these booleans.

### `validation` (5 fields)

- `duplicate_threshold`
- `require_citations`
- `metamodel_validation_strict`
- `fulfillment_confidence_threshold`
- `auto_integrate`

No code reads these thresholds. The validator agent relies on LLM reasoning and prompt instructions rather than config-driven thresholds.

**Fix**: Either implement runtime consumers for these sections or remove them from `defaults.json` and `schema.json`.

## Fully Active (for reference)

All other config values are properly parsed and used at runtime:

- **Top-level**: `agent_id`, `display_name`
- **llm**: `model`, `temperature`, `reasoning_level`, `base_url`
- **workspace**: `structure`, `instructions_template`, `initial_files`
- **tools**: `workspace`, `todo`, `domain`, `completion`
- **connections**: `postgres`, `neo4j`
- **polling**: `enabled`, `table`, `status_field`, `status_value_pending`, `status_value_processing`, `status_value_complete`, `status_value_failed`, `interval_seconds`, `use_skip_locked`
- **limits**: `context_threshold_tokens`, `message_count_threshold`, `message_count_min_tokens`, `tool_retry_count`
- **context_management**: `compact_on_archive`, `keep_recent_tool_results`, `keep_recent_messages`, `summarization_template`, `reasoning_level`, `max_summary_length`
- **phase_settings**: `min_todos`, `max_todos`
