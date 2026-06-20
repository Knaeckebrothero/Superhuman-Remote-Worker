"""Pure capability-grants logic: catalog, resolution, PDP, and the adversarial
matrix (User-Defined Experts, Slice 2). No DB/framework — hermetic.

Spec: docs/done/global_expert_management.md (decisions 8, 9, 19, 21-23).
"""

from src.core.capability_grants import (
    CATALOG,
    evaluate,
    meet,
    resolve_grants,
)

DEFAULTS = {k: v["default"] for k, v in CATALOG.items()}


def _rows(**kv):
    return [{"key": k, "value_json": v} for k, v in kv.items()]


# --- Task 2: catalog + meet --------------------------------------------------


def test_catalog_keys_and_defaults():
    assert set(CATALOG) == {
        "vm_workspace",
        "shell_tools",
        "delegation",
        "datasource_tools",
        "browser",
        "model_selection",
        "autonomy_ceiling",
        "permission_mode",
    }
    assert all(spec["restrict_only"] for spec in CATALOG.values())
    assert CATALOG["vm_workspace"]["default"] is False
    assert CATALOG["shell_tools"]["default"] is False  # deny-by-default
    assert CATALOG["delegation"]["default"] is False
    assert CATALOG["browser"]["default"] is True  # spec-deferred allow
    assert CATALOG["datasource_tools"]["default"] is True
    assert CATALOG["model_selection"]["default"] is None
    assert CATALOG["autonomy_ceiling"]["default"] == "review"
    assert CATALOG["permission_mode"]["default"] == "supervised"


def test_meet_bool_enum_list():
    assert meet(CATALOG["vm_workspace"], True, False) is False
    assert meet(CATALOG["autonomy_ceiling"], "full", "review") == "review"
    assert meet(CATALOG["permission_mode"], "autonomous", "supervised") == "supervised"
    assert meet(CATALOG["model_selection"], None, ["a", "b"]) == ["a", "b"]
    assert meet(CATALOG["model_selection"], ["a", "b"], ["b", "c"]) == ["b"]


# --- Task 3: resolve_grants (restrict-only, escalation-safe) ------------------


def test_lone_user_grant_widens_past_default():
    g = resolve_grants(
        user_rows=_rows(shell_tools=True), project_rows=[], global_rows=[]
    )
    assert g["shell_tools"] is True
    assert g["vm_workspace"] is False and g["model_selection"] is None
    assert g["autonomy_ceiling"] == "review" and g["permission_mode"] == "supervised"


def test_project_cap_clamps_user_grant():
    g = resolve_grants(
        user_rows=_rows(shell_tools=True),
        project_rows=_rows(shell_tools=False),
        global_rows=[],
    )
    assert g["shell_tools"] is False


def test_autonomy_and_permission_clamped_by_global():
    g = resolve_grants(
        user_rows=_rows(autonomy_ceiling="full", permission_mode="autonomous"),
        project_rows=[],
        global_rows=_rows(autonomy_ceiling="review", permission_mode="auto_accept"),
    )
    assert g["autonomy_ceiling"] == "review" and g["permission_mode"] == "auto_accept"


def test_model_selection_intersects():
    g = resolve_grants(
        user_rows=_rows(model_selection=["a", "b"]),
        project_rows=_rows(model_selection=["b", "c"]),
        global_rows=[],
    )
    assert g["model_selection"] == ["b"]


# --- Task 4: the PDP evaluate() ----------------------------------------------


def test_allows_within_grants():
    assert (
        evaluate(
            {"tools": {"shell": ["ls"]}, "autonomy": "review"},
            {**DEFAULTS, "shell_tools": True},
        )
        == []
    )


def test_flags_ungranted_shell_and_vm_and_autonomy():
    v = evaluate(
        {
            "workspace": {"backend": "vm"},
            "tools": {"shell": ["ls"]},
            "autonomy": "full",
        },
        DEFAULTS,
    )
    j = " ".join(v)
    assert "shell_tools" in j and "vm_workspace" in j and "autonomy_ceiling" in j


def test_workspace_upgrade_gate_vm_vs_sandbox():
    """Sec-1 (workspace_tier_upgrade.md §4.4): the upgrade gate re-runs this PDP
    on the post-upgrade fragment {workspace:{backend:target_tier}}.

      * sandbox is the ungated default tier — clean for a default principal.
      * vm trips vm_workspace unless granted.
    """
    # sandbox upgrade — passes by default (no backend gate, no declared tools).
    assert evaluate({"workspace": {"backend": "sandbox"}}, DEFAULTS) == []
    # vm upgrade without the grant — refused.
    v = evaluate({"workspace": {"backend": "vm"}}, DEFAULTS)
    assert len(v) == 1 and "vm_workspace" in v[0]
    # vm upgrade with the grant — clean.
    assert (
        evaluate({"workspace": {"backend": "vm"}}, {**DEFAULTS, "vm_workspace": True})
        == []
    )


def test_workspace_upgrade_gate_refuses_shell_restricted_principal():
    """A principal whose config declares tools.shell but lacks the shell_tools
    grant is refused even for a sandbox upgrade — the upgrade re-runs dispatch
    enforcement on the post-upgrade config, so a shell restriction still bites."""
    post_upgrade = {"workspace": {"backend": "sandbox"}, "tools": {"shell": ["ls"]}}
    v = evaluate(post_upgrade, {**DEFAULTS, "shell_tools": False})
    assert len(v) == 1 and "shell_tools" in v[0]
    # Same config, owner holds the grant → clean.
    assert evaluate(post_upgrade, {**DEFAULTS, "shell_tools": True}) == []


def test_delegation_reads_enabled_not_dict_presence():
    # A disabled delegation settings-dict must NOT trip the gate.
    assert evaluate({"delegation": {"enabled": False, "max_depth": 3}}, DEFAULTS) == []
    assert evaluate(
        {"delegation": {"enabled": True}}, DEFAULTS
    )  # flagged (deny default)


def test_session_permission_mode_gated():
    # sessions use interactive.permission_mode, NOT autonomy.
    v = evaluate({"interactive": {"permission_mode": "autonomous"}}, DEFAULTS)
    assert len(v) == 1 and "permission_mode" in v[0]
    assert evaluate({"interactive": {"permission_mode": "supervised"}}, DEFAULTS) == []


def test_model_not_in_selection():
    v = evaluate(
        {"llm": {"strategic": {"model": "x"}, "tactical": {"model": "y"}}},
        {**DEFAULTS, "model_selection": ["y"]},
    )
    assert len(v) == 1 and "x" in v[0]


def test_admin_short_circuits_and_empty_is_clean():
    assert evaluate({"tools": {"shell": ["ls"]}}, DEFAULTS, is_admin=True) == []
    assert evaluate({}, DEFAULTS) == []


# --- Task 5: canonicalize-before-scan + reject non-ASCII keys -----------------


def test_rejects_duplicate_keys():
    from src.core.expert_resolution import scan_fragment_text

    assert scan_fragment_text('{"llm": {"api_key": null, "api_key": "x"}}')


def test_rejects_non_ascii_key():
    # fullwidth 'api_key' — reject the non-ASCII key outright, don't try to normalize.
    from src.core.expert_resolution import scan_fragment_text

    assert scan_fragment_text('{"llm": {"ａｐｉ＿ｋｅｙ": "x"}}')


def test_allows_clean_fragment_text():
    from src.core.expert_resolution import scan_fragment_text

    assert (
        scan_fragment_text('{"llm": {"model": "gemma-4-moe"}, "tools": {"shell": []}}')
        == []
    )


# --- Task 9: resolve_config capture out-param --------------------------------


def test_resolve_config_capture_exposes_merged_fragment():
    from orchestrator.services.config_resolver import resolve_config

    cap: dict = {}
    resolve_config(base_config_name="defaults", capture=cap, expert_type="worker")
    assert "merged_fragment" in cap and isinstance(cap["merged_fragment"], dict)
    assert (
        "tools" in cap["merged_fragment"]
    )  # base tools present (the deny-by-default subject)


# --- Task 15: adversarial matrix --------------------------------------------


def test_user_grant_cannot_exceed_project_ceiling():
    g = resolve_grants(
        user_rows=[{"key": "autonomy_ceiling", "value_json": "full"}],
        project_rows=[{"key": "autonomy_ceiling", "value_json": "guided"}],
        global_rows=[],
    )
    assert g["autonomy_ceiling"] == "guided"


def test_null_deletion_of_guardrail_caught_in_merged():
    d = {k: v["default"] for k, v in CATALOG.items()}
    assert evaluate({"autonomy": "full"}, d)  # ceiling review -> flagged


def test_cross_layer_credential_assembly_denied():
    from src.core.expert_resolution import hard_deny_scan

    assert hard_deny_scan({"llm": {"model": "x", "api_key": "leaked"}})


def test_worker_base_grandfather_is_exactly_shell_and_delegation():
    """Empirical guard on the central design bet: the real worker base trips
    EXACTLY shell_tools + delegation under deny-default, and grandfathering just
    those two clears it (no self-DoS, nothing else needs grandfathering)."""
    from orchestrator.services.config_resolver import resolve_config

    cap: dict = {}
    resolve_config(base_config_name="defaults", capture=cap, expert_type="worker")
    frag = cap["merged_fragment"]
    deny = {k: v["default"] for k, v in CATALOG.items()}
    flagged = {v.split(":")[0] for v in evaluate(frag, deny)}
    assert flagged == {"shell_tools", "delegation"}
    grand = {**deny, "shell_tools": True, "delegation": True}
    assert evaluate(frag, grand) == []


def test_session_base_grandfather_is_exactly_shell():
    from orchestrator.services.config_resolver import resolve_config

    cap: dict = {}
    resolve_config(
        base_config_name="persistent_defaults", capture=cap, expert_type="session"
    )
    frag = cap["merged_fragment"]
    deny = {k: v["default"] for k, v in CATALOG.items()}
    flagged = {v.split(":")[0] for v in evaluate(frag, deny)}
    assert flagged == {"shell_tools"}
    grand = {**deny, "shell_tools": True, "delegation": True}
    assert evaluate(frag, grand) == []
