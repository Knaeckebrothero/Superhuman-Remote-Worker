-- Attach -> first shell command, per Job Bench member (the "ceremony floor").
--
--   psql "$AUDIT_DB_URL" -v job_ids='uuid-1,uuid-2' \
--     -f bench/queries/first_command_latency.sql
--
-- started_at is the job's first audit row; first_command_at is the first tool
-- pre-row whose payload names the shell tool (either shell mode); first_edit_at
-- the first write_file/edit_file. audit_rows_before_first_command counts every
-- row before that moment -- the size of the ceremony, not just its duration.
-- A NULL first_command_at means the job never ran a command.

\if :{?job_ids}
\else
\echo 'Pass -v job_ids=uuid-1,uuid-2'
\quit
\endif

WITH selected_jobs(job_id) AS (
    SELECT unnest(string_to_array(:'job_ids', ',')::uuid[])
),
first_seen AS (
    SELECT a.job_id, min(a."timestamp") AS started_at
    FROM agent_audit a
    JOIN selected_jobs s USING (job_id)
    GROUP BY a.job_id
),
first_command AS (
    SELECT a.job_id, min(a."timestamp") AS first_command_at
    FROM agent_audit a
    JOIN selected_jobs s USING (job_id)
    WHERE a.step_type = 'tool'
      AND a.event_phase = 'pre'
      AND a.payload->'tool'->>'name' IN ('run_command', 'shell_execute')
    GROUP BY a.job_id
),
first_edit AS (
    SELECT a.job_id, min(a."timestamp") AS first_edit_at
    FROM agent_audit a
    JOIN selected_jobs s USING (job_id)
    WHERE a.step_type = 'tool'
      AND a.event_phase = 'pre'
      AND a.payload->'tool'->>'name' IN ('write_file', 'edit_file')
    GROUP BY a.job_id
)
SELECT
    s.job_id,
    f.started_at,
    c.first_command_at,
    e.first_edit_at,
    round(extract(epoch FROM (c.first_command_at - f.started_at)) / 60.0, 1)
        AS minutes_to_first_command,
    round(extract(epoch FROM (e.first_edit_at - f.started_at)) / 60.0, 1)
        AS minutes_to_first_edit,
    (SELECT count(*) FROM agent_audit a
      WHERE a.job_id = s.job_id
        AND a."timestamp" < coalesce(c.first_command_at, 'infinity'::timestamptz))
        AS audit_rows_before_first_command
FROM selected_jobs s
JOIN first_seen f USING (job_id)
LEFT JOIN first_command c USING (job_id)
LEFT JOIN first_edit e USING (job_id)
ORDER BY s.job_id;
