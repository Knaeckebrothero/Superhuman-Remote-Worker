"""Per-user code-server IDE settings store.

Persists a user's small code-server config (``settings.json``,
``keybindings.json``, snippets) under ``users.settings['ide']['files']`` and
reconciles config pulled from workspaces using filesystem mtimes — newest wins,
per file.

Storage shape::

    users.settings = {
        "ide": {"files": {
            "settings.json":        {"content": "...", "mtime": 1716800000.0},
            "keybindings.json":     {"content": "...", "mtime": 1716800012.0},
            "snippets/python.json": {"content": "...", "mtime": 1716799000.0},
        }},
        # ...other unrelated user settings live alongside "ide"...
    }

``PostgresDB.update_user_settings`` is a SHALLOW top-level ``||`` JSONB merge, so
writing ``{"ide": ...}`` replaces the whole ``ide`` value rather than deep-merging
it. To avoid clobbering sibling files (or sibling ``ide`` sub-keys) we
read-modify-write the entire ``ide`` subtree. This is safe because the only
writer — the reconciler sweeper — is single-threaded.

Conflict resolution is purely mtime-based: an applied file wins only if its mtime
is strictly newer than what is stored. That makes ``apply_pulled_files`` order-
independent across workspaces — the newest edit wins no matter which workspace is
reconciled first.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# code-server's user-data dir (set via --user-data-dir in the workspace image).
CODE_SERVER_USER_DIR = "/var/lib/code-server/User"
# Top-level config files we track (snippets/* are discovered dynamically).
TRACKED_FILES = ("settings.json", "keybindings.json")
SNIPPETS_SUBDIR = "snippets"

# Extensions live in a separate tree from the User config dir.
EXTENSIONS_DIR = "/var/lib/code-server/extensions"
GLOBAL_STORAGE_DIR = f"{CODE_SERVER_USER_DIR}/globalStorage"
# Sentinel the entrypoint waits on while the orchestrator streams license/
# globalStorage state into a freshly-provisioned workspace (Phase B).
SEED_STATE_SENTINEL = "/var/lib/code-server/.ide-seed-state-done"

# SSH ports: workspace containers run sshd on 30022; VMs on 22.
DEFAULT_WS_SSH_PORT = 30022
DEFAULT_VM_SSH_PORT = 22

# Markers framing each file in the remote pull script's stdout. Content is
# base64-encoded between them so arbitrary bytes/newlines survive transport.
_FILE_MARKER = "__SRWFILE__"
_END_MARKER = "__SRWEND__"


def build_pull_script() -> str:
    """Remote shell script that emits each tracked config file with its mtime.

    Output is a sequence of ``__SRWFILE__<name>\\t<mtime>`` headers, the file's
    base64 body, and an ``__SRWEND__`` terminator. Names are relative to the
    code-server User dir, so snippets come through as ``snippets/<file>``.
    """
    return (
        f"cd {CODE_SERVER_USER_DIR} 2>/dev/null || exit 0\n"
        f"for f in {' '.join(TRACKED_FILES)} {SNIPPETS_SUBDIR}/*; do\n"
        '  [ -f "$f" ] || continue\n'
        '  m=$(stat -c %Y "$f" 2>/dev/null) || continue\n'
        f'  printf \'{_FILE_MARKER}%s\\t%s\\n\' "$f" "$m"\n'
        '  base64 "$f"\n'
        f"  printf '{_END_MARKER}\\n'\n"
        "done\n"
    )


def parse_pull_output(stdout: str) -> dict[str, dict]:
    """Parse :func:`build_pull_script` output into ``{name: {content, mtime}}``.

    Malformed sections (bad base64, non-numeric mtime, non-utf8) are skipped
    rather than raising — a single corrupt file must not abort the whole pull.
    """
    result: dict[str, dict] = {}
    lines = stdout.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith(_FILE_MARKER):
            i += 1
            continue
        name, _, mtime_s = line[len(_FILE_MARKER) :].partition("\t")
        i += 1
        body: list[str] = []
        while i < n and lines[i] != _END_MARKER:
            body.append(lines[i].strip())
            i += 1
        try:
            content = base64.b64decode("".join(body)).decode("utf-8")
            mtime = float(mtime_s)
        except (binascii.Error, ValueError, UnicodeDecodeError):
            logger.warning("ide_settings: skipping unparseable config file %r", name)
        else:
            if name:
                result[name] = {"content": content, "mtime": mtime}
        i += 1  # step past the end marker
    return result


_EXT_THEME_FLAG = "THEME"


def build_extensions_list_script() -> str:
    """Remote shell: emit one ``<publisher>.<name>@<version>\\t<THEME|->`` line per
    installed extension. The theme flag is set when the extension's package.json
    declares a ``"themes"`` contribution, so the seed step can install theme
    providers first. Parses package.json with line-wise sed (top-level fields are
    one-per-line in published extensions); robust enough for ordering/inventory.
    """
    return (
        f"cd {EXTENSIONS_DIR} 2>/dev/null || exit 0\n"
        "for d in */ ; do\n"
        '  pj="${d%/}/package.json"\n'
        '  [ -f "$pj" ] || continue\n'
        '  pub=$(sed -n \'s/.*"publisher"[: ]*"\\([^"]*\\)".*/\\1/p\' "$pj" | head -1)\n'
        '  nm=$(sed -n \'s/.*"name"[: ]*"\\([^"]*\\)".*/\\1/p\' "$pj" | head -1)\n'
        '  ver=$(sed -n \'s/.*"version"[: ]*"\\([^"]*\\)".*/\\1/p\' "$pj" | head -1)\n'
        '  [ -n "$pub" ] && [ -n "$nm" ] && [ -n "$ver" ] || continue\n'
        '  flag="-"\n'
        f'  grep -q \'"themes"\' "$pj" && flag="{_EXT_THEME_FLAG}"\n'
        '  printf \'%s.%s@%s\\t%s\\n\' "$pub" "$nm" "$ver" "$flag"\n'
        "done\n"
    )


def parse_extensions_list(stdout: str) -> dict[str, dict]:
    """Parse :func:`build_extensions_list_script` output into
    ``{ext_id: {"version": str, "theme": bool}}``. Lines without a tab are skipped.
    """
    result: dict[str, dict] = {}
    for line in stdout.split("\n"):
        if "\t" not in line:
            continue
        id_ver, _, flag = line.partition("\t")
        ext_id, _, version = id_ver.rpartition("@")
        if not ext_id or not version:
            continue
        result[ext_id] = {"version": version, "theme": flag.strip() == _EXT_THEME_FLAG}
    return result


def build_signature_script() -> str:
    """Remote shell: a cheap content signature over the extensions dir and
    globalStorage (paths + sizes + mtimes), hashed. Used to skip byte-copy when
    nothing changed. ``find -printf`` is GNU; falls back to ``ls -laR`` if absent.
    """
    targets = f"{EXTENSIONS_DIR} {GLOBAL_STORAGE_DIR}"
    return (
        f"if find {targets} -maxdepth 0 >/dev/null 2>&1; then\n"
        f"  (find {targets} -printf '%p %s %T@\\n' 2>/dev/null "
        f"   || ls -laR {targets} 2>/dev/null) | sort | sha256sum\n"
        "else echo ''; fi\n"
    )


def parse_signature(stdout: str) -> str:
    """Take the first whitespace-delimited token (the sha256 hex) from the
    signature script's stdout; empty string when there's nothing to hash."""
    return stdout.strip().split()[0] if stdout.strip() else ""


OPEN_VSX_API = "https://open-vsx.org/api"

# Fetch signature: (url) -> http_status_int
VsxFetch = Callable[[str], Awaitable[int]]


async def _default_vsx_fetch(url: str) -> int:
    """GET an Open VSX API URL; return the HTTP status. Runs urllib in a thread to
    stay dependency-light (no aiohttp import at module load)."""
    import urllib.error
    import urllib.request

    def _get() -> int:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    return await asyncio.to_thread(_get)


class OpenVsxClassifier:
    """Classify an extension as installable from Open VSX (``"openvsx"``) or
    requiring byte-copy (``"bytes"``). Caches by (id, version). On any error,
    defaults to ``"bytes"`` — the safe side (we'll carry the bytes ourselves)."""

    def __init__(self, fetch: Optional[VsxFetch] = None) -> None:
        self._fetch = fetch or _default_vsx_fetch
        self._cache: dict[tuple[str, str], str] = {}

    async def classify(self, ext_id: str, version: str) -> str:
        key = (ext_id, version)
        if key in self._cache:
            return self._cache[key]
        ns, _, name = ext_id.partition(".")
        url = f"{OPEN_VSX_API}/{ns}/{name}/{version}"
        try:
            status = await self._fetch(url)
            source = "openvsx" if status == 200 else "bytes"
        except Exception:  # noqa: BLE001 — classification must never raise
            source = "bytes"
        self._cache[key] = source
        return source


def resolve_ssh_target(context: dict) -> Optional[tuple[str, int]]:
    """Resolve a workspace context to an ``(ssh_host, ssh_port)`` pair.

    Mirrors the precedence the snapshot/suspension path uses, extended to
    restored IDE sessions: container → VM, preferring an in-cluster pod IP over a
    VM's Tailscale ssh_host. Returns None when no reachable target exists.
    """
    ws = context.get("workspace_container") or {}
    vm = context.get("vm") or {}
    ide = context.get("ide_session") or {}

    host = (
        ws.get("pod_ip")
        or ws.get("host")
        or ide.get("pod_ip")
        or vm.get("ssh_host")
        or vm.get("pod_ip")
    )
    if not host:
        return None

    if ws.get("pod_ip") or ws.get("host"):
        port = ws.get("port") or DEFAULT_WS_SSH_PORT
    elif ide.get("pod_ip"):
        port = ide.get("ssh_port") or DEFAULT_WS_SSH_PORT
    else:
        port = vm.get("ssh_port") or DEFAULT_VM_SSH_PORT
    return host, int(port)


def _shq(s: str) -> str:
    """POSIX single-quote-escape ``s`` for safe embedding in a shell command."""
    return "'" + s.replace("'", "'\\''") + "'"


def build_seed_script(files: dict[str, dict]) -> str:
    """Shell script that writes ``files`` into the code-server User dir.

    Content is delivered base64-encoded (binary-safe), each file's mtime is set
    with ``touch -d @<mtime>`` so an untouched seeded session never looks newer
    than the store, and ownership is reset to ``agent-host`` at the end. This same
    script is both mounted into workspace containers (as a ConfigMap ``seed.sh``
    the entrypoint runs) and piped over SSH to VMs on ready / IDE-session restore.
    """
    if not files:
        return "exit 0\n"

    parts: list[str] = [f"mkdir -p {CODE_SERVER_USER_DIR}/{SNIPPETS_SUBDIR}\n"]
    for name, entry in files.items():
        path = f"{CODE_SERVER_USER_DIR}/{name}"
        qpath = _shq(path)
        b64 = base64.b64encode((entry.get("content") or "").encode("utf-8")).decode(
            "ascii"
        )
        parts.append(f'mkdir -p "$(dirname {qpath})"\n')
        parts.append(f"base64 -d > {qpath} <<'__SRWB64__'\n{b64}\n__SRWB64__\n")
        mtime = entry.get("mtime")
        if mtime is not None:
            parts.append(f"touch -d @{mtime} {qpath}\n")
    parts.append(f"chown -R agent-host:agent-host {CODE_SERVER_USER_DIR}\n")
    return "".join(parts)


def build_extension_install_script(items: dict[str, dict]) -> str:
    """Shell that installs the user's Open-VSX extensions via the code-server CLI,
    run as ``agent-host``. Theme providers install **synchronously first** so the
    color theme is present when code-server first paints; the rest install in the
    background. Only ``source == "openvsx"`` items are handled here — ``bytes``
    items arrive via the orchestrator state stream (Phase B). Best-effort: a
    single failed install must not abort the rest (``|| true``)."""
    openvsx = {k: v for k, v in items.items() if v.get("source") == "openvsx"}
    if not openvsx:
        return "exit 0\n"

    def _install(ext_id: str, version: str) -> str:
        ref = _shq(f"{ext_id}@{version}")
        return (
            f"su -c 'code-server --install-extension {ref} "
            f"--extensions-dir {EXTENSIONS_DIR}' agent-host || true\n"
        )

    themes = [(k, v["version"]) for k, v in openvsx.items() if v.get("theme")]
    rest = [(k, v["version"]) for k, v in openvsx.items() if not v.get("theme")]

    parts = [f"mkdir -p {EXTENSIONS_DIR}\n"]
    for ext_id, version in themes:  # synchronous, theme-first
        parts.append(_install(ext_id, version))
    if rest:  # background the long tail
        parts.append("(\n")
        for ext_id, version in rest:
            parts.append(_install(ext_id, version))
        parts.append(f"chown -R agent-host:agent-host {EXTENSIONS_DIR}\n")
        parts.append(") &\n")
    parts.append(f"chown -R agent-host:agent-host {EXTENSIONS_DIR}\n")
    return "".join(parts)


# Runner signature: (host, port, remote_script, key_path, timeout) -> (rc, stdout, stderr)
SshRunner = Callable[..., Awaitable[tuple[int, Any, Any]]]


async def _default_ssh_runner(
    host: str, port: int, script: str, key_path: Optional[str] = None, timeout: int = 20
) -> tuple[int, bytes, bytes]:
    """Run ``script`` on ``agent-host@host`` over SSH; return (rc, stdout, stderr)."""
    # Lazy import keeps this module import-light (no ssh/subprocess deps at import
    # time), mirroring ssh_helpers' own design.
    from services.ssh_helpers import build_agent_ssh_cmd

    cmd = build_agent_ssh_cmd(host, port, script, key_path=key_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout, stderr


async def pull_ide_config(
    ssh_host: str,
    ssh_port: int,
    *,
    key_path: Optional[str] = None,
    timeout: int = 20,
    _runner: Optional[SshRunner] = None,
) -> dict[str, dict]:
    """Read the code-server config files from a workspace over SSH.

    Returns ``{name: {content, mtime}}`` (possibly empty). Never raises — any SSH
    failure, timeout, or non-zero exit yields an empty dict so the reconciler can
    skip an unreachable workspace and move on.
    """
    runner = _runner or _default_ssh_runner
    try:
        rc, stdout, stderr = await runner(
            ssh_host, ssh_port, build_pull_script(), key_path=key_path, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001 — must not abort the sweep
        logger.warning(
            "ide_settings: pull failed for %s:%s — %s", ssh_host, ssh_port, e
        )
        return {}
    if rc != 0:
        err = (
            stderr.decode("utf-8", "replace")
            if isinstance(stderr, (bytes, bytearray))
            else (stderr or "")
        )
        logger.info(
            "ide_settings: pull rc=%s for %s:%s — %s", rc, ssh_host, ssh_port, err[:200]
        )
        return {}
    text = (
        stdout.decode("utf-8", "replace")
        if isinstance(stdout, (bytes, bytearray))
        else (stdout or "")
    )
    return parse_pull_output(text)


async def seed_ide_config(
    ssh_host: str,
    ssh_port: int,
    files: dict[str, dict],
    *,
    key_path: Optional[str] = None,
    timeout: int = 20,
    _runner: Optional[SshRunner] = None,
) -> bool:
    """Write the user's stored config into a workspace over SSH (restore path).

    Returns True on success. Never raises — a failed seed must not block restore.
    """
    if not files:
        return True
    runner = _runner or _default_ssh_runner
    try:
        rc, _stdout, stderr = await runner(
            ssh_host,
            ssh_port,
            build_seed_script(files),
            key_path=key_path,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ide_settings: seed failed for %s:%s — %s", ssh_host, ssh_port, e
        )
        return False
    if rc != 0:
        err = (
            stderr.decode("utf-8", "replace")
            if isinstance(stderr, (bytes, bytearray))
            else (stderr or "")
        )
        logger.warning(
            "ide_settings: seed rc=%s for %s:%s — %s", rc, ssh_host, ssh_port, err[:200]
        )
        return False
    return True


async def seed_ide_config_for_user(
    db: Any,
    user_id: Optional[str],
    ssh_host: str,
    ssh_port: int,
    *,
    key_path: Optional[str] = None,
    _runner: Optional[SshRunner] = None,
) -> bool:
    """Seed a user's stored code-server config + extensions into a workspace over
    SSH.

    Convenience wrapper used by the VM-ready and IDE-session-restore paths
    (containers seed via ConfigMap instead). Writes the config files and installs
    the user's Open-VSX extensions (theme-first). No-ops cleanly when there's no
    user or nothing stored. Never raises.
    """
    if not user_id:
        return True
    store = IdeSettingsStore(db)
    files = await store.get_ide_files(str(user_id))
    extensions = await store.get_extensions(str(user_id))
    if not files and not extensions:
        return True
    runner = _runner or _default_ssh_runner
    script = (
        build_seed_script(files) + "\n" + build_extension_install_script(extensions)
    )
    try:
        rc, _out, stderr = await runner(
            ssh_host, ssh_port, script, key_path=key_path, timeout=60
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ide_settings: seed-for-user failed for %s:%s — %s", ssh_host, ssh_port, e
        )
        return False
    if rc != 0:
        err = (
            stderr.decode("utf-8", "replace")
            if isinstance(stderr, (bytes, bytearray))
            else (stderr or "")
        )
        logger.warning(
            "ide_settings: seed-for-user rc=%s for %s:%s — %s",
            rc,
            ssh_host,
            ssh_port,
            err[:200],
        )
        return False
    return True


PullFn = Callable[[str, int], Awaitable[dict]]


def _coerce_context(context: Any) -> dict:
    """Accept a context that may arrive as a dict or a JSON string."""
    if isinstance(context, str):
        try:
            return json.loads(context)
        except (json.JSONDecodeError, TypeError):
            return {}
    return context or {}


async def reconcile_ide_settings(
    store: IdeSettingsStore, workspaces: list[dict], pull_fn: PullFn
) -> int:
    """Pull config from each active workspace and merge into per-user storage.

    ``workspaces`` is a list of ``{"user_id", "context"}`` rows. Each is resolved
    to an SSH target, pulled, and applied. Order-independent: the store keeps the
    newest mtime per file, so two workspaces of the same user converge on the
    latest edit regardless of iteration order. A failure on one workspace is
    logged and skipped — it never aborts the sweep.

    Returns the total number of files updated across all users this cycle.
    """
    updated_total = 0
    for ws in workspaces:
        user_id = ws.get("user_id")
        if not user_id:
            continue
        target = resolve_ssh_target(_coerce_context(ws.get("context")))
        if not target:
            continue
        host, port = target
        try:
            pulled = await pull_fn(host, port)
        except Exception as e:  # noqa: BLE001 — one bad workspace must not abort
            logger.warning(
                "ide_settings: reconcile pull failed for %s:%s — %s", host, port, e
            )
            continue
        if not pulled:
            continue
        try:
            updated = await store.apply_pulled_files(str(user_id), pulled)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ide_settings: reconcile apply failed for user %s — %s", user_id, e
            )
            continue
        if updated:
            logger.info(
                "ide_settings: updated %d file(s) for user %s from %s",
                len(updated),
                user_id,
                host,
            )
        updated_total += len(updated)
    return updated_total


# List-fn signature: (host, port) -> {id: {version, theme}}
ListFn = Callable[[str, int], Awaitable[dict]]


async def list_ide_extensions(
    ssh_host: str,
    ssh_port: int,
    *,
    key_path: Optional[str] = None,
    timeout: int = 20,
    _runner: Optional[SshRunner] = None,
) -> dict[str, dict]:
    """SSH into a workspace and return installed extensions. Never raises."""
    runner = _runner or _default_ssh_runner
    try:
        rc, stdout, _ = await runner(
            ssh_host,
            ssh_port,
            build_extensions_list_script(),
            key_path=key_path,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ide_settings: ext-list failed for %s:%s — %s", ssh_host, ssh_port, e
        )
        return {}
    if rc != 0:
        return {}
    text = (
        stdout.decode("utf-8", "replace")
        if isinstance(stdout, (bytes, bytearray))
        else (stdout or "")
    )
    return parse_extensions_list(text)


async def reconcile_extensions(
    store: "IdeSettingsStore",
    workspaces: list[dict],
    list_fn: ListFn,
    classifier: "OpenVsxClassifier",
) -> int:
    """For each workspace, list extensions, classify each (openvsx|bytes), and
    merge into the user's manifest. Returns the count of ids added/bumped.
    Order-independent and failure-isolated like ``reconcile_ide_settings``."""
    changed_total = 0
    for ws in workspaces:
        user_id = ws.get("user_id")
        if not user_id:
            continue
        target = resolve_ssh_target(_coerce_context(ws.get("context")))
        if not target:
            continue
        host, port = target
        try:
            listed = await list_fn(host, port)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ide_settings: ext reconcile list failed for %s:%s — %s", host, port, e
            )
            continue
        if not listed:
            continue
        items: dict[str, dict] = {}
        for ext_id, info in listed.items():
            source = await classifier.classify(ext_id, info.get("version", ""))
            items[ext_id] = {
                "version": info.get("version", ""),
                "source": source,
                "theme": bool(info.get("theme")),
            }
        try:
            changed = await store.apply_extensions(str(user_id), items)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ide_settings: ext reconcile apply failed for user %s — %s", user_id, e
            )
            continue
        changed_total += len(changed)
    return changed_total


def _ver_key(v: str) -> tuple:
    """Sort key for version strings: numeric-aware, falls back to string parts.
    ``"2.0.13"`` > ``"2.0.9"``; non-numeric segments compare lexicographically."""
    parts = []
    for seg in str(v).replace("-", ".").split("."):
        parts.append((0, int(seg)) if seg.isdigit() else (1, seg))
    return tuple(parts)


class IdeSettingsStore:
    """Read/write per-user code-server config in ``users.settings['ide']``."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def get_ide_files(self, user_id: str) -> dict[str, dict]:
        """Return the stored config files: ``{name: {"content", "mtime"}}``.

        Empty dict when the user has no stored IDE config.
        """
        settings = await self._db.get_user_settings(user_id)
        if not isinstance(settings, dict):
            return {}
        ide = settings.get("ide")
        files = ide.get("files") if isinstance(ide, dict) else None
        return dict(files) if isinstance(files, dict) else {}

    async def apply_pulled_files(
        self, user_id: str, pulled: dict[str, dict]
    ) -> list[str]:
        """Merge freshly-pulled config files into the user's store.

        ``pulled`` maps a file name (e.g. ``"settings.json"``,
        ``"snippets/python.json"``) to ``{"content": str, "mtime": float}``. A
        file is written only if it is new or its mtime is strictly newer than the
        stored copy. Returns the names actually updated.
        """
        if not pulled:
            return []

        settings = await self._db.get_user_settings(user_id)
        if not isinstance(settings, dict):
            settings = {}
        ide = (
            dict(settings.get("ide") or {})
            if isinstance(settings.get("ide"), dict)
            else {}
        )
        files = (
            dict(ide.get("files") or {}) if isinstance(ide.get("files"), dict) else {}
        )

        updated: list[str] = []
        for name, entry in pulled.items():
            mtime = entry.get("mtime")
            if mtime is None:
                continue
            existing = files.get(name)
            if existing is None or mtime > existing.get("mtime", float("-inf")):
                files[name] = {"content": entry.get("content", ""), "mtime": mtime}
                updated.append(name)

        if not updated:
            return []

        ide["files"] = files
        await self._db.update_user_settings(user_id, {"ide": ide})
        return updated

    async def get_extensions(self, user_id: str) -> dict[str, dict]:
        """Return the stored extension manifest items: ``{id: {version, source, theme}}``."""
        settings = await self._db.get_user_settings(user_id)
        if not isinstance(settings, dict):
            return {}
        ide = settings.get("ide")
        exts = ide.get("extensions") if isinstance(ide, dict) else None
        items = exts.get("items") if isinstance(exts, dict) else None
        return dict(items) if isinstance(items, dict) else {}

    async def apply_extensions(self, user_id: str, items: dict[str, dict]) -> list[str]:
        """Merge a workspace's installed extensions into the user's manifest.

        Union across workspaces, newest-version-wins per id (so an extension
        present only in workspace B survives a reconcile of workspace A). Returns
        the ids added or version-bumped. Read-modify-writes the whole ``ide``
        subtree because ``update_user_settings`` is a shallow merge.
        """
        if not items:
            return []
        settings = await self._db.get_user_settings(user_id)
        if not isinstance(settings, dict):
            settings = {}
        ide = (
            dict(settings.get("ide") or {})
            if isinstance(settings.get("ide"), dict)
            else {}
        )
        exts = (
            dict(ide.get("extensions") or {})
            if isinstance(ide.get("extensions"), dict)
            else {}
        )
        stored = (
            dict(exts.get("items") or {}) if isinstance(exts.get("items"), dict) else {}
        )

        changed: list[str] = []
        for ext_id, entry in items.items():
            version = entry.get("version")
            if not version:
                continue
            prev = stored.get(ext_id)
            if prev is None or _ver_key(version) > _ver_key(prev.get("version", "")):
                stored[ext_id] = {
                    "version": version,
                    "source": entry.get("source", "bytes"),
                    "theme": bool(entry.get("theme", False)),
                }
                changed.append(ext_id)

        if not changed:
            return []
        exts["items"] = stored
        ide["extensions"] = exts
        await self._db.update_user_settings(user_id, {"ide": ide})
        return changed
