"""Direct browser control tools — step-by-step page interaction.

Gives the agent fine-grained control over a persistent Chromium instance:
navigate, inspect DOM, click, type, select, scroll, screenshot, back, close.

Execution model: every action is dispatched to the workspace-side
``browser-exec`` daemon over SSH via ``ToolContext.browser_exec()``.
Chrome's CDP stays on the workspace loopback — it never crosses the pod
boundary — and page content is interpreted inside the NetworkPolicy-
restricted workspace runtime. There is no in-pod (agent-side) browser
execution path. See knowledge-base/knowledge/features/browser_workspace_executor.md and
knowledge-base/knowledge/issues/remove_local_browser_fallback.md.

This module keeps the agent-side logic — URL validation and content-nonce
wrapping — and returns the page state to the LLM as a text block (URL,
title, nonce-wrapped DOM with numbered ``[N]`` element refs). A screenshot,
when present, is emitted as an ``<image_data>`` tag so the graph-side
``extract_image_tags`` post-processor delivers it as a real image content
block (and strips the base64 from the text). Returning a string rather
than a dict is deliberate — see ``_page_state_to_text``.

Vision mode (screenshots in results) auto-selects from the model's
multimodal capability (config/model_config_matrix.yaml ``settings``).
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from .browser_security import validate_url_with_config, wrap_with_nonce
from ..context import ToolContext
from ...services.image_content import IMAGE_DATA_TAG_TEMPLATE

from src.shared.tool_catalog.definitions import (
    BROWSER_DIRECT_TOOLS_METADATA as BROWSER_DIRECT_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)


# ── Tool metadata for registry ───────────────────────────────────────


async def _run_action(context: ToolContext, action: str, **args: Any) -> Dict[str, Any]:
    """Dispatch a browser action to the workspace browser-exec daemon.

    There is deliberately no local fallback: if the workspace cannot run
    browser-exec, the daemon path returns a clear error — actions never
    degrade to in-pod execution.
    """
    return await context.browser_exec(action, **args)


def _page_state_to_text(result: Dict[str, Any]) -> str:
    """Render a browser-exec result dict to the string the LLM receives.

    A screenshot (base64 PNG) is emitted as an ``<image_data>`` tag so the
    graph-side ``extract_image_tags`` post-processor lifts it into a real
    image content block and strips the base64 from the text. The value is
    returned as a *string* (not the raw dict) on purpose: the persistent
    loop ``str()``s tool output and the worker ``ToolNode`` ``json.dumps``es
    it — either of which would escape an in-dict tag and defeat extraction.
    """
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        if result.get("error") == "user_is_driving":
            lines = ["Browser action refused because the user currently has control."]
            url = result.get("url")
            if isinstance(url, str) and url:
                lines.append(f"Current URL: {url}")
            message = result.get("message")
            if isinstance(message, str) and message:
                lines.append(message)
            lines.append(
                "Read-only browser snapshots still work. Ask the user to release "
                "browser control before trying an interactive action again."
            )
            return "\n".join(lines)
        return f"Browser error: {result['error']}"

    wrapped = wrap_with_nonce(result)
    lines: List[str] = []
    for key, label in (("url", "URL"), ("title", "Title")):
        value = wrapped.get(key)
        if value:
            lines.append(f"{label}: {value}")
    baton = wrapped.get("baton")
    if baton in {"agent", "user"}:
        lines.append(f"Browser control: {baton}")
    tabs = wrapped.get("tabs")
    if tabs:
        lines.append(f"Open tabs: {tabs}")
    dom = wrapped.get("dom")
    if dom:
        lines.append(str(dom))
    text = "\n".join(lines)

    screenshot = result.get("screenshot")
    if screenshot:
        tag = IMAGE_DATA_TAG_TEMPLATE.format(mime="image/png", b64=screenshot)
        text = f"{text}\n{tag}" if text else tag
    return text


# ── Tool factory ─────────────────────────────────────────────────────


def create_browser_direct_tools(context: ToolContext) -> List[Any]:
    """Create direct browser control tools with injected context.

    Args:
        context: ToolContext with config and workspace_manager

    Returns:
        List of LangChain tool functions
    """
    browser_cfg = context.config.get("browser", {})

    def _state_args() -> Dict[str, Any]:
        """Per-call flags that shape page-state results (computed agent-side)."""
        return {
            "include_screenshot": context.should_include_screenshots(),
            "max_dom_chars": context.get_max_dom_chars(),
        }

    @tool
    async def browser_navigate(url: str) -> str:
        """Open a URL in the browser and see the page.

        Returns the page's DOM accessibility tree (with numbered element
        references) and optionally a screenshot. Use the reference numbers
        with browser_click, browser_type, and browser_select.

        Args:
            url: The URL to navigate to (must be http/https)

        Returns:
            Page state with dom, url, title, and optionally screenshot
        """
        try:
            url = validate_url_with_config(url, browser_cfg)
        except ValueError as e:
            return f"Browser error: {e}"

        result = await _run_action(context, "navigate", url=url, **_state_args())
        return _page_state_to_text(result)

    @tool
    async def browser_snapshot() -> str:
        """Get the current page state — DOM tree and optional screenshot.

        Use this after performing actions (click, type, scroll) to see
        what changed on the page. The DOM contains numbered references
        like [42]<button>Submit</button> for use with other browser tools.

        Returns:
            Page state with dom, url, title, and optionally screenshot
        """
        result = await _run_action(context, "snapshot", **_state_args())
        return _page_state_to_text(result)

    @tool
    async def browser_click(ref: int) -> str:
        """Click an element on the page by its reference number.

        Reference numbers come from the DOM snapshot (e.g., [42]<button>).
        After clicking, returns the updated page state.

        Args:
            ref: Element reference number from the DOM snapshot

        Returns:
            Updated page state after the click
        """
        result = await _run_action(context, "click", ref=ref, **_state_args())
        return _page_state_to_text(result)

    @tool
    async def browser_type(ref: int, text: str, clear: bool = True) -> str:
        """Type text into an input field.

        Args:
            ref: Element reference number of the input field
            text: Text to type
            clear: Clear existing content first (default True)

        Returns:
            Updated page state after typing
        """
        result = await _run_action(
            context, "type", ref=ref, text=text, clear=clear, **_state_args()
        )
        return _page_state_to_text(result)

    @tool
    async def browser_select(ref: int, value: str) -> str:
        """Select an option from a dropdown element.

        Args:
            ref: Element reference number of the <select> element
            value: The option text or value to select

        Returns:
            Updated page state after selection
        """
        result = await _run_action(
            context, "select", ref=ref, value=value, **_state_args()
        )
        return _page_state_to_text(result)

    @tool
    async def browser_scroll(
        direction: str = "down",
        amount: int = 500,
        ref: Optional[int] = None,
    ) -> str:
        """Scroll the page or a specific element.

        Args:
            direction: Scroll direction — "up", "down", "left", or "right"
            amount: Pixels to scroll (default 500)
            ref: Optional element reference to scroll within (default: page)

        Returns:
            Updated page state after scrolling
        """
        if direction not in ("up", "down", "left", "right"):
            return f"Browser error: Invalid direction: {direction}. Use up/down/left/right."

        result = await _run_action(
            context,
            "scroll",
            direction=direction,
            amount=amount,
            ref=ref,
            **_state_args(),
        )
        return _page_state_to_text(result)

    @tool
    async def browser_screenshot() -> str:
        """Take a screenshot of the current page.

        Always returns an image regardless of the multimodal setting.
        Use this when you need to visually inspect the page even if
        regular snapshots don't include screenshots.

        Returns:
            Dict with screenshot (base64), url, and title
        """
        return _page_state_to_text(await _run_action(context, "screenshot"))

    @tool
    async def browser_back() -> str:
        """Navigate back to the previous page in browser history.

        Returns:
            Updated page state after navigating back
        """
        result = await _run_action(context, "back", **_state_args())
        return _page_state_to_text(result)

    @tool
    async def browser_close() -> str:
        """Close the browser and free resources.

        The browser will restart automatically on the next browser tool
        call. Use this when you're done browsing to free memory.

        Returns:
            Confirmation message
        """
        try:
            await context.close_browser()
            return "Browser closed. It will restart on next use."
        except Exception as e:
            logger.error(f"browser_close failed: {e}", exc_info=True)
            return f"Close failed: {e}"

    return [
        browser_navigate,
        browser_snapshot,
        browser_click,
        browser_type,
        browser_select,
        browser_scroll,
        browser_screenshot,
        browser_back,
        browser_close,
    ]
