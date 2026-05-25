"""Direct browser control tools — step-by-step page interaction.

Gives the agent fine-grained control over a persistent Chromium instance:
navigate, inspect DOM, click, type, select, scroll, screenshot, back, close.

Execution model:
  * Remote workspace (the normal case): each action is dispatched to the
    workspace-side ``browser-exec`` daemon over SSH via
    ``ToolContext.browser_exec()``. Chrome's CDP stays on the workspace
    loopback — it never crosses the pod boundary. See
    docs/features/browser_workspace_executor.md.
  * Local/dev (no remote workspace): browser-use runs in-process here.

Either way, this module keeps the agent-side logic — URL validation and
content-nonce wrapping — and returns the same shape to the LLM:
``{dom, url, title, screenshot?, tabs?}`` with numbered ``[N]`` element refs.

Vision mode (screenshots in results) auto-selects from the model's
multimodal capability (settings_matrix.yaml).

Requires: pip install browser-use>=0.11.0 (local mode only; the workspace
image ships browser-use for the remote daemon).
"""

import asyncio
import base64
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from .browser import _is_remote_browser
from .browser_security import validate_url_with_config, wrap_with_nonce
from ..context import ToolContext

logger = logging.getLogger(__name__)


# ── Tool metadata for registry ───────────────────────────────────────

BROWSER_DIRECT_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "browser_navigate": {
        "module": "research.browser_direct",
        "function": "browser_navigate",
        "description": (
            "Open a URL in the browser and see the page. Use when you need "
            "to inspect, interact with, or visually verify a specific page."
        ),
        "category": "browser_direct",
        "short_description": "Navigate to URL and return page DOM + screenshot.",
        "phases": ["tactical"],
    },
    "browser_snapshot": {
        "module": "research.browser_direct",
        "function": "browser_snapshot",
        "description": (
            "Get the current page state — DOM accessibility tree and optional "
            "screenshot. Use after actions to see what changed."
        ),
        "category": "browser_direct",
        "short_description": "Return current page DOM snapshot + screenshot.",
        "phases": ["tactical"],
    },
    "browser_click": {
        "module": "research.browser_direct",
        "function": "browser_click",
        "description": "Click an element on the page by its reference number from the DOM snapshot.",
        "category": "browser_direct",
        "short_description": "Click element by DOM reference number.",
        "phases": ["tactical"],
    },
    "browser_type": {
        "module": "research.browser_direct",
        "function": "browser_type",
        "description": "Type text into an input field identified by its reference number.",
        "category": "browser_direct",
        "short_description": "Type text into input field by reference number.",
        "phases": ["tactical"],
    },
    "browser_select": {
        "module": "research.browser_direct",
        "function": "browser_select",
        "description": "Select an option from a dropdown by its reference number.",
        "category": "browser_direct",
        "short_description": "Select dropdown option by reference number.",
        "phases": ["tactical"],
    },
    "browser_scroll": {
        "module": "research.browser_direct",
        "function": "browser_scroll",
        "description": (
            "Scroll the page or a specific element. Direction: up, down, left, right."
        ),
        "category": "browser_direct",
        "short_description": "Scroll page or element in a direction.",
        "phases": ["tactical"],
    },
    "browser_screenshot": {
        "module": "research.browser_direct",
        "function": "browser_screenshot",
        "description": "Take a screenshot of the current page. Always returns an image regardless of multimodal setting.",
        "category": "browser_direct",
        "short_description": "Take full screenshot of current page.",
        "phases": ["tactical"],
    },
    "browser_back": {
        "module": "research.browser_direct",
        "function": "browser_back",
        "description": "Navigate back to the previous page in browser history.",
        "category": "browser_direct",
        "short_description": "Go back one page in browser history.",
        "phases": ["tactical"],
    },
    "browser_close": {
        "module": "research.browser_direct",
        "function": "browser_close",
        "description": "Close the browser and free resources. The browser will restart on next use.",
        "category": "browser_direct",
        "short_description": "Close the browser session.",
        "phases": ["tactical"],
    },
}


# ── Local (in-process) execution — dev mode only ─────────────────────
#
# These mirror the workspace-side browser-exec daemon. On a remote workspace
# the agent never runs them; it calls ToolContext.browser_exec() instead.


async def _local_page_state(session: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Build raw page state (DOM + optional screenshot). No nonce wrap here."""
    from browser_use.dom.service import DomService

    dom_service = DomService(browser_session=session)
    serialized_state, _, _ = await dom_service.get_serialized_dom_tree()

    dom_text = serialized_state.llm_representation()
    max_chars = int(args.get("max_dom_chars", 40000))
    if len(dom_text) > max_chars:
        dom_text = dom_text[:max_chars] + "\n... (DOM truncated)"

    result: Dict[str, Any] = {
        "url": await session.get_current_page_url(),
        "title": await session.get_current_page_title(),
        "dom": dom_text,
    }

    if args.get("include_screenshot"):
        try:
            screenshot_bytes = await session.take_screenshot()
            result["screenshot"] = base64.b64encode(screenshot_bytes).decode()
        except Exception as e:
            logger.debug(f"Screenshot failed: {e}")

    try:
        tabs = await session.get_tabs()
        if tabs and len(tabs) > 1:
            result["tabs"] = [
                {"id": t.id, "url": t.url, "title": t.title} for t in tabs
            ]
    except Exception:
        pass

    return result


async def _click_element(session: Any, ref: int) -> None:
    from browser_use.browser.events import ClickElementEvent

    node = await session.get_element_by_index(ref)
    if node is None:
        raise ValueError(
            f"Element ref={ref} not found. Use browser_snapshot to get current refs."
        )
    event = session.event_bus.dispatch(ClickElementEvent(node=node))
    await event
    await event.event_result(raise_if_any=True)


async def _type_text(session: Any, ref: int, text: str, clear: bool = True) -> None:
    from browser_use.browser.events import TypeTextEvent

    node = await session.get_element_by_index(ref)
    if node is None:
        raise ValueError(
            f"Element ref={ref} not found. Use browser_snapshot to get current refs."
        )
    event = session.event_bus.dispatch(TypeTextEvent(node=node, text=text, clear=clear))
    await event
    await event.event_result(raise_if_any=True)


async def _select_option(session: Any, ref: int, value: str) -> None:
    from browser_use.browser.events import SelectDropdownOptionEvent

    node = await session.get_element_by_index(ref)
    if node is None:
        raise ValueError(
            f"Element ref={ref} not found. Use browser_snapshot to get current refs."
        )
    event = session.event_bus.dispatch(
        SelectDropdownOptionEvent(node=node, option=value)
    )
    await event
    await event.event_result(raise_if_any=True)


async def _scroll(
    session: Any, direction: str, amount: int = 500, ref: Optional[int] = None
) -> None:
    from browser_use.browser.events import ScrollEvent

    node = None
    if ref is not None:
        node = await session.get_element_by_index(ref)
    event = session.event_bus.dispatch(
        ScrollEvent(direction=direction, amount=amount, node=node)
    )
    await event
    await event.event_result(raise_if_any=True)


async def _local_action(
    context: ToolContext, action: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Execute one action in-process against a local BrowserSession."""
    session = await context.get_browser_session()

    if action == "navigate":
        await session.navigate_to(args["url"])
        return await _local_page_state(session, args)
    if action == "snapshot":
        return await _local_page_state(session, args)
    if action == "screenshot":
        screenshot_bytes = await session.take_screenshot()
        return {
            "url": await session.get_current_page_url(),
            "title": await session.get_current_page_title(),
            "screenshot": base64.b64encode(screenshot_bytes).decode(),
        }
    if action == "click":
        await _click_element(session, int(args["ref"]))
        return await _local_page_state(session, args)
    if action == "type":
        await _type_text(
            session,
            int(args["ref"]),
            args.get("text", ""),
            clear=args.get("clear", True),
        )
        return await _local_page_state(session, args)
    if action == "select":
        await _select_option(session, int(args["ref"]), args["value"])
        return await _local_page_state(session, args)
    if action == "scroll":
        ref = args.get("ref")
        await _scroll(
            session,
            args.get("direction", "down"),
            int(args.get("amount", 500)),
            int(ref) if ref is not None else None,
        )
        return await _local_page_state(session, args)
    if action == "back":
        page = await session.get_current_page()
        if page is not None:
            await page.evaluate("() => { window.history.back(); }")
            await asyncio.sleep(0.5)
        return await _local_page_state(session, args)

    return {"error": f"unknown action: {action}"}


async def _run_action(context: ToolContext, action: str, **args: Any) -> Dict[str, Any]:
    """Dispatch a browser action: remote → browser-exec, local → in-process."""
    if _is_remote_browser(context):
        return await context.browser_exec(action, **args)
    try:
        return await _local_action(context, action, args)
    except Exception as e:
        logger.error(f"local browser {action} failed: {e}", exc_info=True)
        return {"error": f"{action} failed: {e}"}


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
    async def browser_navigate(url: str) -> dict:
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
            return {"error": str(e)}

        result = await _run_action(context, "navigate", url=url, **_state_args())
        return result if "error" in result else wrap_with_nonce(result)

    @tool
    async def browser_snapshot() -> dict:
        """Get the current page state — DOM tree and optional screenshot.

        Use this after performing actions (click, type, scroll) to see
        what changed on the page. The DOM contains numbered references
        like [42]<button>Submit</button> for use with other browser tools.

        Returns:
            Page state with dom, url, title, and optionally screenshot
        """
        result = await _run_action(context, "snapshot", **_state_args())
        return result if "error" in result else wrap_with_nonce(result)

    @tool
    async def browser_click(ref: int) -> dict:
        """Click an element on the page by its reference number.

        Reference numbers come from the DOM snapshot (e.g., [42]<button>).
        After clicking, returns the updated page state.

        Args:
            ref: Element reference number from the DOM snapshot

        Returns:
            Updated page state after the click
        """
        result = await _run_action(context, "click", ref=ref, **_state_args())
        return result if "error" in result else wrap_with_nonce(result)

    @tool
    async def browser_type(ref: int, text: str, clear: bool = True) -> dict:
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
        return result if "error" in result else wrap_with_nonce(result)

    @tool
    async def browser_select(ref: int, value: str) -> dict:
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
        return result if "error" in result else wrap_with_nonce(result)

    @tool
    async def browser_scroll(
        direction: str = "down",
        amount: int = 500,
        ref: Optional[int] = None,
    ) -> dict:
        """Scroll the page or a specific element.

        Args:
            direction: Scroll direction — "up", "down", "left", or "right"
            amount: Pixels to scroll (default 500)
            ref: Optional element reference to scroll within (default: page)

        Returns:
            Updated page state after scrolling
        """
        if direction not in ("up", "down", "left", "right"):
            return {"error": f"Invalid direction: {direction}. Use up/down/left/right."}

        result = await _run_action(
            context,
            "scroll",
            direction=direction,
            amount=amount,
            ref=ref,
            **_state_args(),
        )
        return result if "error" in result else wrap_with_nonce(result)

    @tool
    async def browser_screenshot() -> dict:
        """Take a screenshot of the current page.

        Always returns an image regardless of the multimodal setting.
        Use this when you need to visually inspect the page even if
        regular snapshots don't include screenshots.

        Returns:
            Dict with screenshot (base64), url, and title
        """
        return await _run_action(context, "screenshot")

    @tool
    async def browser_back() -> dict:
        """Navigate back to the previous page in browser history.

        Returns:
            Updated page state after navigating back
        """
        result = await _run_action(context, "back", **_state_args())
        return result if "error" in result else wrap_with_nonce(result)

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
