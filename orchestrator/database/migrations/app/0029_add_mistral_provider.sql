-- migration:     0029_add_mistral_provider.sql
-- description:   Make 'mistral' a first-class system LLM provider (native
--                Mistral AI API, routed OpenAI-compatible via ReasoningChatOpenAI
--                — see src/core/loader._create_mistral_llm). Widens the
--                valid-provider CHECK on user_api_keys, system_api_keys, and
--                project_api_keys to accept 'mistral'; until this lands an INSERT
--                of a mistral key row is rejected. Mirrors the constraint block
--                at the tail of 0001_initial.sql.
-- depends-on:    0001_initial.sql
-- expected:      < 1s. Three constraint swaps (DROP + ADD); the ADD validates
--                existing rows, none of which use 'mistral' yet.
-- locks:         Brief AccessExclusiveLock per ALTER TABLE for the constraint
--                re-add (validation is fast — small api-key tables).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE user_api_keys    DROP CONSTRAINT IF EXISTS valid_user_api_key_provider;
ALTER TABLE user_api_keys    ADD CONSTRAINT valid_user_api_key_provider
    CHECK (provider IN ('openai', 'anthropic', 'google', 'groq', 'openrouter', 'mistral', 'vision'));

ALTER TABLE system_api_keys  DROP CONSTRAINT IF EXISTS valid_system_api_key_provider;
ALTER TABLE system_api_keys  ADD CONSTRAINT valid_system_api_key_provider
    CHECK (provider IN ('openai', 'anthropic', 'google', 'groq', 'openrouter', 'mistral', 'vision'));

ALTER TABLE project_api_keys DROP CONSTRAINT IF EXISTS valid_project_api_key_provider;
ALTER TABLE project_api_keys ADD CONSTRAINT valid_project_api_key_provider
    CHECK (provider IN ('openai', 'anthropic', 'google', 'groq', 'openrouter', 'mistral', 'vision'));

COMMIT;
