-- migration:     0209_expert_persona_identity_backfill.sql
-- description:   Materialise the current expert display_name into legacy
--                string-valued prompts.persona content wherever the exact
--                framework-owned {agent_display_name} token survives. Persona
--                content is a value in the prompt assembler, never a nested
--                template; the API write boundary and cleaned bundled sources
--                prevent new rows from carrying any assembler-owned token.
--                Exact reserved tokens in display_name are neutralised while
--                materialising. The recursive pass handles nested braces (for
--                example {{available_skills}}) without changing unrelated
--                brace-bearing prose such as {EU}.
--                jsonb_set changes only the persona member and the WHERE guard
--                leaves non-string personas, other prompt members, and rows
--                without the exact legacy token untouched. version,
--                updated_at and updated_by are deliberately unchanged: this is
--                storage repair rather than an authored edit. Data only, so
--                schema_current.sql is unchanged.
-- depends-on:    0028_experts.sql, 0208_threads_subagent_validate.sql
-- expected:      < 1s at current scale (tens of expert rows). One guarded pass
--                over public.experts. Idempotent: repaired rows no longer match.
-- locks:         ROW EXCLUSIVE on public.experts for the UPDATE plus row locks
--                on matched legacy rows only. No table rewrite.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

WITH RECURSIVE legacy_personas AS (
    SELECT id, display_name
      FROM public.experts
     WHERE jsonb_typeof(prompts -> 'persona') = 'string'
       AND strpos(prompts ->> 'persona', '{agent_display_name}') > 0
), neutralized_names AS (
    SELECT id, display_name::text AS value
      FROM legacy_personas
    UNION ALL
    SELECT id,
           replace(
               replace(
                   replace(
                       replace(
                           replace(
                               replace(value, '{phase_number}', 'phase_number'),
                               '{agent_display_name}',
                               'agent_display_name'
                           ),
                           '{expert_identity}',
                           'expert_identity'
                       ),
                       '{available_skills}',
                       'available_skills'
                   ),
                   '{subagent_environment}',
                   'subagent_environment'
               ),
               '{prompt_content}',
               'prompt_content'
           )
      FROM neutralized_names
     WHERE strpos(value, '{phase_number}') > 0
        OR strpos(value, '{agent_display_name}') > 0
        OR strpos(value, '{expert_identity}') > 0
        OR strpos(value, '{available_skills}') > 0
        OR strpos(value, '{subagent_environment}') > 0
        OR strpos(value, '{prompt_content}') > 0
), safe_names AS (
    SELECT id, value
      FROM neutralized_names
     WHERE strpos(value, '{phase_number}') = 0
       AND strpos(value, '{agent_display_name}') = 0
       AND strpos(value, '{expert_identity}') = 0
       AND strpos(value, '{available_skills}') = 0
       AND strpos(value, '{subagent_environment}') = 0
       AND strpos(value, '{prompt_content}') = 0
)
UPDATE public.experts AS experts
   SET prompts = jsonb_set(
       experts.prompts,
       '{persona}',
       to_jsonb(
           replace(
               experts.prompts ->> 'persona',
               '{agent_display_name}',
               safe_names.value
           )
       ),
       false
   )
  FROM safe_names
 WHERE experts.id = safe_names.id;

COMMIT;
