-- migration:     0157_project_officers_post.sql
-- description:   The officer's durable post: one project_officers row per
--                project (thread_id NULL = the post is vacant), plus a
--                pure-SQL backfill that adopts each project's live officer
--                thread and folds already-retired officers into incarnation
--                history. docs/features/officer_post.md §3.
-- depends-on:    0156_unified_job_tool_names.sql
-- expected:      < 5s. One CREATE TABLE + three backfill statements; scans
--                projects once and threads twice (dev: hundreds of rows).
-- locks:         RowExclusive on project_officers during backfill; AccessShare
--                on projects/threads. No existing table is rewritten.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE public.project_officers (
    -- 1:1 by construction — the project IS the key. No surrogate id.
    project_id      UUID PRIMARY KEY
                        REFERENCES public.projects(id) ON DELETE CASCADE,
    -- Current incarnation; NULL = vacant. Deliberately NOT unique: the legion
    -- (centurion.md §9 — one Primus Pilus commanding several centuries)
    -- becomes "several rows share a thread_id" instead of a schema change.
    thread_id       UUID REFERENCES public.threads(id) ON DELETE SET NULL,
    -- The provision fragment, verbatim as the create funnel receives it today:
    -- {officer: {slots, sleep_*, max_*, daily_token_ceiling}, llm: {model,
    -- reasoning_level}, workspace: {backend}, interactive: {permission_mode}}.
    -- Runtime keys (officer.hold, officer.last_respawn_at) are stripped on
    -- write.
    config_override JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- User-owned routing policy for worker messages. Kept on the post so it
    -- exists while vacant and cannot be rewritten by the officer runtime.
    -- Existing/backfilled projects remain direct until explicitly changed.
    communication_policy JSONB NOT NULL DEFAULT
      '{"worker_messages":"user_direct","officer_response_minutes":15}'::jsonb,
    -- Harvested officer_state (digest ring, pages, sitrep fingerprints) from
    -- the last decommission, plus the while-vacant ledger. Empty while
    -- commissioned — the live copy is on the thread.
    state           JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Append-only: [{thread_id, commissioned_at, decommissioned_at, reason}].
    -- Old threads stay readable as ended sessions; this is the index into
    -- them.
    incarnations    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT project_officer_config_is_object
      CHECK (jsonb_typeof(config_override) = 'object'),
    CONSTRAINT project_officer_communication_is_object
      CHECK (jsonb_typeof(communication_policy) = 'object'),
    CONSTRAINT project_officer_state_is_object
      CHECK (jsonb_typeof(state) = 'object'),
    CONSTRAINT project_officer_incarnations_is_array
      CHECK (jsonb_typeof(incarnations) = 'array')
);

COMMENT ON TABLE public.project_officers IS
    'The officer''s durable post — one row per project, always present. '
    'thread_id IS NULL = vacant; commission links a thread, decommission '
    'harvests its state back onto the row (officer_post.md §2). The thread '
    'stays the runtime projection; this row is the durable record.';
COMMENT ON COLUMN public.project_officers.thread_id IS
    'Current incarnation (threads.id), NULL while the post is vacant. '
    'Deliberately not unique so a future legion can share one commander.';
COMMENT ON COLUMN public.project_officers.communication_policy IS
    'Legate-owned worker-message routing policy (officer_message_routing). '
    'Row-only: resolved server-side per message, never mirrored into thread '
    'metadata, not writable by the officer runtime.';
COMMENT ON COLUMN public.project_officers.state IS
    'Harvested metadata.officer_state from the last decommission (digest '
    'ring, page counters, sitrep fingerprints) plus the while-vacant ledger. '
    'Empty while commissioned — the live copy stays on the thread.';
COMMENT ON COLUMN public.project_officers.incarnations IS
    'Append-only history: [{thread_id, commissioned_at, decommissioned_at, '
    'reason}]. The index into old officer threads, which remain readable as '
    'ended sessions.';

CREATE TRIGGER update_project_officers_updated_at
BEFORE UPDATE ON public.project_officers
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();

-- ---------------------------------------------------------------------------
-- Backfill 1/3 — every existing project gets its post, vacant.
-- ---------------------------------------------------------------------------
INSERT INTO public.project_officers (project_id)
SELECT id FROM public.projects
ON CONFLICT (project_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Backfill 2/3 — adopt live officers. For each project's newest non-ended
-- thread with config_override.officer.enabled = 'true' (DISTINCT ON): link it
-- and harvest its config_override minus the runtime keys (#- on an absent
-- path is a no-op). The thread itself is untouched — an officer adopted
-- mid-hold stays held on the thread, and zero wakes fire here.
-- ---------------------------------------------------------------------------
WITH live AS (
    SELECT DISTINCT ON (t.project_id)
           t.project_id,
           t.id AS thread_id,
           ((CASE WHEN jsonb_typeof(t.metadata->'config_override') = 'object'
                  THEN t.metadata->'config_override'
                  ELSE '{}'::jsonb END)
              #- '{officer,hold}'
              #- '{officer,last_respawn_at}') AS harvested
      FROM public.threads t
     WHERE t.project_id IS NOT NULL
       AND t.status != 'ended'
       AND COALESCE(t.metadata->'config_override'->'officer'->>'enabled',
                    'false') = 'true'
     ORDER BY t.project_id, t.created_at DESC
)
UPDATE public.project_officers po
   SET thread_id       = live.thread_id,
       config_override = live.harvested,
       updated_at      = now()
  FROM live
 WHERE po.project_id = live.project_id;

-- ---------------------------------------------------------------------------
-- Backfill 3/3 — fold retired officers into history. For projects whose post
-- is still vacant after 2/3 and whose newest ENDED officer thread is
-- identifiable: harvest config + officer_state onto the row, append an
-- incarnation entry, leave thread_id NULL. This is what makes the provision
-- form seed from the last real kit instead of the hardcoded starter draft.
--
-- Identification: the retire path flips officer.enabled to false before
-- ending the thread, and the materialized session-class stamp writes
-- officer.{enabled,conference}=false on EVERY session — so "has an officer
-- key" would match every ended session. A real ex-officer is a non-conference
-- ended thread that either still carries enabled='true' (crash-ended) or was
-- ever served by the officer machinery (metadata.officer_state exists —
-- sitrep fingerprints/digest/pages are officer-only writes).
-- ---------------------------------------------------------------------------
WITH retired AS (
    SELECT DISTINCT ON (t.project_id)
           t.project_id,
           t.id AS thread_id,
           t.created_at,
           COALESCE(t.ended_at, t.last_activity, now()) AS decommissioned_at,
           ((CASE WHEN jsonb_typeof(t.metadata->'config_override') = 'object'
                  THEN t.metadata->'config_override'
                  ELSE '{}'::jsonb END)
              #- '{officer,hold}'
              #- '{officer,last_respawn_at}') AS harvested,
           (CASE WHEN jsonb_typeof(t.metadata->'officer_state') = 'object'
                 THEN t.metadata->'officer_state'
                 ELSE '{}'::jsonb END) AS officer_state
      FROM public.threads t
     WHERE t.project_id IS NOT NULL
       AND t.status = 'ended'
       AND COALESCE(t.metadata->'config_override'->'officer'->>'conference',
                    'false') != 'true'
       AND (
             COALESCE(t.metadata->'config_override'->'officer'->>'enabled',
                      'false') = 'true'
          OR jsonb_typeof(t.metadata->'officer_state') = 'object'
       )
     ORDER BY t.project_id, t.created_at DESC
)
UPDATE public.project_officers po
   SET config_override = retired.harvested,
       state           = po.state || retired.officer_state,
       incarnations    = po.incarnations || jsonb_build_array(
                             jsonb_build_object(
                                 'thread_id',         retired.thread_id,
                                 'commissioned_at',   retired.created_at,
                                 'decommissioned_at', retired.decommissioned_at,
                                 'reason',            'retired_before_post')),
       updated_at      = now()
  FROM retired
 WHERE po.project_id = retired.project_id
   AND po.thread_id IS NULL;

COMMIT;
