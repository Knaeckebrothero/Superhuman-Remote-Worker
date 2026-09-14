-- migration:     0248_retire_unkeyed_credential_provenance.sql
-- description:   Remove legacy unkeyed Helm fingerprints of credentials.
-- depends-on:    0247_stateless_none_workspace_outcome.sql
-- transactional: yes
-- expected:      < 1s; small credential catalog tables, row locks only.

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '1min';

-- Retire the guessing oracle even for unmanaged rows that the seed Job skips.
-- Preserve any keyed fingerprint a new seed Job has already written. Managed
-- rows with a cleared digest reconcile once on the next seed run. Credentials,
-- ownership and source timestamps are unchanged.
UPDATE system_api_keys
   SET helm_value_hash = NULL
 WHERE helm_value_hash NOT LIKE 'hmac-sha256:%';

UPDATE llm_endpoints
   SET helm_value_hash = NULL
 WHERE helm_value_hash NOT LIKE 'hmac-sha256:%';

COMMIT;
