"""``fork=true`` seeding (U3 B.7): the child starts from the parent's history.

A pure function over a message list. The seed is the parent's DURABLE
``messages`` (the compacted list — never a prepared/transient copy):

1. drop the leading ``SystemMessage`` (the child's own prompt replaces it via
   ``get_current_system_prompt``);
2. drop protected phase blocks (``is_protected_message``) and ``RemoveMessage``
   markers;
3. drop a trailing assistant message with open tool calls (the loop's
   ``repair_tool_pairing`` would strip it anyway; dropping keeps the durable
   child transcript honest) and repair any other orphan;
4. sanitize for the provider boundary when the child's model family differs
   from the parent's (signed reasoning blocks, tool-call id formats);
5. append the fork notice as a ``role=event`` HumanMessage — the brief follows
   as the child's first input.

Cost note for the tool description (WP2): a fork re-sends the parent's whole
prefix on every child call.
"""

from __future__ import annotations

import copy
from typing import Any, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from src.core.context import repair_tool_pairing, sanitize_history_for_provider_boundary
from src.core.message_markers import (
    PERSIST_ROLE_EVENT,
    PERSIST_ROLE_KEY,
    is_protected_message,
)

FORK_NOTICE = (
    "You are a fork of the parent agent: its conversation history precedes this "
    "message. It is context, not your task — the brief for your own work follows."
)


def _is_remove_marker(message: Any) -> bool:
    return type(message).__name__ == "RemoveMessage"


def _families_differ(child_model: Optional[str], parent_model: Optional[str]) -> bool:
    if not child_model or not parent_model:
        return False
    if child_model == parent_model:
        return False
    from src.core.model_registry import family_of

    return family_of(child_model) != family_of(parent_model)


def seed_fork_history(
    parent_messages: List[Any],
    *,
    child_model: Optional[str] = None,
    parent_model: Optional[str] = None,
) -> List[BaseMessage]:
    """The child's initial ``messages`` for a fork (see module docstring)."""
    seed: List[BaseMessage] = []
    for index, message in enumerate(parent_messages or []):
        if not isinstance(message, BaseMessage) or _is_remove_marker(message):
            continue
        if index == 0 and isinstance(message, SystemMessage):
            continue
        if is_protected_message(message):
            continue
        seed.append(copy.copy(message))
    while (
        seed
        and isinstance(seed[-1], AIMessage)
        and getattr(seed[-1], "tool_calls", None)
    ):
        seed.pop()
    seed = list(repair_tool_pairing(seed))
    if _families_differ(child_model, parent_model):
        seed = list(sanitize_history_for_provider_boundary(seed, child_model or ""))
    seed.append(
        HumanMessage(
            content=FORK_NOTICE,
            additional_kwargs={PERSIST_ROLE_KEY: PERSIST_ROLE_EVENT},
        )
    )
    return seed


__all__ = ["FORK_NOTICE", "seed_fork_history"]
