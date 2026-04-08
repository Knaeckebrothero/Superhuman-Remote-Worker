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

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

CDP_PORT = 9222


BROWSER_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "browse_website": {
        "module": "research.browser",
        "function": "browse_website",
        "description": "Navigate website and extract information",
        "category": "research",
        "short_description": "Use browser to navigate and extract web content.",
        "phases": ["tactical"],
    },
    "download_from_website": {
        "module": "research.browser",
        "function": "download_from_website",
        "description": "Download file from website using browser automation",
        "category": "research",
        "short_description": "Navigate to URL and download file (PDF, etc.).",
        "phases": ["tactical"],
    },
}


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
        "pkill -f 'chromium.*remote-debugging-port' || true", timeout=5
    )

    # Start Chromium headless with CDP
    cmd = (
        "chromium-browser"
        " --headless=new"
        " --no-sandbox"
        " --disable-gpu"
        " --disable-dev-shm-usage"
        f" --remote-debugging-port={CDP_PORT}"
        " --remote-debugging-address=0.0.0.0"
        " --user-data-dir=/tmp/chromium-cdp-profile"
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
            "pkill -f 'chromium.*remote-debugging-port' || true", timeout=5
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
    1. browser.model in YAML config (via ToolContext)
    2. BROWSER_LLM_MODEL env var
    3. Fall back to gpt-4o (capable multimodal default)

    Similarly for api_key and base_url.

    Args:
        context: Optional ToolContext with browser config.

    Returns:
        A LangChain chat model instance
    """
    from langchain_openai import ChatOpenAI

    browser_cfg = context.config.get("browser", {}) if context else {}

    model = browser_cfg.get("model") or os.getenv("BROWSER_LLM_MODEL") or "gpt-4o"
    api_key = (
        browser_cfg.get("api_key")
        or os.getenv("BROWSER_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    base_url = browser_cfg.get("base_url") or os.getenv("BROWSER_LLM_BASE_URL")
    if not base_url and model.lower().startswith("openai/"):
        base_url = os.getenv("LLM_BASE_URL")

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


# ── Tool factory ──────────────────────────────────────────────────────


def create_browser_tools(context: ToolContext) -> List[Any]:
    """Create browser automation tools.

    Args:
        context: ToolContext with workspace_manager and config

    Returns:
        List of LangChain tool functions
    """

    @tool
    async def browse_website(
        url: str,
        task: str,
        use_vision: bool = False,
    ) -> str:
        """Delegate a browsing task to an autonomous browser agent.

        Best for simple extraction, multi-page research, or bulk URL processing.
        The browser agent runs independently and returns a text summary.
        For step-by-step interaction or visual verification, use browser_navigate instead.

        Args:
            url: Starting URL to navigate to
            task: Natural language description of what to do (e.g., "Find the abstract and authors")
            use_vision: Use screenshot-based navigation instead of DOM (default False, requires multimodal LLM)

        Returns:
            Extracted information or task completion status
        """
        try:
            from browser_use import Agent, Browser
        except ImportError:
            return (
                "Error: browser-use package not installed.\n"
                "Install with: pip install browser-use"
            )

        remote = _is_remote_browser(context)
        browser = None
        try:
            llm = _get_browser_llm(context)
            browser_kwargs = _get_browser_config(context)
            browser = Browser(**browser_kwargs)

            full_task = f"Go to {url} and {task}"
            agent = Agent(
                task=full_task,
                llm=llm,
                browser=browser,
                use_vision=use_vision,
                max_actions_per_step=4,
            )

            history = await agent.run()
            result = _extract_result(history)
            return result

        except Exception as e:
            logger.error(f"Browser automation error: {e}", exc_info=True)
            return f"Browser automation failed: {e}"
        finally:
            if browser is not None:
                try:
                    await browser.stop()
                except Exception:
                    pass
            if remote:
                _stop_remote_chromium(context.workspace_manager.backend)

    @tool
    async def download_from_website(
        url: str,
        download_task: str = "Find and click the download PDF button",
    ) -> str:
        """Download a file from a website using browser automation.

        Useful for publisher pages with JavaScript download buttons,
        pages requiring cookie acceptance, or complex navigation to
        reach download links.

        For paywalled content, ensure you're connected to institutional
        VPN or have proxy configured before using this tool.

        Args:
            url: Page URL containing the download link
            download_task: Instructions for finding and clicking the download (default: "Find and click the download PDF button")

        Returns:
            Path to downloaded file or error message
        """
        try:
            from browser_use import Agent, Browser
        except ImportError:
            return (
                "Error: browser-use package not installed.\n"
                "Install with: pip install browser-use"
            )

        remote = _is_remote_browser(context)
        browser = None
        try:
            llm = _get_browser_llm(context)

            if remote:
                browser_kwargs = _get_browser_config(context)
            else:
                dest_dir = _get_documents_dir(context)
                dest_dir.mkdir(parents=True, exist_ok=True)
                browser_kwargs = _get_browser_config(context, downloads_path=dest_dir)

            browser = Browser(**browser_kwargs)

            full_task = (
                f"Go to {url} and {download_task}. Wait for the download to complete."
            )
            agent = Agent(
                task=full_task,
                llm=llm,
                browser=browser,
                use_vision=False,
                max_actions_per_step=4,
            )

            history = await agent.run()

            # Check for downloaded files
            if remote:
                backend = context.workspace_manager.backend
                new_files = _find_new_files_remote(backend, "documents")
                if new_files:
                    rel_path = new_files[0]
                    file_size = backend.stat(rel_path)
                    file_name = Path(rel_path).name
                    _register_downloaded_file(context, rel_path, name=file_name)
                    return (
                        f"Downloaded file: {file_name}\n"
                        f"Path: {rel_path}\n"
                        f"Size: {file_size:,} bytes"
                    )
            else:
                downloaded_files = _find_new_files(dest_dir)
                if downloaded_files:
                    downloaded_path = downloaded_files[0]
                    _register_downloaded_file(
                        context,
                        str(downloaded_path),
                        name=downloaded_path.name,
                    )
                    return (
                        f"Downloaded file: {downloaded_path.name}\n"
                        f"Path: {downloaded_path}\n"
                        f"Size: {downloaded_path.stat().st_size:,} bytes"
                    )

            result = _extract_result(history)
            return f"Download may have failed. Agent report:\n{result}"

        except Exception as e:
            logger.error(f"Browser download error: {e}", exc_info=True)
            return f"Browser download failed: {e}"
        finally:
            if browser is not None:
                try:
                    await browser.stop()
                except Exception:
                    pass
            if remote:
                _stop_remote_chromium(context.workspace_manager.backend)

    return [browse_website, download_from_website]


# ── Helpers ───────────────────────────────────────────────────────────


def _extract_result(history) -> str:
    """Extract the final result from browser-use agent history.

    Args:
        history: AgentHistory from browser-use agent.run()

    Returns:
        Formatted result string
    """
    if history is None:
        return "Browser agent completed but returned no result."

    # browser-use returns an AgentHistoryList
    # Try to get the final result
    try:
        # The history object has a final_result() method in newer versions
        if hasattr(history, "final_result"):
            result = history.final_result()
            if result:
                return str(result)

        # Fall back to getting the last action result
        if hasattr(history, "history") and history.history:
            last_entry = history.history[-1]
            if hasattr(last_entry, "result"):
                result = last_entry.result
                if result:
                    # Truncate if too long
                    result_str = str(result)
                    if len(result_str) > 5000:
                        return result_str[:5000] + "\n... (truncated)"
                    return result_str

        # Try string representation as last resort
        result_str = str(history)
        if len(result_str) > 5000:
            return result_str[:5000] + "\n... (truncated)"
        return result_str

    except Exception as e:
        logger.debug(f"Could not extract result from history: {e}")
        return "Browser agent completed. Check workspace documents for any downloaded files."


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
