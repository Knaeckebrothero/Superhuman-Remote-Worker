"""Tool-group policy: an authoring vocabulary that can say "on".

``tools.<category>`` is a list of tool names, and that one field carries two
unrelated facts — **membership** (which tools are in the group) and **policy**
(whether the group is on).  Membership already has an authority:
``TOOL_REGISTRY``, which knows every tool and its category.  Config should
carry policy.

Every registry category has at least one tool, so no group is ever legitimately
empty: every ``[]`` in every config is a disable marker.  That makes "off"
self-describing and "on" inexpressible — re-enabling needs the full member
list, which the caller looks for in the layer it is overriding, which says
``[]``.  Hence the shipped defect: the New Session form can disable a tool
group and can never enable one
(``docs/issues/session_create_tool_toggles_cannot_enable_a_group.md``).

This module is the fix, and it is a **front-end, not a rewrite**.  Five
authoring forms normalise down to the single ``list[str]`` representation the
whole stack already speaks:

===========================  =====================================================
Form                         Resolves to
===========================  =====================================================
``true``                     every config-grantable tool the registry has here
``false``                    ``[]``
``{except: [names]}``        the config-grantable set minus these; tracks the
                             registry
``{only:   [names]}``        exactly these, as written; frozen
``[names]``                  legacy spelling of ``{only: [names]}`` — unchanged
``[]``                       legacy spelling of ``false`` — unchanged
===========================  =====================================================

Both legacy spellings are *already* canonical, so :func:`normalize_tool_policy`
is the **identity function on every declaration that exists today**.  That is
the single most important property here: the enabling commit changes no
resolved tool set anywhere, the eight downstream ``== []`` consumers keep
working untouched, and DB-backed experts need no migration.  It is pinned by
``tests/test_tool_policy.py::TestIdentityProperty`` and by
``tests/test_config_tool_grants_snapshot.py``.

One category is narrower than the table above: **`shell` accepts `only` (or a
bare list) and `false` (or `[]`), and nothing else.** `true` and `except` both
auto-track the registry — `except` is `true` minus a fixed subtraction,
recomputed on every resolution — and a tool added to a code-execution category
must not reach live configs without a diff. See
:data:`ENUMERATE_ONLY_CATEGORIES`.

**Normalisation is layer-local.** Expanding a policy needs only the registry
and the value — never the parent layer.  So it runs per-layer at birth and the
existing ``deep_merge`` "lists replace, dicts merge" semantics carry the whole
layer model unmodified.  A consequence worth stating out loud: a child
``{except: [c]}`` under a parent ``{only: [a, b]}`` resolves to the *whole
category* minus ``c``, which is wider than its parent.  That is intended; see
``TestMergeOrder.test_parent_only_child_except_WIDENS_and_that_is_intended``.

Design: ``docs/features/tool_config_policy_vs_membership.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: The sentinel ``mcp`` resolves to.  Expanded by
#: ``src.tools.registry.expand_tool_wildcards`` *after* MCP discovery.
MCP_WILDCARD = "*"

#: Config keys that are not registry categories but address one.  ``coding`` is
#: the pre-rename spelling of ``shell``, still honoured by ``ToolsConfig``
#: (``tools_data.get("shell", tools_data.get("coding", []))``).  No shipped
#: config uses it; it is mapped here so a policy value under it cannot become a
#: silent ``[]``.
LEGACY_CATEGORY_ALIASES: dict[str, str] = {"coding": "shell"}

#: Categories that must enumerate their tools and may never auto-track the
#: registry.  ``only`` (and its legacy spelling, a bare list) is the sole form
#: that does not auto-track: ``true`` means "this category and whatever is
#: added to it later", and ``{except: [...]}`` means *exactly the same thing*
#: minus a fixed subtraction, recomputed from the registry on every
#: resolution.  For a code-execution category both are the wrong default — a
#: tool added to ``shell`` in the registry would land in every config that used
#: either form, with no diff to review anywhere.  ``only`` forces a titled
#: commit.  ``false`` (and ``[]``) stay legal: turning a category off carries no
#: membership and cannot acquire anything.
#:
#: Note this is *not* the mode-alias argument.  ``run_command`` /
#: ``shell_execute`` being rewritten by ``get_all_tool_names`` from
#: ``extra.shell.mode`` makes naming both halves redundant, not dangerous, and
#: ``config/experts/bughunter`` already names both today.
ENUMERATE_ONLY_CATEGORIES: frozenset[str] = frozenset({"shell"})

_POLICY_KEYS = ("only", "except")


def _enumerate_only_error(category: str, form: str) -> "ToolPolicyError":
    return ToolPolicyError(
        f"tools.{category}: {form} is not accepted. {category} must enumerate "
        f"its tools — write {{only: [...]}} (or a bare list, the same thing) "
        f"or false. Both `true` and `except` auto-track the registry, so a tool "
        f"added to the {category} category later would land here with no diff "
        f"to review; for a code-execution category that is the wrong default. "
        f"See docs/features/tool_config_policy_vs_membership.md, "
        f"'`shell` accepts `only` and `false`'."
    )


class ToolPolicyError(ValueError):
    """A ``tools.<category>`` value is not a legal policy or membership form."""


def _registry():
    """Import the registry lazily.

    ``src/tools/registry`` imports ``src/core/loader`` (through
    ``spawn_subagent``), and ``src/core/loader`` imports this module — so a
    module-level import here would close a cycle.  The list forms, which are
    the overwhelming majority, never reach this.
    """
    from src.tools import registry

    return registry


def config_tool_categories() -> frozenset[str]:
    """Every category a config may address.

    The registry's categories plus ``mcp``, which has no static membership
    because ``register_mcp_tools`` discovers it at runtime.  This is the set
    ``ToolsConfig`` mirrors as dataclass fields and ``config/schema.json``
    mirrors as ``tools`` properties; the three are pinned to agree by
    ``tests/test_tool_policy.py::TestCategoryVocabularyAgreement``.
    """
    return frozenset(_registry().get_categories()) | {"mcp"}


def _grantable(category: str) -> list[str]:
    """The category's members that *config* is allowed to grant, sorted.

    Excludes both classified tiers, for the same reason but not the same rule:

    * ``grant: "code"`` — runtime code binds it instead of config, on a gate
      that lives somewhere else entirely (``officer.enabled``,
      ``cloud_mount.active``, an attached datasource).  Excluding these is what
      makes ``core: true`` reproduce today's six tools exactly.
    * ``grant: "explicit"`` — config may grant it, but only by naming it.
      Holds the four closed session groups' ``true`` expansion at exactly
      ``SESSION_TOOL_OVERRIDE_NAMES``, so a user ticking "Experts & Skills"
      cannot silently acquire ``set_expert_bundle``.

    Neither mark affects an explicitly written name — see
    :func:`expand_tool_policy`.
    """
    reg = _registry()
    return sorted(
        name
        for name in reg.get_tools_by_category(category)
        if "grant" not in reg.TOOL_REGISTRY[name]
    )


def expand_category_true(category: str) -> list[str]:
    """Expand ``tools.<category>: true``.

    Raises:
        ToolPolicyError: for an unknown category, or for ``shell`` — see the
            mode-alias note below.
    """
    _require_known_category(category)

    # ``get_tools_by_category("mcp")`` is PROCESS-LOCAL: ``register_mcp_tools``
    # mutates the global registry per job/session and is never called in the
    # orchestrator process, where resolution happens.  A registry-derived
    # expansion would be ``[]`` in the orchestrator and a different,
    # session-dependent set in every agent.  Keep the existing sentinel.
    if category == "mcp":
        return [MCP_WILDCARD]

    if category in ENUMERATE_ONLY_CATEGORIES:
        raise _enumerate_only_error(category, "true")

    names = _grantable(category)
    if not names:
        # Whole-category ``grant: "code"``.  The expansion is correct — config
        # grants nothing here — but it reads backwards (``sql: true`` resolving
        # to the same ``[]`` that means "off"), so say so rather than let it
        # look like a policy that worked.
        logger.warning(
            "tools.%s: true expands to [] — every tool in this category is "
            "code-granted (%s decides). Config does not manage this category; "
            "delete the key rather than declaring it.",
            category,
            _registry().CODE_GRANTED_CATEGORIES.get(category, "runtime code"),
        )
    return names


def expand_tool_policy(value: Any, category: str) -> list[str]:
    """Resolve one ``tools.<category>`` value to the canonical ``list[str]``.

    Layer-local: consults only ``value`` and the registry, never a parent
    layer.  Always returns a fresh list.

    ``only`` and a bare list are returned **as written** — never intersected
    with the ``true`` expansion.  That asymmetry is the entire point of the
    ``explicit`` tier, and it is load-bearing: ``config/experts/centurion``
    names ``steer_worker_job`` and ``get_stuck_jobs``, both ``explicit``, and
    an intersection would silently revoke them.
    """
    _require_known_category(category)

    if value is True:
        return expand_category_true(category)
    if value is False:
        return []
    if isinstance(value, (list, tuple)):
        return _as_name_list(value, category, "tools.%s" % category)
    if isinstance(value, dict):
        return _expand_mapping(value, category)

    raise ToolPolicyError(
        f"tools.{category}: unsupported value {value!r} "
        f"({type(value).__name__}). Write true, false, a list of tool names, "
        f"{{only: [...]}} or {{except: [...]}}."
    )


def normalize_tool_policy(fragment: Any) -> Any:
    """Return ``fragment`` with every ``tools.<category>`` in canonical form.

    Never mutates the input — config layers are caller-owned and several are
    re-used across resolutions.  A fragment with no ``tools`` mapping is
    returned unchanged, by identity.

    Idempotent, which is what makes the belt-and-braces placement at several
    call sites safe.

    Keys that address no known category pass through **verbatim**, preserving
    today's end-to-end silent-ignore for a typo like ``tools.workspaces``.
    ``config/schema.json``'s ``additionalProperties: false`` is what catches
    those, at authoring time, without changing runtime behaviour.
    """
    if not isinstance(fragment, dict):
        return fragment
    tools = fragment.get("tools")
    if not isinstance(tools, dict):
        return fragment

    known = config_tool_categories()
    normalized: dict[str, Any] = {}
    for key, value in tools.items():
        category = LEGACY_CATEGORY_ALIASES.get(key, key)
        if category not in known:
            normalized[key] = value
            continue
        normalized[key] = expand_tool_policy(value, category)

    return {**fragment, "tools": normalized}


def assert_tool_policy_canonical(tools_data: Any, *, where: str) -> None:
    """Raise unless every addressable ``tools.<category>`` is already a list.

    The last line of defence, immediately before ``ToolsConfig``.  Its fields
    are typed ``List[str]`` and ``get_all_tool_names`` silently returns ``[]``
    for a non-list, so a ``true`` leaking this far would **silently disable**
    the group — the exact failure mode this whole feature exists to repair.
    Raise rather than coerce: a policy value arriving here is a missed
    :func:`normalize_tool_policy` call site, not an input.
    """
    if not isinstance(tools_data, dict):
        return
    known = config_tool_categories()
    for key, value in tools_data.items():
        category = LEGACY_CATEGORY_ALIASES.get(key, key)
        if category not in known:
            continue
        if not isinstance(value, list):
            raise ToolPolicyError(
                f"tools.{key} reached {where} as {type(value).__name__} "
                f"({value!r}). Tool policy must be normalised before the "
                f"dataclass — call normalize_tool_policy() on this fragment. "
                f"See docs/features/tool_config_policy_vs_membership.md."
            )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _require_known_category(category: str) -> None:
    if category not in config_tool_categories():
        raise ToolPolicyError(
            f"tools.{category}: unknown tool category. Known categories: "
            f"{', '.join(sorted(config_tool_categories()))}."
        )


def _as_name_list(value: Iterable[Any], category: str, label: str) -> list[str]:
    names = list(value)
    bad = [n for n in names if not isinstance(n, str)]
    if bad:
        raise ToolPolicyError(
            f"{label}: expected a list of tool-name strings; got {bad!r}."
        )
    return names


def _expand_mapping(value: dict, category: str) -> list[str]:
    keys = set(value)
    present = [k for k in _POLICY_KEYS if k in keys]

    if not keys:
        raise ToolPolicyError(
            f"tools.{category}: an empty mapping is a wordy spelling of false. "
            f"Write false. (It is also a live hazard: the dispatch PDP's "
            f"_truthy() reads {{}} as false and would miss a grant violation.)"
        )
    unknown = sorted(keys - set(_POLICY_KEYS))
    if unknown:
        raise ToolPolicyError(
            f"tools.{category}: unknown policy key(s) {unknown}. "
            f"A mapping takes exactly one of 'only' or 'except'."
        )
    if len(present) != 1:
        raise ToolPolicyError(
            f"tools.{category}: 'only' and 'except' in one mapping. The two "
            f"intuitive readings (only minus except, or one wins) diverge, and "
            f"the combination is reachable by accident through deep_merge. "
            f"Write one or the other."
        )

    key = present[0]
    names = _as_name_list(
        _require_list(value[key], category, key), category, f"tools.{category}.{key}"
    )

    if key == "only":
        if not names:
            raise ToolPolicyError(
                f"tools.{category}: an empty 'only' is a wordy spelling of "
                f"false. Write false. (It is also a live hazard: the dispatch "
                f"PDP's _truthy() reads {{only: []}} as true and would "
                f"fabricate a grant violation for an empty group.)"
            )
        # AS WRITTEN.  Never intersected with expand_category_true — an
        # explicit name is an explicit name, for both `code` and `explicit`
        # marks.  Intersecting would silently revoke centurion's
        # steer_worker_job / get_stuck_jobs.
        return names

    # Checked before the empty-list case so `{except: []}` on an
    # enumerate-only category is told the real rule rather than "write true",
    # which is itself refused there.
    if category in ENUMERATE_ONLY_CATEGORIES:
        raise _enumerate_only_error(category, "'except'")
    if category == "mcp":
        raise ToolPolicyError(
            "tools.mcp: 'except' is not meaningful — MCP tool names are "
            "discovered at runtime, so mcp has no registry membership to "
            "subtract from. Write true (the '*' sentinel), false, or an "
            "explicit list."
        )
    if not names:
        raise ToolPolicyError(
            f"tools.{category}: an empty 'except' is a wordy spelling of true. "
            f"Write true."
        )

    members = set(_registry().get_tools_by_category(category))
    foreign = sorted(set(names) - members)
    if foreign:
        raise ToolPolicyError(
            f"tools.{category}: 'except' names {foreign}, which are not in the "
            f"{category} category. 'except' subtracts from that category and "
            f"nothing else."
        )

    excluded = set(names)
    return [n for n in _grantable(category) if n not in excluded]


def _require_list(value: Any, category: str, key: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise ToolPolicyError(
            f"tools.{category}.{key}: expected a list of tool names; got "
            f"{type(value).__name__}."
        )
    return list(value)


__all__ = [
    "ENUMERATE_ONLY_CATEGORIES",
    "LEGACY_CATEGORY_ALIASES",
    "MCP_WILDCARD",
    "ToolPolicyError",
    "assert_tool_policy_canonical",
    "config_tool_categories",
    "expand_category_true",
    "expand_tool_policy",
    "normalize_tool_policy",
]
