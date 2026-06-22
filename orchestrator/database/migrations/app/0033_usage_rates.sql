-- migration:     0033_usage_rates.sql
-- description:   Effective-dated rate table for the usage-metering ledger
--                (Slice 4 / observability_and_quotas "The rate table"). Maps a
--                (category, resource, unit) to a $/unit price; the ledger
--                snapshots the newest rate <= an event's ts onto usage_events at
--                write time, so history stays immutable. Ships EMPTY — same
--                inert-until-measured posture as litellm.backstop/ratePolicy/
--                quota: v1 records quantities (tokens, vcpu/gib-hours); cost_usd
--                stays NULL until an admin seeds real rates (homelab $/vcpu-hour
--                from pdu power, provider list prices, etc.).
-- depends-on:    0001_initial.sql
-- expected:      < 1s. One empty table.
-- locks:         AccessExclusiveLock on the new table only.
-- transactional: yes

SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '5min';

CREATE TABLE IF NOT EXISTS usage_rates (
    category        TEXT         NOT NULL,    -- 'llm' | 'compute' | 'query' | 'storage'
    -- A specific resource ('gemma-4-moe-strix', 'workspace_pod') or '*' = the
    -- category default (all compute shares one $/vcpu-hour). The ledger resolves
    -- the specific row first, then falls back to '*'.
    resource        TEXT         NOT NULL,
    unit            TEXT         NOT NULL,    -- 'prompt-token'|'vcpu-hour'|'gib-hour'|...
    rate_usd        NUMERIC      NOT NULL,    -- price per unit
    -- Effective-dated: the row with the greatest effective_from <= an event's ts
    -- prices it. Editing a rate inserts a NEW row, never mutates an old one, so a
    -- price change never rewrites already-snapshotted history.
    effective_from  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (category, resource, unit, effective_from)
);

COMMENT ON TABLE usage_rates IS
    'Effective-dated $/unit price config for the usage ledger (app DB; '
    'admin-editable). The orchestrator snapshots the newest rate <= an event ts '
    'onto usage_events.rate_usd/cost_usd at write time. Ships empty: quantities '
    'are metered immediately, dollars only once rates are seeded.';
COMMENT ON COLUMN usage_rates.resource IS
    'Specific resource id or ''*'' (category default). Resolver prefers the '
    'specific row, falls back to ''*''.';
