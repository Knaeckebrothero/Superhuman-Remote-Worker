"""Contract tests for the held-out App Guide model-evaluation harness."""

import copy
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
    assert "read_product_guide(topic_id)" not in system_prompt(no_skill)
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
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert "[product guide topic: jobs]" in requests[1]["messages"][-1]["content"]
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
