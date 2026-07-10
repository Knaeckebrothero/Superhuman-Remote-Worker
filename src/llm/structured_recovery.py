"""Helpers to recover a typed result from noisy structured-output responses."""

from __future__ import annotations

import re
from typing import Optional, Type

from pydantic import BaseModel


def _remove_think_blocks(text: str) -> str:
    return re.sub(r"(?is)<think>.*?</think>\s*", "", text)


def _strip_code_fence(text: str) -> str:
    match = re.search(
        r"(?is)^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", text, flags=re.DOTALL
    )
    if match:
        return match.group(1)
    return text


def _first_json_span(text: str) -> Optional[str]:
    in_string = False
    escape = False
    depth = 0
    start = None
    for idx, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            if depth == 0:
                start = idx
            depth += 1
            continue
        if char in "}]":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    return text[start : idx + 1].strip()
    return None


def recover_structured(
    raw_text: str | None, schema: Type[BaseModel]
) -> Optional[BaseModel]:
    """Attempt to recover a typed payload from model text output.

    The recovery pipeline strips model-specific artifacts (`<think>...</think>`,
    fenced JSON blocks), extracts the first balanced JSON object/array, then
    validates it against the pydantic schema.
    """
    if not raw_text:
        return None

    cleaned = _remove_think_blocks(raw_text)
    cleaned = _strip_code_fence(cleaned)
    candidate = _first_json_span(cleaned)
    if candidate is None:
        return None

    try:
        return schema.model_validate_json(candidate)
    except Exception:
        return None
