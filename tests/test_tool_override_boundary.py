"""One tool vocabulary at every write boundary.

Two defects, one root cause: ``tools.<category>`` was validated by a
hand-curated name list covering four of the registry's twenty-four categories,
and only on the session surface.

**Defect 2 — the fail-open half.** The New Session form renders twelve tool
categories and writes ``tools[cat] = []`` for every one the user unticks
(``cockpit/.../tools-group.component.ts``). The create boundary copied across
only the four allowlisted groups and the runtime-update boundary replaced
``tools`` wholesale with the accepted subset, so unticking ``research``,
``browser_direct``, ``citation``, ``shell``, ``communication``, ``delegation``,
``knowledge`` or ``git`` was **accepted and discarded**. The user was shown a
restriction that was never applied.

**Defect 8 — the security half.** ``POST /api/jobs`` had no tool allowlist at
all: it stripped four lifecycle markers and accepted
``tools.<anything>: [<any registered name>]``. ``load_tools`` resolves a name
against the global registry rather than the key it arrived under, so
``tools.canvas: ["run_command"]`` binds a shell tool — and the PDP reads
``_truthy(tools.get("shell"))``, a category key, so it never sees it.

The fix is one generic validator
(``src.core.tool_policy.validate_tool_override_fragment``) at all three
boundaries. The rule: the category must be real and every name in it must
belong to that category *per the registry*. Anything the boundary will not
honour is **rejected with a 400** — never silently dropped, which would only
replace one silent drop with another.

What this validator is NOT: an authorization gate. Capability grants
(``src/core/capability_grants.py``) remain the single PDP, and
``TestTheGateIsNotThePDP`` pins that separation.

Design: docs/features/tool_config_policy_vs_membership.md.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES
from src.core.tool_policy import (
    MCP_WILDCARD,
    ToolPolicyError,
    validate_tool_override_fragment,
)
from src.tools.registry import TOOL_REGISTRY, get_tools_by_category

#: The categories the cockpit's New Session form renders as checkboxes, in
#: order. Mirror of SESSION_TOOL_CATEGORIES in
#: cockpit/src/app/views/agent-settings/agent-settings.types.ts — the payloads
#: the SHIPPED cockpit sends are the compatibility surface for this change.
COCKPIT_SESSION_CATEGORIES = (
    "research",
    "browser_direct",
    "citation",
    "shell",
    "communication",
    "delegation",
    "canvas",
    "orchestrator",
    "agent_catalog",
    "workflows",
    "knowledge",
    "git",
)

#: The four the boundary used to honour. The other eight were the defect.
PREVIOUSLY_HONOURED = frozenset(SESSION_TOOL_OVERRIDE_NAMES)
PREVIOUSLY_DISCARDED = tuple(
    c for c in COCKPIT_SESSION_CATEGORIES if c not in PREVIOUSLY_HONOURED
)


# =============================================================================
# Defect 2 — the eight discarded categories
# =============================================================================


class TestEveryRenderedCategoryIsHonoured:
    def test_the_defect_covered_eight_of_the_twelve(self):
        """Guard the premise: if the form's category list moves, this test
        tells you before the coverage below quietly narrows."""
        assert len(COCKPIT_SESSION_CATEGORIES) == 12
        assert len(PREVIOUSLY_DISCARDED) == 8
        assert set(PREVIOUSLY_DISCARDED) == {
            "research",
            "browser_direct",
            "citation",
            "shell",
            "communication",
            "delegation",
            "knowledge",
            "git",
        }

    @pytest.mark.parametrize("category", PREVIOUSLY_DISCARDED)
    def test_unticking_a_previously_discarded_category_is_honoured(self, category):
        """THE motivating defect: the payload the cockpit sends for an
        unticked box now survives the boundary."""
        assert validate_tool_override_fragment({"tools": {category: []}}) == {
            category: []
        }

    @pytest.mark.parametrize("category", COCKPIT_SESSION_CATEGORIES)
    def test_every_rendered_category_round_trips(self, category):
        """Whole-form deselect: all twelve, all honoured, none dropped."""
        fragment = {"tools": {c: [] for c in COCKPIT_SESSION_CATEGORIES}}
        assert validate_tool_override_fragment(fragment)[category] == []

    def test_re_enable_payloads_from_the_shipped_bases_validate(self):
        """The other half of what the shipped cockpit sends.

        Re-ticking a category the expert disabled posts
        ``tools[cat] = [...defaults_tools[cat]]`` — the mode base's own list,
        served by ``GET /api/experts/{id}``. Honouring those is only safe if
        they are in-category, so assert it against the registry rather than
        trusting it.
        """
        from src.core.loader import load_and_merge_config, resolve_config_path

        for base in ("session_base", "worker_base"):
            path, _ = resolve_config_path(base)
            tools = (load_and_merge_config(path) or {}).get("tools") or {}
            populated = {k: v for k, v in tools.items() if v}
            assert populated, f"{base} declares no non-empty tool list"
            assert validate_tool_override_fragment({"tools": populated}) == populated


# =============================================================================
# Defect 8 — the smuggle, and it is generic now
# =============================================================================


class TestCrossCategorySmuggling:
    @pytest.mark.parametrize(
        ("category", "smuggled"),
        [
            ("canvas", "run_command"),
            ("canvas", "shell_execute"),
            ("agent_catalog", "create_worker_job"),
            ("workflows", "get_skill"),
            ("orchestrator", "run_command"),
            # The eight that were not validated at all before.
            ("citation", "run_command"),
            ("knowledge", "browser_navigate"),
            ("git", "kb_write"),
            ("research", "spawn_subagent"),
            # And the ones no surface ever checked.
            ("workspace", "run_command"),
            ("core", "web_search"),
            ("loop", "approve_job"),
        ],
    )
    def test_a_foreign_name_is_rejected(self, category, smuggled):
        with pytest.raises(ToolPolicyError) as exc:
            validate_tool_override_fragment({"tools": {category: [smuggled]}})
        message = str(exc.value)
        assert f"tools.{category}" in message
        assert smuggled in message
        # The message names where the tool actually lives, so the caller can
        # fix the fragment instead of guessing.
        assert f"tools.{TOOL_REGISTRY[smuggled]['category']}" in message

    def test_a_smuggle_hidden_in_a_policy_mapping_is_rejected(self):
        """``{only: [...]}`` is the same list in a different coat."""
        with pytest.raises(ToolPolicyError, match="run_command"):
            validate_tool_override_fragment(
                {"tools": {"canvas": {"only": ["run_command"]}}}
            )

    def test_an_unregistered_name_is_rejected_rather_than_swallowed(self):
        """``load_tools`` falls back to per-name loading and *logs* an unknown
        name away (src/agent.py). A typo that silently binds nothing is the
        same class of lie as an ignored restriction — say it here."""
        with pytest.raises(ToolPolicyError) as exc:
            validate_tool_override_fragment({"tools": {"git": ["git_lug"]}})
        assert "not a registered tool" in str(exc.value)


class TestMcpHasNoStaticMembership:
    """``register_mcp_tools`` populates the category per session and is never
    called in the orchestrator process, so membership cannot be checked
    positively. The rule inverts: a name the registry knows under a different
    category is the smuggle; an unrecognised one is a presumed runtime tool."""

    def test_a_registered_foreign_name_is_still_rejected(self):
        with pytest.raises(ToolPolicyError, match="run_command"):
            validate_tool_override_fragment({"tools": {"mcp": ["run_command"]}})

    def test_an_unrecognised_name_passes_as_a_runtime_discovered_tool(self):
        assert validate_tool_override_fragment(
            {"tools": {"mcp": ["notion__search_pages"]}}
        ) == {"mcp": ["notion__search_pages"]}

    def test_true_resolves_to_the_wildcard_sentinel(self):
        assert validate_tool_override_fragment({"tools": {"mcp": True}}) == {
            "mcp": [MCP_WILDCARD]
        }


# =============================================================================
# Rejection, not silence — the shape of every refusal
# =============================================================================


class TestRejectDoNotDrop:
    def test_an_unknown_category_is_rejected_not_ignored(self):
        """``normalize_tool_policy`` passes an unknown key through verbatim
        because a config file's typo is caught by schema.json at authoring
        time. A request has no schema pass, so this is where it is caught."""
        with pytest.raises(ToolPolicyError) as exc:
            validate_tool_override_fragment({"tools": {"workspaces": []}})
        assert "unknown tool category" in str(exc.value)
        assert "workspace" in str(exc.value)  # the known-categories listing

    def test_a_non_object_tools_value_is_rejected(self):
        with pytest.raises(ToolPolicyError, match="keyed by tool category"):
            validate_tool_override_fragment({"tools": ["research"]})

    def test_a_non_string_category_key_is_rejected(self):
        with pytest.raises(ToolPolicyError, match="must be strings"):
            validate_tool_override_fragment({"tools": {3: []}})

    def test_a_non_list_category_value_is_rejected(self):
        with pytest.raises(ToolPolicyError, match="tools.agent_catalog"):
            validate_tool_override_fragment({"tools": {"agent_catalog": "disabled"}})

    def test_a_non_string_tool_name_is_rejected(self):
        with pytest.raises(ToolPolicyError, match="tool-name strings"):
            validate_tool_override_fragment({"tools": {"git": ["git_log", 7]}})

    def test_an_absent_tools_key_is_not_an_error(self):
        assert validate_tool_override_fragment({"llm": {"model": "x"}}) == {}
        assert validate_tool_override_fragment({}) == {}
        assert validate_tool_override_fragment(None) == {}

    def test_an_empty_tools_object_asks_for_nothing(self):
        assert validate_tool_override_fragment({"tools": {}}) == {}

    def test_the_coding_alias_normalises_to_shell(self):
        assert validate_tool_override_fragment(
            {"tools": {"coding": ["run_command"]}}
        ) == {"shell": ["run_command"]}

    def test_an_alias_collision_is_rejected_rather_than_resolved(self):
        """Two values for one category: picking one is the silent drop."""
        with pytest.raises(ToolPolicyError, match="both address the shell category"):
            validate_tool_override_fragment(
                {"tools": {"coding": ["run_command"], "shell": []}}
            )


# =============================================================================
# Constraints inherited from tasks 4 and 5
# =============================================================================


class TestInheritedConstraints:
    def test_only_is_returned_as_written_never_intersected(self):
        """``config/experts/centurion`` names two ``grant: "explicit"`` tools;
        intersecting with ``true``'s expansion would silently revoke them."""
        assert validate_tool_override_fragment(
            {
                "tools": {
                    "orchestrator": {"only": ["steer_worker_job", "get_stuck_jobs"]}
                }
            }
        ) == {"orchestrator": ["steer_worker_job", "get_stuck_jobs"]}

    @pytest.mark.parametrize(
        "name",
        sorted(
            n
            for n, m in TOOL_REGISTRY.items()
            if m.get("grant") in ("code", "explicit")
        ),
    )
    def test_a_classified_tool_stays_nameable_by_name(self, name):
        """``grant`` restricts category-level POLICY, not an explicit name.
        Confusing "not in ``true``'s expansion" with "not allowed" would make
        this validator a second, stricter grant system."""
        category = TOOL_REGISTRY[name]["category"]
        assert validate_tool_override_fragment({"tools": {category: [name]}}) == {
            category: [name]
        }

    @pytest.mark.parametrize("group", sorted(SESSION_TOOL_OVERRIDE_NAMES))
    def test_a_closed_group_true_still_expands_to_the_curated_vocabulary(self, group):
        """The registry's ``explicit`` tier is what holds this, not a name
        list — so a user ticking "Experts & Skills" cannot acquire
        ``set_expert_bundle`` through a category-level ``true``."""
        assert validate_tool_override_fragment({"tools": {group: True}})[
            group
        ] == sorted(SESSION_TOOL_OVERRIDE_NAMES[group])

    def test_shell_accepts_only_and_false_and_nothing_else(self):
        assert validate_tool_override_fragment({"tools": {"shell": []}}) == {
            "shell": []
        }
        assert validate_tool_override_fragment({"tools": {"shell": False}}) == {
            "shell": []
        }
        assert validate_tool_override_fragment(
            {"tools": {"shell": {"only": ["run_command", "shell_read"]}}}
        ) == {"shell": ["run_command", "shell_read"]}
        for refused in (True, {"except": ["shell_read"]}, {"except": []}):
            with pytest.raises(ToolPolicyError, match="must enumerate"):
                validate_tool_override_fragment({"tools": {"shell": refused}})

    def test_bughunters_bare_shell_list_is_legal(self):
        names = ["run_command", "shell_execute", "shell_read", "cancel_command"]
        assert validate_tool_override_fragment({"tools": {"shell": names}}) == {
            "shell": names
        }


class TestOutputIsCanonical:
    """The PDP reads ``_truthy(tools.get("shell"))``. Handing it a raw policy
    value gets two rows wrong (``{}`` reads false, ``{only: []}`` reads true),
    which is why the boundary normalises before the grant check runs."""

    @pytest.mark.parametrize(
        "value",
        [True, False, [], ["git_log"], {"only": ["git_log"]}, {"except": ["git_log"]}],
    )
    def test_every_accepted_form_leaves_as_a_list_of_strings(self, value):
        result = validate_tool_override_fragment({"tools": {"git": value}})["git"]
        assert isinstance(result, list)
        assert all(isinstance(n, str) for n in result)

    def test_the_two_truthy_hazards_never_reach_the_pdp(self):
        for hazard in ({}, {"only": []}):
            with pytest.raises(ToolPolicyError):
                validate_tool_override_fragment({"tools": {"git": hazard}})


class TestTheGateIsNotThePDP:
    """A shape-and-vocabulary gate accepts things the PDP will refuse. If this
    validator started refusing them too, it would be a second authorization
    system — the root cause the whole series is removing."""

    def test_a_grant_gated_category_validates_fine_here(self):
        assert validate_tool_override_fragment(
            {"tools": {"shell": ["run_command"], "browser_direct": ["browser_click"]}}
        ) == {"shell": ["run_command"], "browser_direct": ["browser_click"]}

    def test_the_pdp_still_sees_what_this_lets_through(self):
        from src.core.capability_grants import evaluate

        fragment = {
            "tools": validate_tool_override_fragment(
                {"tools": {"shell": ["run_command"]}}
            )
        }
        assert evaluate(fragment, {"shell_tools": False}) == [
            "shell_tools: tools.shell requires the shell_tools grant"
        ]


# =============================================================================
# The orchestrator wrappers
# =============================================================================


class TestOrchestratorWrapper:
    def test_a_rejection_becomes_a_400(self):
        import orchestrator.main as orch_main

        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_tool_overrides({"tools": {"canvas": ["run_command"]}})
        assert exc.value.status_code == 400
        assert "run_command" in exc.value.detail

    def test_the_fragment_helper_replaces_tools_without_mutating_the_input(self):
        import orchestrator.main as orch_main

        original = {"tools": {"git": True}, "autonomy": "full"}
        result = orch_main._with_validated_tool_overrides(original)

        assert result["tools"] == sorted_git_expansion()
        assert result["autonomy"] == "full"
        assert original["tools"] == {"git": True}, "caller-owned fragment mutated"

    def test_a_fragment_without_tools_is_returned_untouched(self):
        import orchestrator.main as orch_main

        fragment = {"llm": {"model": "x"}}
        assert orch_main._with_validated_tool_overrides(fragment) is fragment
        assert orch_main._with_validated_tool_overrides(None) is None


def sorted_git_expansion() -> dict[str, list[str]]:
    return {
        "git": sorted(
            n for n in get_tools_by_category("git") if "grant" not in TOOL_REGISTRY[n]
        )
    }


# =============================================================================
# The job boundary — Defect 8 end to end
# =============================================================================

USER_ID = str(uuid.uuid4())
JOB_ID = str(uuid.uuid4())


@pytest.fixture
def job_request():
    """A cockpit/public caller. ``_INTERNAL_KEY`` is patched to something else
    in ``_create_job``, so this header is not a key — the internal-path tests
    set it explicitly."""
    return SimpleNamespace(headers={}, query_params={})


@pytest.fixture
def job_db():
    db = MagicMock()
    db.get_user = AsyncMock(return_value={"id": USER_ID, "is_admin": False})
    db.get_project = AsyncMock(return_value=None)
    db.get_job = AsyncMock(return_value=None)
    db.get_thread = AsyncMock(return_value=None)
    db.get_datasource = AsyncMock(return_value=None)
    db.link_datasource_to_job = AsyncMock()
    db.create_job = AsyncMock(return_value={"id": JOB_ID, "status": "created"})
    return db


async def _create_job(db, request, body):
    import security.access as access_module
    from main import create_job

    user = {"id": USER_ID, "is_admin": False}
    patches = [
        patch.object(access_module, "_INTERNAL_KEY", "secret"),
        patch("main.require_approved_user", AsyncMock(return_value=user)),
        patch("security.access.require_approved_user", AsyncMock(return_value=user)),
        patch("main.postgres_db", db),
        patch("main._enforce_readiness_gate", AsyncMock(return_value=None)),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._require_job_project_access", AsyncMock(return_value=None)),
        patch("main._is_experts_db_enabled", MagicMock(return_value=False)),
        patch("main._inherit_parent_datasource_ids", AsyncMock(return_value=[])),
        patch("main._authorize_thread_datasource_ids", AsyncMock(return_value=[])),
        patch("main._enforce_job_create_grants", AsyncMock(return_value=None)),
        patch("services.job_provisioning.provision_job_repo", AsyncMock()),
        patch("main._spawn_scholar_subjob", AsyncMock(return_value=None)),
        patch("main._trigger_dispatch", MagicMock()),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return await create_job(request, body)


class TestJobCreateBoundary:
    @pytest.mark.asyncio
    async def test_the_classic_smuggle_is_a_400_on_the_job_surface(
        self, job_db, job_request
    ):
        """Was accepted outright: four lifecycle markers stripped, then
        ``tools.<anything>: [<any registered name>]`` straight through, with
        only a dispatch PDP that keys off the CATEGORY behind it."""
        from fastapi import HTTPException
        from main import JobCreate

        with pytest.raises(HTTPException) as exc:
            await _create_job(
                job_db,
                job_request,
                JobCreate(
                    description="d",
                    config_override={"tools": {"canvas": ["run_command"]}},
                ),
            )
        assert exc.value.status_code == 400
        assert "run_command" in exc.value.detail
        job_db.create_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_internal_path_is_validated_too(self, job_db, job_request):
        """``X-Internal-Key`` is transport authentication, not authorization,
        and ``create_worker_job`` forwards a MODEL-AUTHORED config_override
        verbatim — which is precisely a caller that can write this fragment."""
        from fastapi import HTTPException
        from main import JobCreate

        parent = str(uuid.uuid4())
        job_db.get_job = AsyncMock(
            return_value={"id": parent, "user_id": USER_ID, "project_id": None}
        )
        internal_request = SimpleNamespace(
            headers={"X-Internal-Key": "secret"}, query_params={}
        )
        with pytest.raises(HTTPException) as exc:
            await _create_job(
                job_db,
                internal_request,
                JobCreate(
                    description="d",
                    parent_job_id=parent,
                    config_override={"tools": {"citation": ["shell_execute"]}},
                ),
            )
        assert exc.value.status_code == 400
        assert "shell_execute" in exc.value.detail
        job_db.create_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_legitimate_fragment_is_persisted_normalised(
        self, job_db, job_request
    ):
        from main import JobCreate

        await _create_job(
            job_db,
            job_request,
            JobCreate(
                description="d",
                config_override={"tools": {"evaluation": True, "communication": []}},
            ),
        )
        persisted = job_db.create_job.await_args.kwargs["config_override"]
        assert persisted["tools"]["communication"] == []
        assert persisted["tools"]["evaluation"] == [
            "approve_job",
            "return_job_with_feedback",
        ]

    @pytest.mark.asyncio
    async def test_the_agents_verification_job_fragment_still_validates(
        self, job_db, job_request
    ):
        """``src/api/orchestrator_client.py`` POSTs this exact fragment for
        every self-verifying job — the one real internal caller with a
        ``tools`` block on this endpoint."""
        from main import JobCreate

        await _create_job(
            job_db,
            job_request,
            JobCreate(
                description="d",
                config_override={
                    "autonomy": "full",
                    "tools": {
                        "evaluation": ["approve_job", "return_job_with_feedback"]
                    },
                },
            ),
        )
        job_db.create_job.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_job_without_tools_is_untouched(self, job_db, job_request):
        from main import JobCreate

        await _create_job(
            job_db,
            job_request,
            JobCreate(description="d", config_override={"autonomy": "full"}),
        )
        persisted = job_db.create_job.await_args.kwargs["config_override"]
        assert persisted == {"autonomy": "full"}


# =============================================================================
# The runtime-update boundary
# =============================================================================


class TestLiveSessionSanitiser:
    def test_every_category_survives_the_agent_side_sanitiser(self):
        from src.api.persistent_app import _sanitize_live_session_config_override

        result = _sanitize_live_session_config_override(
            {"tools": {"canvas": [], "research": [], "shell": ["run_command"]}}
        )
        assert result["tools"] == {
            "canvas": [],
            "research": [],
            "shell": ["run_command"],
        }

    def test_a_smuggle_raises_rather_than_being_filtered_out(self):
        from src.api.persistent_app import _sanitize_live_session_config_override

        with pytest.raises(ValueError, match="run_command"):
            _sanitize_live_session_config_override(
                {"tools": {"canvas": ["run_command"]}}
            )

    def test_the_caller_owned_payload_is_not_mutated(self):
        from src.api.persistent_app import _sanitize_live_session_config_override

        payload = {"tools": {"canvas": True}}
        _sanitize_live_session_config_override(payload)
        assert payload == {"tools": {"canvas": True}}
