-- migration:     0039_drop_per_phase_account_model_defaults.sql
-- description:   Strip the per-user per-phase model defaults
--                (settings.default_strategic_model / default_tactical_model)
--                from users.settings.
--
--                These account-level keys were written SILENTLY by the New Job /
--                New Session model picker (a per-job control that PATCHed a
--                GLOBAL preference on every change) and injected as
--                llm.strategic / llm.tactical phase pins at dispatch. A phase pin
--                beats the top-level model in LLMConfig.get_phase_config, so they
--                shadowed an explicit per-loop/per-job model choice (e.g. a loop
--                pinned to gpt-5.5 ran entirely on gpt-5.3-codex-spark) while
--                being invisible and unmanageable in the UI. The dispatch
--                injection and the UserSettingsUpdate fields were removed in
--                code; this strips the now-dead keys for hygiene. The single
--                top-level default_model preference is kept.
--
--                See docs/issues/loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md
--                (Layer 1).
--
--                Idempotent: the jsonb `-` operator is a no-op for absent keys,
--                and the WHERE clause touches only rows that still carry them.

UPDATE users
SET settings = settings - 'default_strategic_model' - 'default_tactical_model'
WHERE settings ? 'default_strategic_model'
   OR settings ? 'default_tactical_model';
