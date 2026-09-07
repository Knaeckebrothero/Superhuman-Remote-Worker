-- R4/WP1 design spike only. NOT a production migration or a complete store.
-- Installed exclusively in the testcontainers database owned by its test.
-- Proves atomic revision/message receipts and a fresh-transaction claim gate.
-- Capability registration, effect recovery, retention and deletion are future
-- implementation gates documented in universal_react_runtime_wp1_contract.md.
CREATE SCHEMA wp1_spike;

CREATE TABLE wp1_spike.contracts (
    job_id uuid PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    engine_id text NOT NULL,
    state_version integer NOT NULL CHECK (state_version > 0)
);

CREATE FUNCTION wp1_spike.immutable_contract() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'WP1 execution contract is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER wp1_immutable_contract BEFORE UPDATE ON wp1_spike.contracts
FOR EACH ROW EXECUTE FUNCTION wp1_spike.immutable_contract();

CREATE TABLE wp1_spike.state (
    job_id uuid PRIMARY KEY REFERENCES wp1_spike.contracts(job_id) ON DELETE CASCADE,
    revision bigint NOT NULL DEFAULT 0,
    next_seq bigint NOT NULL DEFAULT 1,
    envelope jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE wp1_spike.messages (
    job_id uuid NOT NULL REFERENCES wp1_spike.state(job_id) ON DELETE CASCADE,
    message_id text NOT NULL,
    seq bigint NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (job_id, message_id),
    UNIQUE (job_id, seq)
);
CREATE TABLE wp1_spike.commits (
    job_id uuid NOT NULL REFERENCES wp1_spike.state(job_id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    request_hash text NOT NULL,
    revision bigint NOT NULL,
    PRIMARY KEY (job_id, operation_id),
    UNIQUE (job_id, revision)
);
CREATE TABLE wp1_spike.claim_receipts (
    job_id uuid NOT NULL REFERENCES wp1_spike.contracts(job_id) ON DELETE CASCADE,
    lease_token bigint NOT NULL,
    pod_name text NOT NULL,
    transaction_id bigint NOT NULL,
    PRIMARY KEY (job_id, lease_token)
);

-- A stale receipt for an earlier token or transaction cannot authorize an old
-- binary's unmodified UPDATE. Heartbeats of the exact live claim stay legal.
-- This spike proves the lease transition only; it is not the full rollout gate.
CREATE FUNCTION wp1_spike.require_claim_receipt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.unit_kind <> 'worker_batch' OR NEW.state <> 'leased'
       OR NOT EXISTS (SELECT 1 FROM wp1_spike.contracts WHERE job_id = NEW.unit_id)
    THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.state = 'leased' AND OLD.lease_token = NEW.lease_token
           AND OLD.leased_by IS NOT DISTINCT FROM NEW.leased_by THEN
            RETURN NEW;
        END IF;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM wp1_spike.claim_receipts
        WHERE job_id = NEW.unit_id AND lease_token = NEW.lease_token
          AND pod_name = NEW.leased_by AND transaction_id = txid_current()
    ) THEN
        RAISE EXCEPTION 'WP1 fresh compatible claim receipt required'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER wp1_claim_receipt BEFORE INSERT OR UPDATE ON run_queue
FOR EACH ROW EXECUTE FUNCTION wp1_spike.require_claim_receipt();
