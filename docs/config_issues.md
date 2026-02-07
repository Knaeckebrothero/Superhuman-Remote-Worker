# Config Audit: defaults.yaml Value Usage

Audit of all config values in `config/defaults.yaml` to determine whether each is actually parsed and used at runtime.

## Broken (fixed)

| Field | Status |
|---|---|
| `workspace.max_read_size` | **Fixed.** `max_read_words` is now the primary field. Legacy `max_read_size` is converted to words via `/ 5.5` for backward compatibility. |

## Dead Config (removed)

The following dead config fields were removed:

**Parsed but never read** (removed from dataclasses, defaults, and schema):
- `todo.archive_on_reset` — `TodoManager` always archives unconditionally
- `todo.archive_path` — `TodoManager.archive()` hardcodes `archive/`
- `todo.max_items` — todo count enforcement uses `phase_settings.min_todos`/`max_todos`
- `phase_settings.archive_on_transition` — archiving is unconditional in `archive_phase`
- `description` field is now actively used by the orchestrator expert discovery API

**Aspirational sections** (removed from defaults and schema):
- `document_processing` (5 fields) — no code consumed these values
- `research.web_search_enabled`, `research.graph_search_enabled`, `research.research_depth` — tool availability is controlled by the `tools.research` array
- `validation` (5 fields) — validator relies on LLM reasoning, not config thresholds

**Still active via `extra`** (kept):
- `research.proxy.*` — consumed by browser tool's `ProxyConfig.from_config()`
- `browser.*` — consumed by browser tool's `_get_browser_config()`

## Fully Active (for reference)

All other config values are properly parsed and used at runtime:

- **Top-level**: `agent_id`, `display_name`, `description`
- **llm**: `model`, `provider`, `temperature`, `reasoning_level`, `base_url`, `api_key`, `timeout`, `max_retries`, `multimodal`, phase overrides (`strategic`, `tactical`, `summarization`)
- **workspace**: `structure`, `instructions_template`, `initial_files`, `max_read_words`, `git_versioning`, `git_ignore_patterns`
- **tools**: `workspace`, `core`, `document`, `research`, `citation`, `graph`, `git`
- **connections**: `postgres`, `neo4j`
- **limits**: `context_threshold_tokens`, `message_count_threshold`, `message_count_min_tokens`, `tool_retry_count`, `model_max_context_tokens`, `summarization_safe_limit`, `summarization_chunk_size`
- **context_management**: `compact_on_archive`, `keep_recent_tool_results`, `keep_recent_messages`, `summarization_template`, `reasoning_level`, `max_summary_length`
- **phase_settings**: `min_todos`, `max_todos`
- **research**: `proxy.*` (via extra)
- **browser**: `headless`, `timeout`, `use_vision` (via extra)
- **Expert UI**: `icon`, `color`, `tags` (via extra, consumed by orchestrator)
