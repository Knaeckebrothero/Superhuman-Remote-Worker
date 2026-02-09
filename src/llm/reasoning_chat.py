"""ChatOpenAI wrapper that captures reasoning_content from DeepSeek-style models.

LangChain's ChatOpenAI doesn't capture the `reasoning_content` field that DeepSeek R1
and similar reasoning models return. This module provides a custom wrapper that
intercepts the raw HTTP response to capture and preserve this field.

Also implements Layer 0 context overflow protection by counting tokens in the
actual HTTP request body before sending.

Set DEBUG_LLM_STREAM=1 to print a tail of LLM responses to stderr after each call.
"""

import json
import logging
import os
import sys
from typing import Optional

import httpx
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from .exceptions import ContextOverflowError

logger = logging.getLogger(__name__)

# Token counting constants
DEFAULT_MAX_CONTEXT_TOKENS = 100_000
WARNING_THRESHOLD_RATIO = 0.9

# Try to import tiktoken for accurate token counting
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not available for HTTP-layer token counting")


def _is_debug_stream() -> bool:
    """Check at call time whether debug streaming is enabled."""
    return os.environ.get("DEBUG_LLM_STREAM", "").strip() in ("1", "true", "yes")


def _get_debug_tail_chars() -> int:
    """Get tail buffer size at call time."""
    return int(os.environ.get("DEBUG_LLM_TAIL", "500"))


def count_request_tokens(body: dict, model: str = "gpt-4") -> int:
    """Count tokens in OpenAI API request body.

    Counts tokens in messages, tool definitions, and request overhead.
    This gives an accurate count of what's actually being sent to the API.

    Args:
        body: Parsed JSON body of the API request
        model: Model name for tokenizer selection

    Returns:
        Estimated token count
    """
    if not TIKTOKEN_AVAILABLE:
        # Fallback: approximate as ~4 chars per token
        return len(json.dumps(body)) // 4

    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")

    total = 0

    # Count messages
    for msg in body.get("messages", []):
        # Count role
        total += len(enc.encode(msg.get("role", "")))

        # Count content
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(enc.encode(content))
        elif isinstance(content, list):
            # Handle multimodal content (text parts)
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += len(enc.encode(part["text"]))

        # Count tool calls in assistant messages
        if "tool_calls" in msg:
            total += len(enc.encode(json.dumps(msg["tool_calls"])))

        # Count tool_call_id in tool messages
        if "tool_call_id" in msg:
            total += len(enc.encode(msg["tool_call_id"]))

        # Message structure overhead (~4 tokens per message)
        total += 4

    # Count tool definitions
    for tool in body.get("tools", []):
        total += len(enc.encode(json.dumps(tool)))

    # Request structure overhead
    total += 10

    return total


class ReasoningCapturingClient(httpx.Client):
    """HTTP client that captures reasoning_content and validates context limits.

    This client intercepts all HTTP requests to:
    1. Count tokens in chat completion requests before sending
    2. Raise ContextOverflowError if tokens exceed the limit
    3. Capture reasoning_content from responses (for DeepSeek-style models)
    """

    def __init__(
        self,
        *args,
        timeout: Optional[float] = None,
        max_context_tokens: Optional[int] = None,
        model: str = "gpt-4",
        **kwargs,
    ):
        # Apply timeout if specified
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        super().__init__(*args, **kwargs)

        self._last_reasoning_content: Optional[str] = None
        self._model = model

        # Set max context tokens with fallback chain:
        # 1. Explicit parameter
        # 2. Environment variable
        # 3. Default constant
        self._max_context_tokens = (
            max_context_tokens
            or int(os.environ.get("MAX_CONTEXT_TOKENS", "0"))
            or DEFAULT_MAX_CONTEXT_TOKENS
        )

        logger.debug(
            f"ReasoningCapturingClient initialized: "
            f"max_context_tokens={self._max_context_tokens}, model={self._model}"
        )

    def send(self, request, **kwargs):
        # Token validation for chat completion requests (Layer 0 safety check)
        if "/chat/completions" in str(request.url):
            try:
                body = json.loads(request.content)
                token_count = count_request_tokens(body, self._model)

                # Log warning if approaching limit (90% threshold)
                if token_count > self._max_context_tokens * WARNING_THRESHOLD_RATIO:
                    logger.warning(
                        f"Request approaching context limit: "
                        f"{token_count:,}/{self._max_context_tokens:,} tokens "
                        f"({token_count / self._max_context_tokens * 100:.1f}%)"
                    )

                # Raise error if over limit
                if token_count > self._max_context_tokens:
                    logger.error(
                        f"Context overflow at HTTP layer: "
                        f"{token_count:,} tokens exceeds limit of {self._max_context_tokens:,}"
                    )
                    raise ContextOverflowError(
                        token_count=token_count,
                        limit=self._max_context_tokens,
                        request_size_bytes=len(request.content),
                    )

            except json.JSONDecodeError:
                # Non-JSON request body, skip validation
                logger.debug("Skipping token count for non-JSON request")
            except ContextOverflowError:
                # Re-raise our custom exception
                raise
            except Exception as e:
                # Log but don't fail on counting errors - let the request through
                logger.warning(f"Token counting failed, allowing request: {e}")

        # Send the request
        response = super().send(request, **kwargs)

        # Capture reasoning_content from response (existing behavior)
        if "/chat/completions" in str(request.url):
            try:
                data = json.loads(response.content)
                msg = data.get("choices", [{}])[0].get("message", {})
                self._last_reasoning_content = msg.get("reasoning_content")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        return response


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI that captures reasoning_content and validates context limits.

    This class wraps LangChain's ChatOpenAI to:
    1. Capture the `reasoning_content` field from DeepSeek-style reasoning models
    2. Validate context limits at the HTTP layer (Layer 0 safety check)

    The reasoning content is stored in `additional_kwargs['reasoning_content']`.

    When DEBUG_LLM_STREAM=1 is set, prints the last N characters of each LLM
    response to stderr (default 500, override with DEBUG_LLM_TAIL).

    Usage:
        llm = ReasoningChatOpenAI(
            model="deepseek-reasoner",
            base_url="https://api.deepseek.com/v1",
            api_key="your-key",
            max_context_tokens=128000,  # Optional: context limit
        )
        response = llm.invoke("Solve this problem step by step...")
        reasoning = response.additional_kwargs.get("reasoning_content")
    """

    # Use PrivateAttr for Pydantic compatibility
    _reasoning_client: ReasoningCapturingClient = PrivateAttr(default=None)

    def __init__(self, max_context_tokens: Optional[int] = None, **kwargs):
        # Extract config for our custom client
        timeout = kwargs.get("timeout")
        model = kwargs.get("model", "gpt-4")

        # Create the client with context limit validation
        reasoning_client = ReasoningCapturingClient(
            timeout=timeout,
            max_context_tokens=max_context_tokens,
            model=model,
        )
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
