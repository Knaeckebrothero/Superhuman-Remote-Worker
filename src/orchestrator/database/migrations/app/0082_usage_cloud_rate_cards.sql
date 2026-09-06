-- migration:     0082_usage_cloud_rate_cards.sql
-- description:   Effective-dated public-cloud comparison rates for repricing
--                already-metered workspace CPU and RAM. These are deliberately
--                separate from usage_rates: they are planning estimates, not
--                canonical charges snapshotted onto usage_events.
-- depends-on:    0033_usage_rates.sql
-- expected:      < 1s. Two small tables and three seed cards.
-- locks:         AccessExclusiveLock on new tables only.
-- transactional: yes

SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '5min';

CREATE TABLE IF NOT EXISTS usage_rate_cards (
    id                  TEXT        PRIMARY KEY,
    provider            TEXT        NOT NULL,
    display_name        TEXT        NOT NULL,
    region              TEXT        NOT NULL,
    currency            TEXT        NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    -- sum: independent meters (Fargate / ACI); max: dominant share of one
    -- bundled reference instance (STACKIT VM flavor).
    aggregation         TEXT        NOT NULL CHECK (aggregation IN ('sum', 'max')),
    source_url          TEXT        NOT NULL,
    source_label        TEXT        NOT NULL,
    description         TEXT        NOT NULL DEFAULT '',
    exclusions          TEXT        NOT NULL DEFAULT '',
    enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    sort_order          INTEGER     NOT NULL DEFAULT 100,
    source_checked_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_rate_card_rates (
    rate_card_id                 TEXT        NOT NULL
        REFERENCES usage_rate_cards(id) ON DELETE CASCADE,
    category                     TEXT        NOT NULL,
    resource                     TEXT        NOT NULL DEFAULT '*',
    unit                         TEXT        NOT NULL,
    rate                         NUMERIC     NOT NULL CHECK (rate >= 0),
    -- How many ledger units one priced billing unit contains. Linear cards use
    -- 1; the STACKIT g2i.4 node-share card uses 4 vCPU and 16 GiB.
    capacity_per_billing_unit    NUMERIC     NOT NULL DEFAULT 1
        CHECK (capacity_per_billing_unit > 0),
    effective_from               TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_sku                    TEXT,
    source_metadata               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at                    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_card_id, category, resource, unit, effective_from)
);

CREATE INDEX IF NOT EXISTS usage_rate_card_rates_lookup_idx
    ON usage_rate_card_rates
    (rate_card_id, category, resource, unit, effective_from DESC);

COMMENT ON TABLE usage_rate_cards IS
    'Public-cloud list-price comparison cards. Estimates only: never provider '
    'invoice data and never canonical usage_events cost.';
COMMENT ON COLUMN usage_rate_cards.aggregation IS
    'sum = add independently billed components; max = dominant-share estimate '
    'for a bundled reference instance.';
COMMENT ON COLUMN usage_rate_card_rates.capacity_per_billing_unit IS
    'Ledger quantity represented by one unit charged at rate. Enables bundled '
    'instance share pricing without arbitrarily splitting CPU and RAM cost.';

INSERT INTO usage_rate_cards
    (id, provider, display_name, region, currency, aggregation, source_url,
     source_label, description, exclusions, sort_order, source_checked_at)
VALUES
    (
        'stackit-ske-g2i4-eu01', 'stackit', 'STACKIT SKE · g2i.4 share',
        'EU01 (Germany South)', 'EUR', 'max',
        'https://stackit.com/en/asset/download/37788/file/STACKIT_price_list.pdf?version=26',
        'STACKIT price list v1.0.43 (2026-08-04)',
        'Dominant requested share of a 4 vCPU / 16 GiB g2i.4 worker node.',
        'Excludes the SKE control plane, disks, load balancers, public IPs, egress, tax, and discounts.',
        10, '2026-08-04T00:00:00Z'
    ),
    (
        'aws-fargate-euc1', 'aws', 'AWS ECS Fargate · Linux/x86',
        'eu-central-1 (Frankfurt)', 'USD', 'sum',
        'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/eu-central-1/index.json',
        'AWS public Amazon ECS regional price list',
        'Requested Linux/x86 Fargate vCPU and memory duration.',
        'Excludes EKS, storage above the included allowance, logs, IPs, egress, tax, discounts, and minimum-duration rounding.',
        20, '2026-08-05T00:00:00Z'
    ),
    (
        'azure-aci-dewc', 'azure', 'Azure Container Instances · Standard',
        'germanywestcentral', 'USD', 'sum',
        'https://prices.azure.com/api/retail/prices',
        'Azure Retail Prices API',
        'Requested Standard Linux container-group vCPU and memory duration.',
        'Excludes AKS, storage, networking, egress, tax, discounts, and provider rounding.',
        30, '2026-08-05T00:00:00Z'
    )
ON CONFLICT (id) DO NOTHING;

-- STACKIT g2i.4: EUR 0.20458503352 per node-hour, 4 vCPU / 16 GiB.
INSERT INTO usage_rate_card_rates
    (rate_card_id, category, resource, unit, rate,
     capacity_per_billing_unit, effective_from, source_sku)
VALUES
    ('stackit-ske-g2i4-eu01', 'compute', '*', 'vcpu-hour',
     0.20458503352, 4, '2026-08-04T00:00:00Z',
     'General Purpose Server-g2i.4-EU01'),
    ('stackit-ske-g2i4-eu01', 'compute', '*', 'gib-hour',
     0.20458503352, 16, '2026-08-04T00:00:00Z',
     'General Purpose Server-g2i.4-EU01'),
    ('aws-fargate-euc1', 'compute', '*', 'vcpu-hour',
     0.04656, 1, '2026-07-07T00:00:00Z',
     'EUC1-Fargate-vCPU-Hours:perCPU'),
    ('aws-fargate-euc1', 'compute', '*', 'gib-hour',
     0.00511, 1, '2026-07-07T00:00:00Z',
     'EUC1-Fargate-GB-Hours'),
    ('azure-aci-dewc', 'compute', '*', 'vcpu-hour',
     0.04656, 1, '2020-10-01T00:00:00Z',
     'Standard vCPU Duration'),
    ('azure-aci-dewc', 'compute', '*', 'gib-hour',
     0.00511, 1, '2019-09-01T00:00:00Z',
     'Standard Memory Duration')
ON CONFLICT (rate_card_id, category, resource, unit, effective_from) DO NOTHING;
