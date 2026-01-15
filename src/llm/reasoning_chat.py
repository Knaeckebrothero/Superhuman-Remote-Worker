"""ChatOpenAI wrapper that captures reasoning_content from DeepSeek-style models.

LangChain's ChatOpenAI doesn't capture the `reasoning_content` field that DeepSeek R1
and similar reasoning models return. This module provides a custom wrapper that
intercepts the raw HTTP response to capture and preserve this field.
"""

import json
import logging
from typing import Any, ClassVar, Optional

import httpx
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)


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
        return result
