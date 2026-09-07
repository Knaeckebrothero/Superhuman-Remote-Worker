-- migration:     0035_project_loops.sql
-- description:   Create the project_loops table — the control row for the
--                project self-improvement loop
--                (docs/features/project_self_improvement_loop.md).
--
--                One row = one continuous, budget-bounded loop attached to a
--                project. The orchestrator advances it ONE job at a time: when
--                the loop's current job reaches a terminal state, the
--                `_advance_project_loop` completion hook decrements the budget,
--                rotates to the next role in `role_sequence`, and spawns the
--                next job — until `max_iterations` / `run_until` / the
--                consecutive-failure cap stops it. Agents coordinate through the
--                project knowledge base (a blackboard); this row carries only
--                loop *control* state, never content.
--
--                Two-way link to spawned jobs: `jobs.context->>'loop_id'` (set
--                at spawn time) plus `project_loops.current_job_id` (the
--                in-flight job — the forward pointer the advance hook keys on
--                for idempotency).
--
--                A partial unique index enforces at most ONE active
--                (running|paused) loop per project in v1. A CHECK enforces that
--                at least one stop axis (max_iterations OR run_until) is set, so
--                a loop can never be unbounded on both — the hard floor under
--                runaway, in the spirit of automations' max_fires_per_day and
--                the November 2025 $47K multi-agent runaway postmortem.
-- depends-on:    0034_workspace_intervals.sql
-- expected:      < 100ms. New table + indexes; no existing-data rewrite.
-- locks:         No existing-table locks. New table; the FK additions to
--                projects / users / jobs take a brief ShareRowExclusive on
--                those tables for validation, covered by lock_timeout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS project_loops (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id               UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id                 UUID REFERENCES users(id) ON DELETE SET NULL,

    status                   TEXT NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'paused', 'stopped',
                                               'completed', 'failed')),

    -- Steering. `goal` is snapshotted from projects.goal at start so editing
    -- the project later doesn't silently move the loop's target.
    -- `acceptance_criteria` is the Definition of Done the Critic verifies
    -- against (research: LLM self-evaluation of progress is near-random, so
    -- "done" must anchor to external criteria, never the agent's confidence).
    -- `user_prompt` is optional extra steering folded into every kickoff.
    goal                     TEXT,
    acceptance_criteria      TEXT,
    user_prompt              TEXT,

    -- Injected into every spawned job as config_override.llm.model.
    model                    TEXT,

    -- The role rotation. Each spawned job uses role_sequence[seq_index];
    -- seq_index advances modulo length on every spawn. Default mirrors the
    -- README innovation cycle (scholar proposes, critic selects, developer
    -- executes). `developer` is swappable for any expert (e.g. `default` /
    -- a future `writer`) to make the loop domain-agnostic.
    role_sequence            JSONB NOT NULL
                             DEFAULT '["scholar", "critic", "developer"]'::jsonb,
    seq_index                INTEGER NOT NULL DEFAULT 0,

    -- Budget / stop conditions. At least one of max_iterations / run_until
    -- must be set (CHECK below). `remaining_iterations` is decremented on each
    -- advance; `consecutive_failures` trips the failure cap (reset on success).
    max_iterations           INTEGER,
    remaining_iterations     INTEGER,
    run_until                TIMESTAMPTZ,
    max_consecutive_failures INTEGER NOT NULL DEFAULT 3,

    -- Live state.
    current_job_id           UUID REFERENCES jobs(id) ON DELETE SET NULL,
    total_jobs_run           INTEGER NOT NULL DEFAULT 0,
    consecutive_failures     INTEGER NOT NULL DEFAULT 0,
    last_error               TEXT,
    stop_reason              TEXT,  -- budget | deadline | failures | goal_met | user

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT project_loop_has_budget
        CHECK (max_iterations IS NOT NULL OR run_until IS NOT NULL),
    CONSTRAINT project_loop_role_sequence_nonempty
        CHECK (jsonb_typeof(role_sequence) = 'array'
               AND jsonb_array_length(role_sequence) >= 1)
);

-- At most one active loop per project in v1 (running or paused). Terminal rows
-- (stopped/completed/failed) accumulate as history and are excluded here, so a
-- new loop can start once the previous one ends.
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_loops_one_active
    ON project_loops (project_id)
    WHERE status IN ('running', 'paused');

-- Cockpit list + owner views.
CREATE INDEX IF NOT EXISTS idx_project_loops_project
    ON project_loops (project_id);

CREATE INDEX IF NOT EXISTS idx_project_loops_owner
    ON project_loops (owner_id)
    WHERE owner_id IS NOT NULL;

COMMENT ON TABLE project_loops IS
    'Control row for the project self-improvement loop. One active row per '
    'project advances one job at a time via the _advance_project_loop '
    'completion hook, bounded by max_iterations / run_until / '
    'max_consecutive_failures. Design: '
    'docs/features/project_self_improvement_loop.md.';

COMMENT ON COLUMN project_loops.acceptance_criteria IS
    'Definition of Done the Critic verifies against. Research: LLM '
    'self-evaluation of progress is near-random, so "done" must anchor to '
    'external criteria, never the agent''s own confidence.';

COMMENT ON COLUMN project_loops.role_sequence IS
    'Ordered JSONB array of expert config names rotated one per spawned job '
    '(e.g. ["scholar","critic","developer"]); seq_index points at the next.';

COMMENT ON COLUMN project_loops.stop_reason IS
    'Why the loop ended: budget (iterations exhausted), deadline (run_until '
    'passed), failures (consecutive-failure cap), goal_met (Critic decided), '
    'or user (manual stop).';

COMMIT;
