"""The registry's grant classification, pinned.

``TOOL_REGISTRY`` entries may carry ``grant`` (``"code"`` / ``"explicit"``) and
``gate``.  Nothing reads them yet — the category-level ``true`` expansion that
will consume them lands separately
(knowledge-base/knowledge/features/tool_config_policy_vs_membership.md, "Step A: classify code-only
tools in the registry").  Metadata with no reader is exactly the kind of thing
that rots invisibly, so these tests do two jobs:

1. hold the classification itself — that the code-granted set is the datasource
   map plus five named runtime floors, that the legacy experts-off shim is NOT
   marked, and that every mark carries the gate that makes it auditable;
2. prove the classification is *usable* by computing what ``tools.<cat>: true``
   would expand to under it, and asserting that for the closed session
   groups that is byte-for-byte ``SESSION_TOOL_OVERRIDE_NAMES``.

Test 2 is the safety gate for the whole migration.  ``true`` is strictly wider
than the hand-curated session vocabulary — nine registry entries sit in a
selectable category and are absent from it, six of them control-plane writes
such as ``set_expert_bundle``.  Without this assertion a user ticking
"Experts & Skills" would silently gain the ability to rewrite the expert
catalogue.
"""

from pathlib import Path

import pytest
import yaml

from src.core.datasource_setup import DATASOURCE_TOOL_MAP
from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES
from src.shared.orch_surface.jobs import JOB_DESCRIPTORS
from src.tools.registry import (
    CODE_GRANTED_CATEGORIES,
    TOOL_REGISTRY,
    get_tools_by_category,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _REPO_ROOT / "config"

_VALID_GRANTS = {"code", "explicit"}

# The legacy experts-off compatibility shim: ``persistent_session.py:1470-1520``
# re-appends these canonical lists whenever the matching disable marker is
# absent.  They are NOT code grants — on the resolved path config still decides,
# so marking them would make ``orchestrator`` / ``agent_catalog`` / ``workflows``
# permanently un-enableable, which is the current bug re-introduced by its own
# fix.  Transcribed from the three literals at that site.
_LEGACY_SHIM_TOOLS = frozenset(
    {
        # _ORCHESTRATOR_TOOLS (:1470-1485)
        "get_session_context",
        # Descriptor-derived job_control/job_inspection compatibility defaults
        "create_job",
        "list_jobs",
        "get_job",
        "get_job_file",
        "list_job_files",
        # E4 (officer_supervision_surface): bounded evidence reads joined the
        # session job_inspection defaults.
        "get_job_completion_report",
        "list_job_evidence",
        "read_job_evidence",
        "approve_job",
        "resume_job_with_feedback",
        "cancel_job",
        "pause_job",
        "get_current_project",
        "list_project_jobs",
        "list_project_repositories",
        "get_default_project_repository",
        # _AGENT_CATALOG_TOOLS (:1495-1501)
        "list_experts",
        "get_expert",
        "list_skills",
        "search_skills",
        "get_skill",
        # _WORKFLOW_DEFAULT_TOOLS (:1509-1517)
        "list_automations",
        "get_automation",
        "list_automation_runs",
        "propose_automation",
        "get_project_loop",
        "list_project_loop_jobs",
        "explain_project_loop",
    }
)

# Code writing a *config fragment* is a config grant, not a code grant.
# ``_critic_config_override`` stamps ``tools.evaluation`` onto every critic job
# and ``project_loops`` stamps ``tools.loop`` onto a checkpoint critic, so
# ``evaluation: true`` and ``loop: true`` must keep resolving to these.
_CONFIG_OVERRIDE_STAMPED = frozenset(
    {"approve_job_verdict", "return_job_with_feedback", "loop_plan"}
)

# Runtime floors classified one tool at a time because they are individual
# injections rather than whole categories.  Each is a single append site in
# ``persistent_session.py`` (``request_workspace_upgrade`` also in
# ``agent.py``); the value is the site, for the failure message.
_PER_TOOL_CODE_GRANTS = {
    "sleep": "persistent_session.py:1554-1557",
    "notify_user": "persistent_session.py:1554-1557",
    "request_workspace_upgrade": "persistent_session.py:1547, agent.py:3078",
    "srw_cloud_status": "persistent_session.py:1526",
    "checkout_project_repository": "persistent_session.py:1540",
}


def _classified(grant: str) -> set[str]:
    return {name for name, meta in TOOL_REGISTRY.items() if meta.get("grant") == grant}


def _datasource_granted_names() -> set[str]:
    """Every tool name any attached datasource can put into a tools list."""
    names: set[str] = set()
    for info in DATASOURCE_TOOL_MAP.values():
        names.update(info.get("read", ()))
        names.update(info.get("write", ()))
        for tier_names in info.get("tiers", {}).values():
            names.update(tier_names)
    return names


def _shipped_config_tool_names() -> dict[str, list[str]]:
    """Tool name -> the config files that name it.

    Raw (pre-``$extends``) declarations, scanned RECURSIVELY over the whole
    config tree.  A ``config/*.yaml`` glob silently misses
    ``config/experts/*/config.yaml``, which is where the majority of tool
    declarations live.
    """
    found: dict[str, list[str]] = {}
    paths = sorted(
        p
        for pattern in ("**/*.yaml", "**/*.yml")
        for p in _CONFIG_DIR.glob(pattern)
        if p.is_file()
    )
    assert paths, "no config YAML found — the scan is looking in the wrong place"
    for path in paths:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            continue
        tools = data.get("tools")
        if not isinstance(tools, dict):
            continue
        for names in tools.values():
            if not isinstance(names, list):
                continue
            for name in names:
                if isinstance(name, str):
                    found.setdefault(name, []).append(str(path.relative_to(_REPO_ROOT)))
    return found


def expand_true(category: str) -> set[str]:
    """What ``tools.<category>: true`` would resolve to under the markers.

    Reference implementation of the contract documented in
    ``src/tools/registry.py``; the production version ships with the
    normaliser.  Kept in the test module so this commit adds no reader.
    """
    return {
        name
        for name in get_tools_by_category(category)
        if "grant" not in TOOL_REGISTRY[name]
    }


class TestClassificationShape:
    def test_grant_values_are_from_the_closed_set(self):
        bad = {
            name: meta["grant"]
            for name, meta in TOOL_REGISTRY.items()
            if "grant" in meta and meta["grant"] not in _VALID_GRANTS
        }
        assert not bad, f"unknown grant values: {bad}"

    def test_every_classified_tool_names_its_gate(self):
        """Without ``gate`` the rule is folklore, which is how we got here."""
        missing = sorted(
            name
            for name, meta in TOOL_REGISTRY.items()
            if "grant" in meta and not str(meta.get("gate") or "").strip()
        )
        assert not missing, f"classified without a gate: {missing}"

    def test_gate_only_appears_on_classified_tools(self):
        """``gate`` explains a classification; alone it would mean nothing."""
        stray = sorted(
            name
            for name, meta in TOOL_REGISTRY.items()
            if "gate" in meta and "grant" not in meta
        )
        assert not stray, f"gate without grant: {stray}"


class TestCodeGrants:
    def test_code_granted_set_is_the_datasource_map_plus_the_named_floors(self):
        """Derived, not transcribed: a new connector tool joins automatically."""
        category_granted = {
            name
            for category in CODE_GRANTED_CATEGORIES
            for name in get_tools_by_category(category)
        }
        expected = category_granted | set(_PER_TOOL_CODE_GRANTS)
        assert _classified("code") == expected

    def test_every_datasource_category_is_wholly_code_granted(self):
        """Every connector tool must be reachable from ``DATASOURCE_TOOL_MAP``.

        Both directions matter.  A registered connector tool the map cannot
        grant is unreachable (no config lists these categories); a mapped name
        that is not classified would leak into a category-level ``true``.
        """
        mapped = _datasource_granted_names()
        for info in DATASOURCE_TOOL_MAP.values():
            if info.get("dynamic"):
                continue  # mcp resolves after discovery — see TestMcp
            category = info["category"]
            assert set(get_tools_by_category(category)) == {
                name for name in mapped if TOOL_REGISTRY[name]["category"] == category
            }, f"{category} registry membership and DATASOURCE_TOOL_MAP disagree"
            for name in get_tools_by_category(category):
                assert TOOL_REGISTRY[name].get("grant") == "code", name

    def test_code_granted_count_is_pinned(self):
        """40 = the reviewed repo_pr_status read + the repo_checkout write.

        Both joined by construction: a tool registered in a datasource
        category is code-granted whether or not anyone remembers this pin.
        Bump the number only after checking the new arrival really belongs to
        a connector — the derived test above is what proves membership.
        """
        assert len(_classified("code")) == 40

    def test_no_shipped_config_names_a_code_granted_tool(self):
        """A config naming one is either dead text or a misunderstanding.

        Both readings are bugs: the tool's real switch is its ``gate``, so the
        config line changes nothing while looking like it does.
        """
        by_name = _shipped_config_tool_names()
        offenders = {
            name: sorted(set(files))
            for name, files in by_name.items()
            if TOOL_REGISTRY.get(name, {}).get("grant") == "code"
        }
        assert not offenders, (
            "shipped configs name code-granted tools (their real switch is the "
            f"registry ``gate``, not this list): {offenders}"
        )

    def test_the_scan_actually_reaches_the_expert_configs(self):
        """Guards the test above against a glob that silently finds nothing."""
        by_name = _shipped_config_tool_names()
        sources = {f for files in by_name.values() for f in files}
        assert any(f.startswith("config/experts/") for f in sources), (
            f"expert configs missing from the scan: {sorted(sources)}"
        )
        assert "config/expert_base.yaml" in sources
        assert "config/overlays/worker.yaml" in sources
        assert "config/overlays/session.yaml" in sources
        assert "config/overlays/subagent.yaml" in sources


class TestNotClassified:
    def test_the_legacy_experts_off_shim_is_not_marked(self):
        """Marking these would make their groups permanently un-enableable."""
        marked = sorted(
            name for name in _LEGACY_SHIM_TOOLS if "grant" in TOOL_REGISTRY[name]
        )
        assert not marked, (
            "legacy experts-off shim tools must stay config-grantable "
            f"(persistent_session.py:1470-1520): {marked}"
        )

    def test_the_shim_set_still_matches_the_runtime_lists(self):
        """The shim is 29 names across five categories; drift breaks this gate.

        (26 → 29 with E4's three job_evidence reads in the session defaults.)
        """
        assert len(_LEGACY_SHIM_TOOLS) == 29
        assert _LEGACY_SHIM_TOOLS <= set(TOOL_REGISTRY)
        assert _LEGACY_SHIM_TOOLS == frozenset(
            SESSION_TOOL_OVERRIDE_NAMES["orchestrator"]
            | SESSION_TOOL_OVERRIDE_NAMES["job_control"]
            | SESSION_TOOL_OVERRIDE_NAMES["job_inspection"]
            | SESSION_TOOL_OVERRIDE_NAMES["agent_catalog"]
            | SESSION_TOOL_OVERRIDE_NAMES["workflows"]
        )

    def test_config_override_stamped_tools_are_not_marked(self):
        marked = sorted(
            name for name in _CONFIG_OVERRIDE_STAMPED if "grant" in TOOL_REGISTRY[name]
        )
        assert not marked, (
            "code that writes a config fragment is a config grant; "
            f"`evaluation: true` / `loop: true` must still reach these: {marked}"
        )


class TestExplicitGrants:
    def test_explicit_set_is_pinned(self):
        descriptor_explicit = {
            item.name for item in JOB_DESCRIPTORS if item.grant == "explicit"
        }
        assert _classified("explicit") == descriptor_explicit | {
            # Built-in subagents (U3/U4): each name is written outright in
            # tools.delegation AND gated on delegation.enabled; legacy
            # `delegation: true` expands only to delegate_agent, never the U4
            # controls. (The heavy pair delegate_work /
            # resume_delegation_child was deleted in U3 WP4.)
            "delegate_agent",
            "wait_agent",
            "message_agent",
            "stop_agent",
            "list_agents",
        }
        assert {"steer_job", "get_stuck_jobs"} <= descriptor_explicit
        # The six `*_bundle` tools left this tier on 2026-08-03. They did not
        # become safer — they moved to `catalog_authoring`, a category of their
        # own behind the `catalog_authoring` capability grant, so
        # `agent_catalog: true` cannot reach them because they are not members,
        # not because an exception list says so. Prefer that shape: this tier is
        # for tools whose category genuinely mixes privilege levels.
        assert _classified("explicit").isdisjoint(
            SESSION_TOOL_OVERRIDE_NAMES["catalog_authoring"]
        )

    def test_explicit_tools_stay_nameable(self):
        """centurion names two of them today; that must keep working.

        ``explicit`` restricts category-level *policy*, not an explicit name —
        otherwise this classification would itself be a grant change.
        """
        named = _shipped_config_tool_names()
        assert set(named.get("steer_job", ())) == {
            "config/experts/centurion/config.yaml"
        }
        assert set(named.get("get_stuck_jobs", ())) == {
            "config/experts/centurion/config.yaml"
        }


class TestTrueExpansionIsSafe:
    """The gate for the whole migration: ``true`` must not widen a closed group."""

    @pytest.mark.parametrize("group", sorted(SESSION_TOOL_OVERRIDE_NAMES))
    def test_true_expands_to_exactly_the_session_vocabulary(self, group):
        expanded = expand_true(group)
        vocabulary = set(SESSION_TOOL_OVERRIDE_NAMES[group])
        assert expanded == vocabulary, (
            f"tools.{group}: true would resolve to a different set than the "
            f"closed session vocabulary. Gained {sorted(expanded - vocabulary)}, "
            f"lost {sorted(vocabulary - expanded)}. Either classify the "
            f"difference or justify the widening in a titled commit."
        )

    def test_core_true_is_behaviour_preserving(self):
        """``core: true`` resolves to exactly the six tools configs list today.

        The three omissions every config shares (``sleep``, ``notify_user``,
        ``request_workspace_upgrade``) were never drift — they are an
        unclassified grant path, and the classification is what makes ``true``
        safe to adopt for this category.
        """
        assert expand_true("core") == {
            "next_phase_todos",
            "todo_complete",
            "todo_list",
            "mark_complete",
            "request_replan",
            "job_complete",
        }

    def test_shell_true_still_names_both_halves_of_the_mode_alias(self):
        """Classification removes ``srw_cloud_status`` and nothing else here.

        ``run_command`` and ``shell_execute`` are a mode alias pair —
        ``get_all_tool_names`` (src/core/loader.py) rewrites one to the other
        from ``extra.shell.mode``, so a ``shell: true`` expansion yields a list
        containing the same effective tool twice. That is why the design doc
        says never to migrate ``shell`` to ``true``, and it is not something
        this classification can fix; it is recorded here so the next reader
        does not mistake the expansion for a recommendation.
        """
        assert expand_true("shell") == {
            "run_command",
            "shell_execute",
            "shell_read",
            "cancel_command",
        }

    def test_every_category_can_still_express_on(self):
        """No category may be classified into an empty ``true``...

        ...except the ones where that IS the statement: the six connector
        categories plus the two persistent-session floors have no config-facing
        membership at all, and ``product_help`` / ``session_task`` do not even
        have a ``ToolsConfig`` field to name.
        """
        from src.core.tool_policy import ENUMERATE_ONLY_CATEGORIES

        empty = {
            category
            for category in {meta["category"] for meta in TOOL_REGISTRY.values()}
            if not expand_true(category)
        }
        # `delegation` (U3 WP4) holds one `grant: explicit` tool, so its
        # `true` expansion is empty by design — and the category refuses
        # `true` outright (enumerate-only, like `shell`), so "on" is still
        # expressible: `{only: [delegate_agent]}`, served by
        # `enumerate_only_members`.
        assert empty - ENUMERATE_ONLY_CATEGORIES == set(CODE_GRANTED_CATEGORIES)
        assert empty & ENUMERATE_ONLY_CATEGORIES == {"delegation"}


class TestMcp:
    def test_mcp_has_no_static_registry_membership_to_classify(self):
        """``mcp`` is the one ``ToolsConfig`` field with no registry category.

        ``register_mcp_tools`` populates it per job/session at runtime, so
        ``mcp: true`` normalises to the existing ``"*"`` sentinel instead of
        expanding against the registry. Nothing static exists to mark, and the
        runtime entries must NOT be marked either or the sentinel would resolve
        to nothing.
        """
        assert get_tools_by_category("mcp") == []
        assert "mcp" not in CODE_GRANTED_CATEGORIES
