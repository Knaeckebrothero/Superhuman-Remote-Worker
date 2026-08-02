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
(``src.core.tool_policy.validate_tool_override_fragment``) at every boundary
that accepts a caller-supplied ``tools`` fragment. The rule: the category must
be real and every name in it must belong to that category *per the registry*.
Anything the boundary will not honour is **rejected with a 400** — never
silently dropped, which would only replace one silent drop with another.

Six boundaries, and the coverage below is deliberately split between the
validator and each **call site**, because the defect was a filter at the call
site and a validator-level test cannot see one being re-narrowed:

===============================  ==========================================
Boundary                         Call-site class
===============================  ==========================================
``POST /persistent/threads``     ``TestSessionCreateBoundary``
``PATCH .../config`` (runtime)   ``TestSessionRuntimeUpdateBoundary``
``POST /api/jobs``               ``TestJobCreateBoundary``
``POST /sessions/{id}/prepare``  ``TestPrepareBoundary``
``POST``/``PATCH /automations``  ``TestAutomationBoundary``
``POST``/``PATCH /projects``     ``TestProjectDefaultOverrideBoundary``
===============================  ==========================================

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


# =============================================================================
# The session call sites — I2
# =============================================================================
#
# Everything above this line tests the shared validator. That is not enough,
# and the gap was found by review: re-narrowing `create_thread` or
# `_apply_thread_config_update` back to the four closed groups — Defect 2,
# verbatim — left the whole suite green, because both sites delegate to a
# helper the tests exercised directly. A filter at the call site is invisible
# to a validator-level test. These two classes drive the endpoints.

SESSION_THREAD_ID = "11111111-2222-3333-4444-555555555555"
SESSION_USER_ID = str(uuid.uuid4())


@pytest.fixture
def session_create_env(monkeypatch):
    """`POST /api/persistent/threads` with its collaborators stubbed.

    `resolve_config` is left REAL — it reads the shipped YAML, so the
    assertions below prove the fragment survives an actual resolution rather
    than a mock's idea of one.
    """
    import orchestrator.main as main

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__.return_value = conn
    acquire_cm.__aexit__.return_value = False

    db = SimpleNamespace(
        get_user_settings=AsyncMock(return_value={}),
        create_thread=AsyncMock(return_value=SESSION_THREAD_ID),
        acquire=MagicMock(return_value=acquire_cm),
        list_thread_mounts=AsyncMock(return_value=[]),
        replace_thread_mounts=AsyncMock(),
    )
    user = {"id": SESSION_USER_ID, "is_admin": False}

    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(main, "require_approved_user", AsyncMock(return_value=user))
    monkeypatch.setattr(main, "_enforce_readiness_gate", AsyncMock())
    monkeypatch.setattr(
        main, "_authorize_thread_project_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        main, "_resolve_session_account_defaults", AsyncMock(return_value={})
    )
    monkeypatch.setattr(main, "_is_experts_db_enabled", MagicMock(return_value=False))
    monkeypatch.setattr(main, "_user_experts_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(
        main, "_authorize_thread_datasource_ids", AsyncMock(return_value=[])
    )
    grants = AsyncMock()
    monkeypatch.setattr(main, "_enforce_session_create_grants", grants)
    return main, db, conn, grants


def _thread_create_body(main, config_override):
    return main.ThreadCreateRequest(title="t", config_override=config_override)


def _persisted_thread_override(conn) -> dict:
    """The `tools` block as it lands in threads.metadata — the durable
    artifact, and the thing the user's untick has to survive into."""
    import json

    payload = json.loads(conn.execute.await_args.args[2])
    return payload["config_override"]


class TestSessionCreateBoundary:
    @pytest.mark.asyncio
    async def test_unticking_a_previously_discarded_category_reaches_the_thread(
        self, session_create_env
    ):
        """THE motivating defect, at the endpoint. Before: `tools.research` was
        copied across only if it was one of four groups, so this key never
        reached `threads.metadata.config_override` and the agent bound research
        tools anyway."""
        main, _, conn, _ = session_create_env

        await main.create_thread(
            _thread_create_body(main, {"tools": {"research": [], "git": []}}),
            MagicMock(),
        )

        persisted = _persisted_thread_override(conn)
        assert persisted["tools"]["research"] == []
        assert persisted["tools"]["git"] == []

    @pytest.mark.asyncio
    async def test_the_whole_form_deselect_survives(self, session_create_env):
        main, _, conn, _ = session_create_env

        await main.create_thread(
            _thread_create_body(
                main, {"tools": {c: [] for c in COCKPIT_SESSION_CATEGORIES}}
            ),
            MagicMock(),
        )

        persisted = _persisted_thread_override(conn)
        assert set(persisted["tools"]) == set(COCKPIT_SESSION_CATEGORIES)
        assert all(v == [] for v in persisted["tools"].values())

    @pytest.mark.asyncio
    async def test_the_grant_check_sees_the_widened_fragment(self, session_create_env):
        """A category the boundary now honours must also reach the PDP — the
        fix must not hand the agent tools the grant check never saw."""
        main, _, _, grants = session_create_env

        await main.create_thread(
            _thread_create_body(main, {"tools": {"shell": ["run_command"]}}),
            MagicMock(),
        )

        fragment = grants.await_args.args[0]
        assert fragment["tools"]["shell"] == ["run_command"]

    @pytest.mark.asyncio
    async def test_a_smuggle_is_a_400_and_nothing_is_created(self, session_create_env):
        main, db, _, _ = session_create_env

        with pytest.raises(main.HTTPException) as exc:
            await main.create_thread(
                _thread_create_body(main, {"tools": {"canvas": ["run_command"]}}),
                MagicMock(),
            )
        assert exc.value.status_code == 400
        assert "run_command" in exc.value.detail
        db.create_thread.assert_not_awaited()


@pytest.fixture
def session_patch_env(monkeypatch):
    """The internal runtime PATCH (`agent_update_thread_config`) — the live
    `config.update` boundary, driven at the endpoint."""
    import orchestrator.main as main

    thread_row = {
        "id": SESSION_THREAD_ID,
        "user_id": SESSION_USER_ID,
        "project_id": None,
        "metadata": {"config_override": {"workspace": {"backend": "sandbox"}}},
    }
    db = SimpleNamespace(
        get_thread=AsyncMock(return_value=thread_row),
        get_user=AsyncMock(return_value={"id": SESSION_USER_ID, "is_admin": False}),
        merge_thread_config_override=AsyncMock(return_value=True),
        set_thread_datasource_ids=AsyncMock(return_value=True),
        record_security_event=AsyncMock(),
        resolve_api_keys_for_job=AsyncMock(return_value={}),
        resolve_datasources_for_thread=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_thread_project_ids", AsyncMock(return_value=[]))
    grants = AsyncMock()
    monkeypatch.setattr(main, "_enforce_session_create_grants", grants)
    return main, db, grants


class TestSessionRuntimeUpdateBoundary:
    @pytest.mark.asyncio
    async def test_a_live_untick_outside_the_four_groups_is_persisted(
        self, session_patch_env
    ):
        """Before: this boundary REPLACED `tools` with the accepted four-group
        subset, so a live "turn research off" was acknowledged and dropped."""
        main, db, _ = session_patch_env

        await main.agent_update_thread_config(
            MagicMock(),
            SESSION_THREAD_ID,
            main.AgentThreadConfigUpdateRequest(
                config_override={"tools": {"research": [], "canvas": []}}
            ),
        )

        merged = db.merge_thread_config_override.await_args.args[1]
        assert merged["tools"] == {"research": [], "canvas": []}

    @pytest.mark.asyncio
    async def test_a_live_smuggle_is_a_400_and_nothing_persists(
        self, session_patch_env
    ):
        main, db, _ = session_patch_env

        with pytest.raises(main.HTTPException) as exc:
            await main.agent_update_thread_config(
                MagicMock(),
                SESSION_THREAD_ID,
                main.AgentThreadConfigUpdateRequest(
                    config_override={"tools": {"citation": ["run_command"]}}
                ),
            )
        assert exc.value.status_code == 400
        db.merge_thread_config_override.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_datasource_flip_wins_over_the_requests_own_tools(
        self, session_patch_env
    ):
        """The order in the grant fragment is load-bearing. Now that a request
        may name a connector category, `tools.sql: []` would mask a
        datasource_tools violation — while attach applies the flip LAST, so the
        session would get the tools anyway."""
        main, db, grants = session_patch_env
        db.resolve_datasources_for_thread = AsyncMock(
            return_value=[
                {"type": "postgresql", "name": "PG", "project_read_only": False}
            ]
        )
        db.get_datasource = AsyncMock(
            return_value={"id": "ds-a", "type": "postgresql", "is_global": True}
        )
        with patch.object(
            main, "user_can_access_datasource", AsyncMock(return_value=True)
        ):
            await main.agent_update_thread_config(
                MagicMock(),
                SESSION_THREAD_ID,
                main.AgentThreadConfigUpdateRequest(
                    config_override={"tools": {"sql": []}},
                    datasource_ids=["ds-a"],
                ),
            )

        fragment = grants.await_args.args[0]
        assert fragment["tools"]["sql"], (
            "the request's `sql: []` masked the datasource flip from the PDP"
        )


# =============================================================================
# The three boundaries found by review — I1 and I3
# =============================================================================


class TestPrepareBoundary:
    """`POST /api/sessions/{id}/prepare` takes a `config_override` that flows
    to `_resolve_session_config`, where a non-None value **replaces** the
    thread's persisted override outright — so it is a write boundary, not a
    hint. Owner-only and API-direct (the cockpit posts `{}`)."""

    @pytest.fixture
    def prepare_env(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from orchestrator.routers import sessions as sessions_mod

        async def _fake_auth(request, db):
            return {"id": "u1", "is_approved": True}

        monkeypatch.setattr(sessions_mod, "require_approved_user", _fake_auth)
        monkeypatch.setattr(
            sessions_mod,
            "_get_db",
            lambda: SimpleNamespace(
                get_thread=AsyncMock(
                    return_value={
                        "id": "t1",
                        "user_id": "u1",
                        "agent_id": None,
                        "config_name": "session_base",
                    }
                )
            ),
        )
        do_prepare = MagicMock()
        monkeypatch.setattr(sessions_mod, "_do_prepare", do_prepare)
        monkeypatch.setattr(
            sessions_mod, "_schedule_prepare_task", lambda coro: MagicMock()
        )

        app = FastAPI()
        app.include_router(sessions_mod.router)
        return TestClient(app, raise_server_exceptions=False), do_prepare

    def test_a_smuggle_is_a_400_and_nothing_is_provisioned(self, prepare_env):
        client, do_prepare = prepare_env

        resp = client.post(
            "/api/sessions/t1/prepare",
            json={"config_override": {"tools": {"canvas": ["run_command"]}}},
        )

        assert resp.status_code == 400
        assert "run_command" in resp.text
        do_prepare.assert_not_called()

    def test_a_valid_override_reaches_provisioning_normalised(self, prepare_env):
        client, do_prepare = prepare_env

        resp = client.post(
            "/api/sessions/t1/prepare",
            json={"config_override": {"tools": {"canvas": True, "research": []}}},
        )

        assert resp.status_code == 202
        passed = do_prepare.call_args.kwargs["config_override"]
        assert passed["tools"] == {
            "canvas": ["clear_canvas", "get_canvas", "set_canvas"],
            "research": [],
        }

    def test_the_cockpits_empty_body_is_unaffected(self, prepare_env):
        """The shipped cockpit posts `{}` — no behaviour change for it."""
        client, do_prepare = prepare_env

        assert client.post("/api/sessions/t1/prepare", json={}).status_code == 202
        assert do_prepare.call_args.kwargs["config_override"] is None


class TestAutomationBoundary:
    """An automation's `config_override` is stored raw and handed STRAIGHT to
    `db.create_job` by `create_job_from_automation` — it never crosses
    `POST /api/jobs`, so every cron fire re-plants whatever is stored."""

    @pytest.fixture
    def automations_env(self, monkeypatch):
        from orchestrator.routers import automations as mod

        caller = {"id": SESSION_USER_ID, "is_admin": False}
        db = SimpleNamespace(
            create_automation=AsyncMock(return_value={"id": "a1"}),
            update_automation=AsyncMock(return_value={"id": "a1"}),
        )
        # The router late-imports `from main import postgres_db`, which
        # resolves sys.modules["main"] — patching orchestrator.main misses it.
        monkeypatch.setattr("main.postgres_db", db)
        monkeypatch.setattr(
            mod, "require_approved_user", AsyncMock(return_value=caller)
        )
        monkeypatch.setattr(
            mod,
            "validate_automation_expert_selection",
            AsyncMock(return_value="developer"),
        )
        monkeypatch.setattr(
            mod,
            "_resolve_automation_or_404",
            AsyncMock(
                return_value={
                    "id": "a1",
                    "owner_id": SESSION_USER_ID,
                    "project_id": None,
                    "expert": "developer",
                    "enabled": True,
                    "cron_expr": "0 * * * *",
                    "timezone": "UTC",
                }
            ),
        )
        return mod, db

    def _create_body(self, mod, config_override):
        return mod.AutomationCreate(
            name="a",
            cron_expr="0 * * * *",
            expert="developer",
            prompt="p",
            config_override=config_override,
        )

    @pytest.mark.asyncio
    async def test_a_smuggle_is_a_400_and_nothing_is_stored(self, automations_env):
        from fastapi import HTTPException

        mod, db = automations_env

        with pytest.raises(HTTPException) as exc:
            await mod.create_automation(
                MagicMock(),
                self._create_body(mod, {"tools": {"canvas": ["run_command"]}}),
            )
        assert exc.value.status_code == 400
        db.create_automation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_valid_override_is_stored_normalised(self, automations_env):
        mod, db = automations_env

        await mod.create_automation(
            MagicMock(), self._create_body(mod, {"tools": {"research": []}})
        )

        stored = db.create_automation.await_args.kwargs["config_override"]
        assert stored["tools"] == {"research": []}

    @pytest.mark.asyncio
    async def test_the_patch_path_is_validated_too(self, automations_env):
        """Every fire replays the stored override, so an update is the same
        boundary as a create."""
        from fastapi import HTTPException

        mod, db = automations_env

        with pytest.raises(HTTPException) as exc:
            await mod.update_automation(
                MagicMock(),
                "a1",
                mod.AutomationUpdate(
                    config_override={"tools": {"citation": ["shell_execute"]}}
                ),
            )
        assert exc.value.status_code == 400
        db.update_automation.assert_not_awaited()


class TestProjectDefaultOverrideBoundary:
    """`projects.default_config_override` is merged UNDER every job created in
    the project, so an unvalidated tools block here is a cross-principal
    escalation: the planter is not the runner, and the PDP keys off the
    category name. Validated on WRITE only — no existing row is touched."""

    @pytest.fixture
    def project_env(self, monkeypatch):
        import orchestrator.main as main

        db = SimpleNamespace(
            create_project=AsyncMock(return_value={"id": "p1"}),
            add_project_member=AsyncMock(),
            update_project=AsyncMock(return_value=True),
        )
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main,
            "require_approved_user",
            AsyncMock(return_value={"id": SESSION_USER_ID, "is_admin": False}),
        )
        monkeypatch.setattr(main, "require_project_owner", AsyncMock())
        return main, db

    @pytest.mark.asyncio
    async def test_create_rejects_a_smuggle(self, project_env):
        main, db = project_env

        with pytest.raises(main.HTTPException) as exc:
            await main.create_project(
                main.ProjectCreate(
                    name="p",
                    user_id=SESSION_USER_ID,
                    default_config_override={"tools": {"canvas": ["run_command"]}},
                ),
                MagicMock(),
            )
        assert exc.value.status_code == 400
        db.create_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_patch_rejects_a_smuggle(self, project_env):
        main, db = project_env

        with pytest.raises(main.HTTPException) as exc:
            await main.update_project(
                "p1",
                main.ProjectUpdate(
                    default_config_override={"tools": {"knowledge": ["run_command"]}}
                ),
                MagicMock(),
            )
        assert exc.value.status_code == 400
        db.update_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_patch_that_does_not_send_the_field_is_untouched(self, project_env):
        """Write-only is what makes this back-compatible: an existing row with
        a bad override is never read, let alone rejected, until someone
        rewrites it."""
        main, db = project_env

        await main.update_project("p1", main.ProjectUpdate(name="renamed"), MagicMock())

        assert db.update_project.await_args.args[0] == "p1"
        assert "default_config_override" not in db.update_project.await_args.kwargs

    @pytest.mark.asyncio
    async def test_the_cockpits_memory_toggle_payload_still_works(self, project_env):
        """`project-detail.component.ts::toggleProjectMemory` re-submits the
        WHOLE stored override plus a memory key. It carries no tools block, so
        it is unaffected — this is the payload the write-path check has to keep
        working."""
        main, db = project_env

        await main.update_project(
            "p1",
            main.ProjectUpdate(
                default_config_override={"memory": {"project_scoped": True}}
            ),
            MagicMock(),
        )

        assert db.update_project.await_args.kwargs["default_config_override"] == {
            "memory": {"project_scoped": True}
        }


# =============================================================================
# An affirmative policy that turns nothing on — M-b
# =============================================================================


class TestAffirmativeThatExpandsToNothing:
    """`{"tools": {"sql": true}}` used to return `{"sql": []}` with a 200 and a
    server-side log: the caller asked for ON and got OFF. That is "accepted and
    discarded" at a request boundary — this task's own defect in miniature. A
    warning is right for a config layer and wrong for a request."""

    @pytest.mark.parametrize(
        "category",
        [
            "sql",
            "graph",
            "mongodb",
            "webdav",
            "email",
            "repo",
            "product_help",
            "session_task",
        ],
    )
    def test_true_on_a_wholly_code_granted_category_is_a_400(self, category):
        with pytest.raises(ToolPolicyError) as exc:
            validate_tool_override_fragment({"tools": {category: True}})
        assert "turn nothing on" in str(exc.value)
        # The message names the real gate, so the caller learns why.
        assert "runtime code" in str(exc.value) or "attached" in str(exc.value)

    def test_except_that_expands_to_nothing_is_a_400_too(self):
        with pytest.raises(ToolPolicyError, match="turn nothing on"):
            validate_tool_override_fragment(
                {"tools": {"sql": {"except": ["sql_query"]}}}
            )

    @pytest.mark.parametrize("off", [False, []])
    def test_turning_such_a_category_off_stays_legal(self, off):
        """Asking to turn off a category nobody manages is a harmless no-op,
        and both bases ship exactly this."""
        assert validate_tool_override_fragment({"tools": {"sql": off}}) == {"sql": []}

    def test_a_category_config_does_manage_is_unaffected(self):
        assert validate_tool_override_fragment({"tools": {"canvas": True}})["canvas"]


# =============================================================================
# What actually makes the `explicit`-tier collapse safe — C1
# =============================================================================


class TestNoModelAuthoredPathReachesSessionCreate:
    """Membership is the whole registry category, so a session-create request
    may NAME a `grant: "explicit"` control-plane write (`set_expert_bundle`)
    that the old four-name list refused. `true` still cannot reach them, and
    the tool acts as the session's own user — but neither of those answers
    prompt injection.

    The property that does is that **no model-authored path reaches session
    create's `config_override`**: the MCP `create_persistent_thread` tool
    exposes no such parameter, and `spawn_subagent` uses a fixed environment.
    That is load-bearing and otherwise invisible — adding the parameter would
    silently dissolve the mitigation. So it is pinned here.
    """

    def test_the_mcp_session_create_tool_exposes_no_config_override(self):
        # Parsed, not imported: `orchestrator.mcp.server` needs `fastmcp`,
        # which the orchestrator image ships and this test environment does
        # not. The signature is what matters and the AST has it.
        import ast
        from pathlib import Path

        tree = ast.parse(Path("orchestrator/mcp/server.py").read_text())
        fns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "create_persistent_thread"
        ]
        assert fns, "create_persistent_thread vanished — re-point this test"
        for fn in fns:
            args = fn.args
            params = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
            assert "config_override" not in params, (
                "Adding config_override to the MCP session-create tool opens a "
                "MODEL-AUTHORED path to a boundary that accepts every registry "
                "name in its own category, including the *_bundle "
                "control-plane writes. Decide what a model may name before "
                "adding it — see TestNoModelAuthoredPathReachesSessionCreate."
            )

    def test_the_job_create_tool_does_expose_one_which_is_why_it_is_validated(self):
        """The contrast that makes the point: `create_worker_job` DOES forward
        a model-authored fragment verbatim, which is exactly why the job
        boundary had to be closed rather than trusted."""
        from pathlib import Path

        src = Path("src/tools/orchestrator/jobs.py").read_text()
        assert "async def create_worker_job(" in src
        assert "config_override: Optional[Dict[str, Any]] = None," in src
