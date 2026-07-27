"""Contract tests for the held-out App Guide model-evaluation harness."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.app_guide.run import (
    LLMRoute,
    REQUIRED_NEAR_MISS_TAGS,
    catalog_for_arm,
    current_topic_ids,
    load_cases,
    main,
    model_answer,
    parse_arm_spec,
    product_tool_schema,
    reader_for_catalog,
    score_case,
    system_prompt,
    validate_corpus,
)
from src.core.skill_resolution import APP_GUIDE_LOADER_TOOL, APP_GUIDE_SKILL


@pytest.fixture(scope="module")
def cases():
    return load_cases()


def _case(cases, case_id):
    return next(case for case in cases if case["id"] == case_id)


def test_corpus_is_balanced_and_covers_required_case_classes(cases):
    positives = [case for case in cases if case["expected_trigger"]]
    negatives = [case for case in cases if not case["expected_trigger"]]
    categories = {case["category"] for case in cases}
    tags = {tag for case in cases for tag in case["tags"]}

    assert len(cases) >= 24
    assert 0.4 <= len(positives) / len(cases) <= 0.6
    assert {"broad", "workflow", "availability", "near_miss", "honest_gap"} <= (
        categories
    )
    assert REQUIRED_NEAR_MISS_TAGS <= tags
    assert sum("paraphrase" in case["tags"] for case in cases) >= 3
    assert any(case["expected_gap"] for case in cases)
    assert (
        _case(cases, "workflow-weekly-invoice-connector-limit")["expected_topic"]
        == "automations"
    )
    assert _case(cases, "honest-gap-enterprise-identity")["expected_topic"] == "index"
    assert all(case["expected_topic"] is None for case in negatives)


def test_all_positive_topics_are_current_and_negatives_never_trigger(cases):
    topics = current_topic_ids()

    assert {
        case["expected_topic"] for case in cases if case["expected_trigger"]
    } <= topics
    assert all(
        not case["expected_trigger"]
        for case in cases
        if case["category"] == "near_miss"
    )


def test_schema_validation_rejects_duplicate_ids(cases):
    document = {"schema_version": 1, "cases": copy.deepcopy(cases)}
    document["cases"][1]["id"] = document["cases"][0]["id"]

    with pytest.raises(ValueError, match="duplicate case id"):
        validate_corpus(document)


def test_schema_validation_rejects_unknown_allowed_topic(cases):
    document = {"schema_version": 1, "cases": copy.deepcopy(cases)}
    document["cases"][0]["allowed_topics"] = ["not-a-current-topic"]

    with pytest.raises(ValueError, match="allowed_topics contains unknown"):
        validate_corpus(document)


def test_positive_prose_from_priors_is_not_a_routing_pass(cases):
    case = _case(cases, "workflow-share-email-folder")
    answer = (
        "Open Connectors and use the Folder allowlist. Leaving this empty "
        "shares the whole mailbox, and you must select the connector."
    )

    score = score_case(case, [], answer)

    assert score["answer_score"]["grounding_pass"] is True
    assert score["trajectory"]["observed_trigger"] is False
    assert score["trajectory"]["trigger_pass"] is False
    assert score["passed"] is False


def test_positive_case_requires_the_expected_focused_topic(cases):
    case = _case(cases, "workflow-share-email-folder")
    answer = (
        "Open Connectors and use the Folder allowlist. Leaving this empty "
        "shares the whole mailbox, and you must select the connector."
    )

    correct = score_case(
        case,
        [
            {
                "name": APP_GUIDE_LOADER_TOOL,
                "topic_id": "datasources-email",
            }
        ],
        answer,
    )
    overbroad = score_case(
        case,
        [
            {"name": APP_GUIDE_LOADER_TOOL, "topic_id": "index"},
            {"name": APP_GUIDE_LOADER_TOOL, "topic_id": "datasources"},
            {
                "name": APP_GUIDE_LOADER_TOOL,
                "topic_id": "datasources-email",
            },
        ],
        answer,
    )

    assert correct["passed"] is True
    assert overbroad["trajectory"]["topic_pass"] is False
    assert overbroad["trajectory"]["unexpected_topics"] == ["datasources"]


def test_positive_case_can_allow_a_second_relevant_topic(cases):
    case = _case(cases, "workflow-preserve-important-fact")
    answer = (
        "Compaction is not enough for future work. Write a project knowledge "
        "note instead."
    )
    score = score_case(
        case,
        [
            {"name": APP_GUIDE_LOADER_TOOL, "topic_id": "index"},
            {"name": APP_GUIDE_LOADER_TOOL, "topic_id": "sessions"},
            {
                "name": APP_GUIDE_LOADER_TOOL,
                "topic_id": "memory-and-knowledge",
            },
        ],
        answer,
    )

    assert score["trajectory"]["allowed_topics"] == ["sessions"]
    assert score["trajectory"]["topic_pass"] is True
    assert score["passed"] is True


def test_grounding_normalization_ignores_markdown_bold(cases):
    case = _case(cases, "workflow-share-email-folder")
    answer = (
        "Open **Connectors** and use the **Folder allowlist**. Leaving this "
        "empty shares the whole mailbox. You must explicitly **select it** "
        "for the intended session, job, or project."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "datasources-email"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_email_attachment_fact_accepts_explicit_connector_selection(cases):
    case = _case(cases, "workflow-share-email-folder")
    answer = (
        "Open Connectors and create an Email connector with the Folder "
        "allowlist. Leaving it empty shares the whole mailbox. Explicitly "
        "select this connector for the intended session, job, or project."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "datasources-email"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_broad_session_fact_accepts_settings_qualification(cases):
    case = _case(cases, "broad-session-capabilities")
    answer = (
        "Depending on your settings, I can research and use enabled tools. "
        "Virtual workspaces have files while Container workspaces can add "
        "shell and Git."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "sessions"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_campaign_scope_accepts_neither_ship_nor_kill_wording(cases):
    case = _case(cases, "workflow-project-campaign")
    answer = (
        "In the project Loop, set max iterations and a Critic checkpoint. "
        "Neither ship nor kill stops the entire project loop."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "project-loops"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_required_facts_allow_only_a_bounded_modifier_gap(cases):
    shell_case = _case(cases, "availability-missing-shell")
    shell_calls = [
        {
            "name": APP_GUIDE_LOADER_TOOL,
            "topic_id": "permissions-and-availability",
        }
    ]

    faithful = score_case(
        shell_case,
        shell_calls,
        (
            "Autonomous does not add new tools. Inspect the current workspace "
            "and capability grant."
        ),
    )
    too_far_apart = score_case(
        shell_case,
        shell_calls,
        (
            "Autonomous does not add a completely different collection of "
            "tools. Inspect the current workspace and capability grant."
        ),
    )

    assert faithful["answer_score"]["grounding_pass"] is True
    assert faithful["passed"] is True
    assert too_far_apart["answer_score"]["grounding_pass"] is False
    assert too_far_apart["passed"] is False


def test_availability_fact_accepts_tool_inventory_wording(cases):
    case = _case(cases, "availability-missing-shell")
    answer = (
        "Switching to Autonomous will not make the shell appear. It changes "
        "permission mode, not the available tools. Check the workspace and "
        "capability grant."
    )

    score = score_case(
        case,
        [
            {
                "name": APP_GUIDE_LOADER_TOOL,
                "topic_id": "permissions-and-availability",
            }
        ],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_required_fact_gap_accepts_faithful_connector_limit(cases):
    case = _case(cases, "workflow-weekly-invoice-connector-limit")
    answer = (
        "There is no exact Cockpit setup. Automation-fired jobs do not allow "
        "you to attach connectors to the jobs they create."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "automations"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_connector_limit_accepts_support_attaching_wording(cases):
    case = _case(cases, "workflow-weekly-invoice-connector-limit")
    answer = (
        "The guide does not document a supported built-in workflow for this "
        "exact outcome. Automation-fired jobs do not support attaching "
        "connectors, so they cannot use the Email connector."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "automations"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_email_selection_accepts_explicit_attach_wording(cases):
    case = _case(cases, "availability-email-send-now")
    answer = (
        "Creating it does not prove current access. Explicitly attach it to "
        "this session, then check the Send tier and send without human "
        "approval setting."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "datasources-email"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_fleet_capacity_fact_accepts_can_queue_paraphrase(cases):
    case = _case(cases, "workflow-background-fleet")
    answer = (
        "Enable Fleet Management in Tools. You can queue up to three "
        "independent jobs, then monitor them on the Jobs page and in the Inbox."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "fleet-and-delegation"}],
        answer,
    )

    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_forbidden_claims_remain_contiguous_to_avoid_negation_false_positive(cases):
    case = _case(cases, "availability-email-send-now")
    answer = (
        "The connector must be explicitly selected for this session. Check "
        "the send tier; creating it does not prove access. It can not "
        "definitely send right now."
    )

    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "datasources-email"}],
        answer,
    )

    assert score["answer_score"]["critical_forbidden_hits"] == []
    assert score["answer_score"]["grounding_pass"] is True
    assert score["passed"] is True


def test_near_miss_passes_only_without_product_reader(cases):
    case = _case(cases, "near-miss-html-canvas")

    without_reader = score_case(case, [], "Canvas and SVG have different models.")
    with_reader = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "canvas-and-browser"}],
        "Canvas and SVG have different models.",
    )

    assert without_reader["passed"] is True
    assert with_reader["trajectory"]["trigger_pass"] is False
    assert with_reader["passed"] is False


def test_critical_forbidden_claim_has_zero_tolerance(cases):
    case = _case(cases, "workflow-weekly-invoice-connector-limit")
    score = score_case(
        case,
        [{"name": APP_GUIDE_LOADER_TOOL, "topic_id": "automations"}],
        (
            "The guide does not cover this, but use the weekly invoice toggle "
            "to configure it."
        ),
    )

    assert score["trajectory"]["pass"] is True
    assert score["answer_score"]["critical_forbidden_hits"] == ["invented-billing-page"]
    assert score["answer_score"]["grounding_pass"] is False
    assert score["passed"] is False


def test_current_and_no_skill_arms_differ_only_at_managed_guide_seam(monkeypatch):
    monkeypatch.delenv("APP_GUIDE_BREAK_GLASS_DISABLED", raising=False)
    current = catalog_for_arm(parse_arm_spec("current"))
    no_skill = catalog_for_arm(parse_arm_spec("no-skill"))

    assert APP_GUIDE_SKILL in current["files"]
    assert APP_GUIDE_SKILL not in no_skill["files"]
    assert reader_for_catalog(current) is not None
    assert reader_for_catalog(no_skill) is None
    assert "read_product_guide(topic_id)" in system_prompt(current)
    assert "<managed_product_guide" in system_prompt(current)
    assert "on every relevant turn" in system_prompt(current)
    assert "read_product_guide(topic_id)" not in system_prompt(no_skill)
    assert "<managed_product_guide" not in system_prompt(no_skill)
    assert {
        entry["name"] for entry in current["menu"] if entry["name"] != APP_GUIDE_SKILL
    } == {entry["name"] for entry in no_skill["menu"]}


def test_tool_schema_exposes_only_logical_topic_id(monkeypatch):
    monkeypatch.delenv("APP_GUIDE_BREAK_GLASS_DISABLED", raising=False)
    reader = reader_for_catalog(catalog_for_arm(parse_arm_spec("current")))
    schema = product_tool_schema(reader)

    parameters = schema["function"]["parameters"]
    assert schema["function"]["name"] == APP_GUIDE_LOADER_TOOL
    assert parameters["required"] == ["topic_id"]
    assert set(parameters["properties"]) == {"topic_id"}
    assert parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_run_case_uses_and_closes_a_fresh_client_each_time(monkeypatch, cases):
    import eval.app_guide.run as harness

    created = []

    class Client:
        def __init__(self):
            self.entered = False
            self.exited = False

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            self.exited = True

    def client_factory(_route, *, timeout):
        assert timeout == 17
        client = Client()
        created.append(client)
        return client

    async def fake_model_answer(**kwargs):
        assert kwargs["client"] is created[-1]
        return "Canvas and SVG have different models.", [], {}

    monkeypatch.setattr(LLMRoute, "client", client_factory)
    monkeypatch.setattr(harness, "model_answer", fake_model_answer)
    case = _case(cases, "near-miss-html-canvas")
    arm = parse_arm_spec("no-skill")
    catalog = catalog_for_arm(arm)

    for _ in range(2):
        row = await harness.run_case(
            case=case,
            arm=arm,
            catalog=catalog,
            route=LLMRoute(model="fake", base_url=None, api_key="not-recorded"),
            timeout=17,
            max_tool_rounds=2,
            max_tokens=200,
        )
        assert row["passed"] is True

    assert len(created) == 2
    assert all(client.entered and client.exited for client in created)


@pytest.mark.asyncio
async def test_live_loop_calls_real_reader_but_keeps_trajectory_bounded(monkeypatch):
    monkeypatch.delenv("APP_GUIDE_BREAK_GLASS_DISABLED", raising=False)
    catalog = catalog_for_arm(parse_arm_spec("current"))
    reader = reader_for_catalog(catalog)
    requests = []

    class Completions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name=APP_GUIDE_LOADER_TOOL,
                                arguments='{"topic_id":"jobs"}',
                            ),
                        )
                    ],
                )
            else:
                message = SimpleNamespace(
                    content="Use Jobs → New Job.",
                    tool_calls=[],
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=2,
                    total_tokens=12,
                ),
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )
    answer, calls, usage = await model_answer(
        client=client,
        route=LLMRoute(model="fake", base_url=None, api_key="not-recorded"),
        catalog=catalog,
        reader=reader,
        prompt="How do I create a job?",
        max_tool_rounds=2,
        max_tokens=200,
    )

    assert answer == "Use Jobs → New Job."
    assert calls[0]["name"] == APP_GUIDE_LOADER_TOOL
    assert calls[0]["topic_id"] == "jobs"
    assert calls[0]["result_status"] == "ready"
    assert "result" not in calls[0]
    assert calls[0]["result_chars"] > 1000
    assert len(calls[0]["result_sha256"]) == 64
    assert requests[1]["messages"][-2]["role"] == "tool"
    assert "[product guide topic: jobs]" in requests[1]["messages"][-2]["content"]
    assert requests[1]["messages"][-1]["role"] == "user"
    assert (
        "<managed_product_guide_turn_boundary" in requests[1]["messages"][-1]["content"]
    )
    assert usage == {
        "prompt_tokens": 20,
        "completion_tokens": 4,
        "total_tokens": 24,
    }


def test_validate_only_cli_does_not_require_model_credentials(monkeypatch, capsys):
    monkeypatch.delenv("APP_GUIDE_EVAL_MODEL", raising=False)
    monkeypatch.delenv("APP_GUIDE_EVAL_API_KEY", raising=False)

    assert main(["--validate-only"]) == 0
    output = capsys.readouterr().out
    assert '"cases":' in output
    assert '"corpus_sha256":' in output
    assert "api_key" not in output.casefold()


@pytest.mark.parametrize(
    ("complete", "release_gate", "expected_exit"),
    [
        (True, False, 1),
        (True, True, 0),
        (False, False, 0),
    ],
)
def test_cli_fails_only_a_complete_failed_release_gate(
    monkeypatch,
    capsys,
    complete,
    release_gate,
    expected_exit,
):
    import eval.app_guide.run as harness

    async def fake_run(_args):
        return Path("/synthetic/eval"), {
            "arms": {
                "current": {
                    "errors": 0,
                    "complete_corpus": complete,
                    "release_gate_pass": release_gate,
                }
            }
        }

    monkeypatch.setattr(harness, "run", fake_run)

    assert main([]) == expected_exit
    assert "api_key" not in capsys.readouterr().out.casefold()
