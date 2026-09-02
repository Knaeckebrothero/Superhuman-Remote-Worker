"""Contract checks for the packaged Canvas gateway PostgreSQL role."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = ROOT / "helm/files/canvas-viewer-role.sql"
SAFETY_SQL = ROOT / "helm/files/canvas-viewer-role-safety.sql"
SELF_CONFIGURE_SQL = ROOT / "helm/files/canvas-viewer-self-configure.sql"
GRANTS_SQL = ROOT / "helm/files/canvas-viewer-grants.sql"
OPERATOR_SCRIPT = ROOT / "scripts/provision-canvas-gateway-database.sh"


def test_canvas_gateway_role_script_is_secret_safe_and_fail_closed() -> None:
    source = ROLE_SQL.read_text()

    assert "\\getenv canvas_role CANVAS_VIEWER_POSTGRES_USER" in source
    assert "\\getenv canvas_password CANVAS_VIEWER_POSTGRES_PASSWORD" in source
    assert "\\set ECHO none" in source
    assert "length(:'canvas_password')" not in source
    assert "PASSWORD %L" not in source
    assert 'CREATE ROLE :"canvas_role"' in source
    assert 'ALTER ROLE :"canvas_role"' in source
    assert "PASSWORD :'canvas_password'" in source
    assert "\\echo" not in source
    assert "NOINHERIT" in source
    # Naming these protected negative attributes still requires the issuer to
    # hold them. Safe PostgreSQL defaults and the preflight enforce the values.
    assert "NOSUPERUSER" not in source
    assert "NOCREATEDB" not in source
    assert "NOCREATEROLE" not in source
    assert "NOREPLICATION" not in source
    assert "NOBYPASSRLS" not in source
    assert "pg_auth_members" in source
    assert "pg_shdepend" in source


def test_canvas_gateway_owner_preflight_fails_closed() -> None:
    source = SAFETY_SQL.read_text()

    assert "rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication" in source
    assert "pg_auth_members" in source
    assert "pg_shdepend" in source
    assert "caller_owns_database" in source
    assert "caller_owns_required_relations" in source
    assert "has_schema_privilege(:'canvas_role', 'public', 'CREATE')" in source
    assert "has_database_privilege(" in source
    assert "canvas_role_inherits_create_database" in source
    assert "aclexplode" in source
    assert "GRANT CONNECT ON DATABASE" in source


def test_canvas_gateway_target_configures_only_its_own_search_path() -> None:
    source = SELF_CONFIGURE_SQL.read_text()

    assert "session_user = current_user" in source
    assert "ALTER ROLE CURRENT_USER IN DATABASE" in source
    assert "search_path = pg_catalog, public, pg_temp" in source
    assert "PASSWORD" not in source


def test_canvas_gateway_role_script_grants_only_the_gateway_write_surface() -> None:
    source = GRANTS_SQL.read_text()
    grant_lines = "\n".join(
        line for line in source.splitlines() if line.lstrip().startswith("'GRANT")
    )

    assert "public.users" in source
    assert "public.threads" in source
    assert "public.srw_sessions" in source
    assert "public.canvases" in source
    assert "public.canvas_view_attachments" in source
    assert "public.canvas_view_bootstraps" in source
    assert "public.canvas_origin_sessions" in source
    assert "INSERT (id, session_secret_hash" in source
    assert "UPDATE (origin_session_id, last_seen_at)" in source
    assert "UPDATE (challenge_hash, browser_binding_hash, ready_receipt_hash" in source
    assert "UPDATE (expires_at, last_renewed_at, revoked_at" in source
    assert "GRANT DELETE" not in grant_lines
    assert "GRANT TRUNCATE" not in grant_lines
    assert "GRANT CREATE" not in grant_lines
    assert "GRANT ALL" not in grant_lines
    assert "user_api_keys" not in source
    assert "system_api_keys" not in source


def test_canvas_gateway_role_script_is_packaged_for_helm() -> None:
    helm_job = (
        ROOT / "helm/templates/canvas-gateway/database-role-job.yaml"
    ).read_text()
    for filename in (
        "canvas-viewer-role.sql",
        "canvas-viewer-role-safety.sql",
        "canvas-viewer-self-configure.sql",
        "canvas-viewer-grants.sql",
    ):
        assert f'.Files.Get "files/{filename}"' in helm_job
        assert f"  {filename}: |" in helm_job
        assert f"/etc/srw-canvas-db/{filename}" in helm_job
    assert "helm.sh/hook" not in helm_job
    assert "ttlSecondsAfterFinished:" not in helm_job
    assert ".Release.Revision" in helm_job


def test_canvas_gateway_operator_script_keeps_secrets_out_of_argv_and_output() -> None:
    source = OPERATOR_SCRIPT.read_text()

    assert "set +x" in source
    assert "umask 077" in source
    assert "CANVAS_VIEWER_POSTGRES_PASSWORD_FILE" in source
    assert 'PGPASSWORD="$CANVAS_VIEWER_POSTGRES_PASSWORD"' in source
    assert "unset PGPASSWORD" in source
    assert '--from-file="password=$TMP_DIR/password"' in source
    assert '--from-file="username=$TMP_DIR/username"' in source
    assert "--type=kubernetes.io/basic-auth" in source
    assert "cnpg.io/reload=true" in source
    assert "--from-literal" not in source
    assert "--dry-run=client -o yaml" in source
    assert "apply -f - >/dev/null" in source
    assert "export CANVAS_VIEWER_POSTGRES_PASSWORD" not in source
    assert "echo $CANVAS_VIEWER_POSTGRES_PASSWORD" not in source
    assert "printf '%s' \"$CANVAS_VIEWER_POSTGRES_PASSWORD\"" in source
