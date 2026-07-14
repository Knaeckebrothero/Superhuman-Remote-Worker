"""Contract checks for the packaged Canvas gateway PostgreSQL role."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = ROOT / "helm/files/canvas-viewer-role.sql"


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


def test_canvas_gateway_role_script_is_packaged_once_for_helm_and_compose() -> None:
    helm_job = (
        ROOT / "helm/templates/canvas-gateway/database-role-job.yaml"
    ).read_text()
    assert '.Files.Get "files/canvas-viewer-role.sql"' in helm_job

    for relative_path in ("docker-compose.yaml", "docker-compose.local.yaml"):
        compose = (ROOT / relative_path).read_text()
        assert compose.count("./helm/files/canvas-viewer-role.sql:") == 1
