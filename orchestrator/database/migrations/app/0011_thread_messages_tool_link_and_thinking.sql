-- migration:     0011_thread_messages_tool_link_and_thinking.sql
-- description:   Add tool_call_id + thinking columns to thread_messages so
--                historical sessions can reconstruct tool-result correlation
--                and reasoning content on reload.
--
--                Background: a logical assistant turn produces one row per
--                LangChain iteration. AIMessage rows carry the tool-call
--                invocation metadata in `tool_calls` JSONB. The corresponding
--                ToolMessage rows carry the result `content` but had no back-
--                reference to which call they fulfill, so the history-load
--                path (cockpit historyToTurns) dropped them entirely. The
--                new per-event turn-card UI made the empty tool-card body
--                visible; this column fixes the link.
--
--                Thinking content similarly had no column — `additional_kwargs.
--                reasoning_content` and Anthropic thinking blocks were
--                extracted into live SSE events but never persisted, so
--                historical AssistantTurns rendered with no ThoughtEvents.
--
--                Both fields are nullable; existing rows keep returning NULL
--                and the frontend treats them as missing (= same behavior as
--                today for pre-deploy sessions). Forward-only fix — no
--                backfill is possible because the original links are lost.
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. ADD COLUMN with NULL is metadata-only.
-- locks:         AccessExclusiveLock briefly on thread_messages for ADD
--                COLUMN. No row rewrite.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE thread_messages
    ADD COLUMN IF NOT EXISTS tool_call_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS thinking     TEXT;

COMMENT ON COLUMN thread_messages.tool_call_id IS
    'Set only on role=''tool'' rows. Mirrors LangChain ToolMessage.tool_call_id '
    'and points back to the matching tool_calls[].id on the AIMessage row that '
    'produced this result. NULL on ai/human/system rows.';
COMMENT ON COLUMN thread_messages.thinking IS
    'Set only on role=''ai'' rows for reasoning models. Captures '
    'additional_kwargs.reasoning_content (non-Anthropic) or the concatenated '
    'content of type=''thinking'' blocks (Anthropic). NULL when the model '
    'does not emit a visible reasoning channel.';

COMMIT;
