"""Bridge nested session-create overrides into the validated rebuild.

``POST /api/persistent/threads`` rebuilds ``config_override`` from validated
fragments: it starts from ``{}`` and copies in the pieces it names. Anything it
does not name is dropped — silently, until this module. The New Session form
lifts ``model`` and ``permission_mode`` to top-level request fields but leaves
``reasoning_level`` and ``temperature`` NESTED under ``config_override.llm``,
which is exactly the shape the rebuild never read. Five earlier fields fell
into the same hole (permission mode, workspace backend, eight tool groups, the
officer block, then reasoning). See
knowledge-base/knowledge/issues/live_settings_silently_dropped_on_stateless_sessions.md
§Defect B.

Two pure helpers, so the contract is unit-testable without the create handler:

- :func:`bridge_nested_llm_override` folds the nested LLM keys into the rebuilt
  override. A top-level request field always wins over its nested twin.
- :func:`ignored_override_paths` is the warn phase of a strict contract
  (Kubernetes KEP-2885 shape: Ignore → Warn → Strict): every nested path whose
  value the rebuild did not carry, so callers can log and surface it before a
  later change turns the list into a 400.
"""

from __future__ import annotations

from typing import Any, Callable

#: LLM keys the create handler honours, top-level or nested.
LLM_KEYS: tuple[str, ...] = ("model", "temperature", "reasoning_level")


class SessionOverrideError(ValueError):
    """A nested override key is present but its value is malformed."""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def bridge_nested_llm_override(
    request_override: dict[str, Any] | None,
    config_override: dict[str, Any],
    *,
    validate_reasoning_level: Callable[[Any], str],
) -> list[str]:
    """Fold ``request_override["llm"][k]`` for ``k in LLM_KEYS`` into
    ``config_override["llm"]`` where the rebuilt override has no value yet.

    Mutates ``config_override`` in place and returns the bridged key paths
    (``["llm.reasoning_level", ...]``) for the caller's audit line. A key the
    caller already bridged from a top-level request field is left alone: the
    explicit field is the stronger statement of intent.

    Raises :class:`SessionOverrideError` for a malformed nested value.
    ``validate_reasoning_level`` is the create handler's own vocabulary check
    and may raise its own error type (an ``HTTPException`` today); it is
    called only for a nested level that is actually being bridged.
    """
    nested_llm = _as_dict(_as_dict(request_override).get("llm"))
    if not nested_llm:
        return []
    target = config_override.setdefault("llm", {})
    if not isinstance(target, dict):
        return []
    bridged: list[str] = []
    for key in LLM_KEYS:
        if key not in nested_llm or key in target:
            continue
        value = nested_llm[key]
        if value is None:
            continue
        if key == "model":
            if not isinstance(value, str) or not value.strip():
                raise SessionOverrideError(
                    "config_override.llm.model must be a non-empty string"
                )
            target[key] = value
        elif key == "temperature":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SessionOverrideError(
                    "config_override.llm.temperature must be a number"
                )
            target[key] = value
        else:
            target[key] = validate_reasoning_level(value)
        bridged.append(f"llm.{key}")
    return bridged


#: The live ``delegation`` keys after U3 (loader.normalize_delegation_block).
DELEGATION_KEYS: frozenset[str] = frozenset(
    {"enabled", "max_concurrent", "run_in_background_default"}
)
#: Pre-U3 keys the loader drops with a deprecation warning; tolerated here for
#: the same reason (a stored layer may still carry them), never persisted. One
#: source of truth: the loader's own list, so the two boundaries cannot drift.
try:
    from shared.runtime.core.loader import _LEGACY_DELEGATION_KEYS as _LOADER_LEGACY

    LEGACY_DELEGATION_KEYS: frozenset[str] = frozenset(_LOADER_LEGACY)
except ImportError:  # pragma: no cover - the loader is always importable here
    LEGACY_DELEGATION_KEYS = frozenset(
        {
            "max_depth",
            "default_timeout",
            "max_timeout",
            "allowed_configs",
            "mode",
            "light",
        }
    )


def validate_delegation_override(value: Any) -> dict[str, Any]:
    """The ``delegation`` block a session request may carry, validated.

    ``delegate_agent`` and its control plane are ``grant: explicit``: the
    factory builds them only when ``tools.delegation`` names them AND
    ``delegation.enabled`` is true. The Cockpit's Delegation toggle therefore
    writes BOTH (tools-group.component.ts ``getOverrides``), and this is the
    boundary that must carry the second half — the create rebuild dropped it,
    so a ticked "Delegation" produced five names the agent then refused to
    bind ("11 configured tool(s) did not bind"). Malformed values are a
    :class:`SessionOverrideError`, never a silent drop.
    """
    if not isinstance(value, dict):
        raise SessionOverrideError("config_override.delegation must be an object")
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if key in LEGACY_DELEGATION_KEYS:
            continue
        if key not in DELEGATION_KEYS:
            raise SessionOverrideError(
                f"config_override.delegation.{key} is not a session delegation setting"
            )
        if key in ("enabled", "run_in_background_default"):
            if not isinstance(raw, bool):
                raise SessionOverrideError(
                    f"config_override.delegation.{key} must be a boolean"
                )
            out[key] = raw
        else:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise SessionOverrideError(
                    "config_override.delegation.max_concurrent must be a positive integer"
                )
            out[key] = raw
    return out


def bridge_nested_delegation_override(
    request_override: dict[str, Any] | None,
    config_override: dict[str, Any],
) -> list[str]:
    """Fold a validated ``request_override["delegation"]`` into the rebuilt
    override. Returns the bridged key paths; ``[]`` when nothing was sent."""
    raw = _as_dict(request_override).get("delegation")
    if raw is None:
        return []
    validated = validate_delegation_override(raw)
    if not validated:
        return []
    target = config_override.setdefault("delegation", {})
    if not isinstance(target, dict):
        return []
    bridged: list[str] = []
    for key, val in validated.items():
        if key in target:
            continue
        target[key] = val
        bridged.append(f"delegation.{key}")
    return bridged


def ignored_override_paths(
    request_override: dict[str, Any] | None,
    config_override: dict[str, Any],
) -> list[str]:
    """Key paths the caller sent under ``config_override`` that the rebuilt
    override does not carry with the same value.

    Compared by VALUE, not by allow-list, so a key that arrived twice (the
    form sends ``model`` top-level AND nested) is not reported when the
    rebuild carries it. One level of nesting is inspected — the sections the
    create handler validates (``llm``, ``interactive``, ``workspace``,
    ``tools``, ``officer``) are all flat dicts of scalars or lists — and an
    unknown top-level section is reported as a whole.

    Sorted for a stable audit line.
    """
    request = _as_dict(request_override)
    ignored: list[str] = []
    for section, sent in request.items():
        carried = config_override.get(section)
        if isinstance(sent, dict):
            carried_dict = carried if isinstance(carried, dict) else {}
            for key, value in sent.items():
                if key not in carried_dict or carried_dict[key] != value:
                    ignored.append(f"{section}.{key}")
        elif carried != sent:
            ignored.append(section)
    return sorted(ignored)
