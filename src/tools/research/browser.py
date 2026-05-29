"""Browser automation tools using browser-use.

Provides tools for navigating websites and downloading files using
AI-driven browser automation. Supports both DOM-based (text-only LLM)
and vision-based (multimodal LLM) modes.

When the workspace uses a remote backend (container pod or VM), Chromium
is started on the workspace and controlled via CDP over the network.
Downloads land directly on the workspace filesystem.

Requires: pip install browser-use
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


from ..context import ToolContext

logger = logging.getLogger(__name__)

CDP_PORT = 9222


BROWSER_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {}
# Autonomous browser sub-agent tools (browse_website / download_from_website)
# were deprecated in favor of the direct browser_* tools, driven by the main
# agent via the workspace browser-exec daemon. The helpers below are retained
# because papers.py still uses them for its PDF-download fallback.
# See docs/features/browser_workspace_executor.md.


# ── Remote Chromium lifecycle ─────────────────────────────────────────


def _is_remote_browser(context: ToolContext) -> bool:
    """Check if browser should run on the remote workspace."""
    browser_config = context.config.get("browser", {})
    remote_mode = browser_config.get("remote", "auto")
    if remote_mode == "local":
        return False

    if not context.has_workspace():
        return False

    backend = context.workspace_manager.backend
    return backend.host is not None


def _start_remote_chromium(backend, downloads_path: str) -> str:
    """Start Chromium on the remote workspace and return the CDP URL.

    Kills any leftover Chromium, starts a fresh headless instance with
    ``--remote-debugging-port``, and polls until the CDP endpoint is ready.

    Args:
        backend: WorkspaceBackend with exec_command and host.
        downloads_path: Absolute path on the remote host for downloads.

    Returns:
        CDP WebSocket URL (e.g. ``ws://10.42.0.50:9222/devtools/browser/...``).

    Raises:
        RuntimeError: If Chromium fails to start within the timeout.
    """
    host = backend.host

    # Kill any existing Chromium (idempotent)
    backend.exec_command(
        "pkill -f 'agent-chromium.*remote-debugging-port' || true", timeout=5
    )

    # Start Chromium headless with CDP. `agent-chromium` is a stable symlink
    # to the Playwright-bundled binary, provisioned by docker/agent-vm-base
    # /scripts/provision-stage1.sh and docker/Dockerfile.workspace.
    cmd = (
        "agent-chromium"
        " --headless=new"
        " --no-sandbox"
        " --disable-gpu"
        " --disable-dev-shm-usage"
        f" --remote-debugging-port={CDP_PORT}"
        " --remote-debugging-address=0.0.0.0"
        " --user-data-dir=/tmp/agent-chromium-cdp-profile"
    )
    backend.exec_command(f"nohup {cmd} > /tmp/chromium-cdp.log 2>&1 &", timeout=10)

    # Poll for the CDP WebSocket URL
    for attempt in range(10):
        time.sleep(0.5)
        try:
            output = backend.exec_command(
                f"curl -s http://localhost:{CDP_PORT}/json/version", timeout=5
            )
            if "webSocketDebuggerUrl" in output:
                data = json.loads(output)
                ws_url = data["webSocketDebuggerUrl"]
                # Replace loopback with actual host so agent pod can reach it
                ws_url = ws_url.replace("localhost", host).replace("127.0.0.1", host)
                logger.info(f"Remote Chromium ready at {ws_url}")
                return ws_url
        except Exception:
            continue

    raise RuntimeError(
        f"Chromium failed to start on {host}:{CDP_PORT} (checked {10} times over 5s)"
    )


def _stop_remote_chromium(backend) -> None:
    """Stop Chromium on the remote workspace."""
    try:
        backend.exec_command(
            "pkill -f 'agent-chromium.*remote-debugging-port' || true", timeout=5
        )
    except Exception:
        pass


def _find_new_files_remote(
    backend, relative_dir: str, max_age_seconds: int = 120
) -> List[str]:
    """Find recently created files on a remote workspace.

    Args:
        backend: WorkspaceBackend with exec_command.
        relative_dir: Directory relative to workspace root.
        max_age_seconds: Maximum file age.

    Returns:
        List of workspace-relative paths, newest first.
    """
    try:
        abs_dir = backend.resolve_path(relative_dir)
        minutes = max(max_age_seconds / 60, 0.1)
        output = backend.exec_command(
            f"find {abs_dir} -maxdepth 1 -type f "
            f"-mmin -{minutes:.1f} -printf '%T@ %p\\n' 2>/dev/null | sort -rn",
            timeout=10,
        )
        files = []
        root = backend.root.rstrip("/")
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                abs_path = parts[1]
                if abs_path.startswith(root):
                    rel = abs_path[len(root) :].lstrip("/")
                    files.append(rel)
        return files
    except Exception as e:
        logger.debug(f"Remote file scan failed: {e}")
        return []


# ── LLM + config helpers ─────────────────────────────────────────────


def _get_browser_llm(context: Optional[ToolContext] = None):
    """Create an LLM instance for browser-use sub-agent.

    Resolution order:
    1. ``browser.model`` / ``browser.base_url`` / ``browser.api_key`` in the
       ToolContext config (populated from ``job.config_override`` when the
       orchestrator dispatches a job)
    2. Legacy env vars (``BROWSER_LLM_MODEL``, ``BROWSER_LLM_BASE_URL``,
       ``BROWSER_LLM_API_KEY`` / ``OPENAI_API_KEY``) — deprecated; a
       DeprecationWarning is emitted whenever one is used
    3. Hard default of ``gpt-4o`` for the model (capable multimodal) and the
       OpenAI-hosted URL for the base

    Args:
        context: Optional ToolContext with browser config.

    Returns:
        A LangChain chat model instance
    """
    import warnings

    from langchain_openai import ChatOpenAI

    browser_cfg = context.config.get("browser", {}) if context else {}

    model = browser_cfg.get("model")
    if not model:
        env_model = os.getenv("BROWSER_LLM_MODEL")
        if env_model:
            warnings.warn(
                "BROWSER_LLM_MODEL env var is deprecated — configure the "
                "browser model via the agent's YAML `browser.model` or the "
                "orchestrator's `config_override`.",
                DeprecationWarning,
                stacklevel=2,
            )
            model = env_model
        else:
            model = "gpt-4o"

    api_key = (
        browser_cfg.get("api_key")
        or os.getenv("BROWSER_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )

    base_url = browser_cfg.get("base_url")
    if not base_url:
        env_url = os.getenv("BROWSER_LLM_BASE_URL")
        if env_url:
            warnings.warn(
                "BROWSER_LLM_BASE_URL env var is deprecated — register the "
                "browser endpoint via Admin → Providers (or pass "
                "`browser.base_url` in config_override).",
                DeprecationWarning,
                stacklevel=2,
            )
            base_url = env_url
        elif model.lower().startswith("openai/"):
            legacy = os.getenv("LLM_BASE_URL")
            if legacy:
                warnings.warn(
                    "LLM_BASE_URL fallback for browser tools is deprecated — "
                    "set `browser.base_url` explicitly.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                base_url = legacy

    kwargs = {
        "model": model,
        "temperature": 0.0,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    return ChatOpenAI(**kwargs)


def _get_browser_config(
    context: ToolContext, downloads_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Build browser configuration kwargs.

    For remote backends: starts Chromium on the workspace and returns a
    ``cdp_url`` for Browser() to connect to.
    For local backends: returns headless/downloads config (existing behavior).

    Args:
        context: ToolContext with config and workspace_manager.
        downloads_path: Override download directory (local mode only).

    Returns:
        Dict of kwargs for Browser() constructor. Contains either
        ``cdp_url`` (remote) or ``headless``/``downloads_path`` (local).
    """
    from .utils.network import ProxyConfig

    browser_config = context.config.get("browser", {})

    # ── Remote browser (workspace pod / VM) ──
    if _is_remote_browser(context):
        backend = context.workspace_manager.backend
        remote_docs = backend.resolve_path("documents")

        # Ensure documents dir exists on remote
        backend.mkdir("documents")

        cdp_url = _start_remote_chromium(backend, remote_docs)

        kwargs: Dict[str, Any] = {"cdp_url": cdp_url}
        return kwargs

    # ── Local browser (existing behavior) ──
    headless_env = os.getenv("BROWSER_HEADLESS", "").lower()
    if headless_env in ("true", "1", "yes"):
        headless = True
    elif headless_env in ("false", "0", "no"):
        headless = False
    else:
        headless = browser_config.get("headless", True)

    if downloads_path is None:
        if context.has_workspace():
            downloads_path = context.workspace_manager.get_path("documents")
        else:
            downloads_path = Path("./downloads")

    # Ensure downloads directory exists
    downloads_path.mkdir(parents=True, exist_ok=True)

    kwargs = {
        "headless": headless,
        "accept_downloads": True,
        "downloads_path": str(downloads_path),
        "auto_download_pdfs": True,
    }

    # Proxy configuration (local browser only — remote Chromium handles its own network)
    proxy_config_data = context.config.get("research", {}).get("proxy", {})
    proxy = ProxyConfig.from_config(proxy_config_data)
    if proxy.is_configured:
        browser_use_proxy = proxy.to_browser_use_proxy()
        if browser_use_proxy:
            kwargs["proxy"] = browser_use_proxy
            logger.info(
                f"Browser using proxy: {proxy.type.value}://{proxy.host}:{proxy.port}"
            )

    return kwargs


def _get_documents_dir(context: ToolContext) -> Path:
    """Get the documents directory from workspace, or a fallback."""
    if context.has_workspace():
        return context.workspace_manager.get_path("documents")
    return Path("./downloads")


# ── Helpers ───────────────────────────────────────────────────────────


def _find_new_files(directory: Path, max_age_seconds: int = 60) -> List[Path]:
    """Find recently created files in a local directory.

    Args:
        directory: Directory to scan
        max_age_seconds: Maximum file age in seconds

    Returns:
        List of recently created file paths, sorted by modification time (newest first)
    """
    now = time.time()
    new_files = []

    if not directory.exists():
        return []

    for path in directory.iterdir():
        if path.is_file() and (now - path.stat().st_mtime) < max_age_seconds:
            new_files.append(path)

    return sorted(new_files, key=lambda p: p.stat().st_mtime, reverse=True)


def _register_downloaded_file(
    context: ToolContext, file_path: str, name: str = ""
) -> None:
    """Register a downloaded file as a citation source.

    Works for both local paths and workspace-relative paths (remote).

    Args:
        context: ToolContext with citation engine.
        file_path: Path to the file (absolute local or workspace-relative).
        name: Display name for the citation source.
    """
    try:
        source_id = context.get_or_register_doc_source(
            file_path, name=name or Path(file_path).name
        )
        logger.info(
            f"Registered downloaded file as citation source {source_id}: "
            f"{name or file_path}"
        )
    except Exception as e:
        logger.debug(f"Could not register downloaded file as citation source: {e}")
