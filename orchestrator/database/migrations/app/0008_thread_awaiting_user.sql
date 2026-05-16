-- migration:     0008_thread_awaiting_user.sql
-- description:   Add 'awaiting_user' and 'suspended' to threads.status; add
--                threads.awaiting_user_since + extend_count for the
--                attention-sleep watchdog and magic-link extend window.
--
--                Phase 5 of headless persistent sessions
--                (docs/features/headless_persistent_sessions.md). State
--                machine:
--                  active ──→ awaiting_user ──→ suspended
--                       ▲          │
--                       └──────────┘  (reattach or magic-link wake)
--
--                The agent writes 'awaiting_user' + awaiting_user_since
--                when a turn completes at natural pause AND no client is
--                attached. The attention_sleep_sweeper in orchestrator/main.py
--                finds rows older than the configured TTL and calls
--                workspace_suspension_service.suspend_thread_workspace(),
--                then UPDATEs status='suspended'. The magic-link
--                "extend window" POST bumps awaiting_user_since forward,
--                up to extend_count = 4 (4× 60-min = 4h hard ceiling).
-- depends-on:    0007_thread_pk_identity.sql
-- expected:      < 1s on dev DB. DROP/ADD CHECK is metadata-only on PG 11+
--                when the new constraint is a superset of the old one
--                (existing rows already satisfy it); ADD COLUMN with
--                NULL/DEFAULT is metadata-only.
-- locks:         AccessExclusiveLock briefly on threads for DROP/ADD
--                CONSTRAINT and ADD COLUMN. No row rewrite.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Replace the status CHECK to include awaiting_user + suspended.
-- The old constraint name from schema.sql is valid_thread_status, but
-- 0002_collapse_thread_status.sql may have renamed it. Drop by both names
-- to be safe (IF EXISTS makes either path a no-op).
ALTER TABLE threads DROP CONSTRAINT IF EXISTS valid_thread_status;
ALTER TABLE threads DROP CONSTRAINT IF EXISTS threads_status_check;

ALTER TABLE threads
    ADD CONSTRAINT valid_thread_status CHECK (
        status IN ('created', 'active', 'idle', 'awaiting_user', 'suspended', 'ended')
    );

-- When the agent flipped the thread to awaiting_user. The watchdog uses
-- this as the timer reference; "extend window" POST bumps it forward.
ALTER TABLE threads
    ADD COLUMN IF NOT EXISTS awaiting_user_since TIMESTAMPTZ;

-- How many times the magic-link extend-window button has been clicked.
-- Caps the total awaiting_user duration at 4× the per-extend window
-- (4 × 60min = 4h ceiling) so a malicious or buggy client cannot keep
-- the agent pod alive indefinitely by hammering /magic/extend.
ALTER TABLE threads
    ADD COLUMN IF NOT EXISTS extend_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN threads.awaiting_user_since IS
    'Set by the agent when it reaches a natural pause untethered. The '
    'attention_sleep_sweeper suspends the workspace when this exceeds '
    'headless_attention_sleep_minutes. Cleared on reattach.';
COMMENT ON COLUMN threads.extend_count IS
    'Number of magic-link extend-window clicks since this awaiting_user '
    'session began. Capped at 4 (= 4h ceiling at default 60min/extend).';

-- Hot path: watchdog scans for stale awaiting_user rows every 60s.
-- Predicate-narrowed index keeps the scan cheap regardless of thread
-- table size.
CREATE INDEX IF NOT EXISTS idx_threads_awaiting_user_since
    ON threads (awaiting_user_since)
    WHERE status = 'awaiting_user';

COMMIT;
