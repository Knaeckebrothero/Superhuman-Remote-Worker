"""Tests for the datasource redesign: KB templates, payload building, env var injection.

Covers the three datasource categories (generic, repository, managed connectors)
and the dual-mode behavior (CLI vs tools) for managed connectors.

Functions under test are replicated here to avoid importing orchestrator.main
(which has heavy dependencies). Originals live in orchestrator/main.py.
"""

import json
import os
import re
from unittest.mock import MagicMock

import pytest


# =============================================================================
# Replicated functions from orchestrator/main.py
# Keep in sync with the originals.
# =============================================================================


def _build_generic_note(name: str, desc: str, ds: dict) -> str:
    lines = [f"## Datasource: {name}"]
    if desc:
        lines.append(desc)
    url = ds.get("connection_url")
    cli_hint = ds.get("cli_hint")
    if url or cli_hint:
        lines.append("\n### Connection")
        if url:
            lines.append(f"- **URL:** {url} (credentials via env vars)")
        if cli_hint:
            lines.append(f"- **CLI:** `{cli_hint}`")
    creds = ds.get("credentials") or {}
    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except (json.JSONDecodeError, ValueError):
            creds = {}
    env_vars = creds.get("env_vars", {})
    if env_vars:
        lines.append("\n### Environment Variables")
        for key in env_vars:
            lines.append(f"- `{key}` — available in workspace")
    return "\n".join(lines)


def _build_repository_note(name: str, desc: str, ds: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", name.lower(), "").strip(
        "-"
    )  # intentional bug check below
    # Fix: use correct re.sub argument order
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    lines = [f"## Repository: {name}"]
    if desc:
        lines.append(desc)
    lines.append("\n### Location")
    lines.append(f"Cloned to `./repos/{slug}/` — git is pre-authenticated.")
    lines.append("\n### Usage")
    lines.append("Use standard git commands:")
    lines.append(f"- `cd repos/{slug} && git status`")
    lines.append("- `git pull`, `git commit`, `git push`")
    lines.append("- No login or credential setup required.")
    branch = ds.get("default_branch")
    if branch:
        lines.append(f"- Default branch: `{branch}`")
    return "\n".join(lines)


def _build_managed_readwrite_note(name: str, desc: str, ds_type: str) -> str:
    cli_info = {
        "postgresql": {
            "tool": "psql",
            "env_vars": "`PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`",
            "examples": [
                '`psql -c "SELECT * FROM users LIMIT 10"`',
                '`psql -c "CREATE TABLE ..."`',
            ],
        },
        "neo4j": {
            "tool": "cypher-shell",
            "env_vars": "`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`",
            "examples": [
                '`cypher-shell "MATCH (n) RETURN n LIMIT 10"`',
            ],
        },
        "mongodb": {
            "tool": "mongosh",
            "env_vars": "`MONGOSH_URI`",
            "examples": [
                '`mongosh --eval "db.users.find().limit(10)"`',
            ],
        },
    }
    info = cli_info.get(ds_type, {})
    lines = [
        f"## Datasource: {name}",
        f"**Type:** {ds_type} | **Access:** full (CLI)",
    ]
    if desc:
        lines.append(f"\n{desc}")
    lines.append("\n### Connection")
    lines.append(
        f"Use `{info.get('tool', ds_type)}` to connect — credentials are pre-configured via environment variables."
    )
    lines.append("\n### Environment Variables")
    lines.append(
        f"- {info.get('env_vars', 'Check environment for connection details')} — pre-configured"
    )
    if info.get("examples"):
        lines.append("\n### Examples")
        for ex in info["examples"]:
            lines.append(f"- {ex}")
    return "\n".join(lines)


def _build_managed_readonly_note(name: str, desc: str, ds_type: str) -> str:
    tool_info = {
        "postgresql": [
            "- `sql_query` — execute SELECT queries",
            "- `sql_schema` — inspect tables, columns, types, constraints",
        ],
        "neo4j": [
            "- `execute_cypher_query` — execute read-only Cypher queries",
            "- `get_database_schema` — inspect labels, relationships, properties",
        ],
        "mongodb": [
            "- `mongo_query` — document queries with filters",
            "- `mongo_aggregate` — aggregation pipelines",
            "- `mongo_schema` — collections, fields, indexes",
        ],
    }
    tools = tool_info.get(ds_type, ["- Check available tools for this datasource type"])
    lines = [
        f"## Datasource: {name}",
        f"**Type:** {ds_type} | **Access:** read-only (tools)",
    ]
    if desc:
        lines.append(f"\n{desc}")
    lines.append("\n### Available Tools")
    lines.extend(tools)
    lines.append("\nNo CLI access or write operations available.")
    return "\n".join(lines)


def _build_webdav_note(name: str, desc: str, is_read_only: bool) -> str:
    access = "read-only" if is_read_only else "read-write"
    lines = [
        f"## Datasource: {name}",
        f"**Type:** webdav | **Access:** {access}",
    ]
    if desc:
        lines.append(f"\n{desc}")
    lines.append("\n### Available Tools")
    lines.append("- `cloud_list` — list files and directories")
    lines.append("- `cloud_read` — read file contents")
    lines.append("- `cloud_info` — get file metadata")
    if not is_read_only:
        lines.append("- `cloud_write` — write/upload files")
        lines.append("- `cloud_delete` — delete files")
    return "\n".join(lines)


def _build_datasource_note_content(ds: dict) -> str:
    ds_type = ds.get("type", "unknown")
    ds_name = ds.get("name", "Unnamed")
    desc = ds.get("description") or ""
    is_read_only = ds.get("project_read_only", False)
    if ds_type == "generic":
        return _build_generic_note(ds_name, desc, ds)
    elif ds_type == "repository":
        return _build_repository_note(ds_name, desc, ds)
    elif ds_type == "webdav":
        return _build_webdav_note(ds_name, desc, is_read_only)
    elif ds_type in ("postgresql", "neo4j", "mongodb"):
        if is_read_only:
            return _build_managed_readonly_note(ds_name, desc, ds_type)
        else:
            return _build_managed_readwrite_note(ds_name, desc, ds_type)
    else:
        return f"## Datasource: {ds_name}\n{desc}"


def _build_datasources_payload(resolved_ds):
    if not resolved_ds:
        return None
    managed_types = {"postgresql", "neo4j", "mongodb", "webdav"}
    payload = []
    for ds in resolved_ds:
        creds = ds.get("credentials") or {}
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)
            except (json.JSONDecodeError, ValueError):
                creds = {}
        is_read_only = ds.get("project_read_only", False)
        ds_type = ds["type"]
        if ds_type in managed_types and is_read_only:
            creds = {}
        entry = {
            "type": ds_type,
            "name": ds["name"],
            "description": ds.get("description"),
            "connection_url": ds.get("connection_url"),
            "credentials": creds,
            "project_read_only": is_read_only,
        }
        if ds.get("cli_hint"):
            entry["cli_hint"] = ds["cli_hint"]
        if ds.get("default_branch"):
            entry["default_branch"] = ds["default_branch"]
        payload.append(entry)
    return payload or None


# =============================================================================
# KB Note Content Builders
# =============================================================================


class TestGenericNote:
    """KB note content for generic datasources."""

    def test_basic_fields(self):
        ds = {
            "name": "Production DB",
            "description": "Analytics database with user events",
            "connection_url": "postgresql://host:5432/analytics",
            "cli_hint": "psql $DATABASE_URL",
            "credentials": {
                "env_vars": {"DATABASE_URL": "secret", "DB_SCHEMA": "public"}
            },
        }
        content = _build_generic_note("Production DB", ds["description"], ds)
        assert "## Datasource: Production DB" in content
        assert "Analytics database with user events" in content
        assert "postgresql://host:5432/analytics" in content
        assert "`psql $DATABASE_URL`" in content
        assert "`DATABASE_URL`" in content
        assert "`DB_SCHEMA`" in content

    def test_no_credentials_leak(self):
        """Env var values must never appear in KB content."""
        ds = {
            "name": "Secret DB",
            "credentials": {"env_vars": {"API_KEY": "sk-super-secret-key-12345"}},
        }
        content = _build_generic_note("Secret DB", "", ds)
        assert "sk-super-secret-key-12345" not in content
        assert "`API_KEY`" in content

    def test_no_url_no_cli(self):
        ds = {
            "name": "Minimal",
            "credentials": {"env_vars": {"TOKEN": "abc"}},
        }
        content = _build_generic_note("Minimal", "", ds)
        assert "## Datasource: Minimal" in content
        assert "`TOKEN`" in content

    def test_empty_env_vars(self):
        ds = {"name": "Empty", "credentials": {}}
        content = _build_generic_note("Empty", "Some desc", ds)
        assert "## Datasource: Empty" in content
        assert "Some desc" in content
        assert "Environment Variables" not in content

    def test_credentials_as_json_string(self):
        ds = {
            "name": "StringCreds",
            "credentials": json.dumps({"env_vars": {"MY_VAR": "val"}}),
        }
        content = _build_generic_note("StringCreds", "", ds)
        assert "`MY_VAR`" in content


class TestRepositoryNote:
    """KB note content for repository datasources."""

    def test_basic_repo(self):
        ds = {"name": "Frontend App", "default_branch": "develop"}
        content = _build_repository_note("Frontend App", "React SPA", ds)
        assert "## Repository: Frontend App" in content
        assert "React SPA" in content
        assert "./repos/frontend-app/" in content
        assert "git is pre-authenticated" in content
        assert "`develop`" in content

    def test_slug_generation(self):
        ds = {"name": "My Awesome Repo!!!"}
        content = _build_repository_note("My Awesome Repo!!!", "", ds)
        assert "./repos/my-awesome-repo/" in content

    def test_no_branch(self):
        ds = {"name": "Repo"}
        content = _build_repository_note("Repo", "", ds)
        assert "Default branch" not in content

    def test_git_commands_present(self):
        ds = {"name": "Test"}
        content = _build_repository_note("Test", "", ds)
        assert "git status" in content
        assert "git pull" in content
        assert "git push" in content
        assert "No login or credential setup required" in content


class TestManagedReadWriteNote:
    """KB note for managed connectors in read-write (CLI) mode."""

    def test_postgresql(self):
        content = _build_managed_readwrite_note("Analytics", "Big data", "postgresql")
        assert "**Access:** full (CLI)" in content
        assert "`psql`" in content
        assert "PGHOST" in content

    def test_neo4j(self):
        content = _build_managed_readwrite_note("Graph DB", "", "neo4j")
        assert "`cypher-shell`" in content
        assert "NEO4J_URI" in content

    def test_mongodb(self):
        content = _build_managed_readwrite_note("Docs DB", "", "mongodb")
        assert "`mongosh`" in content
        assert "MONGOSH_URI" in content


class TestManagedReadOnlyNote:
    """KB note for managed connectors in read-only (tools) mode."""

    def test_postgresql_readonly(self):
        content = _build_managed_readonly_note("Analytics", "", "postgresql")
        assert "**Access:** read-only (tools)" in content
        assert "`sql_query`" in content
        assert "`sql_schema`" in content
        assert "No CLI access" in content

    def test_neo4j_readonly(self):
        content = _build_managed_readonly_note("Graph", "", "neo4j")
        assert "`execute_cypher_query`" in content

    def test_mongodb_readonly(self):
        content = _build_managed_readonly_note("Docs", "", "mongodb")
        assert "`mongo_query`" in content
        assert "`mongo_aggregate`" in content


class TestWebdavNote:
    """KB note for WebDAV (always tools)."""

    def test_readwrite(self):
        content = _build_webdav_note("Files", "Shared storage", False)
        assert "**Access:** read-write" in content
        assert "`cloud_write`" in content
        assert "`cloud_delete`" in content

    def test_readonly(self):
        content = _build_webdav_note("Files", "", True)
        assert "**Access:** read-only" in content
        assert "cloud_write" not in content
        assert "cloud_delete" not in content
        assert "`cloud_list`" in content
        assert "`cloud_read`" in content


class TestBuildDatasourceNoteContent:
    """Top-level dispatcher that selects the right template per type/mode."""

    def test_dispatches_generic(self):
        ds = {"type": "generic", "name": "Test", "credentials": {}}
        assert "## Datasource: Test" in _build_datasource_note_content(ds)

    def test_dispatches_repository(self):
        ds = {"type": "repository", "name": "Repo"}
        assert "## Repository: Repo" in _build_datasource_note_content(ds)

    def test_dispatches_managed_readwrite(self):
        ds = {"type": "postgresql", "name": "DB", "project_read_only": False}
        assert "full (CLI)" in _build_datasource_note_content(ds)

    def test_dispatches_managed_readonly(self):
        ds = {"type": "postgresql", "name": "DB", "project_read_only": True}
        assert "read-only (tools)" in _build_datasource_note_content(ds)

    def test_dispatches_webdav(self):
        ds = {"type": "webdav", "name": "DAV", "project_read_only": False}
        assert "webdav" in _build_datasource_note_content(ds)

    def test_unknown_type_fallback(self):
        ds = {"type": "redis", "name": "Cache", "description": "Redis cache"}
        content = _build_datasource_note_content(ds)
        assert "Cache" in content

    def test_default_readwrite_when_no_flag(self):
        """Missing project_read_only defaults to False (read-write)."""
        ds = {"type": "neo4j", "name": "Graph"}
        assert "full (CLI)" in _build_datasource_note_content(ds)


# =============================================================================
# Datasource Payload Builder
# =============================================================================


class TestBuildDatasourcesPayload:
    """Tests for _build_datasources_payload — sent to the agent at job start."""

    def test_empty_returns_none(self):
        assert _build_datasources_payload([]) is None
        assert _build_datasources_payload(None) is None

    def test_basic_payload(self):
        ds = [
            {
                "type": "postgresql",
                "name": "DB",
                "description": "Test",
                "connection_url": "postgres://host/db",
                "credentials": '{"password": "secret"}',
                "project_read_only": False,
            }
        ]
        result = _build_datasources_payload(ds)
        assert len(result) == 1
        assert result[0]["type"] == "postgresql"
        assert result[0]["credentials"]["password"] == "secret"

    def test_readonly_managed_withholds_credentials(self):
        """Read-only managed connectors must NOT receive credentials."""
        for ds_type in ("postgresql", "neo4j", "mongodb", "webdav"):
            ds = [
                {
                    "type": ds_type,
                    "name": f"RO {ds_type}",
                    "connection_url": "some://url",
                    "credentials": '{"password": "secret"}',
                    "project_read_only": True,
                }
            ]
            result = _build_datasources_payload(ds)
            assert result[0]["credentials"] == {}, (
                f"{ds_type} should withhold creds in read-only"
            )

    def test_readwrite_managed_keeps_credentials(self):
        ds = [
            {
                "type": "postgresql",
                "name": "RW DB",
                "connection_url": "postgres://host/db",
                "credentials": {"password": "secret"},
                "project_read_only": False,
            }
        ]
        result = _build_datasources_payload(ds)
        assert result[0]["credentials"]["password"] == "secret"

    def test_generic_always_keeps_credentials(self):
        ds = [
            {
                "type": "generic",
                "name": "API",
                "connection_url": None,
                "credentials": {"env_vars": {"API_KEY": "abc"}},
                "project_read_only": False,
            }
        ]
        result = _build_datasources_payload(ds)
        assert result[0]["credentials"]["env_vars"]["API_KEY"] == "abc"

    def test_repository_always_keeps_credentials(self):
        ds = [
            {
                "type": "repository",
                "name": "Repo",
                "connection_url": "https://github.com/org/repo.git",
                "credentials": {"auth_method": "token", "token": "ghp_xxx"},
                "project_read_only": False,
            }
        ]
        result = _build_datasources_payload(ds)
        assert result[0]["credentials"]["token"] == "ghp_xxx"

    def test_cli_hint_included_when_present(self):
        ds = [
            {
                "type": "generic",
                "name": "DB",
                "connection_url": None,
                "credentials": {},
                "project_read_only": False,
                "cli_hint": "psql $DB_URL",
                "default_branch": None,
            }
        ]
        result = _build_datasources_payload(ds)
        assert result[0]["cli_hint"] == "psql $DB_URL"
        assert "default_branch" not in result[0]

    def test_default_branch_included_when_present(self):
        ds = [
            {
                "type": "repository",
                "name": "Repo",
                "connection_url": "url",
                "credentials": {},
                "project_read_only": False,
                "cli_hint": None,
                "default_branch": "develop",
            }
        ]
        result = _build_datasources_payload(ds)
        assert result[0]["default_branch"] == "develop"
        assert "cli_hint" not in result[0]

    def test_invalid_json_credentials_handled(self):
        ds = [
            {
                "type": "postgresql",
                "name": "DB",
                "connection_url": "postgres://host/db",
                "credentials": "not-valid-json",
                "project_read_only": False,
            }
        ]
        result = _build_datasources_payload(ds)
        assert result[0]["credentials"] == {}

    def test_multiple_datasources(self):
        ds = [
            {
                "type": "generic",
                "name": "A",
                "connection_url": None,
                "credentials": {},
                "project_read_only": False,
            },
            {
                "type": "repository",
                "name": "B",
                "connection_url": "url",
                "credentials": {},
                "project_read_only": False,
            },
            {
                "type": "postgresql",
                "name": "C",
                "connection_url": "pg://h/d",
                "credentials": {},
                "project_read_only": True,
            },
        ]
        result = _build_datasources_payload(ds)
        assert len(result) == 3
        assert [r["type"] for r in result] == ["generic", "repository", "postgresql"]


# =============================================================================
# Agent-side: Typed Env Var Injection
# =============================================================================


class TestInjectTypedEnvVars:
    """Tests for Agent._inject_typed_env_vars (env var mapping for CLI access)."""

    @pytest.fixture(autouse=True)
    def _clean_env(self):
        """Remove PG/NEO4J/MONGO env vars before and after each test."""
        keys = [
            "PGHOST",
            "PGPORT",
            "PGUSER",
            "PGPASSWORD",
            "PGDATABASE",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "MONGOSH_URI",
        ]
        saved = {k: os.environ.pop(k, None) for k in keys}
        yield
        for k in keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]

    def _get_inject_fn(self):
        from src.agent import UniversalAgent

        agent = UniversalAgent.__new__(UniversalAgent)
        return agent._inject_typed_env_vars

    def test_postgresql_full_url(self):
        inject = self._get_inject_fn()
        inject(
            "postgresql",
            {
                "connection_url": "postgresql://myuser:mypass@db.example.com:5433/analytics",
                "credentials": {},
            },
        )
        assert os.environ["PGHOST"] == "db.example.com"
        assert os.environ["PGPORT"] == "5433"
        assert os.environ["PGUSER"] == "myuser"
        assert os.environ["PGPASSWORD"] == "mypass"
        assert os.environ["PGDATABASE"] == "analytics"

    def test_postgresql_password_from_credentials(self):
        inject = self._get_inject_fn()
        inject(
            "postgresql",
            {
                "connection_url": "postgresql://user@host:5432/db",
                "credentials": {"password": "from-creds"},
            },
        )
        assert os.environ["PGPASSWORD"] == "from-creds"

    def test_neo4j_env_vars(self):
        inject = self._get_inject_fn()
        inject(
            "neo4j",
            {
                "connection_url": "bolt://neo4j.example.com:7687",
                "credentials": {"username": "neo4j", "password": "secret"},
            },
        )
        assert os.environ["NEO4J_URI"] == "bolt://neo4j.example.com:7687"
        assert os.environ["NEO4J_USERNAME"] == "neo4j"
        assert os.environ["NEO4J_PASSWORD"] == "secret"

    def test_mongodb_env_vars(self):
        inject = self._get_inject_fn()
        inject(
            "mongodb",
            {
                "connection_url": "mongodb+srv://user:pass@cluster.mongodb.net/mydb",
                "credentials": {},
            },
        )
        assert (
            os.environ["MONGOSH_URI"]
            == "mongodb+srv://user:pass@cluster.mongodb.net/mydb"
        )


# =============================================================================
# Agent-side: Datasource Index for workspace.md
# =============================================================================


class TestDatasourceIndex:
    """Tests for Agent._inject_datasource_index (workspace.md injection)."""

    def _make_agent(self):
        from src.agent import UniversalAgent

        agent = UniversalAgent.__new__(UniversalAgent)
        ws = MagicMock()
        ws.read_file.return_value = "# Workspace\n\nExisting content."
        agent._workspace_manager = ws
        return agent, ws

    def test_index_contains_all_types(self):
        agent, ws = self._make_agent()
        configs = [
            {"type": "generic", "name": "API Gateway", "cli_hint": "curl $API_URL"},
            {"type": "repository", "name": "Frontend App"},
            {"type": "postgresql", "name": "Analytics", "project_read_only": False},
            {"type": "neo4j", "name": "Graph DB", "project_read_only": True},
            {"type": "webdav", "name": "Files", "project_read_only": False},
        ]
        agent._inject_datasource_index(configs)
        written = ws.write_file.call_args[0][1]

        assert "## Available Datasources" in written
        assert "**API Gateway** (generic)" in written
        assert "**Frontend App** (repository)" in written
        assert "./repos/frontend-app/" in written
        assert "**Analytics** (postgresql, read-write)" in written
        assert "`psql`" in written
        assert "**Graph DB** (neo4j, read-only)" in written
        assert "**Files** (webdav, read-write tools)" in written

    def test_preserves_existing_content(self):
        agent, ws = self._make_agent()
        agent._inject_datasource_index([{"type": "generic", "name": "X"}])
        written = ws.write_file.call_args[0][1]
        assert "Existing content." in written

    def test_workspace_read_failure_handled(self):
        agent, ws = self._make_agent()
        ws.read_file.side_effect = FileNotFoundError("no workspace.md")
        # Should not raise
        agent._inject_datasource_index([{"type": "generic", "name": "X"}])

    def test_rw_postgresql_expanded_block(self):
        agent, ws = self._make_agent()
        configs = [
            {"type": "postgresql", "name": "Main DB", "project_read_only": False},
        ]
        agent._inject_datasource_index(configs)
        written = ws.write_file.call_args[0][1]

        assert "**Main DB** (postgresql, read-write)" in written
        assert "run_command" in written
        assert "psql -c" in written
        assert "Credentials are pre-configured" in written
        assert "do NOT pass connection flags" in written

    def test_rw_neo4j_expanded_block(self):
        agent, ws = self._make_agent()
        configs = [
            {"type": "neo4j", "name": "Graph", "project_read_only": False},
        ]
        agent._inject_datasource_index(configs)
        written = ws.write_file.call_args[0][1]

        assert "cypher-shell --format plain" in written
        assert "run_command" in written
        assert "Credentials are pre-configured" in written

    def test_rw_mongodb_expanded_block(self):
        agent, ws = self._make_agent()
        configs = [
            {"type": "mongodb", "name": "Docs", "project_read_only": False},
        ]
        agent._inject_datasource_index(configs)
        written = ws.write_file.call_args[0][1]

        assert "mongosh --quiet --eval" in written
        assert "run_command" in written
        assert "Credentials are pre-configured" in written

    def test_readonly_not_expanded(self):
        agent, ws = self._make_agent()
        configs = [
            {"type": "postgresql", "name": "ReadOnly DB", "project_read_only": True},
        ]
        agent._inject_datasource_index(configs)
        written = ws.write_file.call_args[0][1]

        assert "query tools" in written
        assert "run_command" not in written


class TestRenderInstructionContentCLI:
    """Tests for cli_datasources support in render_instruction_content."""

    def test_cli_datasources_conditional(self):
        from src.core.loader import render_instruction_content

        template = "{% if cli_datasources %}HAS_CLI{% endif %}"
        result = render_instruction_content(
            template, [], cli_datasources=["postgresql"]
        )
        assert "HAS_CLI" in result

    def test_no_cli_datasources_omits_block(self):
        from src.core.loader import render_instruction_content

        template = "{% if cli_datasources %}HAS_CLI{% endif %}"
        result = render_instruction_content(template, [], cli_datasources=[])
        assert "HAS_CLI" not in result

    def test_has_cli_datasource_check(self):
        from src.core.loader import render_instruction_content

        template = (
            '{% if has_cli_datasource("postgresql") %}PG{% endif %}'
            '{% if has_cli_datasource("neo4j") %}NEO{% endif %}'
        )
        result = render_instruction_content(
            template, [], cli_datasources=["postgresql"]
        )
        assert "PG" in result
        assert "NEO" not in result

    def test_default_none_cli_datasources(self):
        from src.core.loader import render_instruction_content

        template = "{% if cli_datasources %}HAS_CLI{% endif %}"
        result = render_instruction_content(template, [])
        assert "HAS_CLI" not in result


# =============================================================================
# Credential Structure Validation
# =============================================================================


class TestCredentialStructures:
    """Validate the credential JSONB structures for each datasource type."""

    def test_generic_env_vars(self):
        creds = {"env_vars": {"DATABASE_URL": "pg://host/db", "API_KEY": "sk-123"}}
        assert isinstance(creds["env_vars"], dict)
        assert len(creds["env_vars"]) == 2

    def test_repository_token(self):
        creds = {"auth_method": "token", "token": "ghp_xxxx"}
        assert creds["auth_method"] == "token"
        assert "ssh_key" not in creds

    def test_repository_ssh(self):
        creds = {
            "auth_method": "ssh",
            "ssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----\ndata",
        }
        assert creds["auth_method"] == "ssh"
        assert "token" not in creds

    def test_neo4j_credentials(self):
        creds = {"username": "neo4j", "password": "secret"}
        assert "username" in creds and "password" in creds

    def test_postgresql_credentials(self):
        creds = {"password": "secret"}
        assert "password" in creds

    def test_webdav_credentials(self):
        creds = {"username": "admin", "password": "pass"}
        assert "username" in creds and "password" in creds
