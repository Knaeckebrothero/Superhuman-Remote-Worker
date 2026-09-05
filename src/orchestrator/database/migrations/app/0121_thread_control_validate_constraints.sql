-- migration:     0121_thread_control_validate_constraints.sql
-- description:   Backfill explicit legacy narration values, then validate the
--                narration vocabulary and composite control-receipt foreign
--                key added NOT VALID by 0119.
-- depends-on:    0119_thread_control_inbox.sql (the receipt index in 0120 is
--                independent and the runner applies .notx files last)
-- expected:      Proportional to threads + thread_events. Validation uses
--                SHARE UPDATE EXCLUSIVE locks, so normal reads and writes
--                remain available during both scans.
-- locks:         SHARE UPDATE EXCLUSIVE on threads and thread_events.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '10min';

-- Preserve values explicitly stored on the thread. Effective values inherited
-- only through expert/account resolution intentionally remain NULL. Doing the
-- row update here, after 0119 committed, avoids holding 0119's ACCESS EXCLUSIVE
-- catalog lock for the duration of this scan.
UPDATE threads
SET narration_mode = metadata #>> '{config_override,interactive,narration_mode}'
WHERE metadata #>> '{config_override,interactive,narration_mode}'
      IN ('silent', 'verbose', 'auto')
  AND narration_mode IS NULL;

ALTER TABLE threads
    VALIDATE CONSTRAINT valid_narration_mode;

ALTER TABLE thread_events
    VALIDATE CONSTRAINT thread_events_control_request_thread_fkey;

COMMIT;
