"""Shared datasource setup logic for both job agents and persistent sessions.

Processes datasource configs received from the orchestrator: connects managed
connectors, injects env vars for CLI access, clones repositories, and builds
a datasource index for workspace.md.
"""

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a datasource name to a lowercase slug for env vars / filenames."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def process_datasources(
    ds_configs: List[Dict[str, Any]],
    workspace_dir: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Process datasource configs and create connections/env vars.

    Supports multiple datasources of the same type by using named
    connection profiles (pg_service.conf) or per-datasource env vars
    (MONGO_{SLUG}_URI, NEO4J_{SLUG}_URI).

    Args:
        ds_configs: List of datasource config dicts from the orchestrator.
        workspace_dir: Workspace directory for repository cloning.

    Returns:
        Tuple of (datasources_dict, client_registry, cli_ds_types):
        - datasources_dict: Connection objects keyed by type for ToolContext
        - client_registry: Parent clients (e.g. MongoClient) for cleanup
        - cli_ds_types: List of datasource types configured for CLI access
    """
    datasources_dict: Dict[str, Any] = {}
    client_registry: Dict[str, Any] = {}
    cli_ds_types: List[str] = []

    # Group datasources by type for multi-source setup
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    generic_list: List[Dict[str, Any]] = []
    repo_list: List[Dict[str, Any]] = []
    read_only_list: List[Dict[str, Any]] = []

    for ds in ds_configs:
        ds_type = ds.get("type")
        if not ds_type:
            continue

        if ds_type == "generic":
            generic_list.append(ds)
        elif ds_type == "repository":
            repo_list.append(ds)
        else:
            is_read_only = ds.get("project_read_only", False)
            if not is_read_only and ds_type in ("postgresql", "neo4j", "mongodb"):
                by_type.setdefault(ds_type, []).append(ds)
            else:
                read_only_list.append(ds)

    # Generic datasources: inject env vars into process environment
    for ds in generic_list:
        creds = ds.get("credentials") or {}
        env_vars = creds.get("env_vars", {})
        for key, value in env_vars.items():
            os.environ[key] = str(value)
        logger.info(
            "Injected %d env vars for generic datasource: %s",
            len(env_vars),
            ds.get("name", "unnamed"),
        )

    # Repository datasources: clone repos
    for ds in repo_list:
        try:
            setup_repository_datasource(ds, workspace_dir)
        except Exception as e:
            logger.warning("Failed to setup repository datasource: %s", e)

    # CLI-mode managed connectors: set up named connections
    if "postgresql" in by_type:
        inject_postgresql_services(by_type["postgresql"])
        cli_ds_types.append("postgresql")

    if "mongodb" in by_type:
        inject_mongodb_env_vars(by_type["mongodb"])
        cli_ds_types.append("mongodb")

    if "neo4j" in by_type:
        inject_neo4j_env_vars(by_type["neo4j"])
        cli_ds_types.append("neo4j")

    # Read-only managed connectors: create tool connections
    for ds in read_only_list:
        ds_type = ds["type"]
        try:
            conn, client = create_datasource_connection(ds)
            datasources_dict[ds_type] = conn
            if client:
                client_registry[ds_type] = client
            logger.info(
                "Connected to %s datasource: %s",
                ds_type,
                ds.get("name", "unnamed"),
            )
        except Exception as e:
            logger.warning("Failed to connect to %s datasource: %s", ds_type, e)

    return datasources_dict, client_registry, cli_ds_types


def close_datasource_connections(
    connections: Dict[str, Any],
    clients: Dict[str, Any],
) -> None:
    """Close all datasource connections and parent clients."""
    for ds_type, conn in connections.items():
        try:
            if hasattr(conn, "close"):
                conn.close()
                logger.debug("Closed %s datasource connection", ds_type)
        except Exception as e:
            logger.warning("Error closing %s datasource: %s", ds_type, e)

    for ds_type, client in clients.items():
        try:
            if hasattr(client, "close"):
                client.close()
                logger.debug("Closed %s datasource client", ds_type)
        except Exception as e:
            logger.warning("Error closing %s datasource client: %s", ds_type, e)


# ---------------------------------------------------------------------------
# Named connection generators (Phase 2: multi-source)
# ---------------------------------------------------------------------------


def inject_postgresql_services(datasources: List[Dict[str, Any]]) -> None:
    """Generate ~/.pg_service.conf entries for all PostgreSQL datasources.

    Uses PostgreSQL's native service file for named connection profiles.
    All libpq-based tools (psql, pg_dump, etc.) support PGSERVICE.
    """
    service_file = os.path.expanduser("~/.pg_service.conf")
    os.environ["PGSERVICEFILE"] = service_file

    entries: List[str] = []
    for ds in datasources:
        slug = _slugify(ds["name"])
        url = ds.get("connection_url", "")
        creds = ds.get("credentials") or {}
        parsed = urlparse(url)

        entries.append(f"[{slug}]")
        if parsed.hostname:
            entries.append(f"host={parsed.hostname}")
        if parsed.port:
            entries.append(f"port={parsed.port}")
        if parsed.username:
            entries.append(f"user={parsed.username}")
        password = parsed.password or creds.get("password", "")
        if password:
            entries.append(f"password={password}")
        db_name = parsed.path.lstrip("/").split("?")[0]
        if db_name:
            entries.append(f"dbname={db_name}")
        entries.append("")  # blank line between services

        logger.info("Configured pg_service entry '%s' for: %s", slug, ds.get("name"))

    with open(service_file, "a") as f:
        f.write("\n".join(entries))

    # Backward compat: also set legacy env vars when only one PG datasource
    if len(datasources) == 1:
        _inject_legacy_pg_env(datasources[0])


def inject_mongodb_env_vars(datasources: List[Dict[str, Any]]) -> None:
    """Set per-datasource MONGO_{SLUG}_URI environment variables."""
    for ds in datasources:
        slug = _slugify(ds["name"]).upper()
        url = ds.get("connection_url", "")
        os.environ[f"MONGO_{slug}_URI"] = url
        logger.info("Set MONGO_%s_URI for: %s", slug, ds.get("name"))

    # Backward compat: also set MONGOSH_URI when only one
    if len(datasources) == 1:
        os.environ["MONGOSH_URI"] = datasources[0].get("connection_url", "")


def inject_neo4j_env_vars(datasources: List[Dict[str, Any]]) -> None:
    """Set per-datasource NEO4J_{SLUG}_* environment variables."""
    for ds in datasources:
        slug = _slugify(ds["name"]).upper()
        url = ds.get("connection_url", "")
        creds = ds.get("credentials") or {}
        os.environ[f"NEO4J_{slug}_URI"] = url
        os.environ[f"NEO4J_{slug}_USERNAME"] = creds.get("username", "neo4j")
        os.environ[f"NEO4J_{slug}_PASSWORD"] = creds.get("password", "")
        logger.info("Set NEO4J_%s_* for: %s", slug, ds.get("name"))

    # Backward compat: also set legacy env vars when only one
    if len(datasources) == 1:
        creds = datasources[0].get("credentials") or {}
        os.environ["NEO4J_URI"] = datasources[0].get("connection_url", "")
        os.environ["NEO4J_USERNAME"] = creds.get("username", "neo4j")
        os.environ["NEO4J_PASSWORD"] = creds.get("password", "")


def _inject_legacy_pg_env(ds: Dict[str, Any]) -> None:
    """Set legacy PGHOST/PGPORT/etc. env vars for a single PostgreSQL datasource."""
    url = ds.get("connection_url", "")
    creds = ds.get("credentials") or {}
    parsed = urlparse(url)
    if parsed.hostname:
        os.environ["PGHOST"] = parsed.hostname
    if parsed.port:
        os.environ["PGPORT"] = str(parsed.port)
    if parsed.username:
        os.environ["PGUSER"] = parsed.username
    password = parsed.password or creds.get("password", "")
    if password:
        os.environ["PGPASSWORD"] = password
    db_name = parsed.path.lstrip("/").split("?")[0]
    if db_name:
        os.environ["PGDATABASE"] = db_name


# ---------------------------------------------------------------------------
# Typed connections (read-only tool mode)
# ---------------------------------------------------------------------------


def create_datasource_connection(
    ds: Dict[str, Any],
) -> Tuple[Any, Any]:
    """Create a connection to an external datasource.

    Returns:
        Tuple of (connection, parent_client). parent_client is non-None
        only for MongoDB where the MongoClient must be kept for cleanup.
    """
    ds_type = ds["type"]
    url = ds.get("connection_url") or ""
    creds = ds.get("credentials") or {}

    if ds_type == "neo4j":
        from src.database.neo4j_db import Neo4jDB

        db = Neo4jDB(
            uri=url,
            username=creds.get("username", "neo4j"),
            password=creds.get("password", ""),
        )
        db.connect()
        return db, None

    elif ds_type == "postgresql":
        import psycopg

        conn = psycopg.connect(url, autocommit=False)
        conn.execute("SELECT 1")
        conn.rollback()
        return conn, None

    elif ds_type == "mongodb":
        from pymongo import MongoClient

        client = MongoClient(url, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        parsed = urlparse(url)
        db_name = parsed.path.lstrip("/").split("?")[0] or "default"
        db = client[db_name]
        return db, client

    elif ds_type == "webdav":
        from webdav3.client import Client

        client = Client(
            {
                "webdav_hostname": url,
                "webdav_login": creds.get("username"),
                "webdav_password": creds.get("password"),
            }
        )
        client.list("/")
        return client, None

    else:
        raise ValueError(f"Unknown datasource type: {ds_type}")


# ---------------------------------------------------------------------------
# Repository setup
# ---------------------------------------------------------------------------


def setup_repository_datasource(
    ds: Dict[str, Any],
    workspace_dir: Optional[str] = None,
) -> None:
    """Clone a repository into the workspace and configure git credentials.

    Uses per-repo core.sshCommand for SSH auth, which avoids conflicts
    when multiple repos share a hostname (e.g. two GitHub repos with
    different deploy keys).
    """
    repo_url = ds.get("connection_url", "")
    creds = ds.get("credentials") or {}
    name = re.sub(r"[^a-z0-9]+", "-", ds.get("name", "repo").lower()).strip("-")
    branch = ds.get("default_branch")

    workspace_dir = workspace_dir or os.getcwd()
    repos_dir = os.path.join(workspace_dir, "repos")
    os.makedirs(repos_dir, exist_ok=True)
    clone_path = os.path.join(repos_dir, name)

    if os.path.exists(clone_path):
        logger.info("Repository already exists at %s, skipping clone", clone_path)
        return

    auth_method = creds.get("auth_method", "token")
    clone_env = {**os.environ}

    if auth_method == "ssh":
        ssh_dir = os.path.expanduser("~/.ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        key_file = os.path.join(ssh_dir, f"repo_{name}")
        with open(key_file, "w") as f:
            f.write(creds.get("ssh_key", ""))
        os.chmod(key_file, 0o600)

        # Clone with explicit SSH command (avoids ~/.ssh/config conflicts)
        ssh_cmd = f"ssh -i {key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        clone_env["GIT_SSH_COMMAND"] = ssh_cmd

    elif auth_method == "token" and creds.get("token"):
        cred_file = os.path.expanduser("~/.git-credentials")
        parsed = urlparse(repo_url)
        host = parsed.hostname or "github.com"
        scheme = parsed.scheme or "https"
        cred_line = f"{scheme}://oauth2:{creds['token']}@{host}"
        with open(cred_file, "a") as f:
            f.write(cred_line + "\n")
        os.chmod(cred_file, 0o600)
        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"],
            check=False,
            capture_output=True,
        )

    cmd = ["git", "clone", repo_url, clone_path]
    if branch:
        cmd.extend(["--branch", branch])
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, env=clone_env
    )
    if result.returncode != 0:
        logger.warning("Git clone failed: %s", result.stderr)
        raise RuntimeError(f"Failed to clone repository: {result.stderr}")
    logger.info("Cloned repository to %s", clone_path)

    # Set persistent per-repo SSH command so future git ops use the right key
    if auth_method == "ssh":
        subprocess.run(
            [
                "git",
                "config",
                "core.sshCommand",
                f"ssh -i {key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
            ],
            cwd=clone_path,
            check=False,
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Workspace index
# ---------------------------------------------------------------------------


def inject_datasource_index(
    ds_configs: List[Dict[str, Any]],
    workspace_manager: Any,
) -> None:
    """Inject a compact datasource index into workspace.md.

    Lists every datasource with its specific named access method so the
    agent knows how to connect to each one.
    """
    lines = ["\n\n## Available Datasources\n"]

    # Group by category for readable output
    repos = [ds for ds in ds_configs if ds.get("type") == "repository"]
    databases = [
        ds for ds in ds_configs if ds.get("type") in ("postgresql", "neo4j", "mongodb")
    ]
    others = [
        ds
        for ds in ds_configs
        if ds.get("type") not in ("repository", "postgresql", "neo4j", "mongodb")
    ]

    if repos:
        lines.append("### Repositories")
        for ds in repos:
            slug = re.sub(r"[^a-z0-9]+", "-", ds.get("name", "repo").lower()).strip("-")
            lines.append(
                f"- **{ds.get('name')}** — cloned at `./repos/{slug}/`, git pre-authenticated"
            )
        lines.append("")

    if databases:
        lines.append("### Databases")
        for ds in databases:
            ds_type = ds.get("type", "unknown")
            name = ds.get("name", "Unnamed")
            is_ro = ds.get("project_read_only", False)

            if is_ro:
                lines.append(f"- **{name}** ({ds_type}, read-only) — query tools")
            else:
                lines.append(_format_rw_cli_entry(name, ds_type))
        lines.append("")

    if others:
        lines.append("### Other")
        for ds in others:
            ds_type = ds.get("type", "unknown")
            name = ds.get("name", "Unnamed")
            is_ro = ds.get("project_read_only", False)

            if ds_type == "generic":
                cli = ds.get("cli_hint", "CLI via env vars")
                lines.append(f"- **{name}** (generic) — {cli}")
            elif ds_type == "webdav":
                access = "read-only tools" if is_ro else "read-write tools"
                lines.append(f"- **{name}** (webdav, {access})")
            else:
                lines.append(f"- **{name}** ({ds_type})")
        lines.append("")

    try:
        existing = workspace_manager.read_file("workspace.md")
        workspace_manager.write_file("workspace.md", existing + "\n".join(lines))
        logger.info(
            "Injected datasource index (%d entries) into workspace.md",
            len(ds_configs),
        )
    except Exception as e:
        logger.warning("Failed to inject datasource index: %s", e)


def _format_rw_cli_entry(name: str, ds_type: str) -> str:
    """Format a one-line CLI usage entry for a read-write managed datasource."""
    slug = _slugify(name)
    slug_upper = slug.upper()

    if ds_type == "postgresql":
        return (
            f"- **{name}** (postgresql, read-write): "
            f"`PGSERVICE={slug} psql` — credentials pre-configured"
        )
    elif ds_type == "neo4j":
        return (
            f"- **{name}** (neo4j, read-write): "
            f'`cypher-shell --address "$NEO4J_{slug_upper}_URI" '
            f'--username "$NEO4J_{slug_upper}_USERNAME" '
            f'--password "$NEO4J_{slug_upper}_PASSWORD"`'
        )
    elif ds_type == "mongodb":
        return (
            f'- **{name}** (mongodb, read-write): `mongosh "$MONGO_{slug_upper}_URI"`'
        )
    else:
        return f"- **{name}** ({ds_type}, read-write) — CLI via env vars"
