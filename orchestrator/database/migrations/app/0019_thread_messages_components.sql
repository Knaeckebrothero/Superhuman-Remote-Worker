-- migration:     0019_thread_messages_components.sql
-- description:   Add provider-agnostic component columns to thread_messages so a
--                turn can be stored as normalized components (reasoning, text,
--                tool_calls, tool_results) PLUS the verbatim provider-raw payload
--                needed for faithful same-provider replay (forward-compat with
--                the OpenAI Responses API / Anthropic; the live gpt-5.5 path is
--                Chat Completions where normalized replay suffices). Supersedes
--                the lossy `thinking` column (kept for legacy reads). See
--                docs/features/persistent_session_source_of_truth.md (D2, Q3).
--
--                All columns nullable -> metadata-only, no row rewrite, no
--                backfill (dev-cutover; old rows return NULL and degrade
--                gracefully, same as 0011).
-- depends-on:    0011_thread_messages_tool_link_and_thinking.sql
-- expected:      < 1s on dev DB. ADD COLUMN with NULL is metadata-only.
-- locks:         AccessExclusiveLock briefly on thread_messages for ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE thread_messages
    ADD COLUMN IF NOT EXISTS reasoning         JSONB,
    ADD COLUMN IF NOT EXISTS tool_results      JSONB,
    ADD COLUMN IF NOT EXISTS provider          TEXT,
    ADD COLUMN IF NOT EXISTS provider_raw      JSONB,
    ADD COLUMN IF NOT EXISTS additional_kwargs JSONB,
    ADD COLUMN IF NOT EXISTS response_metadata JSONB;

COMMENT ON COLUMN thread_messages.reasoning IS
    'Normalized reasoning items (role=ai). Written going forward; supersedes the '
    'legacy `thinking` TEXT column (kept for historical reads).';
COMMENT ON COLUMN thread_messages.provider_raw IS
    'Verbatim provider response payload for audit + faithful same-provider replay '
    '(forward-compat with Responses API / Anthropic). Captured from the COMPLETED '
    '(non-streamed) response.';
COMMENT ON COLUMN thread_messages.provider IS
    'Provider tag for the row, e.g. openai-chat | openai-responses | anthropic. '
    'Selects the replay path in src/llm/session_components.py.';

COMMIT;
