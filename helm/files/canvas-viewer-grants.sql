-- Reconcile only database/schema/relation ACLs as the application object
-- owner. Role identity and password belong to CNPG's DatabaseRole on CNPG.
\ir canvas-viewer-role-safety.sql

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT pg_advisory_xact_lock(
    hashtextextended('srw:canvas-gateway-role', 0)
) AS canvas_role_lock
\gset

-- Remove direct privileges from an earlier/experimental role contract before
-- installing the exact allowlist. Column ACLs are independent from table ACLs
-- and therefore need their own generated REVOKE statements.
SELECT format(
    'REVOKE %s (%s) ON TABLE %I.%I FROM %I',
    privilege_type,
    string_agg(format('%I', column_name), ', ' ORDER BY ordinal_position),
    table_schema,
    table_name,
    :'canvas_role'
)
FROM information_schema.column_privileges privileges
JOIN information_schema.columns columns
  USING (table_catalog, table_schema, table_name, column_name)
WHERE grantee = :'canvas_role'
GROUP BY table_schema, table_name, privilege_type
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %I',
    table_schema, table_name, :'canvas_role'
)
FROM information_schema.table_privileges
WHERE grantee = :'canvas_role'
GROUP BY table_schema, table_name
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM %I',
    sequence_schema, sequence_name, :'canvas_role'
)
FROM information_schema.sequences
WHERE has_sequence_privilege(
    :'canvas_role', format('%I.%I', sequence_schema, sequence_name), 'USAGE'
) OR has_sequence_privilege(
    :'canvas_role', format('%I.%I', sequence_schema, sequence_name), 'SELECT'
) OR has_sequence_privilege(
    :'canvas_role', format('%I.%I', sequence_schema, sequence_name), 'UPDATE'
)
\gexec
-- Clear direct database grants before restoring only CONNECT. TEMP inherited
-- from PostgreSQL's PUBLIC role remains outside the application-schema
-- contract, but pg_temp is forced last in every gateway connection.
SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I',
    current_database(), :'canvas_role'
)
\gexec
-- The runtime contract covers only the application schema. Do not issue
-- REVOKE against pg_catalog/information_schema merely because PUBLIC gives
-- every login effective USAGE there; the application owner does not own those
-- system schemas.
SELECT format('REVOKE ALL PRIVILEGES ON SCHEMA public FROM %I', :'canvas_role')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'canvas_role')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'canvas_role')
\gexec

SELECT format(
    'GRANT SELECT (id, is_admin, is_approved) ON TABLE public.users TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, user_id, metadata) ON TABLE public.threads TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, user_id, absolute_expires_at, revoked_at) '
    'ON TABLE public.srw_sessions TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (thread_id, canvas_id, source, title, renderer, editable, '
    'alt_text, presentation_revision, source_fingerprint, source_version, '
    'origin_generation, created_at, updated_at) '
    'ON TABLE public.canvases TO %I',
    :'canvas_role'
)
\gexec

SELECT format(
    'GRANT SELECT (id, user_id, thread_id, canvas_id, parent_srw_session_id, '
    'embedding_origin, cookie_mode, expires_at, closed_at), '
    'UPDATE (origin_session_id, last_seen_at) '
    'ON TABLE public.canvas_view_attachments TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, attachment_id, expected_presentation_revision, '
    'source_fingerprint, workspace_generation, origin_generation, expires_at, '
    'challenge_hash, browser_binding_hash, ready_receipt_hash, '
    'exchange_token_hash, authorized_at, consumed_at), '
    'UPDATE (challenge_hash, browser_binding_hash, ready_receipt_hash, '
    'consumed_at, consumed_origin_session_id) '
    'ON TABLE public.canvas_view_bootstraps TO %I',
    :'canvas_role'
)
\gexec
SELECT format(
    'GRANT SELECT (id, session_secret_hash, user_id, thread_id, canvas_id, '
    'parent_srw_session_id, source_fingerprint, workspace_generation, '
    'origin_generation, embedding_origin, cookie_mode, expires_at, revoked_at), '
    'INSERT (id, session_secret_hash, user_id, thread_id, canvas_id, '
    'parent_srw_session_id, issued_presentation_revision, source_fingerprint, '
    'workspace_generation, origin_generation, embedding_origin, cookie_mode, '
    'expires_at), UPDATE (expires_at, last_renewed_at, revoked_at, '
    'revocation_reason, updated_at) '
    'ON TABLE public.canvas_origin_sessions TO %I',
    :'canvas_role'
)
\gexec

COMMIT;
