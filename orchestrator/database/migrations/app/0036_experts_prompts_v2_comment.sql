-- migration:     0036_experts_prompts_v2_comment.sql
-- description:   Document the v2 keys of experts.prompts. DB-backed and forked
--                experts now carry strategic/tactical/summarization in addition to
--                persona+instructions (Part 2 — full DB-expert prompt parity). The
--                column is open JSONB with no CHECK, so storing the new keys needs
--                NO structural change — this migration only refreshes the stale
--                table comment so the schema docs stay honest. The inline CREATE
--                TABLE comment in 0028 ("v1 keys: persona, instructions") is frozen
--                and cannot be amended; COMMENT ON TABLE is the live record.
-- depends-on:    0028_experts.sql
-- expected:      < 1s on dev DB. Catalog-only (COMMENT ON), no table lock/rewrite.
-- transactional: yes

BEGIN;

COMMENT ON TABLE experts IS
    'DB-backed user/admin experts (overlay over bundled config/experts/). '
    'config = fragment vs the expert_type base; prompts = {persona, instructions, '
    'strategic, tactical, summarization} (Part 2 — one family-agnostic version per '
    'segment; model adaptation stays in the systemprompt_<family> wrapper). '
    'Design: docs/features/global_expert_management.md.';

COMMIT;
