-- migration:     0211_image_delivery_rows_event_role.sql
-- description:   Disarm phantom queued inputs: reclassify the synthetic
--                image-delivery rows ("Image content from tool call <id>:")
--                from role='human' to role='event'.
--                These rows are written mid-turn by the session loop to hand a
--                tool's screenshot/page image to a multimodal model
--                (src/services/image_content.py make_multimodal_user_message).
--                They travel as HumanMessage because that is the one type every
--                provider accepts anywhere in a conversation — they are not user
--                turns. Persisted as 'human' they satisfied the stateless
--                run-queue's oldest-unanswered predicate
--                (role='human' AND seq > consumed_seq, _PENDING_INPUT_SQL in
--                src/api/turn_executor.py), so each one became an unanswered
--                input that the next fresh attach claimed as pending[0] ahead of
--                the user's real message — reopening an already-used
--                turn_number, re-answering the previous message, and delaying
--                the real one by a full turn.
--                The write site now stamps the 'event' persist role; this repairs
--                rows written before that. 'event' is a transcript/queue
--                distinction only — _db_rows_to_lc_messages restores 'event' rows
--                as HumanMessages, so no session's model context changes.
--                The anchored regex plus the tool_calls/tool_call_id guards keep
--                this to the exact synthetic marker; a user bubble that merely
--                mentions the phrase does not match. Data only, so
--                schema_current.sql is unchanged.
-- depends-on:    0001_initial.sql, 0210_thread_terminal_reclaim_projection.sql
-- expected:      < 1s at current scale (129 of ~36k rows on dev). One guarded
--                sequential pass over public.thread_messages. Idempotent:
--                repaired rows no longer match role='human'.
-- locks:         ROW EXCLUSIVE on public.thread_messages for the UPDATE plus row
--                locks on matched rows only. No table rewrite, no index change.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

UPDATE public.thread_messages AS message
   SET role = 'event'
 WHERE message.role = 'human'
   AND message.tool_calls IS NULL
   AND message.tool_call_id IS NULL
   AND message.content ~ '^Image content from tool call [^[:space:]]+:[[:space:]]*$'
   -- A synthetic image row is never an accepted input and so never has a
   -- delivery row. Guard it anyway: 'event' rows WITH a live stateless
   -- delivery are first-class queued inputs in _PENDING_INPUT_SQL, so an
   -- unguarded flip could convert a phantom into a real one rather than
   -- retiring it.
   AND NOT EXISTS (
       SELECT 1
         FROM public.thread_input_deliveries AS delivery
        WHERE delivery.message_id = message.id
          AND delivery.thread_id  = message.thread_id
   );

COMMIT;
