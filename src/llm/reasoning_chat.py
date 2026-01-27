"""ChatOpenAI wrapper that captures reasoning_content from DeepSeek-style models.

LangChain's ChatOpenAI doesn't capture the `reasoning_content` field that DeepSeek R1
and similar reasoning models return. This module provides a custom wrapper that
intercepts the raw HTTP response to capture and preserve this field.

Set DEBUG_LLM_STREAM=1 to print a tail of LLM responses to stderr after each call.
"""

import json
import logging
import os
import sys
from typing import Any, Optional

import httpx
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)


def _is_debug_stream() -> bool:
    """Check at call time whether debug streaming is enabled."""
    return os.environ.get("DEBUG_LLM_STREAM", "").strip() in ("1", "true", "yes")


def _get_debug_tail_chars() -> int:
    """Get tail buffer size at call time."""
    return int(os.environ.get("DEBUG_LLM_TAIL", "500"))


class ReasoningCapturingClient(httpx.Client):
    """HTTP client that captures reasoning_content from API responses."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_reasoning_content: Optional[str] = None

    def send(self, request, **kwargs):
        response = super().send(request, **kwargs)
        if "/chat/completions" in str(request.url):
            try:
                data = json.loads(response.content)
                msg = data.get("choices", [{}])[0].get("message", {})
                self._last_reasoning_content = msg.get("reasoning_content")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        return response


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI that captures reasoning_content into additional_kwargs.

    This class wraps LangChain's ChatOpenAI to capture the `reasoning_content`
    field from DeepSeek-style reasoning models. The reasoning content is stored
    in the response message's `additional_kwargs['reasoning_content']`.

    When DEBUG_LLM_STREAM=1 is set, prints the last N characters of each LLM
    response to stderr (default 500, override with DEBUG_LLM_TAIL).

    Usage:
        llm = ReasoningChatOpenAI(
            model="deepseek-reasoner",
            base_url="https://api.deepseek.com/v1",
            api_key="your-key"
        )
        response = llm.invoke("Solve this problem step by step...")
        reasoning = response.additional_kwargs.get("reasoning_content")
    """

    # Use PrivateAttr for Pydantic compatibility
    _reasoning_client: ReasoningCapturingClient = PrivateAttr(default=None)

    def __init__(self, **kwargs):
        # Create the client before calling super().__init__
        reasoning_client = ReasoningCapturingClient()
        kwargs["http_client"] = reasoning_client
        super().__init__(**kwargs)
        # Store after init
        self._reasoning_client = reasoning_client

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        # Inject reasoning_content into the response
        if self._reasoning_client and self._reasoning_client._last_reasoning_content:
            for gen in result.generations:
                if hasattr(gen, "message"):
                    gen.message.additional_kwargs["reasoning_content"] = (
                        self._reasoning_client._last_reasoning_content
                    )
                    logger.debug(
                        f"Captured reasoning_content: "
                        f"{len(self._reasoning_client._last_reasoning_content)} chars"
                    )
            self._reasoning_client._last_reasoning_content = None

        # Debug: print tail of response to stderr
        if _is_debug_stream():
            tail_chars = _get_debug_tail_chars()
            for gen in result.generations:
                msg = getattr(gen, "message", None)
                if not msg:
                    continue
                content = getattr(msg, "content", "") or ""
                tool_calls = getattr(msg, "tool_calls", None) or []
                reasoning = (msg.additional_kwargs or {}).get("reasoning_content", "")

                # Build debug output
                parts = []
                if reasoning:
                    r_tail = reasoning[-tail_chars:] if len(reasoning) > tail_chars else reasoning
                    parts.append(f"\033[33m[reasoning {len(reasoning)} chars]\033[0m ...{r_tail}")
                if content:
                    c_tail = content[-tail_chars:] if len(content) > tail_chars else content
                    parts.append(f"\033[36m[content {len(content)} chars]\033[0m ...{c_tail}")
                if tool_calls:
                    tc_summary = ", ".join(
                        tc.get("name", "?") for tc in tool_calls
                    )
                    parts.append(f"\033[32m[tools: {tc_summary}]\033[0m")
                if not content and not tool_calls:
                    parts.append("\033[31m[empty response — no content, no tools]\033[0m")

                for part in parts:
                    sys.stderr.write(f"\n{part}\n")
                sys.stderr.flush()

        return result
