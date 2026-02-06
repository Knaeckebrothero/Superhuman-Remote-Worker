"""Browser automation tools using browser-use.

Provides tools for navigating websites and downloading files using
AI-driven browser automation. Supports both DOM-based (text-only LLM)
and vision-based (multimodal LLM) modes.

Requires: pip install browser-use playwright
          playwright install chromium
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


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


def _get_browser_llm():
    """Create an LLM instance for browser-use agent.

    Uses BROWSER_LLM_MODEL env var (default: gpt-4o-mini).
    Falls back to OPENAI_API_KEY for authentication.

    Returns:
        A LangChain chat model instance
    """
    from langchain_openai import ChatOpenAI

    model = os.getenv("BROWSER_LLM_MODEL", "gpt-4o-mini")
    api_key = os.getenv("BROWSER_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("BROWSER_LLM_BASE_URL") or os.getenv("LLM_BASE_URL")

    kwargs = {
        "model": model,
        "temperature": 0.0,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    return ChatOpenAI(**kwargs)


def _get_browser_config(context: ToolContext, downloads_path: Optional[Path] = None) -> Dict[str, Any]:
    """Build browser configuration kwargs.

    Reads settings from agent config (extra.browser section) and env vars.

    Args:
        context: ToolContext with config
        downloads_path: Override download directory

    Returns:
        Dict of kwargs for Browser() constructor
    """
    from .utils.network import ProxyConfig

    # Get browser config from agent config extras
    browser_config = context.config.get("browser", {})

    # Headless mode: config -> env -> default True
    headless_env = os.getenv("BROWSER_HEADLESS", "").lower()
    if headless_env in ("true", "1", "yes"):
        headless = True
    elif headless_env in ("false", "0", "no"):
        headless = False
    else:
        headless = browser_config.get("headless", True)

    # Downloads directory
    if downloads_path is None:
        if context.has_workspace():
            downloads_path = Path(context.workspace_manager.workspace_dir) / "documents"
        else:
            downloads_path = Path("./downloads")

    # Ensure downloads directory exists
    downloads_path.mkdir(parents=True, exist_ok=True)

    kwargs: Dict[str, Any] = {
        "headless": headless,
        "accept_downloads": True,
        "downloads_path": str(downloads_path),
        "auto_download_pdfs": True,
    }

    # Proxy configuration (uses browser-use ProxySettings)
    proxy_config_data = context.config.get("research", {}).get("proxy", {})
    proxy = ProxyConfig.from_config(proxy_config_data)
    if proxy.is_configured:
        browser_use_proxy = proxy.to_browser_use_proxy()
        if browser_use_proxy:
            kwargs["proxy"] = browser_use_proxy
            logger.info(f"Browser using proxy: {proxy.type.value}://{proxy.host}:{proxy.port}")

    return kwargs


def _get_documents_dir(context: ToolContext) -> Path:
    """Get the documents directory from workspace, or a fallback."""
    if context.has_workspace():
        return Path(context.workspace_manager.workspace_dir) / "documents"
    return Path("./downloads")


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
        """Navigate a website and complete a task using browser automation.

        Uses an AI agent to interact with the page via DOM/accessibility tree
        (default) or screenshots (vision mode). Works with dynamic JavaScript
        pages, cookie banners, and complex navigation.

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
                "Install with: pip install browser-use && playwright install chromium"
            )

        browser = None
        try:
            # Create LLM for browser agent
            llm = _get_browser_llm()

            # Configure browser
            browser_kwargs = _get_browser_config(context)
            browser = Browser(**browser_kwargs)

            # Build full task with starting URL
            full_task = f"Go to {url} and {task}"

            # Create and run browser agent
            agent = Agent(
                task=full_task,
                llm=llm,
                browser=browser,
                use_vision=use_vision,
                max_actions_per_step=4,
            )

            history = await agent.run()

            # Extract final result from history
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
                "Install with: pip install browser-use && playwright install chromium"
            )

        dest_dir = _get_documents_dir(context)
        dest_dir.mkdir(parents=True, exist_ok=True)

        browser = None
        try:
            # Create LLM for browser agent
            llm = _get_browser_llm()

            # Configure browser with downloads path
            browser_kwargs = _get_browser_config(context, downloads_path=dest_dir)
            browser = Browser(**browser_kwargs)

            # Build download task
            full_task = (
                f"Go to {url} and {download_task}. "
                f"Wait for the download to complete."
            )

            # Create and run browser agent
            agent = Agent(
                task=full_task,
                llm=llm,
                browser=browser,
                use_vision=False,  # DOM-based is more reliable for downloads
                max_actions_per_step=4,
            )

            history = await agent.run()

            # Check for downloaded files
            downloaded_files = _find_new_files(dest_dir)
            if downloaded_files:
                # Register the first downloaded file as a citation source
                downloaded_path = downloaded_files[0]
                _register_downloaded_file(context, downloaded_path)
                return (
                    f"Downloaded file: {downloaded_path.name}\n"
                    f"Path: {downloaded_path}\n"
                    f"Size: {downloaded_path.stat().st_size:,} bytes"
                )

            # No files found - return what the agent reported
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

    return [browse_website, download_from_website]


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
    """Find recently created files in directory.

    Args:
        directory: Directory to scan
        max_age_seconds: Maximum file age in seconds

    Returns:
        List of recently created file paths, sorted by modification time (newest first)
    """
    import time

    now = time.time()
    new_files = []

    if not directory.exists():
        return []

    for path in directory.iterdir():
        if path.is_file() and (now - path.stat().st_mtime) < max_age_seconds:
            new_files.append(path)

    return sorted(new_files, key=lambda p: p.stat().st_mtime, reverse=True)


def _register_downloaded_file(context: ToolContext, file_path: Path) -> None:
    """Register a downloaded file as a citation source.

    Args:
        context: ToolContext with citation engine
        file_path: Path to the downloaded file
    """
    try:
        source_id = context.get_or_register_doc_source(
            str(file_path), name=file_path.name
        )
        logger.info(f"Registered downloaded file as citation source {source_id}: {file_path.name}")
    except Exception as e:
        logger.debug(f"Could not register downloaded file as citation source: {e}")
