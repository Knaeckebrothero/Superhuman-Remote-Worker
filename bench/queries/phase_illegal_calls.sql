-- U2 WP5 audit checks for one or more Job Bench members.
--
-- Run against the audit database, passing a comma-separated UUID list:
--   psql "$AUDIT_DB_URL" -v job_ids='uuid-1,uuid-2' \
--     -f bench/queries/phase_illegal_calls.sql
--
-- The first result must report zero unsafe calls. A mismatched attempt is safe
-- only when the runtime phase gate recorded the expected failed ToolMessage.
-- The second result describes persistent phase-block delivery. Skills-mode
-- jobs should have zero zero_block_requests and normally advance 1->2->3...
-- by one block per phase. Compaction may remove superseded blocks; legacy-mode
-- jobs intentionally report only zero blocks.

\if :{?job_ids}
\else
\echo 'Pass -v job_ids=uuid-1,uuid-2'
\quit
\endif

WITH selected_jobs(job_id) AS (
    SELECT unnest(string_to_array(:'job_ids', ',')::uuid[])
),
single_phase(tool_name, allowed_phase) AS (
    VALUES
        ('approve_job_verdict', 'strategic'),
        ('job_complete', 'strategic'),
        ('next_phase_todos', 'strategic'),
        ('return_job_with_feedback', 'strategic'),
        ('browser_back', 'tactical'),
        ('browser_click', 'tactical'),
        ('browser_close', 'tactical'),
        ('browser_navigate', 'tactical'),
        ('browser_screenshot', 'tactical'),
        ('browser_scroll', 'tactical'),
        ('browser_select', 'tactical'),
        ('browser_snapshot', 'tactical'),
        ('browser_type', 'tactical'),
        ('crawl_website', 'tactical'),
        ('cypher_execute', 'tactical'),
        ('cypher_query', 'tactical'),
        ('download_paper', 'tactical'),
        ('email_draft', 'tactical'),
        ('email_flag', 'tactical'),
        ('email_move', 'tactical'),
        ('email_send', 'tactical'),
        ('extract_webpage', 'tactical'),
        ('get_database_schema', 'tactical'),
        ('get_paper_info', 'tactical'),
        ('map_website', 'tactical'),
        ('mongo_aggregate', 'tactical'),
        ('mongo_insert', 'tactical'),
        ('mongo_query', 'tactical'),
        ('mongo_schema', 'tactical'),
        ('mongo_update', 'tactical'),
        ('request_replan', 'tactical'),
        ('research_topic', 'tactical'),
        ('search_papers', 'tactical'),
        ('sql_execute', 'tactical'),
        ('sql_query', 'tactical'),
        ('sql_schema', 'tactical'),
        ('web_search', 'tactical'),
        ('webdav_delete', 'tactical'),
        ('webdav_write', 'tactical')
),
phase_mismatches AS (
    SELECT
        pre.job_id,
        pre.phase,
        pre.phase_number,
        one.tool_name,
        one.allowed_phase,
        post.payload AS result_payload
    FROM agent_audit AS pre
    JOIN selected_jobs USING (job_id)
    JOIN single_phase AS one
      ON one.tool_name = pre.payload->'tool'->>'name'
    LEFT JOIN LATERAL (
        SELECT payload
        FROM agent_audit
        WHERE pre_id = pre.id AND event_phase = 'post'
        ORDER BY id DESC
        LIMIT 1
    ) AS post ON true
    WHERE pre.event_phase = 'pre'
      AND pre.step_type = 'tool'
      AND pre.phase IS DISTINCT FROM one.allowed_phase
),
classified AS (
    SELECT *,
        coalesce(result_payload->'tool'->>'success', '') = 'false'
        AND coalesce(result_payload->'tool'->>'error', '') LIKE format(
            'Error: %L is a %s-phase tool;%%',
            tool_name,
            allowed_phase
        ) AS gate_rejected
    FROM phase_mismatches
)
SELECT
    CASE WHEN GROUPING(job_id) = 1 THEN 'ALL' ELSE job_id::text END AS job_id,
    phase,
    phase_number,
    tool_name,
    count(*) AS mismatched_attempts,
    count(*) FILTER (WHERE gate_rejected) AS gate_rejections,
    count(*) FILTER (WHERE NOT gate_rejected) AS unsafe_or_unclassified
FROM classified
GROUP BY GROUPING SETS ((job_id, phase, phase_number, tool_name), ())
ORDER BY job_id, phase_number, tool_name;

WITH selected_jobs(job_id) AS (
    SELECT unnest(string_to_array(:'job_ids', ',')::uuid[])
),
request_blocks AS (
    SELECT
        request.id,
        request.job_id,
        count(*) FILTER (
            WHERE message->>'content' LIKE '[phase: %'
        ) AS phase_blocks
    FROM llm_requests AS request
    JOIN selected_jobs USING (job_id)
    CROSS JOIN LATERAL jsonb_array_elements(request.request->'messages') AS message
    WHERE request.call_type = 'main'
    GROUP BY request.id, request.job_id
),
block_changes AS (
    SELECT *,
        phase_blocks - lag(phase_blocks) OVER (
            PARTITION BY job_id ORDER BY id
        ) AS delta
    FROM request_blocks
)
SELECT
    job_id,
    count(*) AS requests,
    count(*) FILTER (WHERE phase_blocks = 0) AS zero_block_requests,
    min(phase_blocks) AS min_blocks,
    max(phase_blocks) AS max_blocks,
    count(*) FILTER (WHERE delta > 1) AS multi_block_additions,
    count(*) FILTER (WHERE delta < 0) AS compaction_drops,
    string_agg(phase_blocks::text, '->' ORDER BY id)
        FILTER (WHERE delta IS NULL OR delta <> 0) AS block_count_changes
FROM block_changes
GROUP BY job_id
ORDER BY job_id;
