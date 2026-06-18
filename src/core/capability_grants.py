# src/core/capability_grants.py
"""Pure capability-grants logic: catalog, resolution, policy decision. No DB/
framework imports — hermetically unit-testable (the security boundary). Async DB
glue is in orchestrator/services/grants_service.py.

Spec: docs/features/global_expert_management.md (decisions 8, 9, 19, 21-23).
Restrict-only (decision 22): a more-specific scope may only narrow an inherited
value. Deny-by-default for security keys; existing users grandfathered by the
0030 migration backfill (shell_tools, delegation)."""

from __future__ import annotations

from typing import Any

_AUTONOMY_ORDER = ["dependent", "guided", "partial", "review", "full"]
_PERMISSION_ORDER = ["supervised", "auto_accept", "autonomous"]

CATALOG: dict[str, dict[str, Any]] = {
    "vm_workspace": {"type": "bool", "default": False, "restrict_only": True},
    "shell_tools": {"type": "bool", "default": False, "restrict_only": True},
    "delegation": {"type": "bool", "default": False, "restrict_only": True},
    "datasource_tools": {"type": "bool", "default": True, "restrict_only": True},
    "browser": {"type": "bool", "default": True, "restrict_only": True},
    "model_selection": {"type": "list", "default": None, "restrict_only": True},
    "autonomy_ceiling": {
        "type": "enum",
        "default": "review",
        "restrict_only": True,
        "order": _AUTONOMY_ORDER,
    },
    "permission_mode": {
        "type": "enum",
        "default": "supervised",
        "restrict_only": True,
        "order": _PERMISSION_ORDER,
    },
}


def meet(spec: dict, a: Any, b: Any) -> Any:
    """Greatest-lower-bound (the restrict-only combinator). bool->AND,
    enum->more restrictive by catalog order, list->intersection (None = ⊤)."""
    t = spec["type"]
    if t == "bool":
        return bool(a) and bool(b)
    if t == "enum":
        order = spec["order"]
        return a if order.index(a) <= order.index(b) else b
    if t == "list":
        if a is None:
            return b
        if b is None:
            return a
        bset = set(b)
        return [x for x in a if x in bset]
    raise ValueError(f"unknown catalog type {t!r}")


def _scope_value(rows: list[dict], key: str, spec: dict) -> Any:
    """The value one scope asserts for key, meeting duplicates (multi-project ->
    most restrictive). None => scope does not set the key."""
    vals = [r["value_json"] for r in rows if r.get("key") == key]
    if not vals:
        return None
    acc = vals[0]
    for v in vals[1:]:
        acc = meet(spec, acc, v)
    return acc


def resolve_grants(
    *, user_rows: list[dict], project_rows: list[dict], global_rows: list[dict]
) -> dict[str, Any]:
    """Resolve every catalog key for one principal. granted = most-specific scope
    that sets it (user>project>global) else catalog default; restrict-only keys
    are clamped to the meet of every scope that set the key (decision 22 — a child
    can never widen past a parent cap)."""
    out: dict[str, Any] = {}
    for key, spec in CATALOG.items():
        u = _scope_value(user_rows, key, spec)
        p = _scope_value(project_rows, key, spec)
        gl = _scope_value(global_rows, key, spec)
        set_pairs = [(s, v) for s, v in ((2, u), (1, p), (0, gl)) if v is not None]
        if not set_pairs:
            out[key] = spec["default"]
            continue
        granted = max(set_pairs, key=lambda t: t[0])[1]
        if spec["restrict_only"]:
            eff = granted
            for _s, v in set_pairs:
                eff = meet(spec, eff, v)
            out[key] = eff
        else:
            out[key] = granted
    return out


def _truthy(x: Any) -> bool:
    return x not in (None, False, 0, "", [], {})


def _fragment_models(fragment: dict) -> list[str]:
    llm = fragment.get("llm") or {}
    out = []
    for v in (
        llm.get("model"),
        (llm.get("strategic") or {}).get("model"),
        (llm.get("tactical") or {}).get("model"),
    ):
        if isinstance(v, str) and v:
            out.append(v)
    return out


def _enum_exceeds(value: Any, ceiling: str, order: list[str]) -> bool:
    return (
        isinstance(value, str)
        and value in order
        and order.index(value) > order.index(ceiling)
    )


def evaluate(fragment: dict, grants: dict, *, is_admin: bool = False) -> list[str]:
    """Violation messages for a config vs a resolved grant set ([] = allowed). The
    single PDP: fed the raw expert fragment at save-time and the full merged config
    at dispatch. Admins short-circuit. Absent gated keys never violate."""
    if is_admin:
        return []
    v: list[str] = []
    tools = fragment.get("tools") or {}
    ws = fragment.get("workspace") or {}
    deleg = fragment.get("delegation") or {}
    inter = fragment.get("interactive") or {}

    if not grants.get("vm_workspace", False) and ws.get("backend") == "vm":
        v.append("vm_workspace: workspace.backend='vm' requires the vm_workspace grant")
    if not grants.get("shell_tools", False) and _truthy(tools.get("shell")):
        v.append("shell_tools: tools.shell requires the shell_tools grant")
    # delegation gates on the .enabled flag (a settings dict) OR a non-empty tool list,
    # NOT mere presence of the settings dict.
    if not grants.get("delegation", False) and (
        deleg.get("enabled") is True or _truthy(tools.get("delegation"))
    ):
        v.append("delegation: delegation requires the delegation grant")
    if not grants.get("datasource_tools", True) and any(
        _truthy(tools.get(k)) for k in ("sql", "mongodb", "graph")
    ):
        v.append("datasource_tools: datasource tools are not permitted")
    if not grants.get("browser", True) and _truthy(tools.get("browser_direct")):
        v.append("browser: tools.browser_direct is not permitted")

    allowed = grants.get("model_selection")  # None = all
    if allowed is not None:
        for m in _fragment_models(fragment):
            if m not in allowed:
                v.append(f"model_selection: model '{m}' is not in the permitted set")

    if _enum_exceeds(
        fragment.get("autonomy"),
        grants.get("autonomy_ceiling", "review"),
        _AUTONOMY_ORDER,
    ):
        v.append(
            f"autonomy_ceiling: autonomy '{fragment.get('autonomy')}' exceeds the ceiling"
        )
    if _enum_exceeds(
        inter.get("permission_mode"),
        grants.get("permission_mode", "supervised"),
        _PERMISSION_ORDER,
    ):
        v.append(
            f"permission_mode: '{inter.get('permission_mode')}' exceeds the ceiling"
        )
    return v
