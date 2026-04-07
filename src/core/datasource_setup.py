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


def process_datasources(
    ds_configs: List[Dict[str, Any]],
    workspace_dir: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Process datasource configs and create connections/env vars.

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

    for ds in ds_configs:
        ds_type = ds.get("type")
        if not ds_type:
            continue

        # Generic datasources: inject env vars into process environment
        if ds_type == "generic":
            creds = ds.get("credentials") or {}
            env_vars = creds.get("env_vars", {})
            for key, value in env_vars.items():
                os.environ[key] = str(value)
            logger.info(
                "Injected %d env vars for generic datasource: %s",
                len(env_vars),
                ds.get("name", "unnamed"),
            )
            continue

        # Repository datasources: clone repo and configure git credentials
        if ds_type == "repository":
            try:
                setup_repository_datasource(ds, workspace_dir)
            except Exception as e:
                logger.warning("Failed to setup repository datasource: %s", e)
            continue

        # Managed connectors: connect via typed drivers (for read-only tools)
        # In read-write mode, inject env vars instead (no tool connection needed)
        is_read_only = ds.get("project_read_only", False)
        if not is_read_only and ds_type in ("postgresql", "neo4j", "mongodb"):
            inject_typed_env_vars(ds_type, ds)
            cli_ds_types.append(ds_type)
            logger.info(
                "Injected env vars for %s CLI access: %s",
                ds_type,
                ds.get("name", "unnamed"),
            )
            continue

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


def inject_typed_env_vars(ds_type: str, ds: Dict[str, Any]) -> None:
    """Inject well-known environment variables for managed connector CLI access."""
    url = ds.get("connection_url", "")
    creds = ds.get("credentials") or {}

    if ds_type == "postgresql":
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

    elif ds_type == "neo4j":
        os.environ["NEO4J_URI"] = url
        os.environ["NEO4J_USERNAME"] = creds.get("username", "neo4j")
        os.environ["NEO4J_PASSWORD"] = creds.get("password", "")

    elif ds_type == "mongodb":
        os.environ["MONGOSH_URI"] = url


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


def setup_repository_datasource(
    ds: Dict[str, Any],
    workspace_dir: Optional[str] = None,
) -> None:
    """Clone a repository into the workspace and configure git credentials."""
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

    if auth_method == "ssh":
        ssh_dir = os.path.expanduser("~/.ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        key_file = os.path.join(ssh_dir, f"repo_{name}")
        with open(key_file, "w") as f:
            f.write(creds.get("ssh_key", ""))
        os.chmod(key_file, 0o600)

        parsed = urlparse(repo_url)
        host = parsed.hostname or "github.com"

        config_path = os.path.join(ssh_dir, "config")
        with open(config_path, "a") as f:
            f.write(
                f"\nHost {host}\n  IdentityFile {key_file}\n  StrictHostKeyChecking accept-new\n"
            )

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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logger.warning("Git clone failed: %s", result.stderr)
        raise RuntimeError(f"Failed to clone repository: {result.stderr}")
    logger.info("Cloned repository to %s", clone_path)


def inject_datasource_index(
    ds_configs: List[Dict[str, Any]],
    workspace_manager: Any,
) -> None:
    """Inject a compact datasource index into workspace.md.

    Ensures the agent always knows what datasources are available.
    """
    lines = ["\n\n## Available Datasources\n"]
    for ds in ds_configs:
        ds_type = ds.get("type", "unknown")
        name = ds.get("name", "Unnamed")
        is_ro = ds.get("project_read_only", False)

        if ds_type == "generic":
            cli = ds.get("cli_hint", "CLI via env vars")
            lines.append(f"- **{name}** (generic) — {cli}")
        elif ds_type == "repository":
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            lines.append(f"- **{name}** (repository) — cloned at `./repos/{slug}/`")
        elif ds_type == "webdav":
            access = "read-only tools" if is_ro else "read-write tools"
            lines.append(f"- **{name}** (webdav, {access})")
        elif ds_type in ("postgresql", "neo4j", "mongodb"):
            if is_ro:
                lines.append(f"- **{name}** ({ds_type}, read-only) — query tools")
            else:
                lines.append(_format_rw_cli_block(name, ds_type))
        else:
            lines.append(f"- **{name}** ({ds_type})")

    try:
        existing = workspace_manager.read_file("workspace.md")
        workspace_manager.write_file("workspace.md", existing + "\n".join(lines))
        logger.info(
            "Injected datasource index (%d entries) into workspace.md",
            len(ds_configs),
        )
    except Exception as e:
        logger.warning("Failed to inject datasource index: %s", e)


def _format_rw_cli_block(name: str, ds_type: str) -> str:
    """Format an expanded CLI usage block for a read-write managed datasource."""
    blocks = {
        "postgresql": (
            f"- **{name}** (postgresql, read-write):\n"
            f"  Use `run_command` with `psql`. Credentials are pre-configured — do NOT pass connection flags.\n"
            f"  ```\n"
            f"  psql -c \"SELECT table_name FROM information_schema.tables WHERE table_schema='public'\"\n"
            f'  psql -c "\\dt"\n'
            f"  ```"
        ),
        "neo4j": (
            f"- **{name}** (neo4j, read-write):\n"
            f"  Use `run_command` with `cypher-shell`. Credentials are pre-configured — do NOT pass connection flags.\n"
            f"  ```\n"
            f'  cypher-shell --format plain "MATCH (n) RETURN labels(n), count(*)"\n'
            f"  cypher-shell --format plain \"CREATE (n:Note {{text: 'hello'}}) RETURN n\"\n"
            f"  ```"
        ),
        "mongodb": (
            f"- **{name}** (mongodb, read-write):\n"
            f"  Use `run_command` with `mongosh`. Credentials are pre-configured — do NOT pass connection flags.\n"
            f"  ```\n"
            f'  mongosh --quiet --eval "db.getCollectionNames()"\n'
            f'  mongosh --quiet --eval "db.users.find().limit(5)"\n'
            f"  ```"
        ),
    }
    return blocks.get(
        ds_type,
        f"- **{name}** ({ds_type}, read-write) — CLI via env vars",
    )
