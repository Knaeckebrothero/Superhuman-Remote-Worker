-- migration:     0075_project_loop_officer_scheduling.sql
-- description:   scheduling='officer' for project loops (centurion.md §7):
--                the loop-advance path stops materializing the next job and
--                enqueues an officer wake instead — the century's centurion
--                decides what runs next from backlog + sitrep + charter.
--                Two CHECK changes: admit the new mode, and relax the budget
--                requirement for officer rows (an officer loop is naturally
--                unbounded — the officer and his token ceiling are the brake,
--                not an iteration count).
-- depends-on:    0074_session_wake_events.sql
-- transactional: YES.

ALTER TABLE project_loops DROP CONSTRAINT IF EXISTS project_loop_scheduling_known;
ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_scheduling_known
    CHECK (scheduling IN ('standard', 'campaign', 'officer'))
    NOT VALID;
ALTER TABLE project_loops
    VALIDATE CONSTRAINT project_loop_scheduling_known;

ALTER TABLE project_loops DROP CONSTRAINT IF EXISTS project_loop_has_budget;
ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_has_budget
    CHECK (
        max_iterations IS NOT NULL
        OR run_until IS NOT NULL
        OR scheduling = 'officer'
    )
    NOT VALID;
ALTER TABLE project_loops
    VALIDATE CONSTRAINT project_loop_has_budget;
