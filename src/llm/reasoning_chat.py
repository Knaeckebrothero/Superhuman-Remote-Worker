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
from typing import TYPE_CHECKING, Optional

import httpx
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from .exceptions import ContextOverflowError

if TYPE_CHECKING:
    from .key_ring import KeyRing

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

    # Handle Responses API format (input field instead of messages)
    if not body.get("messages"):
        for item in body.get("input", []):
            if isinstance(item, str):
                total += len(enc.encode(item))
            elif isinstance(item, dict):
                total += len(enc.encode(item.get("role", "")))
                content = item.get("content", "")
                if isinstance(content, str):
                    total += len(enc.encode(content))
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and "text" in part:
                            total += len(enc.encode(part["text"]))
                total += 4
        instructions = body.get("instructions", "")
        if instructions:
            total += len(enc.encode(instructions))

    # Count tool definitions
    for tool in body.get("tools", []):
        total += len(enc.encode(json.dumps(tool)))

    # Request structure overhead
    total += 10

    return total


def _extract_responses_api_reasoning(message) -> None:
    """Extract reasoning from Responses API content blocks into additional_kwargs.

    The Responses API returns content as a list of typed blocks. Reasoning blocks
    have type=="reasoning" with summary/content lists containing text items.
    This function moves reasoning text into additional_kwargs["reasoning_content"]
    and flattens remaining content blocks to a plain string.
    """
    content = message.content
    if not isinstance(content, list):
        return

    reasoning_parts = []
    non_reasoning = []

    for block in content:
        if isinstance(block, dict) and block.get("type") == "reasoning":
            for item in block.get("summary", []):
                if isinstance(item, dict) and "text" in item:
                    reasoning_parts.append(item["text"])
            for item in block.get("content", []):
                if isinstance(item, dict) and "text" in item:
                    reasoning_parts.append(item["text"])
        else:
            non_reasoning.append(block)

    if reasoning_parts:
        message.additional_kwargs["reasoning_content"] = "\n".join(reasoning_parts)

    # Flatten remaining content to strings
    cleaned = []
    for block in non_reasoning:
        if isinstance(block, str):
            cleaned.append(block)
        elif isinstance(block, dict) and "text" in block:
            cleaned.append(block["text"])
        # Skip non-text blocks (function_call items are already in tool_calls)

    message.content = " ".join(cleaned).strip() if all(isinstance(c, str) for c in cleaned) else cleaned or ""


class ReasoningCapturingClient(httpx.Client):
    """HTTP client that captures reasoning_content and validates context limits.

    This client intercepts all HTTP requests to:
    1. Override the Authorization header with the KeyRing's current key (if configured)
    2. Count tokens in chat completion requests before sending
    3. Raise ContextOverflowError if tokens exceed the limit
    4. Capture reasoning_content from responses (for DeepSeek-style models)
    5. Rotate to next API key on auth/quota failures (401, 403, quota-429)
    """

    def __init__(
        self,
        *args,
        timeout: Optional[float] = None,
        max_context_tokens: Optional[int] = None,
        model: str = "gpt-4",
        key_ring: Optional["KeyRing"] = None,
        **kwargs,
    ):
        # Apply timeout if specified
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        super().__init__(*args, **kwargs)

        self._last_reasoning_content: Optional[str] = None
        self._model = model
        self._key_ring = key_ring

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
            f"max_context_tokens={self._max_context_tokens}, model={self._model}, "
            f"key_ring={'yes' if key_ring else 'no'}"
        )

    def send(self, request, **kwargs):
        url_str = str(request.url)
        is_chat = "/chat/completions" in url_str
        is_responses = url_str.rstrip("/").endswith("/responses") or "/responses/" in url_str
        is_llm_request = is_chat or is_responses

        # Inject current key from KeyRing into the request header
        if self._key_ring and is_llm_request:
            try:
                current = self._key_ring.current_key
                request.headers["authorization"] = f"Bearer {current}"
            except RuntimeError:
                # All keys exhausted — let the request go with whatever header it has
                logger.error("KeyRing: all keys exhausted, sending with original header")

        # Token validation for LLM requests (Layer 0 safety check)
        if is_llm_request:
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

        # Key rotation: retry once on auth/quota errors
        if is_llm_request and self._key_ring and self._key_ring.has_alternatives:
            rotated_response = self._handle_key_rotation(request, response, **kwargs)
            if rotated_response is not None:
                response = rotated_response

        # Capture reasoning_content from response (existing behavior)
        if is_chat:
            try:
                data = json.loads(response.content)
                msg = data.get("choices", [{}])[0].get("message", {})
                self._last_reasoning_content = msg.get("reasoning_content")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        return response

    def _handle_key_rotation(
        self, request: httpx.Request, response: httpx.Response, **kwargs
    ) -> Optional[httpx.Response]:
        """Check if response indicates auth/quota failure and retry with next key.

        Returns a new response if rotation succeeded, or None to keep the original.
        """
        status = response.status_code

        if status == 401 or status == 403:
            return self._rotate_and_retry(request, f"HTTP {status} auth error", **kwargs)

        if status == 429 and self._is_quota_error(response):
            return self._rotate_and_retry(request, "quota exceeded (429)", **kwargs)

        return None

    def _is_quota_error(self, response: httpx.Response) -> bool:
        """Distinguish quota-429 (rotate) from rate-limit-429 (don't rotate).

        Heuristics:
        - retry-after < 3600 with no quota signals -> rate limit (don't rotate)
        - Body contains quota/billing keywords -> quota (rotate)
        - No retry-after and no clear signal -> assume quota (conservative)
        """
        # Check retry-after header
        retry_after_raw = response.headers.get("retry-after")
        retry_after = None
        if retry_after_raw:
            try:
                retry_after = float(retry_after_raw)
            except (ValueError, TypeError):
                pass

        # Check response body for quota signals
        quota_keywords = ("quota", "billing", "insufficient_quota", "exceeded")
        body_text = ""
        try:
            body_text = response.text.lower()
        except Exception:
            pass

        has_quota_signal = any(kw in body_text for kw in quota_keywords)

        if has_quota_signal:
            return True

        if retry_after is not None and retry_after < 3600:
            # Short retry-after without quota signal -> rate limit
            return False

        # No retry-after and no clear signal -> assume quota (conservative)
        return True

    def _rotate_and_retry(
        self, request: httpx.Request, reason: str, **kwargs
    ) -> Optional[httpx.Response]:
        """Rotate to next key and retry the request once.

        Returns the retry response, or None if rotation failed.
        """
        new_key = self._key_ring.rotate(reason)
        if new_key is None:
            logger.error(f"Key rotation failed: no alternative keys available ({reason})")
            return None

        # Override header with new key and retry
        request.headers["authorization"] = f"Bearer {new_key}"
        logger.info(f"Retrying request with rotated key after: {reason}")
        return super().send(request, **kwargs)


class AsyncReasoningCapturingClient(httpx.AsyncClient):
    """Async HTTP client that captures reasoning_content and validates context limits.

    Async counterpart of ReasoningCapturingClient. Used by LangChain's async path
    (ainvoke/agenerate) which creates its own httpx.AsyncClient by default, bypassing
    the sync http_client entirely. Passing this as http_async_client ensures key
    rotation, Layer 0 overflow checks, and reasoning capture work in async mode.
    """

    def __init__(
        self,
        *args,
        timeout: Optional[float] = None,
        max_context_tokens: Optional[int] = None,
        model: str = "gpt-4",
        key_ring: Optional["KeyRing"] = None,
        **kwargs,
    ):
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        super().__init__(*args, **kwargs)

        self._last_reasoning_content: Optional[str] = None
        self._model = model
        self._key_ring = key_ring

        self._max_context_tokens = (
            max_context_tokens
            or int(os.environ.get("MAX_CONTEXT_TOKENS", "0"))
            or DEFAULT_MAX_CONTEXT_TOKENS
        )

        logger.debug(
            f"AsyncReasoningCapturingClient initialized: "
            f"max_context_tokens={self._max_context_tokens}, model={self._model}, "
            f"key_ring={'yes' if key_ring else 'no'}"
        )

    async def send(self, request, **kwargs):
        url_str = str(request.url)
        is_chat = "/chat/completions" in url_str
        is_responses = url_str.rstrip("/").endswith("/responses") or "/responses/" in url_str
        is_llm_request = is_chat or is_responses

        # Inject current key from KeyRing into the request header
        if self._key_ring and is_llm_request:
            try:
                current = self._key_ring.current_key
                request.headers["authorization"] = f"Bearer {current}"
            except RuntimeError:
                logger.error("KeyRing: all keys exhausted, sending with original header")

        # Token validation for LLM requests (Layer 0 safety check)
        if is_llm_request:
            try:
                body = json.loads(request.content)
                token_count = count_request_tokens(body, self._model)

                if token_count > self._max_context_tokens * WARNING_THRESHOLD_RATIO:
                    logger.warning(
                        f"Request approaching context limit: "
                        f"{token_count:,}/{self._max_context_tokens:,} tokens "
                        f"({token_count / self._max_context_tokens * 100:.1f}%)"
                    )

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
                logger.debug("Skipping token count for non-JSON request")
            except ContextOverflowError:
                raise
            except Exception as e:
                logger.warning(f"Token counting failed, allowing request: {e}")

        # Send the request (async)
        response = await super().send(request, **kwargs)

        # Key rotation: retry once on auth/quota errors
        if is_llm_request and self._key_ring and self._key_ring.has_alternatives:
            rotated_response = await self._handle_key_rotation(request, response, **kwargs)
            if rotated_response is not None:
                response = rotated_response

        # Capture reasoning_content from response
        if is_chat:
            try:
                data = json.loads(response.content)
                msg = data.get("choices", [{}])[0].get("message", {})
                self._last_reasoning_content = msg.get("reasoning_content")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        return response

    async def _handle_key_rotation(
        self, request: httpx.Request, response: httpx.Response, **kwargs
    ) -> Optional[httpx.Response]:
        """Check if response indicates auth/quota failure and retry with next key."""
        status = response.status_code

        if status == 401 or status == 403:
            return await self._rotate_and_retry(request, f"HTTP {status} auth error", **kwargs)

        if status == 429 and self._is_quota_error(response):
            return await self._rotate_and_retry(request, "quota exceeded (429)", **kwargs)

        return None

    def _is_quota_error(self, response: httpx.Response) -> bool:
        """Distinguish quota-429 (rotate) from rate-limit-429 (don't rotate)."""
        retry_after_raw = response.headers.get("retry-after")
        retry_after = None
        if retry_after_raw:
            try:
                retry_after = float(retry_after_raw)
            except (ValueError, TypeError):
                pass

        quota_keywords = ("quota", "billing", "insufficient_quota", "exceeded")
        body_text = ""
        try:
            body_text = response.text.lower()
        except Exception:
            pass

        has_quota_signal = any(kw in body_text for kw in quota_keywords)

        if has_quota_signal:
            return True

        if retry_after is not None and retry_after < 3600:
            return False

        return True

    async def _rotate_and_retry(
        self, request: httpx.Request, reason: str, **kwargs
    ) -> Optional[httpx.Response]:
        """Rotate to next key and retry the request once."""
        new_key = self._key_ring.rotate(reason)
        if new_key is None:
            logger.error(f"Key rotation failed: no alternative keys available ({reason})")
            return None

        request.headers["authorization"] = f"Bearer {new_key}"
        logger.info(f"Retrying request with rotated key after: {reason}")
        return await super().send(request, **kwargs)


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
    _async_reasoning_client: AsyncReasoningCapturingClient = PrivateAttr(default=None)

    def __init__(self, max_context_tokens: Optional[int] = None, key_ring: Optional["KeyRing"] = None, **kwargs):
        # Extract config for our custom client
        timeout = kwargs.get("timeout")
        model = kwargs.get("model", "gpt-4")

        # Create sync client (used by invoke/_generate)
        reasoning_client = ReasoningCapturingClient(
            timeout=timeout,
            max_context_tokens=max_context_tokens,
            model=model,
            key_ring=key_ring,
        )
        # Create async client (used by ainvoke/_agenerate — the actual production path)
        async_reasoning_client = AsyncReasoningCapturingClient(
            timeout=timeout,
            max_context_tokens=max_context_tokens,
            model=model,
            key_ring=key_ring,
        )
        kwargs["http_client"] = reasoning_client
        kwargs["http_async_client"] = async_reasoning_client
        super().__init__(**kwargs)
        # Store after init
        self._reasoning_client = reasoning_client
        self._async_reasoning_client = async_reasoning_client

    def _post_process_result(self, result):
        """Post-process LLM result: capture reasoning and debug output.

        Handles both Chat Completions (reasoning via HTTP client) and
        Responses API (reasoning in content blocks) formats.
        """
        # 1. Inject Chat Completions reasoning from HTTP layer (sync or async client)
        reasoning_content = None
        if self._reasoning_client and self._reasoning_client._last_reasoning_content:
            reasoning_content = self._reasoning_client._last_reasoning_content
            self._reasoning_client._last_reasoning_content = None
        elif self._async_reasoning_client and self._async_reasoning_client._last_reasoning_content:
            reasoning_content = self._async_reasoning_client._last_reasoning_content
            self._async_reasoning_client._last_reasoning_content = None

        if reasoning_content:
            for gen in result.generations:
                if hasattr(gen, "message"):
                    gen.message.additional_kwargs["reasoning_content"] = reasoning_content
                    logger.debug(
                        f"Captured reasoning_content: {len(reasoning_content)} chars"
                    )

        # 2. Extract Responses API reasoning from content blocks
        for gen in result.generations:
            if hasattr(gen, "message"):
                _extract_responses_api_reasoning(gen.message)

        # 3. Debug: print tail of response to stderr
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

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        return self._post_process_result(result)

    async def _agenerate(self, *args, **kwargs):
        result = await super()._agenerate(*args, **kwargs)
        return self._post_process_result(result)
