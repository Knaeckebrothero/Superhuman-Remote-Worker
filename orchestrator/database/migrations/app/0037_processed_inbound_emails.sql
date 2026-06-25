-- migration:     0037_processed_inbound_emails.sql
-- description:   IMAP inbound-email dedup claim table (HA / M1 leader election,
--                docs/superpowers/plans/2026-06-25-orchestrator-m1-leader-election.md
--                Task 5).
--
--                The imap poller is a singleton loop gated to the elected
--                leader, but leader election has no fencing: during a partition
--                or Postgres failover two replicas can briefly both hold
--                leadership and both poll IMAP. The pre-existing dedup was a
--                racy read (SELECT ... FROM message_log WHERE email_message_id),
--                so two pollers could each see an inbound reply as "new" and
--                inject it into the job twice.
--
--                This table is a purpose-built insert-as-claim guard: the poller
--                INSERTs the inbound Message-ID with ON CONFLICT DO NOTHING and
--                only routes the reply if it won the insert. A dedicated table
--                (rather than a UNIQUE index retrofit on the large, hot
--                message_log) keeps the message_log write path untouched and
--                needs no dedup-of-existing-rows. One row per processed inbound
--                email; prunable later (rows older than the longest a job can
--                wait for a reply are dead weight) — not swept in v1, matching
--                message_log itself.
-- depends-on:    0036_experts_prompts_v2_comment.sql
-- expected:      < 50ms. New empty table + PK index; no existing-data rewrite.
-- locks:         No existing-table locks. New standalone table, no FKs.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS processed_inbound_emails (
    email_message_id TEXT        PRIMARY KEY,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE processed_inbound_emails IS
    'Insert-as-claim dedup guard for the IMAP poller (HA / M1). The poller '
    'INSERTs an inbound RFC822 Message-ID here (ON CONFLICT DO NOTHING) and '
    'routes the reply only if it won the insert, so the transient dual-leader '
    'window cannot inject the same reply into a job twice. One row per '
    'processed inbound email; prunable.';

COMMIT;
