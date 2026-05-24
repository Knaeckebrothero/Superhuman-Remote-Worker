"""Provider-agnostic message components for the durable session transcript.

Converts a LangChain response into four normalized components (reasoning, text,
tool calls, tool results) PLUS the verbatim provider-raw payload, and back into a
provider request. See docs/features/persistent_session_source_of_truth.md (D2/D6).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


@dataclass
class MessageComponents:
    role: str
    text: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: List[dict] = field(default_factory=list)
    tool_results: List[dict] = field(default_factory=list)
    provider: Optional[str] = None
    provider_raw: Optional[Any] = None
    additional_kwargs: dict = field(default_factory=dict)
    response_metadata: dict = field(default_factory=dict)


def _content_text(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "".join(parts)
        return joined or None
    return None


def normalize_response(
    message: BaseMessage,
    provider: str,
    raw_output: Optional[Any] = None,
) -> MessageComponents:
    """Normalize a LangChain message into components + verbatim raw.

    ``raw_output`` is the provider's verbatim COMPLETED response (the
    ``chat.completion`` object for the live Chat Completions path; the Responses
    ``output[]`` array if/when that API is adopted). Stored for audit and
    forward-compat replay; the live CC path replays from normalized components.
    """
    if isinstance(message, ToolMessage):
        return MessageComponents(
            role="tool",
            tool_results=[{
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }],
            provider=provider,
        )

    if isinstance(message, AIMessage):
        ak = dict(message.additional_kwargs or {})
        return MessageComponents(
            role="ai",
            text=_content_text(message.content),
            reasoning=ak.get("reasoning_content"),
            tool_calls=list(message.tool_calls or []),
            provider=provider,
            provider_raw=raw_output,
            additional_kwargs=ak,
            response_metadata=dict(message.response_metadata or {}),
        )

    return MessageComponents(
        role=getattr(message, "type", "human"),
        text=_content_text(message.content),
        provider=provider,
    )


def _tool_calls_to_cc(tool_calls: List[dict]) -> List[dict]:
    """LangChain tool_calls ({name, args, id}) -> Chat Completions wire shape."""
    cc = []
    for tc in tool_calls:
        args = tc.get("args", {})
        cc.append({
            "id": tc.get("id"),
            "type": "function",
            "function": {
                "name": tc.get("name"),
                "arguments": args if isinstance(args, str) else json.dumps(args),
            },
        })
    return cc


def components_to_provider_messages(
    components: List["MessageComponents"],
    target_provider: str = "openai-chat",
) -> list:
    """Rebuild a provider request (message array) from stored components.

    v1 implements the live Chat Completions path: normalized reconstruction
    (assistant content + tool_calls; tool results as role=tool). ``provider_raw``
    is audit/forward-compat and is NOT replayed here. Verbatim-raw replay for the
    real Responses API / Anthropic is forward-compat (raises NotImplementedError).
    """
    if target_provider != "openai-chat":
        raise NotImplementedError(
            f"verbatim-raw replay for provider {target_provider!r} is forward-compat; "
            "v1 supports normalized Chat Completions reconstruction only"
        )
    out: list = []
    for c in components:
        if c.role == "tool":
            for tr in c.tool_results:
                out.append({"role": "tool",
                            "tool_call_id": tr["tool_call_id"],
                            "content": tr["content"]})
        elif c.role == "ai":
            msg = {"role": "assistant", "content": c.text or ""}
            if c.tool_calls:
                msg["tool_calls"] = _tool_calls_to_cc(c.tool_calls)
            out.append(msg)
        else:
            out.append({"role": "user" if c.role in ("human", "user") else c.role,
                        "content": c.text or ""})
    return out
