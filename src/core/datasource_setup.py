"""Shared datasource setup logic for both job agents and persistent sessions.

Processes datasource configs received from the orchestrator: connects managed
connectors, injects env vars for CLI access, clones repositories, materializes
credential files (kubeconfig, ssh_key, generic_file), and builds a datasource
index for datasources.md.
"""

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Kept in sync with orchestrator/security/credential_files.py. The orchestrator
# stores ``target_path`` already resolved against ``/home/srw``; at materialization
# time we may need to remap to a different home (tests use a tmp dir).
AGENT_HOME = "/home/srw"
CREDENTIAL_FILE_TYPES = frozenset({"kubeconfig", "ssh_key", "generic_file"})


def _slugify(name: str) -> str:
    """Convert a datasource name to a lowercase slug for env vars / filenames."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _ds_slug_hyphen(name: str) -> str:
    """Hyphenated slug for filenames and kubeconfig context prefixes.

    Matches ``slugify_datasource_name`` in
    ``orchestrator/security/credential_files.py``.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "unnamed"


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
# Credential file materialization (kubeconfig / ssh_key / generic_file)
# ---------------------------------------------------------------------------


def _retarget(path: str, home_dir: str) -> str:
    """Swap ``/home/srw`` for ``home_dir`` so tests can use a tmp directory.

    The orchestrator's validator already resolved ``~`` against
    ``/home/srw``; in production no swap is needed. In tests we pass a
    tmp ``home_dir`` and rewrite the prefix at write time.
    """
    if not path or home_dir == AGENT_HOME:
        return path
    if path == AGENT_HOME:
        return home_dir
    if path.startswith(AGENT_HOME + "/"):
        return home_dir + path[len(AGENT_HOME) :]
    return path


def _mkdir_tracking(path: str, created_dirs: List[str]) -> None:
    """``mkdir -p`` while recording each directory we (not the OS image) created.

    Cleanup uses this list to ``rmdir`` only the directories we made, leaving
    pre-existing ones like ``~/.ssh`` (which may have ``known_hosts``) intact.
    """
    if not path or path == "/" or os.path.isdir(path):
        return
    parent = os.path.dirname(path)
    if parent and parent != path:
        _mkdir_tracking(parent, created_dirs)
    try:
        os.mkdir(path)
        created_dirs.append(path)
    except FileExistsError:
        pass


def _prefix_kubeconfig_yaml(yaml_str: str, prefix: str) -> str:
    """Prefix every cluster/user/context name in a kubeconfig with ``<prefix>-``.

    Multi-cluster jobs merge several kubeconfigs into ``~/.kube/config``;
    without prefixing, two uploads with a context named ``default`` would
    collide. We pre-prefix per-datasource so the agent sees deterministic,
    collision-free context names like ``prod-eu-default``.

    Returns the re-emitted YAML. On a parse failure the original string is
    returned and a warning is logged — the upload may still be usable,
    just with un-prefixed contexts.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; skipping kubeconfig prefixing")
        return yaml_str

    try:
        doc = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        logger.warning("Kubeconfig YAML parse failed (%s); writing un-prefixed", e)
        return yaml_str
    if not isinstance(doc, dict):
        return yaml_str

    def _pfx(name: Any) -> Any:
        return f"{prefix}-{name}" if isinstance(name, str) and name else name

    for cluster in doc.get("clusters") or []:
        if isinstance(cluster, dict) and "name" in cluster:
            cluster["name"] = _pfx(cluster["name"])
    for user in doc.get("users") or []:
        if isinstance(user, dict) and "name" in user:
            user["name"] = _pfx(user["name"])
    for ctx in doc.get("contexts") or []:
        if isinstance(ctx, dict):
            if "name" in ctx:
                ctx["name"] = _pfx(ctx["name"])
            inner = ctx.get("context")
            if isinstance(inner, dict):
                if "cluster" in inner:
                    inner["cluster"] = _pfx(inner["cluster"])
                if "user" in inner:
                    inner["user"] = _pfx(inner["user"])
    if doc.get("current-context"):
        doc["current-context"] = _pfx(doc["current-context"])

    return yaml.safe_dump(doc, sort_keys=False)


def _merge_kubeconfigs(
    kubeconfig_paths: List[str],
    home_dir: str,
    manifest: Dict[str, Any],
) -> Optional[str]:
    """Merge per-datasource kubeconfigs into ``~/.kube/config`` using ``kubectl``.

    Returns the merged absolute path on success. On failure (no kubectl,
    bad input) returns ``None`` and the per-datasource files remain
    available individually — the caller falls back to a colon-separated
    ``KUBECONFIG`` so kubectl can still find them.
    """
    if not kubeconfig_paths:
        return None
    merged_path = os.path.join(home_dir, ".kube", "config")
    if os.path.exists(merged_path):
        logger.warning(
            "Refusing to overwrite existing %s; agent will use per-ds KUBECONFIG list",
            merged_path,
        )
        return None
    _mkdir_tracking(os.path.dirname(merged_path), manifest["dirs"])
    env = {**os.environ, "KUBECONFIG": ":".join(kubeconfig_paths)}
    try:
        result = subprocess.run(
            ["kubectl", "config", "view", "--flatten", "--merge"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        logger.warning("kubectl not installed; falling back to KUBECONFIG=<colon-list>")
        return None
    except subprocess.CalledProcessError as e:
        logger.warning("kubectl config view failed: %s", e.stderr.strip())
        return None
    except subprocess.TimeoutExpired:
        logger.warning("kubectl config view timed out")
        return None

    try:
        with open(merged_path, "w") as f:
            f.write(result.stdout)
        os.chmod(merged_path, 0o600)
    except OSError as e:
        logger.warning("Failed to write merged kubeconfig %s: %s", merged_path, e)
        return None
    manifest["files"].append(merged_path)
    return merged_path


def process_credential_files(
    ds_configs: List[Dict[str, Any]],
    home_dir: str = AGENT_HOME,
) -> Dict[str, Any]:
    """Materialize ``credentials.files[]`` for credential-file datasource types.

    For each ``kubeconfig``/``ssh_key``/``generic_file`` datasource:
      - ``mkdir -p`` the parent (recording new dirs in the manifest).
      - For ``kubeconfig`` entries, prefix cluster/user/context names with
        the datasource slug so multi-cluster merging is collision-free.
      - Write contents at the resolved absolute path with the requested mode.
      - If ``env_var`` is set, inject ``os.environ[env_var] = abs_path``.
      - Skip (warn) any path that already exists on disk — we don't clobber
        anything we didn't write.

    After all per-datasource files are written, if any kubeconfigs were
    attached, merge them via ``kubectl config view --flatten --merge`` into
    ``~/.kube/config`` and set ``KUBECONFIG`` to that path.

    Args:
        ds_configs: Datasource configs from the orchestrator (already
            validated by ``normalize_credential_files``).
        home_dir: Override the ``/home/srw`` prefix in stored target_paths.
            Production leaves the default; tests pass a tmp directory.

    Returns:
        Manifest dict consumed by :func:`cleanup_credential_files` at job
        teardown::

            {
                "files":    [abs paths written],
                "dirs":     [abs dirs we created],
                "env_vars": [env var names we set],
            }
    """
    manifest: Dict[str, Any] = {
        "files": [],
        "dirs": [],
        "env_vars": [],
    }
    kubeconfig_paths: List[str] = []

    for ds in ds_configs:
        ds_type = ds.get("type")
        if ds_type not in CREDENTIAL_FILE_TYPES:
            continue
        creds = ds.get("credentials") or {}
        files = creds.get("files") or []
        ds_name = ds.get("name", "unnamed")
        ds_slug = _ds_slug_hyphen(ds_name)

        for entry in files:
            target_path = entry.get("target_path") or ""
            absolute = _retarget(target_path, home_dir)
            if not absolute:
                logger.warning(
                    "Skipping file entry with empty target_path on '%s'", ds_name
                )
                continue
            if os.path.exists(absolute):
                logger.warning(
                    "Refusing to overwrite existing file at %s (datasource '%s')",
                    absolute,
                    ds_name,
                )
                continue
            contents = entry.get("contents", "")
            if ds_type == "kubeconfig":
                contents = _prefix_kubeconfig_yaml(contents, ds_slug)
            mode_str = entry.get("mode") or "0600"
            try:
                mode_int = int(mode_str, 8)
            except ValueError:
                logger.warning("Bad mode %r on '%s'; using 0600", mode_str, ds_name)
                mode_int = 0o600

            parent = os.path.dirname(absolute)
            if parent:
                _mkdir_tracking(parent, manifest["dirs"])
            try:
                with open(absolute, "w") as f:
                    f.write(contents)
                os.chmod(absolute, mode_int)
            except OSError as e:
                logger.warning(
                    "Failed to write credential file %s for '%s': %s",
                    absolute,
                    ds_name,
                    e,
                )
                continue
            manifest["files"].append(absolute)
            logger.info(
                "Materialized credential file for '%s' at %s (mode %s)",
                ds_name,
                absolute,
                mode_str,
            )

            env_var = entry.get("env_var")
            if env_var:
                os.environ[env_var] = absolute
                manifest["env_vars"].append(env_var)

            if ds_type == "kubeconfig":
                kubeconfig_paths.append(absolute)

    if kubeconfig_paths:
        merged = _merge_kubeconfigs(kubeconfig_paths, home_dir, manifest)
        if merged:
            os.environ["KUBECONFIG"] = merged
        else:
            os.environ["KUBECONFIG"] = ":".join(kubeconfig_paths)
        manifest["env_vars"].append("KUBECONFIG")

    return manifest


def cleanup_credential_files(manifest: Optional[Dict[str, Any]]) -> None:
    """Undo a :func:`process_credential_files` manifest. Best-effort, never raises.

    Removes materialized files, unsets env vars, and ``rmdir``s only the
    directories the materialization step created (pre-existing dirs like
    ``~/.ssh`` are left alone).
    """
    if not manifest:
        return

    for env_var in manifest.get("env_vars", []) or []:
        os.environ.pop(env_var, None)

    for path in manifest.get("files", []) or []:
        try:
            os.unlink(path)
            logger.debug("Removed credential file %s", path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Failed to remove credential file %s: %s", path, e)

    # Deepest first so children come out before their parents.
    for d in sorted(manifest.get("dirs", []) or [], key=lambda p: -len(p)):
        try:
            os.rmdir(d)
        except OSError:
            # Non-empty or already gone; either way, nothing to do.
            pass


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
    """Inject a compact datasource index into datasources.md.

    Lists every datasource with its specific named access method so the
    agent knows how to connect to each one. The system prompts point the
    agent at datasources.md for connection names.
    """
    lines = ["\n\n## Available Datasources\n"]

    # Group by category for readable output
    repos = [ds for ds in ds_configs if ds.get("type") == "repository"]
    databases = [
        ds for ds in ds_configs if ds.get("type") in ("postgresql", "neo4j", "mongodb")
    ]
    creds = [ds for ds in ds_configs if ds.get("type") in CREDENTIAL_FILE_TYPES]
    others = [
        ds
        for ds in ds_configs
        if ds.get("type")
        not in (
            "repository",
            "postgresql",
            "neo4j",
            "mongodb",
            "kubeconfig",
            "ssh_key",
            "generic_file",
        )
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

    if creds:
        lines.append("### Credential Files")
        for ds in creds:
            ds_type = ds.get("type", "unknown")
            name = ds.get("name", "Unnamed")
            slug = _ds_slug_hyphen(name)
            ds_creds = ds.get("credentials") or {}
            files = ds_creds.get("files") or []
            if ds_type == "kubeconfig":
                lines.append(
                    f"- **{name}** (kubeconfig) — merged into `~/.kube/config`; "
                    f"contexts prefixed `{slug}-*`. Try `kubectl config get-contexts`."
                )
            elif ds_type == "ssh_key":
                lines.append(
                    f"- **{name}** (ssh_key) — private key at `~/.ssh/{slug}`. "
                    f"Add a host block in `~/.ssh/config` to use it."
                )
            else:  # generic_file
                paths = (
                    ", ".join(f"`{f.get('target_path')}`" for f in files) or "<none>"
                )
                lines.append(f"- **{name}** (file) — {paths}")
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
        try:
            existing = workspace_manager.read_file("datasources.md")
        except (FileNotFoundError, ValueError, OSError):
            existing = ""
        workspace_manager.write_file("datasources.md", existing + "\n".join(lines))
        logger.info(
            "Injected datasource index (%d entries) into datasources.md",
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
