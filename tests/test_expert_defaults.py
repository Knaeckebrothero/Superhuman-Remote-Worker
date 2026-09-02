"""Mode-specific virtual framework-default expert details."""

from unittest.mock import AsyncMock

import pytest
import yaml

import orchestrator.main as orchestrator_main
from orchestrator.main import _load_expert_detail
from src.core.tool_policy import enumerate_only_members


@pytest.mark.asyncio
async def test_session_defaults_use_persistent_base():
    detail = await _load_expert_detail("defaults", defaults_type="session")

    config = detail["config"]
    assert config["agent_id"] == "session_base"
    assert config["llm"]["max_retries"] == 3
    assert config["tools"]["communication"] == []
    assert config["tools"]["delegation"] == []


@pytest.mark.asyncio
async def test_unspecified_defaults_type_remains_worker_for_compatibility():
    detail = await _load_expert_detail("defaults")

    config = detail["config"]
    assert config["agent_id"] == "worker_base"
    assert config["llm"]["max_retries"] == 0


@pytest.mark.asyncio
async def test_db_expert_detail_includes_settings_matrix_and_no_defaults_tools(
    monkeypatch,
):
    """`defaults_tools` is gone, and its absence is the assertion.

    It existed for exactly one caller — the cockpit's re-enable payload, which
    sent ``tools[cat] = [...defaults_tools[cat]]``. Every category worth
    re-enabling ships ``[]`` in both bases, so that payload was empty and the
    tick emitted nothing at all. The forms now write a policy (``true``, or the
    ``enumerate_only`` enumeration for ``shell``) and the write boundary
    expands it against the registry, so re-serving the base's lists would only
    invite the dead path back.
    """
    expert_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
    monkeypatch.setattr(
        orchestrator_main.postgres_db,
        "get_expert_by_id",
        AsyncMock(
            return_value={
                "id": expert_id,
                "display_name": "DB Worker",
                "description": "",
                "icon": "smart_toy",
                "color": "#6B7280",
                "tags": [],
                "expert_type": "worker",
                "is_global": False,
                "managed_key": None,
                "config": {"tools": {"shell": []}},
                "prompts": {},
            }
        ),
    )

    detail = await _load_expert_detail(expert_id)

    assert "defaults_tools" not in detail
    assert "gpt-5.6" in detail["settings_matrix"]
    # What replaced it, and it is a different kind of thing: not a copy of a
    # config layer, but the registry's answer to "what must a caller write to
    # turn this category on" for the one category that refuses `true`.
    assert detail["enumerate_only"] == enumerate_only_members()


@pytest.mark.asyncio
async def test_bundled_expert_detail_serves_the_write_vocabulary_too():
    """Job create and the expert editor read this route and never the preview.

    They perform no resolved toolset read — the preview route resolves with
    ``expert_type="session"`` and would model the wrong thing for a worker — so
    without ``enumerate_only`` here, ticking Shell in either form emits
    ``tools.shell: true`` and gets a 400 naming a rule the form gives the user
    no way to satisfy. ``enumerate_only_members()`` is registry-derived and
    entirely independent of expert type, so serving it costs nothing and is
    correct on both.
    """
    detail = await _load_expert_detail("worker_base")

    assert detail["enumerate_only"] == enumerate_only_members()
    assert detail["enumerate_only"]["shell"]


@pytest.mark.asyncio
async def test_the_served_vocabulary_is_what_the_write_boundary_accepts():
    """The round trip the forms actually perform."""
    from src.core.tool_policy import validate_tool_override_fragment

    detail = await _load_expert_detail("worker_base")
    for category, names in detail["enumerate_only"].items():
        accepted = validate_tool_override_fragment(
            {"tools": {category: {"only": names}}}
        )
        assert accepted[category] == names


class TestAccountDefaultsLayer:
    """`include_account_defaults` must reproduce `resolve_config`'s precedence.

    The create forms render this config; when it disagrees with what
    create/dispatch resolves, controls keyed off a resolved value are wrong.
    The concrete regression: session detail reported `workspace.backend` as
    session_base's `sandbox` while `create_thread` resolved the account's
    `virtual`, so the New Session datasource picker kept clone-based repository
    connectors selected and every create 400'd on the lite-backend rule.
    """

    @pytest.fixture
    def account_user(self, monkeypatch):
        """A user whose account pins a model but no session workspace tier."""
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(return_value={"default_model": "account-pinned-model"}),
        )
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "resolve_default_for_capability",
            AsyncMock(return_value=None),
        )
        return "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_session_detail_off_by_default_reports_base_backend(self):
        detail = await _load_expert_detail("defaults", defaults_type="session")

        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_session_detail_reports_the_backend_create_will_resolve(
        self, account_user
    ):
        detail = await _load_expert_detail(
            "defaults",
            defaults_type="session",
            user_id=account_user,
            include_account_defaults=True,
        )

        # The account default, not session_base's `sandbox` — this is the value
        # the picker's lite-tier gate has to agree with.
        assert detail["config"]["workspace"]["backend"] == "virtual"
        assert detail["config"]["llm"]["model"] == "account-pinned-model"

    @pytest.mark.asyncio
    async def test_saved_session_tier_preference_wins_over_platform_default(
        self, monkeypatch, account_user
    ):
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(
                return_value={"persistent_agent": {"workspace_backend": "sandbox"}}
            ),
        )

        detail = await _load_expert_detail(
            "defaults",
            defaults_type="session",
            user_id=account_user,
            include_account_defaults=True,
        )

        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_worker_detail_gets_the_model_floor_but_no_session_tier(
        self, account_user
    ):
        detail = await _load_expert_detail(
            "defaults", user_id=account_user, include_account_defaults=True
        )

        assert detail["config"]["llm"]["model"] == "account-pinned-model"
        # Jobs have no account workspace layer at dispatch, so the worker base's
        # own backend must survive — feeding sessions' `virtual` here would make
        # the New Job form lie in the opposite direction.
        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_expert_fragment_still_beats_the_account_layer(
        self, monkeypatch, account_user
    ):
        expert_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_expert_by_id",
            AsyncMock(
                return_value={
                    "id": expert_id,
                    "display_name": "Pinned Session",
                    "description": "",
                    "icon": "smart_toy",
                    "color": "#6B7280",
                    "tags": [],
                    "expert_type": "session",
                    "is_global": False,
                    "managed_key": None,
                    "config": {"workspace": {"backend": "sandbox"}},
                    "prompts": {},
                }
            ),
        )

        detail = await _load_expert_detail(
            expert_id, user_id=account_user, include_account_defaults=True
        )

        # base -> account -> expert: the expert is the most specific layer here.
        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_anonymous_caller_has_no_account_layer(self):
        detail = await _load_expert_detail(
            "defaults", defaults_type="session", include_account_defaults=True
        )

        assert detail["config"]["workspace"]["backend"] == "sandbox"


# --- U1 WP2: the public base ids and the bundled listing survive the split ---


class TestPublicBaseIdsAfterTheRootSplit:
    """``worker_base`` / ``session_base`` are public ids the cockpit fetches
    (``GET /api/experts/worker_base?account_defaults=true`` on the job form,
    ``/api/experts/session_base?type=session`` on the session form). After
    the split the files behind them are ``expert_base.yaml`` + a role overlay;
    the detail must still be the fully merged role base, not one file."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("expert_id", "defaults_type", "role"),
        [
            ("worker_base", None, "worker"),
            ("default", None, "worker"),
            ("worker_base", "worker", "worker"),
            ("session_base", "session", "session"),
            ("session_base", None, "session"),  # the id itself names the role
            ("persistent_defaults", None, "session"),
        ],
    )
    async def test_base_id_detail_is_the_merged_role_base(
        self, expert_id, defaults_type, role
    ):
        from src.core.loader import ROLE_ROOTS, load_role_base

        detail = await _load_expert_detail(expert_id, defaults_type=defaults_type)

        expected = dict(load_role_base(role))
        expected.pop("connections", None)
        assert detail["config"] == expected
        assert detail["config"]["agent_id"] == ROLE_ROOTS[role]
        # a raw read of an overlay alone would lack these
        if role == "worker":
            assert detail["config"]["phase_settings"]["min_todos"] == 2
            assert detail["config"]["instruction_files"]
        else:
            assert "get_canvas" in detail["config"]["tools"]["canvas"]
        assert detail["config"]["llm"]["model"]  # expert_base's block is there
        assert detail["instructions"]  # template fallback still resolves

    @pytest.mark.asyncio
    async def test_session_base_id_with_account_defaults(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(return_value={"default_model": "account-pinned-model"}),
        )
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "resolve_default_for_capability",
            AsyncMock(return_value=None),
        )
        detail = await _load_expert_detail(
            "session_base",
            defaults_type="session",
            user_id="11111111-1111-4111-8111-111111111111",
            include_account_defaults=True,
        )
        assert detail["config"]["agent_id"] == "session_base"
        assert detail["config"]["llm"]["model"] == "account-pinned-model"
        assert detail["config"]["workspace"]["backend"] == "virtual"
        assert detail["effective_models"]["session"]["model"] == "account-pinned-model"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expert_id", ["developer", "assistant", "centurion"])
    async def test_bundled_expert_detail_matches_the_loader_chain(self, expert_id):
        """The served config equals the loader's merged chain for the expert's
        own role (base + leaf), minus the keys the API never serves."""
        from src.core.loader import load_and_merge_config, resolve_config_path

        detail = await _load_expert_detail(expert_id)

        path, _ = resolve_config_path(expert_id)
        expected = load_and_merge_config(path)
        expected.pop("connections", None)
        assert detail["config"] == expected


def test_scan_experts_lists_exactly_the_bundled_experts_with_unchanged_roles():
    """The listing the cockpit filters by id: only ``config/experts/*``, with
    the role inferred from the chain root — never the roots themselves."""
    experts = orchestrator_main._scan_experts()
    listed = {e.id: e.expert_type for e in experts}
    assert listed == {
        "assistant": "session",
        "bughunter": "worker",
        "centurion": "session",
        "critic": "worker",
        "curator": "worker",
        "designer": "worker",
        "designer-interactive": "session",
        "developer": "worker",
        "general-worker": "worker",
        "product-qa": "worker",
        "scholar": "worker",
        "writer": "worker",
    }
    for forbidden in (
        "expert_base",
        "overlays",
        "worker",
        "session",
        "subagent",
        "worker_base",
        "session_base",
        "subagent_base",
    ):
        assert forbidden not in listed
    assistant = next(e for e in experts if e.id == "assistant")
    assert assistant.display_name and assistant.tags and assistant.description


# --- U1 WP4: role tags on the listing, the subagent library, the role param --


def test_scan_experts_adds_the_role_tag():
    """`tags ∪ {chain-root role}` on every bundled entry, once, after the
    authored tags (an authored role tag is kept where it is)."""
    experts = orchestrator_main._scan_experts()
    for e in experts:
        assert e.tags.count(e.expert_type) == 1, e.id
    by_id = {e.id: e for e in experts}
    config_dir = orchestrator_main._get_config_dir()

    def authored(expert_id: str) -> list[str]:
        raw = yaml.safe_load(
            (config_dir / "experts" / expert_id / "config.yaml").read_text(
                encoding="utf-8"
            )
        )
        return list(raw.get("tags") or [])

    assert by_id["developer"].tags == [*authored("developer"), "worker"]
    assert by_id["assistant"].tags == [*authored("assistant"), "session"]
    # general-worker authors its role tag already: kept in place, not doubled.
    assert "worker" in authored("general-worker")
    assert by_id["general-worker"].tags == authored("general-worker")


def test_subagent_library_is_scanned_separately_and_tagged():
    library = orchestrator_main._scan_subagent_library()
    ids = {e.id for e in library}
    assert "explorer" in ids
    assert ids.isdisjoint({e.id for e in orchestrator_main._scan_experts()})
    explorer = next(e for e in library if e.id == "explorer")
    assert "subagent" in explorer.tags and explorer.tags.count("subagent") == 1
    assert explorer.expert_type == "worker"  # chain-root fallback; never lists by it
    assert explorer.description and explorer.display_name == "Explorer"
    # Detail lookup: bundled first, then the library, else nothing.
    assert orchestrator_main._listed_expert("developer").expert_type == "worker"
    assert orchestrator_main._listed_expert("explorer") is explorer or (
        orchestrator_main._listed_expert("explorer").id == "explorer"
    )
    assert orchestrator_main._listed_expert("no-such-expert") is None


class TestRoleParameter:
    """`_load_expert_detail(role=...)` resolves an expert in another role —
    the preview a cross-role picker needs (universal experts, D4)."""

    @pytest.mark.asyncio
    async def test_bundled_worker_resolved_in_the_session_role(self):
        detail = await _load_expert_detail("developer", role="session")

        cfg = detail["config"]
        assert detail["resolved_role"] == "session"
        assert "get_canvas" in cfg["tools"]["canvas"]  # the session overlay
        assert cfg["llm"]["max_retries"] == 3
        assert (
            cfg["tools"]["shell"] and cfg["delegation"]["enabled"] is True
        )  # expert wins
        own = await _load_expert_detail("developer")
        assert own["resolved_role"] == "worker"
        assert own["config"]["llm"]["max_retries"] == 0

    @pytest.mark.asyncio
    async def test_session_expert_resolved_in_the_worker_role(self):
        detail = await _load_expert_detail("assistant", role="worker")

        cfg = detail["config"]
        assert detail["resolved_role"] == "worker"
        assert cfg["phase_settings"]["min_todos"] == 2
        assert cfg["autonomy"] == "review"
        assert "next_phase_todos" in cfg["tools"]["core"]

    @pytest.mark.asyncio
    async def test_role_wins_over_a_base_id(self):
        detail = await _load_expert_detail("session_base", role="worker")
        assert detail["config"]["agent_id"] == "worker_base"
        assert detail["resolved_role"] == "worker"
        detail = await _load_expert_detail("worker_base", role="subagent")
        assert detail["config"]["agent_id"] == "subagent_base"
        assert "autonomy" not in detail["config"]

    @pytest.mark.asyncio
    async def test_library_entry_detail_defaults_to_the_subagent_role(self):
        detail = await _load_expert_detail("explorer")

        cfg = detail["config"]
        assert detail["resolved_role"] == "subagent"
        assert cfg["agent_id"] == "explorer"
        assert cfg["llm"]["model"] == "inherit"
        assert "autonomy" not in cfg and "verification" not in cfg
        assert "$ignore_keys" not in cfg
        assert cfg["tools"]["workspace"] == [
            "read_file",
            "use_skill",
            "list_files",
            "search_files",
            "file_exists",
            "get_document_info",
        ]
        assert cfg["interactive"]["permission_mode"] == "autonomous"
        assert detail["enumerate_only"] == enumerate_only_members()
        # …and in another role on request.
        as_worker = await _load_expert_detail("explorer", role="worker")
        assert as_worker["resolved_role"] == "worker"
        assert as_worker["config"]["phase_settings"]["min_todos"] == 2

    @pytest.mark.asyncio
    async def test_db_row_resolved_in_another_role_keeps_its_identity(
        self, monkeypatch
    ):
        expert_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_expert_by_id",
            AsyncMock(
                return_value={
                    "id": expert_id,
                    "display_name": "Session Helper",
                    "description": "",
                    "icon": "smart_toy",
                    "color": "#6B7280",
                    "tags": ["session"],
                    "expert_type": "session",
                    "is_global": False,
                    "managed_key": None,
                    "config": {"workspace": {"backend": "sandbox"}},
                    "prompts": {},
                }
            ),
        )

        detail = await _load_expert_detail(expert_id, role="worker")

        assert detail["expert_type"] == "session"  # identity: the row's own role
        assert detail["resolved_role"] == "worker"
        assert detail["config"]["phase_settings"]["min_todos"] == 2
        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    async def test_effective_models_read_the_roster_wide_pin(self, monkeypatch):
        expert_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_expert_by_id",
            AsyncMock(
                return_value={
                    "id": expert_id,
                    "display_name": "Lead",
                    "description": "",
                    "icon": "smart_toy",
                    "color": "#6B7280",
                    "tags": ["worker"],
                    "expert_type": "worker",
                    "is_global": False,
                    "managed_key": None,
                    "config": {
                        "llm": {"model": "lead-model"},
                        "subagents": {"llm": {"model": "reader-model"}},
                    },
                    "prompts": {},
                }
            ),
        )
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "resolve_default_for_capability",
            AsyncMock(return_value=None),
        )

        detail = await _load_expert_detail(
            expert_id, user_id="11111111-1111-4111-8111-111111111111"
        )

        effective = detail["effective_models"]
        assert effective["model"] == {"model": "lead-model", "source": "expert"}
        assert effective["subagent"] == {"model": "reader-model", "source": "expert"}
        assert effective["session"] == effective["model"]
        # The per-phase aliases went with U1 WP6 (the cockpit reads `model`).
        assert set(effective) == {"model", "subagent", "session"}


class TestShellBoundBundledExpertsPinTheirTier:
    """A shell-bound bundled expert must not start a session on the lite tier.

    Session resolution is ``expert_base < account layer < the expert's own
    file``. The account layer ALWAYS emits ``workspace.backend`` (the owner's
    saved tier, else the platform default ``virtual``), so ``expert_base.yaml``'s
    ``backend: sandbox`` is dead for sessions; only a backend declared in the
    expert's OWN ``config.yaml`` sits above that layer. On ``virtual`` the
    capability gate strips shell, browser and git, and the agent can only ask a
    human for an upgrade — a designer that cannot render, or a developer that
    cannot run anything, is not the expert the picker advertised.

    Ruling 2026-09-02 (vault issue
    ``expert_workspace_requirements_do_not_select_runtime_tier``): experts are
    templates — the create form shows the expert's backend the way it shows its
    model, and the user may still change it before create — so a shell-bound
    expert declares ``sandbox`` itself. Jobs are unaffected either way: the
    workspace contract stamps the job tier from ``config_override`` and drops
    the expert YAML's backend.
    """

    PINNED_TO_SANDBOX = (
        "designer",
        "designer-interactive",
        "developer",
        "bughunter",
        "product-qa",
        "critic",
    )
    # Deliberately takes no position: its shell exists for the rare LaTeX
    # build, so it follows the account default and asks for an upgrade when
    # a command is actually needed.
    FOLLOWS_ACCOUNT_DEFAULT = ("scholar",)

    @pytest.fixture
    def virtual_default_user(self, monkeypatch):
        """A logged-in owner with no saved tier → the platform default ``virtual``."""
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "get_user_settings",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            orchestrator_main.postgres_db,
            "resolve_default_for_capability",
            AsyncMock(return_value=None),
        )
        return "22222222-2222-4222-8222-222222222222"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expert_id", PINNED_TO_SANDBOX)
    async def test_shell_bound_expert_starts_a_session_on_sandbox(
        self, expert_id, virtual_default_user
    ):
        detail = await _load_expert_detail(
            expert_id,
            user_id=virtual_default_user,
            include_account_defaults=True,
            role="session",
        )

        assert detail["config"]["workspace"]["backend"] == "sandbox"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expert_id", FOLLOWS_ACCOUNT_DEFAULT)
    async def test_upgrade_on_demand_expert_follows_the_account_default(
        self, expert_id, virtual_default_user
    ):
        detail = await _load_expert_detail(
            expert_id,
            user_id=virtual_default_user,
            include_account_defaults=True,
            role="session",
        )

        assert detail["config"]["workspace"]["backend"] == "virtual"

    def test_every_bundled_expert_with_shell_tools_takes_a_position_on_its_tier(
        self,
    ):
        """Tripwire: the next shell-bound expert must decide, not inherit.

        Reads each bundled file raw (not merged) so an inherited
        ``expert_base`` value cannot satisfy it. Listing shell tools without
        declaring ``workspace.backend`` is a silent vote for "starts blind";
        either pin a tier here or add the expert to the documented exception
        list above.
        """
        from pathlib import Path

        experts_dir = Path(__file__).resolve().parents[1] / "config" / "experts"
        undecided: list[str] = []
        for path in sorted(experts_dir.glob("*/config.yaml")):
            raw = yaml.safe_load(path.read_text()) or {}
            shell = (raw.get("tools") or {}).get("shell")
            lists_shell = isinstance(shell, list) and len(shell) > 0
            declared = (raw.get("workspace") or {}).get("backend")
            expert = path.parent.name
            if expert in self.FOLLOWS_ACCOUNT_DEFAULT:
                assert declared is None, (
                    f"{expert} is listed as following the account default but "
                    f"declares workspace.backend={declared!r}; drop one or the other"
                )
                continue
            if lists_shell and declared is None:
                undecided.append(expert)

        assert not undecided, (
            "Bundled experts list shell tools but declare no workspace.backend "
            f"in their own config.yaml: {undecided}"
        )
