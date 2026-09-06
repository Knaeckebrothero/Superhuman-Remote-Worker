"""Contracts for the targeted M2 App Guide capability model matrix."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from eval.app_guide.capability_fixtures import (
    capability_fixture,
    capability_fixture_json,
)
from eval.app_guide.run import (
    APP_GUIDE_LOADER_TOOL,
    LLMRoute,
    PRODUCT_CAPABILITIES_TOOL_NAME,
    catalog_for_arm,
    load_capability_cases,
    model_answer,
    parse_arm_spec,
    reader_for_catalog,
    score_capability_case,
    validate_capability_corpus,
)
from shared.runtime.core.product_capabilities import AgentAction, SessionState
from agent.tools.product_capabilities import CapabilityToolStatus


@pytest.fixture
def cases():
    return load_capability_cases()


def _case(cases, case_id):
    return next(case for case in cases if case["id"] == case_id)


def _reader_call(round_number=1, topic="datasources-email"):
    return {
        "round": round_number,
        "name": APP_GUIDE_LOADER_TOOL,
        "topic_id": topic,
        "topic": None,
        "capability_ids": [],
        "argument_keys": ["topic_id"],
        "argument_status": "valid",
    }


def _capability_call(round_number=2, *, topic=None):
    return {
        "round": round_number,
        "name": PRODUCT_CAPABILITIES_TOOL_NAME,
        "topic_id": None,
        "topic": topic,
        "capability_ids": ["datasources.email.send"],
        "argument_keys": ["capability_ids"],
        "argument_status": "valid",
    }


def _operation_call(round_number=3, *, extra_key=None):
    keys = ["body", "subject", "to"]
    if extra_key:
        keys.append(extra_key)
    return {
        "round": round_number,
        "name": "email_send",
        "topic_id": None,
        "topic": None,
        "capability_ids": [],
        "argument_keys": sorted(keys),
        "argument_status": "valid",
    }


def test_capability_corpus_is_the_eight_cell_targeted_matrix(cases):
    assert len(cases) == 8
    assert {case["category"] for case in cases} == {
        "stable",
        "near_miss",
        "dynamic",
        "failure",
        "action",
        "rollback",
    }
    assert sum(case["expected_capability_ids"] is not None for case in cases) == 5
    assert sum(case["expected_operation"] is not None for case in cases) == 1
    assert sum(case["capability_tool"] == "absent" for case in cases) == 1


def test_capability_corpus_rejects_unknown_fixture(cases):
    document = {
        "schema_version": 1,
        "suite": "app_guide_capabilities",
        "cases": copy.deepcopy(cases),
    }
    document["cases"][0]["capability_fixture"] = "unvalidated-prose"

    with pytest.raises(ValueError, match="capability_fixture is unknown"):
        validate_capability_corpus(document)


def test_stable_how_to_forbids_a_capability_call(cases):
    case = _case(cases, "stable-email-folder-how-to")
    answer = "Use the Folder allowlist, then select the connector for this session."

    score = score_capability_case(case, [_reader_call()], answer)
    overchecked = score_capability_case(
        case,
        [_reader_call(), _capability_call()],
        answer,
    )

    assert score["passed"] is True
    assert overchecked["trajectory"]["capability_pass"] is False
    assert overchecked["passed"] is False


def test_capability_near_miss_forbids_live_lookup(cases):
    case = _case(cases, "near-miss-capability-terminology")
    answer = (
        "Supported means implemented in the running product. Whether something "
        "can run now also depends on deployment, grants, and session state. "
        "That is a definition only, without inspecting your session."
    )

    score = score_capability_case(
        case,
        [_reader_call(topic=case["expected_topic"])],
        answer,
    )
    overchecked = score_capability_case(
        case,
        [
            _reader_call(topic=case["expected_topic"]),
            _capability_call(),
        ],
        answer,
    )

    assert score["passed"] is True
    assert overchecked["trajectory"]["capability_pass"] is False
    assert overchecked["passed"] is False


def test_dynamic_query_requires_exact_ids_and_strict_reader_first_order(cases):
    case = _case(cases, "dynamic-email-send-ready")
    answer = (
        "Build: supported; deployment: enabled; user: allowed; session: ready. "
        "The agent can execute, but this is an advisory snapshot and the "
        "operation must recheck current policy."
    )
    correct = score_capability_case(
        case,
        [_reader_call(1), _capability_call(2)],
        answer,
    )
    same_round = score_capability_case(
        case,
        [_reader_call(1), _capability_call(1)],
        answer,
    )
    guide_topic_as_capability_topic = score_capability_case(
        case,
        [_reader_call(1), _capability_call(2, topic="datasources-email")],
        answer,
    )

    assert correct["passed"] is True
    assert same_round["trajectory"]["strict_order_pass"] is False
    assert guide_topic_as_capability_topic["trajectory"]["capability_pass"] is False


def test_partial_and_mixed_fixtures_use_the_production_output_contract():
    partial = capability_fixture("email_send_partial")
    mixed = capability_fixture("email_send_mixed")

    assert partial.status is CapabilityToolStatus.PARTIAL
    assert partial.response is not None
    assert partial.response.capabilities[0].session.state is SessionState.UNKNOWN
    assert partial.response.capabilities[0].agent_action is AgentAction.UNKNOWN
    assert mixed.status is CapabilityToolStatus.READY
    assert mixed.response is not None
    assert mixed.response.product.mixed_build is True
    assert "person@example" not in capability_fixture_json("email_send_ready")


def test_mixed_build_case_requires_readiness_and_version_caution(cases):
    case = _case(cases, "dynamic-email-send-mixed-build")
    calls = [_reader_call(), _capability_call()]
    correct = (
        "The session can execute email sending, but components report different "
        "revisions, so we cannot assume one version."
    )
    version_only = (
        "Components report different revisions, so we cannot assume one version."
    )

    assert score_capability_case(case, calls, correct)["passed"] is True
    missing_readiness = score_capability_case(case, calls, version_only)
    assert (
        "current-send-readiness"
        in missing_readiness["answer_score"]["missing_required"]
    )
    assert missing_readiness["passed"] is False
    for denial in (
        "The session can never execute email sending",
        "The session can not send email",
        "The session can't send email",
        "I cannot determine whether this session can send email",
        "Whether this session can execute email sending is unknown",
    ):
        wrong_polarity = score_capability_case(
            case,
            calls,
            (
                f"{denial}, while components report different revisions, "
                "so we cannot assume one version."
            ),
        )
        assert (
            "current-send-readiness"
            in wrong_polarity["answer_score"]["missing_required"]
        )
        assert wrong_polarity["passed"] is False


def test_action_case_requires_operation_after_snapshot_without_authority_args(cases):
    case = _case(cases, "action-email-changed-before-send")
    answer = (
        "The email was not sent: the binding changed, so retry on the next turn "
        "with current state."
    )
    correct = score_capability_case(
        case,
        [_reader_call(1), _capability_call(2), _operation_call(3)],
        answer,
    )
    same_round = score_capability_case(
        case,
        [_reader_call(1), _capability_call(2), _operation_call(2)],
        answer,
    )
    snapshot_as_authority = score_capability_case(
        case,
        [
            _reader_call(1),
            _capability_call(2),
            _operation_call(3, extra_key="authorization"),
        ],
        answer,
    )

    assert correct["passed"] is True
    assert same_round["trajectory"]["strict_order_pass"] is False
    assert snapshot_as_authority["trajectory"]["operation_pass"] is False


def test_rollback_keeps_guide_and_marks_current_state_unknown(cases):
    case = _case(cases, "rollback-current-email-state")
    answer = (
        "You can still select the connector for the session, but I cannot "
        "inspect current availability, so I cannot confirm attachment."
    )

    score = score_capability_case(case, [_reader_call()], answer)

    assert score["passed"] is True
    assert score["trajectory"]["observed_capability_calls"] == 0


@pytest.mark.asyncio
async def test_model_loop_exposes_and_dispatches_ordered_capability_operation_tools(
    monkeypatch,
):
    monkeypatch.delenv("APP_GUIDE_BREAK_GLASS_DISABLED", raising=False)
    catalog = catalog_for_arm(parse_arm_spec("current"))
    reader = reader_for_catalog(catalog)
    requests = []

    class Completions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            step = len(requests)
            if step == 1:
                name = APP_GUIDE_LOADER_TOOL
                arguments = '{"topic_id":"datasources-email"}'
            elif step == 2:
                name = PRODUCT_CAPABILITIES_TOOL_NAME
                arguments = '{"capability_ids":["datasources.email.send"]}'
            elif step == 3:
                name = "email_send"
                arguments = (
                    '{"subject":"M2 check","body":"Synthetic test",'
                    '"to":["person@example.test"]}'
                )
            else:
                message = SimpleNamespace(
                    content=(
                        "The email was not sent because the binding changed; "
                        "retry on the next turn with current state."
                    ),
                    tool_calls=[],
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message)],
                    usage=None,
                )
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id=f"call-{step}",
                        function=SimpleNamespace(name=name, arguments=arguments),
                    )
                ],
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=None,
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    answer, calls, _usage = await model_answer(
        client=client,
        route=LLMRoute(model="fake", base_url=None, api_key="not-recorded"),
        catalog=catalog,
        reader=reader,
        prompt="Check and send the synthetic message.",
        max_tool_rounds=4,
        max_tokens=300,
        capability_result=capability_fixture_json("email_send_ready"),
        operation_result=(
            "Error: the email connector binding changed. No email was sent."
        ),
    )

    assert [call["name"] for call in calls] == [
        APP_GUIDE_LOADER_TOOL,
        PRODUCT_CAPABILITIES_TOOL_NAME,
        "email_send",
    ]
    assert [call["round"] for call in calls] == [1, 2, 3]
    assert calls[1]["capability_ids"] == ["datasources.email.send"]
    assert calls[2]["result_status"] == "refused"
    assert "result" not in calls[1] and "result" not in calls[2]
    exposed = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert exposed == {
        APP_GUIDE_LOADER_TOOL,
        PRODUCT_CAPABILITIES_TOOL_NAME,
        "email_send",
    }
    assert answer.startswith("The email was not sent")
