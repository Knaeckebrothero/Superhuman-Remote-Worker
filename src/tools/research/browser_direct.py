"""Direct browser control tools — step-by-step page interaction.

Gives the agent fine-grained control over a persistent Chromium instance:
navigate, inspect DOM, click, type, select, scroll, screenshot, back, close.

Vision mode (screenshots in results) auto-selects based on the model's
multimodal capability from settings_matrix.yaml.

Uses browser-use's BrowserSession for low-level CDP interaction. The
session is managed by ToolContext (lazy-start, health-check, reconnect).

Requires: pip install browser-use>=0.11.0
"""

import base64
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

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


# ── Helpers ──────────────────────────────────────────────────────────


async def _get_page_state(context: ToolContext, session: Any) -> Dict[str, Any]:
    """Get current page state (DOM + optional screenshot)."""
    from browser_use.dom.service import DomService

    dom_service = DomService(browser_session=session)
    serialized_state, _, _ = await dom_service.get_serialized_dom_tree()

    dom_text = serialized_state.llm_representation()
    max_chars = context.get_max_dom_chars()
    if len(dom_text) > max_chars:
        dom_text = dom_text[:max_chars] + "\n... (DOM truncated)"

    url = await session.get_current_page_url()
    title = await session.get_current_page_title()

    result: Dict[str, Any] = {
        "url": url,
        "title": title,
        "dom": dom_text,
    }

    # Include screenshot if model supports vision
    if context.should_include_screenshots():
        try:
            screenshot_bytes = await session.take_screenshot()
            result["screenshot"] = base64.b64encode(screenshot_bytes).decode()
        except Exception as e:
            logger.debug(f"Screenshot failed: {e}")

    # Include tab list
    try:
        tabs = await session.get_tabs()
        if tabs and len(tabs) > 1:
            result["tabs"] = [
                {"id": t.id, "url": t.url, "title": t.title} for t in tabs
            ]
    except Exception:
        pass

    return wrap_with_nonce(result)


async def _click_element(session: Any, ref: int) -> None:
    """Click an element by its index in the selector map."""
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
    """Type text into an element by its index."""
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
    """Select a dropdown option by element index."""
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
    """Scroll the page or a specific element."""
    from browser_use.browser.events import ScrollEvent

    node = None
    if ref is not None:
        node = await session.get_element_by_index(ref)

    event = session.event_bus.dispatch(
        ScrollEvent(direction=direction, amount=amount, node=node)
    )
    await event
    await event.event_result(raise_if_any=True)


# ── Tool factory ─────────────────────────────────────────────────────


def create_browser_direct_tools(context: ToolContext) -> List[Any]:
    """Create direct browser control tools with injected context.

    Args:
        context: ToolContext with config and workspace_manager

    Returns:
        List of LangChain tool functions
    """
    browser_cfg = context.config.get("browser", {})

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

        try:
            session = await context.get_browser_session()
            await session.navigate_to(url)
            return await _get_page_state(context, session)
        except Exception as e:
            logger.error(f"browser_navigate failed: {e}", exc_info=True)
            return {"error": f"Navigation failed: {e}"}

    @tool
    async def browser_snapshot() -> dict:
        """Get the current page state — DOM tree and optional screenshot.

        Use this after performing actions (click, type, scroll) to see
        what changed on the page. The DOM contains numbered references
        like [42]<button>Submit</button> for use with other browser tools.

        Returns:
            Page state with dom, url, title, and optionally screenshot
        """
        try:
            session = await context.get_browser_session()
            return await _get_page_state(context, session)
        except Exception as e:
            logger.error(f"browser_snapshot failed: {e}", exc_info=True)
            return {"error": f"Snapshot failed: {e}"}

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
        try:
            session = await context.get_browser_session()
            await _click_element(session, ref)
            return await _get_page_state(context, session)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"browser_click failed: {e}", exc_info=True)
            return {"error": f"Click failed: {e}"}

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
        try:
            session = await context.get_browser_session()
            await _type_text(session, ref, text, clear=clear)
            return await _get_page_state(context, session)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"browser_type failed: {e}", exc_info=True)
            return {"error": f"Type failed: {e}"}

    @tool
    async def browser_select(ref: int, value: str) -> dict:
        """Select an option from a dropdown element.

        Args:
            ref: Element reference number of the <select> element
            value: The option text or value to select

        Returns:
            Updated page state after selection
        """
        try:
            session = await context.get_browser_session()
            await _select_option(session, ref, value)
            return await _get_page_state(context, session)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"browser_select failed: {e}", exc_info=True)
            return {"error": f"Select failed: {e}"}

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

        try:
            session = await context.get_browser_session()
            await _scroll(session, direction, amount, ref)
            return await _get_page_state(context, session)
        except Exception as e:
            logger.error(f"browser_scroll failed: {e}", exc_info=True)
            return {"error": f"Scroll failed: {e}"}

    @tool
    async def browser_screenshot() -> dict:
        """Take a screenshot of the current page.

        Always returns an image regardless of the multimodal setting.
        Use this when you need to visually inspect the page even if
        regular snapshots don't include screenshots.

        Returns:
            Dict with screenshot (base64), url, and title
        """
        try:
            session = await context.get_browser_session()
            screenshot_bytes = await session.take_screenshot()
            url = await session.get_current_page_url()
            title = await session.get_current_page_title()
            return {
                "url": url,
                "title": title,
                "screenshot": base64.b64encode(screenshot_bytes).decode(),
            }
        except Exception as e:
            logger.error(f"browser_screenshot failed: {e}", exc_info=True)
            return {"error": f"Screenshot failed: {e}"}

    @tool
    async def browser_back() -> dict:
        """Navigate back to the previous page in browser history.

        Returns:
            Updated page state after navigating back
        """
        try:
            session = await context.get_browser_session()
            page = await session.get_current_page()
            if page is not None:
                await page.evaluate("() => { window.history.back(); }")
                # Wait briefly for navigation
                import asyncio

                await asyncio.sleep(0.5)
            return await _get_page_state(context, session)
        except Exception as e:
            logger.error(f"browser_back failed: {e}", exc_info=True)
            return {"error": f"Back navigation failed: {e}"}

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
