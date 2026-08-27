-- migration:     0156_unified_job_tool_names.sql
-- description:   Hard-rename stored agent/session job tools and split the old
--                tools.orchestrator list into descriptor-owned job_control,
--                job_inspection, and the remaining non-job orchestrator list.
--                Also disambiguates the critic's report-bearing verdict tool
--                as approve_job_verdict so lifecycle approve_job has one schema.
--                Runtime/config aliases are intentionally not introduced.
-- depends-on:    0155_job_wake_orphan_convergence.sql
-- expected:      Short JSONB updates; no production connection is made by the
--                repository change itself. Idempotent on already-migrated rows.
-- locks:         Row locks on matching config-bearing rows only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE FUNCTION pg_temp.migrate_job_tool_config(config JSONB)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    tools JSONB;
    source_name TEXT;
    source_value JSONB;
    item JSONB;
    raw_name TEXT;
    mapped_name TEXT;
    orchestrator_names TEXT[] := ARRAY[]::TEXT[];
    control_names TEXT[] := ARRAY[]::TEXT[];
    inspection_names TEXT[] := ARRAY[]::TEXT[];
    evaluation_names TEXT[] := ARRAY[]::TEXT[];
    control_inventory CONSTANT TEXT[] := ARRAY[
        'approve_job', 'assign_job', 'cancel_job', 'create_job', 'delete_job',
        'pause_job', 'promote_job', 'resume_job_with_feedback',
        'send_message_to_job', 'steer_job'
    ];
    inspection_inventory CONSTANT TEXT[] := ARRAY[
        'get_audit_bulk', 'get_audit_timerange', 'get_audit_trail',
        'get_chat_bulk', 'get_chat_history', 'get_current_todos',
        'get_frozen_job', 'get_job', 'get_job_diff', 'get_job_file',
        'get_job_log', 'get_job_progress', 'get_job_summary',
        'get_llm_request', 'get_message_thread', 'get_shell_state',
        'get_stuck_jobs', 'get_todo_archive', 'get_todos',
        'get_workspace_overview', 'list_job_commits', 'list_job_files',
        'list_jobs', 'list_llm_requests', 'list_message_threads',
        'list_todo_archives', 'search_audit'
    ];
    had_orchestrator BOOLEAN;
    had_control BOOLEAN;
    had_inspection BOOLEAN;
    had_evaluation BOOLEAN;
BEGIN
    -- ``<>`` is not enough here: a missing ``tools`` member makes
    -- jsonb_typeof() SQL NULL, which would fall through the IF and eventually
    -- feed SQL NULL to jsonb_set().  Several valid stored expert fragments do
    -- not declare tools at all, so treat missing/null/non-object values as a
    -- no-op explicitly.
    IF jsonb_typeof(config) IS DISTINCT FROM 'object'
       OR jsonb_typeof(config->'tools') IS DISTINCT FROM 'object' THEN
        RETURN config;
    END IF;

    tools := config->'tools';
    had_orchestrator := tools ? 'orchestrator';
    had_control := tools ? 'job_control';
    had_inspection := tools ? 'job_inspection';
    had_evaluation := tools ? 'evaluation';
    IF NOT (had_orchestrator OR had_control OR had_inspection OR had_evaluation) THEN
        RETURN config;
    END IF;

    -- Stored request/config layers are canonical list policies by the time
    -- they reach PostgreSQL. Refuse to reinterpret an unexpected raw policy
    -- object or malformed member; leaving it untouched is safer than dropping
    -- a grant during rollout.
    FOREACH source_name IN ARRAY ARRAY[
        'orchestrator', 'job_control', 'job_inspection', 'evaluation'
    ]
    LOOP
        source_value := tools->source_name;
        IF source_value IS NULL THEN
            CONTINUE;
        END IF;
        IF jsonb_typeof(source_value) <> 'array' THEN
            RETURN config;
        END IF;
        FOR item IN SELECT value FROM jsonb_array_elements(source_value)
        LOOP
            IF jsonb_typeof(item) <> 'string' THEN
                RETURN config;
            END IF;
        END LOOP;
    END LOOP;

    FOREACH source_name IN ARRAY ARRAY[
        'orchestrator', 'job_control', 'job_inspection', 'evaluation'
    ]
    LOOP
        source_value := tools->source_name;
        IF source_value IS NULL THEN
            CONTINUE;
        END IF;
        FOR item IN SELECT value FROM jsonb_array_elements(source_value)
        LOOP
            raw_name := item #>> '{}';
            mapped_name := CASE
                WHEN source_name = 'evaluation' AND raw_name = 'approve_job'
                    THEN 'approve_job_verdict'
                ELSE CASE raw_name
                WHEN 'create_worker_job' THEN 'create_job'
                WHEN 'cancel_worker_job' THEN 'cancel_job'
                WHEN 'pause_worker_job' THEN 'pause_job'
                WHEN 'resume_worker_job' THEN 'resume_job_with_feedback'
                WHEN 'approve_worker_job' THEN 'approve_job'
                WHEN 'steer_worker_job' THEN 'steer_job'
                WHEN 'list_worker_jobs' THEN 'list_jobs'
                WHEN 'get_worker_job' THEN 'get_job'
                WHEN 'get_job_workspace_file' THEN 'get_job_file'
                WHEN 'list_job_workspace_files' THEN 'list_job_files'
                ELSE raw_name
                END
            END;

            IF source_name = 'evaluation' THEN
                IF NOT (mapped_name = ANY(evaluation_names)) THEN
                    evaluation_names := array_append(evaluation_names, mapped_name);
                END IF;
            ELSIF mapped_name = ANY(control_inventory) THEN
                IF NOT (mapped_name = ANY(control_names)) THEN
                    control_names := array_append(control_names, mapped_name);
                END IF;
            ELSIF mapped_name = ANY(inspection_inventory) THEN
                IF NOT (mapped_name = ANY(inspection_names)) THEN
                    inspection_names := array_append(inspection_names, mapped_name);
                END IF;
            ELSIF source_name = 'orchestrator' THEN
                IF NOT (mapped_name = ANY(orchestrator_names)) THEN
                    orchestrator_names := array_append(orchestrator_names, mapped_name);
                END IF;
            ELSIF source_name = 'job_control' THEN
                IF NOT (mapped_name = ANY(control_names)) THEN
                    control_names := array_append(control_names, mapped_name);
                END IF;
            ELSE
                IF NOT (mapped_name = ANY(inspection_names)) THEN
                    inspection_names := array_append(inspection_names, mapped_name);
                END IF;
            END IF;
        END LOOP;
    END LOOP;

    IF had_orchestrator THEN
        tools := jsonb_set(tools, '{orchestrator}', to_jsonb(orchestrator_names));
    END IF;
    IF had_orchestrator OR had_control OR cardinality(control_names) > 0 THEN
        tools := jsonb_set(tools, '{job_control}', to_jsonb(control_names));
    END IF;
    IF had_orchestrator OR had_inspection OR cardinality(inspection_names) > 0 THEN
        tools := jsonb_set(tools, '{job_inspection}', to_jsonb(inspection_names));
    END IF;
    IF had_evaluation THEN
        tools := jsonb_set(tools, '{evaluation}', to_jsonb(evaluation_names));
    END IF;
    RETURN jsonb_set(config, '{tools}', tools);
END;
$$;

CREATE FUNCTION pg_temp.migrate_resolved_job_tool_config(config JSONB)
RETURNS JSONB
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN jsonb_typeof(config->'agent') = 'object'
        THEN jsonb_set(
            config,
            '{agent}',
            pg_temp.migrate_job_tool_config(config->'agent')
        )
        ELSE config
    END
$$;

UPDATE experts
SET config = pg_temp.migrate_job_tool_config(config)
WHERE config IS DISTINCT FROM pg_temp.migrate_job_tool_config(config);

UPDATE project_experts
SET config_override = pg_temp.migrate_job_tool_config(config_override)
WHERE config_override IS DISTINCT FROM pg_temp.migrate_job_tool_config(config_override);

UPDATE projects
SET default_config_override = pg_temp.migrate_job_tool_config(default_config_override)
WHERE default_config_override IS DISTINCT FROM
      pg_temp.migrate_job_tool_config(default_config_override);

UPDATE automations
SET config_override = pg_temp.migrate_job_tool_config(config_override)
WHERE config_override IS DISTINCT FROM pg_temp.migrate_job_tool_config(config_override);

UPDATE config_overrides
SET value_json = pg_temp.migrate_job_tool_config(value_json)
WHERE value_json IS DISTINCT FROM pg_temp.migrate_job_tool_config(value_json);

UPDATE threads
SET metadata = jsonb_set(
    metadata,
    '{config_override}',
    pg_temp.migrate_job_tool_config(metadata->'config_override')
)
WHERE metadata->'config_override' IS DISTINCT FROM
      pg_temp.migrate_job_tool_config(metadata->'config_override');

-- A paused/resumable job hydrates its frozen resolved_config instead of fresh
-- YAML. Migrating that embedded agent config is required for the hard rename;
-- prompts, instructions, timestamps, and every non-tool field remain intact.
UPDATE jobs
SET config_override = pg_temp.migrate_job_tool_config(config_override),
    resolved_config = pg_temp.migrate_resolved_job_tool_config(resolved_config)
WHERE config_override IS DISTINCT FROM pg_temp.migrate_job_tool_config(config_override)
   OR resolved_config IS DISTINCT FROM
      pg_temp.migrate_resolved_job_tool_config(resolved_config);

COMMIT;
