"""Repair list-shaped tool arguments that a model emitted in the wrong shape.

Small models routinely fail to produce a JSON array for an array-typed tool
parameter. Two shapes dominate, and both were observed live on 2026-08-16 when
a commissioned officer could not tag a backlog ticket:

    tags='["a", "b"]'            # the array, JSON-encoded into a string
    tags={'item': ['a', 'b']}    # the array, wrapped by schema handling

Pydantic rejects both, the model gets ``Input should be a valid list``, and it
retries with the *other* wrong shape. The officer above burned eight
consecutive ``kb_write``/``kb_update`` calls alternating between them and then
gave up and dropped the argument, producing a note with no tags — a silent
wrong answer rather than a failure.

This module is deliberately narrow. It only rewrites an argument when the tool
schema says that parameter is a list AND the delivered value is unambiguously
that list in another encoding. Anything else is passed through untouched so a
genuine type error still surfaces as a type error.

Pure stdlib so it can be used from any tool call path without dragging in the
agent runtime.
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any, Dict, List, Tuple

# Keys a provider or schema shim wraps an array in. A single-key mapping whose
# key is one of these and whose value is a list is the wrapper, not data.
_WRAPPER_KEYS = frozenset({"item", "items", "value", "values", "list", "array"})


def _annotation_accepts_list(annotation: Any) -> bool:
    """Whether this annotation admits a list (through Optional/Union too)."""

    if annotation is None:
        return False
    origin = typing.get_origin(annotation)
    if origin in (list, List):
        return True
    # Optional[List[str]] / List[str] | None — inspect the members.
    if origin is typing.Union or isinstance(annotation, types.UnionType):
        return any(_annotation_accepts_list(arg) for arg in typing.get_args(annotation))
    return annotation is list


def _list_fields(args_schema: Any) -> set[str]:
    """Names of list-typed parameters on a pydantic tool schema."""

    fields = getattr(args_schema, "model_fields", None)
    if not isinstance(fields, dict):
        return set()
    return {
        name
        for name, field in fields.items()
        if _annotation_accepts_list(getattr(field, "annotation", None))
    }


def _as_list(value: Any) -> Any:
    """Return the list hiding inside ``value``, or ``value`` unchanged."""

    if isinstance(value, list):
        return value

    if isinstance(value, str):
        text = value.strip()
        # Only attempt the obvious case. A bare word is a string the model
        # meant as a string, and guessing ["word"] would invent data.
        if not (text.startswith("[") and text.endswith("]")):
            return value
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return value
        return parsed if isinstance(parsed, list) else value

    if isinstance(value, dict):
        if len(value) == 1:
            ((key, inner),) = value.items()
            if str(key).lower() in _WRAPPER_KEYS and isinstance(inner, list):
                return inner
        # {"0": "a", "1": "b"} — an array that lost its brackets. Require every
        # key to be an integer index so a real mapping is never flattened.
        if value and all(str(k).lstrip("-").isdigit() for k in value):
            try:
                return [value[k] for k in sorted(value, key=lambda k: int(str(k)))]
            except (KeyError, TypeError, ValueError):
                return value

    return value


def coerce_tool_args(args_schema: Any, args: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize list-typed arguments in ``args`` against ``args_schema``.

    Returns the (possibly new) argument mapping and the names of the arguments
    that were rewritten, so the caller can log the repair rather than hide it.
    """

    if not isinstance(args, dict) or not args:
        return args if isinstance(args, dict) else {}, []
    targets = _list_fields(args_schema)
    if not targets:
        return args, []

    repaired: Dict[str, Any] = dict(args)
    changed: List[str] = []
    for name in targets:
        if name not in repaired:
            continue
        current = repaired[name]
        if isinstance(current, list) or current is None:
            continue
        candidate = _as_list(current)
        if candidate is not current and isinstance(candidate, list):
            repaired[name] = candidate
            changed.append(name)
    return (repaired, changed) if changed else (args, [])
