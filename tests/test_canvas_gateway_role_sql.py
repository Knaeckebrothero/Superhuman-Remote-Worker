"""Contract checks for the packaged Canvas gateway PostgreSQL role."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = ROOT / "helm/files/canvas-viewer-role.sql"
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
    assert "NOSUPERUSER" in source
    assert "NOCREATEROLE" in source
    assert "NOBYPASSRLS" in source
    assert "pg_auth_members" in source
    assert "pg_shdepend" in source
    assert "has_schema_privilege(:'canvas_role', 'public', 'CREATE')" in source
    assert "REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I" in source
    assert "has_database_privilege(" in source
    assert "canvas_role_can_create_database" in source


def test_canvas_gateway_role_script_grants_only_the_gateway_write_surface() -> None:
    source = ROLE_SQL.read_text()
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
    assert '.Files.Get "files/canvas-viewer-role.sql"' in helm_job


def test_canvas_gateway_operator_script_keeps_secrets_out_of_argv_and_output() -> None:
    source = OPERATOR_SCRIPT.read_text()

    assert "set +x" in source
    assert "umask 077" in source
    assert "CANVAS_VIEWER_POSTGRES_PASSWORD_FILE" in source
    assert 'PGPASSWORD="$CANVAS_VIEWER_POSTGRES_PASSWORD"' in source
    assert "unset PGPASSWORD" in source
    assert '--from-file="$CANVAS_VIEWER_SECRET_PASSWORD_KEY=' in source
    assert "--from-literal" not in source
    assert "--dry-run=client -o yaml" in source
    assert "apply -f - >/dev/null" in source
    assert "export CANVAS_VIEWER_POSTGRES_PASSWORD" not in source
    assert "echo $CANVAS_VIEWER_POSTGRES_PASSWORD" not in source
    assert "printf '%s' \"$CANVAS_VIEWER_POSTGRES_PASSWORD\"" in source
