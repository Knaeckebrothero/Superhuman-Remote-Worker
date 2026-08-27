-- migration:     0192_notifications.sql
-- description:   Durable per-recipient notification feed plus a per-channel
--                delivery ledger — the source of truth every channel delivers
--                FROM (knowledge-base/knowledge/features/unified_notification_system.md
--                §3-4, decisions D2/D3/D10). Slice 1 of three: new tables only.
-- depends-on:    0191_managed_repository_process_zero_authority.sql
-- expected:      < 5s. Two new empty tables; no existing table is touched.
-- locks:         none on existing tables.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- The feed row. ``id`` is minted by the orchestrator as
-- uuid5(NAMESPACE_URL, 'srw-notification-v1:{recipient_kind}:{recipient_id}:{dedup_key}')
-- so a replayed record() call lands on the same primary key and the insert is
-- idempotent (ON CONFLICT (id) DO NOTHING); the unique index below states the
-- same invariant in relational terms and guards any writer that mints its own id.
CREATE TABLE public.notifications (
    id             UUID PRIMARY KEY,
    recipient_kind TEXT NOT NULL,
    recipient_id   UUID NOT NULL,
    category       TEXT NOT NULL,
    severity       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    -- What the notification is ABOUT. source_id is text because message
    -- threads are short hex tokens and loops are 'loop-xxxxxx', not uuids.
    source_kind    TEXT,
    source_id      TEXT,
    dedup_key      TEXT NOT NULL,
    -- Server-declared action set: [{type, label_key, style, input, input_name, params}].
    actions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Category-specific presentation data (job_id, config_name, freeze_type, ...).
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Engagement (Knock semantics): seen = rendered in the recipient's feed,
    -- read = explicitly marked, interacted = an action was taken on it.
    seen_at        TIMESTAMPTZ,
    read_at        TIMESTAMPTZ,
    interacted_at  TIMESTAMPTZ,
    -- Resolution of the underlying SOURCE, by anyone (an officer approving the
    -- job resolves the human's row too). 'user:<uuid>' | 'system:<hook>' | 'officer:<thread>'.
    resolved_at    TIMESTAMPTZ,
    resolved_by    TEXT,
    archived_at    TIMESTAMPTZ,
    CONSTRAINT notifications_recipient_kind
        CHECK (recipient_kind IN ('user', 'officer')),
    CONSTRAINT notifications_severity
        CHECK (severity IN ('low', 'normal', 'high', 'critical')),
    CONSTRAINT notifications_source_ref
        CHECK ((source_kind IS NULL) = (source_id IS NULL)),
    CONSTRAINT notifications_actions_shape
        CHECK (jsonb_typeof(actions) = 'array'),
    CONSTRAINT notifications_payload_shape
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT notifications_read_implies_seen
        CHECK (read_at IS NULL OR seen_at IS NOT NULL),
    CONSTRAINT notifications_dedup_key
        CHECK (dedup_key <> '' AND length(dedup_key) <= 512)
);

CREATE UNIQUE INDEX uq_notifications_recipient_dedup
    ON public.notifications (recipient_kind, recipient_id, dedup_key);
-- Keyset feed scan: ORDER BY created_at DESC, id DESC per recipient.
CREATE INDEX ix_notifications_feed
    ON public.notifications (recipient_kind, recipient_id, created_at DESC, id DESC);
-- The bell badge.
CREATE INDEX ix_notifications_unseen
    ON public.notifications (recipient_kind, recipient_id)
    WHERE seen_at IS NULL AND archived_at IS NULL;
-- resolve_source(kind, id): stamp every open row about one source.
CREATE INDEX ix_notifications_open_by_source
    ON public.notifications (source_kind, source_id)
    WHERE resolved_at IS NULL;

COMMENT ON TABLE public.notifications IS
    'Per-recipient notification feed: the source of truth every channel delivers '
    'FROM. Engagement state (seen/read/interacted), resolution of the underlying '
    'source, and the server-declared action set live here. Idempotent on '
    '(recipient_kind, recipient_id, dedup_key); the id is derived from that triple.';
COMMENT ON COLUMN public.notifications.dedup_key IS
    'Caller-supplied idempotency key, e.g. freeze_notification:<completion command id>. '
    'A replayed record() with the same key returns the existing row and sends nothing twice.';
COMMENT ON COLUMN public.notifications.resolved_by IS
    'Who settled the underlying source: user:<uuid> | system:<hook name> | officer:<thread id>.';

-- One row per channel attempt. The claim row is inserted with state=pending
-- BEFORE the send (headless_notifications.claim_sent_notification shape), so a
-- dual-leader replay or a journal-replayed completion effect cannot send twice:
-- the partial unique index admits one live pending/sent row per
-- (notification, channel), and only a failed row frees the slot for attempt+1.
CREATE TABLE public.notification_deliveries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    notification_id   UUID NOT NULL
        REFERENCES public.notifications(id) ON DELETE CASCADE,
    channel           TEXT NOT NULL,
    state             TEXT NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    -- Slice 2: which notification_steps row produced this attempt, and the
    -- batch (digest) send it rode in. NULL for in_app and immediate sends.
    step_index        INTEGER,
    batch_id          UUID,
    -- Audit: the address/topic at send time, and the provider's message id
    -- (SMTP Message-ID) for reply routing and read-state correlation.
    recipient_address TEXT,
    provider_msg_id   TEXT,
    attempted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at        TIMESTAMPTZ,
    error             TEXT,
    CONSTRAINT notification_delivery_channel
        CHECK (channel IN ('in_app', 'email', 'ntfy', 'slack_webhook', 'discord_webhook', 'push')),
    CONSTRAINT notification_delivery_state
        CHECK (state IN ('pending', 'sent', 'failed', 'suppressed')),
    CONSTRAINT notification_delivery_attempt
        CHECK (attempt > 0),
    CONSTRAINT notification_delivery_settled
        CHECK ((state = 'pending') = (settled_at IS NULL))
);

CREATE UNIQUE INDEX uq_notification_delivery_claim
    ON public.notification_deliveries (notification_id, channel)
    WHERE state IN ('pending', 'sent');
CREATE INDEX ix_notification_deliveries_notification
    ON public.notification_deliveries (notification_id);
CREATE INDEX ix_notification_deliveries_provider_msg
    ON public.notification_deliveries (provider_msg_id)
    WHERE provider_msg_id IS NOT NULL;
CREATE INDEX ix_notification_deliveries_batch
    ON public.notification_deliveries (batch_id)
    WHERE batch_id IS NOT NULL;

COMMENT ON TABLE public.notification_deliveries IS
    'One row per channel attempt for a notification. Answers "did the mail actually '
    'go out, and when" and carries the provider message id for reply routing. The '
    'claim row is inserted BEFORE the send so replays cannot double-send.';

COMMIT;
