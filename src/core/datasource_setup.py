"""Shared datasource setup logic for both job agents and persistent sessions.

Processes datasource configs received from the orchestrator: connects managed
connectors, injects env vars for CLI access, clones repositories onto the
workspace backend, materializes credential files (kubeconfig, ssh_key,
generic_file), and builds a datasource index for datasources.md.

Repository datasources are cloned exclusively on the workspace via
``clone_repository_datasources()`` (GitManager + shell-capable backend).
There is no agent-local clone path — the former subprocess ``git clone``
branch wrote credentials and repos onto the agent pod and was removed
(docs/features/no_workspace_agent_mode.md §9.4).
"""

import logging
import os
import re
import shlex
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


# Datasource type → tool category + read/write tool sets. Single source of
# truth for BOTH trust boundaries: the orchestrator's
# _build_datasource_tool_override (job dispatch, thread create/resume) and the
# agent's session attach path delegate to datasource_tool_categories() below.
# These were previously two hand-maintained copies that disagreed on
# read-write managed connectors (agent: write tools; orchestrator: CLI-only).
DATASOURCE_TOOL_MAP: Dict[str, Dict[str, Any]] = {
    "neo4j": {
        "category": "graph",
        "read": ["cypher_query", "get_database_schema"],
        "write": ["cypher_query", "cypher_execute", "get_database_schema"],
    },
    "postgresql": {
        "category": "sql",
        "read": ["sql_query", "sql_schema"],
        "write": ["sql_query", "sql_schema", "sql_execute"],
    },
    "mongodb": {
        "category": "mongodb",
        "read": ["mongo_query", "mongo_aggregate", "mongo_schema"],
        "write": [
            "mongo_query",
            "mongo_aggregate",
            "mongo_schema",
            "mongo_insert",
            "mongo_update",
        ],
    },
    "webdav": {
        "category": "webdav",
        "read": ["webdav_list", "webdav_read", "webdav_info"],
        "write": [
            "webdav_list",
            "webdav_read",
            "webdav_info",
            "webdav_write",
            "webdav_delete",
        ],
    },
}


def datasource_tool_categories(
    datasources: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Map attached datasources to tool-category overrides.

    Semantics:

    - type not attached → category stripped (``[]``) so stale tools from a
      previously attached datasource never survive,
    - ALL datasources of a type read-only → read tools,
    - any read-write → write tools, backed by the real connection
      process_datasources() now creates for read-write connectors too.

    History: read-write managed connectors (postgresql/neo4j/mongodb) used
    to map to ``[]`` — "CLI mode" — which was dead on remote workspace
    backends and left them with no access path at all
    (docs/issues/datasource_cli_mode_dead_on_remote.md, fixed via
    direction 1: connection-backed write tools).
    """
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for ds in datasources:
        ds_type = ds.get("type")
        if ds_type:
            by_type.setdefault(ds_type, []).append(ds)

    categories: Dict[str, List[str]] = {}
    for ds_type, tool_info in DATASOURCE_TOOL_MAP.items():
        category = tool_info["category"]
        ds_list = by_type.get(ds_type, [])
        if not ds_list:
            categories[category] = []
        elif all(ds.get("project_read_only", False) for ds in ds_list):
            categories[category] = list(tool_info["read"])
        else:
            categories[category] = list(tool_info["write"])
    return categories


def process_datasources(
    ds_configs: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Process datasource configs and create connections/env vars.

    ALL managed connectors (postgresql, neo4j, mongodb, webdav) get a real
    tool connection now — read-write ones included. The former CLI-mode
    routing for read-write managed connectors (env vars / pg_service.conf,
    no connection) was a dead path on remote workspace backends: the env
    landed in the agent process while the shell runs on the workspace host,
    leaving read-write datasources with no access at all. See
    docs/issues/datasource_cli_mode_dead_on_remote.md (fix direction 1);
    the inject_* helpers below are kept for a future real CLI-forwarding
    feature but are no longer called from here.

    Repository datasources are NOT handled here — callers filter them out
    and clone via clone_repository_datasources() once the workspace backend
    is ready. Any repository entry that still reaches this function is
    skipped with a warning; there is no agent-local clone path.

    Args:
        ds_configs: List of datasource config dicts from the orchestrator.

    Returns:
        Tuple of (datasources_dict, client_registry, cli_ds_types):
        - datasources_dict: Connection objects keyed by type for ToolContext
        - client_registry: Parent clients (e.g. MongoClient) for cleanup
        - cli_ds_types: Always empty since the CLI-mode retirement; kept in
          the signature so callers' prompt-block plumbing (the future CLI
          feature's seam) stays in place.
    """
    datasources_dict: Dict[str, Any] = {}
    client_registry: Dict[str, Any] = {}
    cli_ds_types: List[str] = []

    generic_list: List[Dict[str, Any]] = []
    connector_list: List[Dict[str, Any]] = []

    for ds in ds_configs:
        ds_type = ds.get("type")
        if not ds_type:
            continue

        if ds_type == "generic":
            generic_list.append(ds)
        elif ds_type == "repository":
            logger.warning(
                "Repository datasource %r ignored by process_datasources(); "
                "repos clone onto the workspace backend via "
                "clone_repository_datasources().",
                ds.get("name", "unnamed"),
            )
        elif ds_type == "kb":
            # External OKF KBs are centrally indexed. The agent receives only
            # datasource id/display/config metadata and never opens a connector
            # or clones the repository.
            logger.debug(
                "OKF Knowledge Base %r handled by KnowledgeStore bindings",
                ds.get("name", "unnamed"),
            )
        else:
            connector_list.append(ds)

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

    # Managed connectors: create tool connections. The registry is
    # TYPE-keyed (last-one-wins), and datasource_tool_categories() binds
    # write tools when ANY datasource of a type is read-write — so process
    # read-only entries first and read-write last, ensuring the connection
    # that wins the type slot matches the granted tool surface (write tools
    # must never end up bound to a read-only-linked connection).
    for ds in sorted(
        connector_list, key=lambda d: not d.get("project_read_only", False)
    ):
        ds_type = ds["type"]
        try:
            conn, client = create_datasource_connection(ds)
            datasources_dict[ds_type] = conn
            if client:
                client_registry[ds_type] = client
            logger.info(
                "Connected to %s datasource: %s (%s)",
                ds_type,
                ds.get("name", "unnamed"),
                "read-only" if ds.get("project_read_only", False) else "read-write",
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


def resolve_repo_clone_names(
    repo_datasources: List[Dict[str, Any]],
) -> List[str]:
    """Return the clone-directory name for each repository datasource, in order.

    The directory under ``repos/`` uses the upstream repo name from the URL
    (falling back to the datasource-label slug), with a numeric suffix when
    two datasources resolve to the same name (e.g. forks of one upstream).
    Shared by clone_repository_datasources() and inject_datasource_index()
    so the datasources.md index always points at the real clone paths.
    """
    from ..utils.git_url import repo_name_from_url

    names: List[str] = []
    used: set[str] = set()
    for ds in repo_datasources:
        ds_slug = (
            re.sub(r"[^a-z0-9]+", "-", ds.get("name", "repo").lower()).strip("-")
            or "repo"
        )
        base = repo_name_from_url(ds.get("connection_url", ""), fallback=ds_slug)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        if name != base:
            logger.info(
                "Repo name collision for %s; cloning into %s instead", base, name
            )
        used.add(name)
        names.append(name)
    return names


def clone_repository_datasources(
    repo_datasources: List[Dict[str, Any]],
    workspace_manager: Any,
) -> None:
    """Clone repository datasources onto the workspace backend.

    Every operation runs on the workspace: SSH key material lands in the
    workspace home via ``backend.write_home_file``, host config via
    ``backend.shell_run``, and the clone itself is
    ``GitManager.clone(backend=...)`` (git on the workspace over SSH).

    There is deliberately NO agent-local fallback: without a shell-capable
    backend the datasources are skipped with an error. Repository
    datasources require a full workspace — lite tiers reject them at
    dispatch (docs/features/no_workspace_agent_mode.md §4/§7).

    Args:
        repo_datasources: Datasource config dicts of type "repository".
        workspace_manager: WorkspaceManager whose backend hosts the clones;
            successful clones are registered in its ``source_repos``.
    """
    if not repo_datasources:
        return

    backend = getattr(workspace_manager, "backend", None)
    if backend is None or not getattr(backend, "supports_shell", False):
        logger.error(
            "Repository datasources require a workspace backend with shell "
            "support; skipping %d repository datasource(s), no local clone "
            "fallback: %s",
            len(repo_datasources),
            ", ".join(ds.get("name", "unnamed") for ds in repo_datasources),
        )
        return

    from ..managers.git_manager import GitManager
    from ..utils.ssh_key import normalize_private_key

    clone_names = resolve_repo_clone_names(repo_datasources)
    for ds, repo_name in zip(repo_datasources, clone_names):
        # ds_name is the safe form of the user-supplied datasource label.
        # It stays the SSH key filename and SSH config alias so that two
        # datasources with different keys for the same repo don't clobber
        # each other's auth material.
        ds_name = (
            re.sub(r"[^a-z0-9]+", "-", ds.get("name", "repo").lower()).strip("-")
            or "repo"
        )
        try:
            repo_url = ds.get("connection_url", "")
            branch = ds.get("default_branch")
            creds = ds.get("credentials") or {}

            # Determine auth method: explicit field, or infer from
            # credentials keys (ssh_key present → ssh).
            auth_method = creds.get("auth_method")
            if not auth_method:
                if creds.get("ssh_key"):
                    auth_method = "ssh"
                elif creds.get("token"):
                    auth_method = "token"

            if auth_method == "ssh" and creds.get("ssh_key"):
                # Normalize defensively: orchestrator validation already
                # runs on save, but legacy rows in the datasources table
                # may predate it. Cheap insurance.
                ssh_key_text = normalize_private_key(creds["ssh_key"])

                parsed = urlparse(repo_url)
                host = parsed.hostname or "localhost"

                # Write SSH key and configure on the workspace.
                # write_home_file lands the key under $HOME without
                # tripping the workspace-boundary check on write_file;
                # resolve_home_path gives us the absolute path for the
                # subsequent chmod and SSH config IdentityFile entry.
                rel_key = f".ssh/repo_{ds_name}"
                key_path = backend.resolve_home_path(rel_key)
                backend.shell_run(
                    "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                    timeout=10,
                    tab_name="git",
                )
                backend.write_home_file(rel_key, ssh_key_text)
                backend.shell_run(
                    f"chmod 600 {shlex.quote(key_path)}",
                    timeout=10,
                    tab_name="git",
                )
                # Append SSH config for this host
                ssh_config = (
                    f"\nHost {host}\n"
                    f"  IdentityFile {key_path}\n"
                    f"  StrictHostKeyChecking accept-new\n"
                )
                backend.shell_run(
                    f"printf %s {shlex.quote(ssh_config)} >> ~/.ssh/config",
                    timeout=10,
                    tab_name="git",
                )

                # Convert HTTPS URL to SSH URL so git uses the key.
                # strip("/") handles trailing slashes too — datasource URLs
                # entered as `.../repo/` would otherwise become `repo/.git`,
                # which GitHub's SSH server rejects.
                if parsed.scheme in ("http", "https"):
                    path = parsed.path.strip("/")
                    if not path.endswith(".git"):
                        path += ".git"
                    repo_url = f"git@{host}:{path}"
                    logger.info(
                        "Converted HTTPS URL to SSH for %s: %s",
                        ds_name,
                        repo_url,
                    )

            elif (auth_method == "token" or not auth_method) and creds.get("token"):
                parsed = urlparse(repo_url)
                repo_url = parsed._replace(
                    netloc=f"oauth2:{creds['token']}@{parsed.hostname}"
                    + (f":{parsed.port}" if parsed.port else "")
                ).geturl()

            target = workspace_manager.path / "repos" / repo_name
            remote_cwd = f"repos/{repo_name}"
            git_mgr = GitManager.clone(
                repo_url,
                target,
                backend=backend,
                remote_cwd=remote_cwd,
            )
            if git_mgr:
                if branch:
                    git_mgr.checkout_branch(branch)
                workspace_manager.source_repos[repo_name] = git_mgr
                logger.info(
                    "Cloned repository datasource %r into repos/%s",
                    ds_name,
                    repo_name,
                )
            else:
                logger.warning(
                    "Failed to clone repository datasource %r (target repos/%s)",
                    ds_name,
                    repo_name,
                )
        except Exception as e:
            logger.warning(
                "Failed to clone repository datasource %s: %s",
                ds.get("name", "unnamed"),
                e,
            )


# ---------------------------------------------------------------------------
# Workspace index
# ---------------------------------------------------------------------------


def _declared_ro_note(ds: Dict[str, Any]) -> str:
    """Advisory suffix for publisher-declared read-only datasources.

    Declarative only (docs/features/public_datasources.md): credentials are
    the enforcement boundary; this just tells the agent the intent. Distinct
    from ``project_read_only`` (per-link connector mode), which switches the
    tool surface.
    """
    return " (declared read-only — treat as no-write)" if ds.get("read_only") else ""


def inject_datasource_index(
    ds_configs: List[Dict[str, Any]],
    workspace_manager: Any,
) -> None:
    """Inject a compact datasource index into datasources.md.

    Lists every datasource with its specific named access method so the
    agent knows how to connect to each one. The system prompts point the
    agent at datasources.md for connection names.

    Rewrites rather than re-appends: any existing "## Available Datasources"
    section (historically always the trailing section — it is only ever
    appended after workspace init) is cut before the new one is written, so
    live datasource changes (live_session_settings.md Slice B) re-running
    this injection never duplicate the index.
    """
    lines = ["\n\n## Available Datasources\n"]

    # Group by category for readable output
    repos = [ds for ds in ds_configs if ds.get("type") == "repository"]
    knowledge_bases = [ds for ds in ds_configs if ds.get("type") == "kb"]
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
            "kb",
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
        # Same name resolution as clone_repository_datasources() — the index
        # must point at the directories the clones actually land in.
        for ds, clone_name in zip(repos, resolve_repo_clone_names(repos)):
            lines.append(
                f"- **{ds.get('name')}** — cloned at `./repos/{clone_name}/`, "
                f"git pre-authenticated{_declared_ro_note(ds)}"
            )
        lines.append("")

    if knowledge_bases:
        lines.append("### OKF Knowledge Bases")
        for ds in knowledge_bases:
            name = ds.get("name", "Unnamed")
            root = str((ds.get("config") or {}).get("root_path") or "")
            root_hint = f", root `{root}`" if root else ""
            lines.append(
                f"- **{name}** (centrally indexed, read-only{root_hint}) — "
                "use `kb_search`, `kb_list`, and `kb_read`"
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
                lines.append(
                    f"- **{name}** ({ds_type}, read-write) — query + write tools"
                    + _declared_ro_note(ds)
                )
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
                lines.append(f"- **{name}** (generic) — {cli}{_declared_ro_note(ds)}")
            elif ds_type == "webdav":
                access = "read-only tools" if is_ro else "read-write tools"
                lines.append(f"- **{name}** (webdav, {access}){_declared_ro_note(ds)}")
            else:
                lines.append(f"- **{name}** ({ds_type}){_declared_ro_note(ds)}")
        lines.append("")

    if not ds_configs:
        # A live remove-all still needs the section rewritten — an agent
        # re-reading the file must not act on connection names that no
        # longer resolve.
        lines.append("_No datasources attached._")
        lines.append("")

    try:
        try:
            existing = workspace_manager.read_file("datasources.md")
        except (FileNotFoundError, ValueError, OSError):
            existing = ""
        marker = existing.find("## Available Datasources")
        if marker != -1:
            existing = existing[:marker].rstrip("\n")
        workspace_manager.write_file("datasources.md", existing + "\n".join(lines))
        logger.info(
            "Injected datasource index (%d entries) into datasources.md",
            len(ds_configs),
        )
    except Exception as e:
        logger.warning("Failed to inject datasource index: %s", e)


# NOTE: the former _format_rw_cli_entry (PGSERVICE/cypher-shell/mongosh usage
# lines for read-write connectors) was removed with the CLI-mode retirement —
# it advertised commands that cannot work on remote workspace backends. A
# future real CLI-forwarding feature reintroduces it properly
# (docs/issues/datasource_cli_mode_dead_on_remote.md, direction 2).
