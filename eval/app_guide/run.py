"""Standalone routing and grounding evaluation for SRW's managed App Guide.

The harness intentionally lives outside ``config/skills/app-guide`` so the
runtime skill cannot see held-out questions or expectations. It exercises the
real managed catalog, production menu fencing, and production
``read_product_guide`` implementation.

Run ``python -m eval.app_guide.run --help`` from the repository root.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from src.core.expert_resolution import fence_skills_menu
from src.core.skill_resolution import (
    APP_GUIDE_LOADER_TOOL,
    APP_GUIDE_SKILL,
    add_persistent_system_skills,
    managed_product_guide_system_floor,
)
from src.tools.context import ToolContext
from src.tools.product_help import create_product_help_tools

SCHEMA_VERSION = 1
DEFAULT_CASES_PATH = Path(__file__).with_name("cases.yaml")
DEFAULT_RUNS_ROOT = Path(__file__).with_name("runs")
ALLOWED_CATEGORIES = frozenset(
    {"broad", "workflow", "availability", "near_miss", "honest_gap"}
)
ALLOWED_CRITICALITIES = frozenset({"standard", "critical"})
ALLOWED_SEVERITIES = frozenset({"standard", "critical"})
REQUIRED_NEAR_MISS_TAGS = frozenset(
    {"repository_onboarding", "application_code", "generic_advice", "similar_term"}
)
CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
PHRASE_TOKEN_RE = re.compile(r"[a-z0-9]+")
MAX_REQUIRED_FACT_TOKEN_GAP = 3

SYSTEM_FRAME = """\
You are an assistant running inside Superhuman Remote Worker (SRW). Answer the
user's actual request concisely. A listed skill is relevant only when its
description matches the request. Do not load an irrelevant skill, invent
product UI, or imply that explaining a feature proves it is enabled in this
deployment or callable in this session.

{managed_floor}
{skills_menu}"""


@dataclass(frozen=True)
class Arm:
    """One guide delivery arm."""

    name: str
    previous_skills_root: Path | None = None


@dataclass(frozen=True)
class LLMRoute:
    """OpenAI-compatible route; credentials are read only from environment."""

    model: str
    base_url: str | None
    api_key: str

    @classmethod
    def from_env(cls) -> "LLMRoute":
        model = os.environ.get("APP_GUIDE_EVAL_MODEL")
        api_key = os.environ.get("APP_GUIDE_EVAL_API_KEY")
        if not model:
            raise RuntimeError("set APP_GUIDE_EVAL_MODEL for a live run")
        if not api_key:
            raise RuntimeError("set APP_GUIDE_EVAL_API_KEY for a live run")
        return cls(
            model=model,
            base_url=os.environ.get("APP_GUIDE_EVAL_BASE_URL") or None,
            api_key=api_key,
        )

    def client(self, *, timeout: float):
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
        )


def current_topic_ids(skills_root: Path | None = None) -> set[str]:
    """Return logical topic IDs present in a source skill root."""

    root = skills_root or Path(__file__).resolve().parents[2] / "config" / "skills"
    references = root / APP_GUIDE_SKILL / "references"
    topics = {
        path.stem
        for path in references.glob("*.md")
        if path.is_file() and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", path.stem)
    }
    topics.add("index")
    return topics


def _expect_type(
    value: Any,
    expected_type: type | tuple[type, ...],
    location: str,
) -> None:
    if not isinstance(value, expected_type):
        expected = (
            ", ".join(item.__name__ for item in expected_type)
            if isinstance(expected_type, tuple)
            else expected_type.__name__
        )
        raise ValueError(f"{location} must be {expected}")


def _validate_expectations(
    entries: Any,
    *,
    location: str,
    forbidden: bool,
) -> None:
    _expect_type(entries, list, location)
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        item_location = f"{location}[{index}]"
        _expect_type(entry, dict, item_location)
        required_keys = {"id", "any_of"}
        if forbidden:
            required_keys.add("severity")
        missing = required_keys - set(entry)
        if missing:
            raise ValueError(f"{item_location} missing {', '.join(sorted(missing))}")
        expectation_id = entry["id"]
        if not isinstance(expectation_id, str) or not CASE_ID_RE.fullmatch(
            expectation_id
        ):
            raise ValueError(f"{item_location}.id has invalid format")
        if expectation_id in seen:
            raise ValueError(f"{location} repeats id {expectation_id!r}")
        seen.add(expectation_id)
        alternatives = entry["any_of"]
        if (
            not isinstance(alternatives, list)
            or not alternatives
            or any(
                not isinstance(item, str) or not item.strip() for item in alternatives
            )
        ):
            raise ValueError(f"{item_location}.any_of must contain non-empty strings")
        if forbidden and entry["severity"] not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"{item_location}.severity must be one of {sorted(ALLOWED_SEVERITIES)}"
            )


def validate_corpus(
    document: Any,
    *,
    known_topics: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate schema plus the held-out corpus balance/coverage contract."""

    _expect_type(document, dict, "corpus")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"corpus.schema_version must be {SCHEMA_VERSION}")
    cases = document.get("cases")
    _expect_type(cases, list, "corpus.cases")
    if len(cases) < 24:
        raise ValueError("corpus must contain at least 24 cases")

    topics = known_topics if known_topics is not None else current_topic_ids()
    seen_ids: set[str] = set()
    seen_prompts: set[str] = set()
    category_counts: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    positive_count = 0
    critical_forbidden = 0

    required_fields = {
        "id",
        "prompt",
        "category",
        "tags",
        "expected_trigger",
        "expected_topic",
        "expected_gap",
        "criticality",
        "required_facts",
        "forbidden_claims",
    }

    for index, case in enumerate(cases):
        location = f"corpus.cases[{index}]"
        _expect_type(case, dict, location)
        missing = required_fields - set(case)
        if missing:
            raise ValueError(f"{location} missing {', '.join(sorted(missing))}")

        case_id = case["id"]
        if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
            raise ValueError(f"{location}.id has invalid format")
        if case_id in seen_ids:
            raise ValueError(f"duplicate case id {case_id!r}")
        seen_ids.add(case_id)

        prompt = case["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{location}.prompt must be a non-empty string")
        normalized_prompt = " ".join(prompt.casefold().split())
        if normalized_prompt in seen_prompts:
            raise ValueError(f"duplicate prompt in {case_id!r}")
        seen_prompts.add(normalized_prompt)

        category = case["category"]
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(
                f"{location}.category must be one of {sorted(ALLOWED_CATEGORIES)}"
            )
        category_counts[category] += 1

        case_tags = case["tags"]
        if not isinstance(case_tags, list) or any(
            not isinstance(tag, str) or not tag for tag in case_tags
        ):
            raise ValueError(f"{location}.tags must be a list of strings")
        tags.update(case_tags)

        if not isinstance(case["expected_trigger"], bool):
            raise ValueError(f"{location}.expected_trigger must be bool")
        if not isinstance(case["expected_gap"], bool):
            raise ValueError(f"{location}.expected_gap must be bool")
        if case["criticality"] not in ALLOWED_CRITICALITIES:
            raise ValueError(
                f"{location}.criticality must be one of {sorted(ALLOWED_CRITICALITIES)}"
            )

        expected_topic = case["expected_topic"]
        if expected_topic is not None and not isinstance(expected_topic, str):
            raise ValueError(f"{location}.expected_topic must be string or null")
        if expected_topic is not None and expected_topic not in topics:
            raise ValueError(
                f"{location}.expected_topic {expected_topic!r} is not a current topic"
            )
        allowed_topics = case.get("allowed_topics", [])
        if (
            not isinstance(allowed_topics, list)
            or any(not isinstance(topic, str) for topic in allowed_topics)
            or len(set(allowed_topics)) != len(allowed_topics)
        ):
            raise ValueError(
                f"{location}.allowed_topics must be a unique list of strings"
            )
        unknown_allowed_topics = sorted(set(allowed_topics) - topics)
        if unknown_allowed_topics:
            raise ValueError(
                f"{location}.allowed_topics contains unknown topic(s): "
                + ", ".join(unknown_allowed_topics)
            )
        if expected_topic in allowed_topics:
            raise ValueError(
                f"{location}.allowed_topics must not repeat expected_topic"
            )

        _validate_expectations(
            case["required_facts"],
            location=f"{location}.required_facts",
            forbidden=False,
        )
        _validate_expectations(
            case["forbidden_claims"],
            location=f"{location}.forbidden_claims",
            forbidden=True,
        )
        critical_forbidden += sum(
            item["severity"] == "critical" for item in case["forbidden_claims"]
        )

        if case["expected_trigger"]:
            positive_count += 1
            if expected_topic is None:
                raise ValueError(f"{location} positive case needs expected_topic")
            if not case["required_facts"]:
                raise ValueError(f"{location} positive case needs required_facts")
        elif expected_topic is not None:
            raise ValueError(f"{location} negative case must use a null topic")
        elif allowed_topics:
            raise ValueError(f"{location} negative case cannot allow topics")

        if category == "near_miss" and case["expected_trigger"]:
            raise ValueError(f"{location} near_miss must not trigger")
        if category != "near_miss" and not case["expected_trigger"]:
            raise ValueError(f"{location} only near_miss cases may be negative")
        if case["expected_gap"] and (
            category != "honest_gap"
            or not case["expected_trigger"]
            or expected_topic != "index"
        ):
            raise ValueError(
                f"{location} gap case must trigger index in honest_gap category"
            )

    ratio = positive_count / len(cases)
    if not 0.4 <= ratio <= 0.6:
        raise ValueError("expected-trigger balance must remain between 40% and 60%")
    if category_counts["broad"] < 2:
        raise ValueError("corpus needs at least two broad product questions")
    if category_counts["workflow"] < 8:
        raise ValueError("corpus needs at least eight focused workflow questions")
    if category_counts["availability"] < 2:
        raise ValueError("corpus needs at least two availability questions")
    if category_counts["near_miss"] < 8:
        raise ValueError("corpus needs at least eight near-miss negatives")
    if category_counts["honest_gap"] < 1 or tags["off_document"] < 1:
        raise ValueError("corpus needs an explicit off-document honest-gap case")
    if tags["paraphrase"] < 3:
        raise ValueError("corpus needs at least three paraphrase cases")
    missing_near_miss_tags = REQUIRED_NEAR_MISS_TAGS - set(tags)
    if missing_near_miss_tags:
        raise ValueError(
            "corpus missing near-miss coverage: "
            + ", ".join(sorted(missing_near_miss_tags))
        )
    if critical_forbidden < 1:
        raise ValueError("corpus needs at least one critical forbidden claim")

    return cases


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load and validate a corpus file."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return validate_corpus(document)


def parse_arm_spec(spec: str) -> Arm:
    """Parse ``current``, ``no-skill``, or ``previous=/skills/root``."""

    if spec == "current":
        return Arm("current")
    if spec == "no-skill":
        return Arm("no-skill")
    if spec.startswith("previous="):
        raw_path = spec.split("=", 1)[1].strip()
        if not raw_path:
            raise ValueError("previous arm requires a skills-root path")
        root = Path(raw_path).expanduser().resolve()
        if not (root / APP_GUIDE_SKILL / "SKILL.md").is_file():
            raise ValueError("previous arm path must contain app-guide/SKILL.md")
        return Arm("previous", previous_skills_root=root)
    raise ValueError(
        f"unknown arm {spec!r}; use current, no-skill, or previous=/skills/root"
    )


def _without_app_guide(catalog: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(catalog)
    result["menu"] = [
        entry
        for entry in result.get("menu", [])
        if not isinstance(entry, dict) or entry.get("name") != APP_GUIDE_SKILL
    ]
    files = result.get("files")
    if isinstance(files, dict):
        files.pop(APP_GUIDE_SKILL, None)
    return result


def _guide_entry(catalog: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            entry
            for entry in catalog.get("menu", [])
            if isinstance(entry, dict) and entry.get("name") == APP_GUIDE_SKILL
        ),
        None,
    )


def catalog_for_arm(arm: Arm) -> dict[str, Any]:
    """Resolve one arm while keeping non-guide system skills constant."""

    current = add_persistent_system_skills({})
    if arm.name == "no-skill":
        return _without_app_guide(current)
    if arm.name == "previous":
        base = _without_app_guide(current)
        catalog = add_persistent_system_skills(
            base,
            skills_root=arm.previous_skills_root,
        )
    else:
        catalog = current
    if _guide_entry(catalog) is None:
        raise RuntimeError(
            f"{arm.name} arm could not load the managed App Guide; "
            "check the bundle and break-glass environment"
        )
    return catalog


def reader_for_catalog(catalog: dict[str, Any]):
    """Instantiate the production reader for a guide-bearing catalog."""

    if _guide_entry(catalog) is None:
        return None
    tools = create_product_help_tools(ToolContext(config={"_resolved_skills": catalog}))
    return next(
        (tool for tool in tools if tool.name == APP_GUIDE_LOADER_TOOL),
        None,
    )


def system_prompt(catalog: dict[str, Any]) -> str:
    """Render the production-fenced skills menu in a fixed eval frame."""

    menu = fence_skills_menu(catalog.get("menu", []))
    floor = managed_product_guide_system_floor(
        catalog,
        [APP_GUIDE_LOADER_TOOL],
    )
    return SYSTEM_FRAME.format(
        managed_floor=floor,
        skills_menu=menu or "(no skills are available)",
    )


def product_tool_schema(reader: Any) -> dict[str, Any]:
    """Return the OpenAI function schema for the production reader."""

    return {
        "type": "function",
        "function": {
            "name": reader.name,
            "description": reader.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": (
                            "Use index when uncertain, otherwise one exact "
                            "logical topic ID from the App Guide."
                        ),
                    }
                },
                "required": ["topic_id"],
                "additionalProperties": False,
            },
        },
    }


def _normalized_text(value: str) -> str:
    value = (
        value.casefold()
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("→", "->")
        .replace("**", "")
        .replace("__", "")
    )
    return " ".join(value.split())


def _contains_required_fact(normalized_answer: str, alternative: str) -> bool:
    """Match one required fact while allowing a few intervening modifiers."""

    normalized_alternative = _normalized_text(alternative)
    if normalized_alternative in normalized_answer:
        return True

    answer_tokens = PHRASE_TOKEN_RE.findall(normalized_answer)
    alternative_tokens = PHRASE_TOKEN_RE.findall(normalized_alternative)
    if not alternative_tokens:
        return False

    for start, token in enumerate(answer_tokens):
        if token != alternative_tokens[0]:
            continue
        position = start
        for wanted in alternative_tokens[1:]:
            window_end = min(
                len(answer_tokens),
                position + MAX_REQUIRED_FACT_TOKEN_GAP + 2,
            )
            try:
                relative = answer_tokens[position + 1 : window_end].index(wanted)
            except ValueError:
                break
            position += relative + 1
        else:
            return True
    return False


def _score_expectations(
    answer: str,
    expectations: Sequence[dict[str, Any]],
    *,
    forbidden: bool,
) -> list[dict[str, Any]]:
    normalized = _normalized_text(answer)
    scored = []
    for expectation in expectations:
        match = next(
            (
                alternative
                for alternative in expectation["any_of"]
                if (
                    _normalized_text(alternative) in normalized
                    if forbidden
                    else _contains_required_fact(normalized, alternative)
                )
            ),
            None,
        )
        row = {"id": expectation["id"]}
        if forbidden:
            row.update(
                {
                    "severity": expectation["severity"],
                    "hit": match is not None,
                    "matched": match,
                }
            )
        else:
            row.update({"present": match is not None, "matched": match})
        scored.append(row)
    return scored


def score_case(
    case: dict[str, Any],
    calls: Sequence[dict[str, Any]],
    answer: str,
) -> dict[str, Any]:
    """Score routing separately from deterministic answer grounding."""

    reader_calls = [call for call in calls if call.get("name") == APP_GUIDE_LOADER_TOOL]
    observed_trigger = bool(reader_calls)
    observed_topics = [
        call["topic_id"]
        for call in reader_calls
        if isinstance(call.get("topic_id"), str)
    ]
    trigger_pass = observed_trigger is case["expected_trigger"]

    expected_topic = case["expected_topic"]
    if not case["expected_trigger"]:
        topic_pass = not observed_topics
        unexpected_topics: list[str] = observed_topics
    else:
        allowed_topics = {
            expected_topic,
            "index",
            *case.get("allowed_topics", []),
        }
        unexpected_topics = [
            topic for topic in observed_topics if topic not in allowed_topics
        ]
        topic_pass = expected_topic in observed_topics and not unexpected_topics

    required = _score_expectations(
        answer,
        case["required_facts"],
        forbidden=False,
    )
    forbidden = _score_expectations(
        answer,
        case["forbidden_claims"],
        forbidden=True,
    )
    missing_required = [item["id"] for item in required if not item["present"]]
    forbidden_hits = [item["id"] for item in forbidden if item["hit"]]
    critical_forbidden_hits = [
        item["id"]
        for item in forbidden
        if item["hit"] and item["severity"] == "critical"
    ]

    trajectory_pass = trigger_pass and topic_pass
    grounding_pass = not missing_required and not forbidden_hits
    return {
        "trajectory": {
            "expected_trigger": case["expected_trigger"],
            "observed_trigger": observed_trigger,
            "trigger_pass": trigger_pass,
            "expected_topic": expected_topic,
            "allowed_topics": case.get("allowed_topics", []),
            "observed_topics": observed_topics,
            "unexpected_topics": unexpected_topics,
            "topic_pass": topic_pass,
            "pass": trajectory_pass,
        },
        "answer_score": {
            "required_facts": required,
            "missing_required": missing_required,
            "forbidden_claims": forbidden,
            "forbidden_hits": forbidden_hits,
            "critical_forbidden_hits": critical_forbidden_hits,
            "grounding_pass": grounding_pass,
        },
        "passed": trajectory_pass and grounding_pass,
    }


def _tool_result_status(result: str) -> str:
    lowered = result.casefold()
    if result.startswith("[managed product guide:"):
        return "ready"
    if "unknown product-guide topic" in lowered:
        return "unknown_topic"
    if "invalid product-guide topic" in lowered:
        return "invalid_topic"
    if "unavailable" in lowered:
        return "unavailable"
    return "other"


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    result = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, field, None)
        if isinstance(value, int):
            result[field] = value
    return result


async def model_answer(
    *,
    client: Any,
    route: LLMRoute,
    catalog: dict[str, Any],
    reader: Any | None,
    prompt: str,
    max_tool_rounds: int,
    max_tokens: int,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Run one fresh-context case and return answer plus bounded trajectory."""

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(catalog)},
        {"role": "user", "content": prompt},
    ]
    tool_schema = product_tool_schema(reader) if reader is not None else None
    calls: list[dict[str, Any]] = []
    total_usage: Counter[str] = Counter()

    for round_index in range(max_tool_rounds + 1):
        kwargs: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if tool_schema is not None:
            kwargs["tools"] = [tool_schema]
            kwargs["tool_choice"] = "auto"
        response = await client.chat.completions.create(**kwargs)
        total_usage.update(_usage_dict(getattr(response, "usage", None)))
        message = response.choices[0].message
        content = message.content or ""
        tool_calls = list(message.tool_calls or [])
        if not tool_calls:
            return str(content).strip(), calls, dict(total_usage)
        if round_index >= max_tool_rounds:
            return str(content).strip(), calls, dict(total_usage)

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "tool_calls": [],
        }
        for tool_call in tool_calls:
            assistant_message["tool_calls"].append(
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
            )
        messages.append(assistant_message)

        for tool_call in tool_calls:
            name = tool_call.function.name
            topic_id = None
            argument_status = "valid"
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
                argument_status = "invalid_json"
            if isinstance(arguments, dict) and isinstance(
                arguments.get("topic_id"), str
            ):
                topic_id = arguments["topic_id"]
            elif argument_status == "valid":
                argument_status = "invalid_shape"

            if (
                name == APP_GUIDE_LOADER_TOOL
                and reader is not None
                and topic_id is not None
            ):
                result = str(reader.invoke({"topic_id": topic_id}))
            elif name == APP_GUIDE_LOADER_TOOL:
                result = (
                    "The managed SRW product guide reader is unavailable or "
                    "received an invalid topic. Do not guess product behavior."
                )
            else:
                result = "Unknown evaluation tool. Continue without inventing output."

            calls.append(
                {
                    "round": round_index + 1,
                    "name": name,
                    "topic_id": topic_id,
                    "argument_status": argument_status,
                    "result_status": _tool_result_status(result),
                    "result_chars": len(result),
                    "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )

    return "", calls, dict(total_usage)


async def run_case(
    *,
    case: dict[str, Any],
    arm: Arm,
    catalog: dict[str, Any],
    route: LLMRoute,
    timeout: float,
    max_tool_rounds: int,
    max_tokens: int,
) -> dict[str, Any]:
    """Execute and score one synthetic case without retaining guide text."""

    started = time.perf_counter()
    # ToolContext is deliberately rebound for every case. Together with the
    # new message list and client in model_answer this prevents cross-case
    # state leakage.
    reader = reader_for_catalog(catalog)
    try:
        async with route.client(timeout=timeout) as client:
            answer, calls, usage = await model_answer(
                client=client,
                route=route,
                catalog=catalog,
                reader=reader,
                prompt=case["prompt"],
                max_tool_rounds=max_tool_rounds,
                max_tokens=max_tokens,
            )
        error_type = None
    except Exception as exc:  # provider errors become bounded result rows
        answer, calls, usage = "", [], {}
        error_type = type(exc).__name__

    score = score_case(case, calls, answer)
    return {
        "schema_version": SCHEMA_VERSION,
        "arm": arm.name,
        "case_id": case["id"],
        "category": case["category"],
        "criticality": case["criticality"],
        "expected_gap": case["expected_gap"],
        "prompt": case["prompt"],
        "answer": answer,
        "wording_quality": {
            "evaluated": False,
            "reason": "kept separate from deterministic grounding and trajectory",
        },
        "tool_trajectory": {"calls": calls},
        **score,
        "usage": usage,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error_type": error_type,
    }


def summarize_results(
    rows: Sequence[dict[str, Any]],
    *,
    full_corpus_size: int,
) -> dict[str, Any]:
    """Aggregate arm metrics and no-skill deltas."""

    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)

    arm_summaries: dict[str, dict[str, Any]] = {}
    for arm, arm_rows in sorted(by_arm.items()):
        by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in arm_rows:
            by_category[row["category"]].append(row)
        positives = [row for row in arm_rows if row["trajectory"]["expected_trigger"]]
        negatives = [
            row for row in arm_rows if not row["trajectory"]["expected_trigger"]
        ]

        def rate(numerator: int, denominator: int) -> float:
            return round(numerator / denominator, 4) if denominator else 0.0

        arm_summaries[arm] = {
            "cases": len(arm_rows),
            "complete_corpus": len(arm_rows) == full_corpus_size,
            "passed": sum(bool(row["passed"]) for row in arm_rows),
            "pass_rate": rate(
                sum(bool(row["passed"]) for row in arm_rows), len(arm_rows)
            ),
            "trajectory_pass_rate": rate(
                sum(bool(row["trajectory"]["pass"]) for row in arm_rows),
                len(arm_rows),
            ),
            "grounding_pass_rate": rate(
                sum(bool(row["answer_score"]["grounding_pass"]) for row in arm_rows),
                len(arm_rows),
            ),
            "positive_reader_call_rate": rate(
                sum(bool(row["trajectory"]["observed_trigger"]) for row in positives),
                len(positives),
            ),
            "positive_topic_pass_rate": rate(
                sum(bool(row["trajectory"]["topic_pass"]) for row in positives),
                len(positives),
            ),
            "near_miss_reader_false_positive_rate": rate(
                sum(bool(row["trajectory"]["observed_trigger"]) for row in negatives),
                len(negatives),
            ),
            "critical_forbidden_count": sum(
                len(row["answer_score"]["critical_forbidden_hits"]) for row in arm_rows
            ),
            "errors": sum(row["error_type"] is not None for row in arm_rows),
            "by_category": {
                category: {
                    "cases": len(category_rows),
                    "passed": sum(bool(row["passed"]) for row in category_rows),
                }
                for category, category_rows in sorted(by_category.items())
            },
        }
        summary = arm_summaries[arm]
        summary["release_gate_pass"] = bool(
            summary["complete_corpus"]
            and summary["passed"] == summary["cases"]
            and summary["critical_forbidden_count"] == 0
            and summary["errors"] == 0
        )

    comparisons = {}
    baseline = arm_summaries.get("no-skill")
    if baseline:
        for arm, summary in arm_summaries.items():
            if arm == "no-skill":
                continue
            comparisons[f"{arm}_vs_no-skill"] = {
                metric: round(summary[metric] - baseline[metric], 4)
                for metric in (
                    "pass_rate",
                    "trajectory_pass_rate",
                    "grounding_pass_rate",
                    "positive_reader_call_rate",
                    "positive_topic_pass_rate",
                    "near_miss_reader_false_positive_rate",
                )
            }

    return {
        "schema_version": SCHEMA_VERSION,
        "arms": arm_summaries,
        "comparisons": comparisons,
    }


def _git_revision(repo_root: Path) -> dict[str, Any]:
    """Return bounded source identity without paths or diff content."""

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = "unknown", True
    return {"commit": revision, "tracked_worktree_dirty": dirty}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _select_cases(
    cases: list[dict[str, Any]],
    *,
    selected_ids: Sequence[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    if selected_ids:
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError("--case IDs must not be repeated")
        by_id = {case["id"]: case for case in cases}
        unknown = sorted(set(selected_ids) - set(by_id))
        if unknown:
            raise ValueError("unknown case id(s): " + ", ".join(unknown))
        selected = [by_id[case_id] for case_id in selected_ids]
    else:
        selected = list(cases)
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no cases selected")
    return selected


async def run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    cases_path = Path(args.cases).resolve()
    all_cases = load_cases(cases_path)
    selected_cases = _select_cases(
        all_cases,
        selected_ids=args.case,
        limit=args.limit,
    )
    arms = [parse_arm_spec(spec) for spec in (args.arm or ["current"])]
    if len({arm.name for arm in arms}) != len(arms):
        raise ValueError("arm names must be unique in one run")

    route = LLMRoute.from_env()
    catalogs = {arm.name: catalog_for_arm(arm) for arm in arms}
    readers = {arm.name: reader_for_catalog(catalogs[arm.name]) for arm in arms}
    for arm in arms:
        if arm.name != "no-skill" and readers[arm.name] is None:
            raise RuntimeError(f"{arm.name} arm did not instantiate the product reader")

    run_id = uuid.uuid4().hex[:12]
    started_at = datetime.now(timezone.utc).isoformat()
    output_dir = (
        Path(args.out).resolve()
        if args.out
        else DEFAULT_RUNS_ROOT / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{run_id}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    rows: list[dict[str, Any]] = []
    for arm in arms:
        for case in selected_cases:
            row = await run_case(
                case=case,
                arm=arm,
                catalog=catalogs[arm.name],
                route=route,
                timeout=args.timeout,
                max_tool_rounds=args.max_tool_rounds,
                max_tokens=args.max_tokens,
            )
            row["run_id"] = run_id
            rows.append(row)
            status = "PASS" if row["passed"] else "FAIL"
            error = f" error={row['error_type']}" if row["error_type"] else ""
            print(f"{arm.name:8} {case['id']:40} {status}{error}", flush=True)

    summary = summarize_results(rows, full_corpus_size=len(all_cases))
    repo_root = Path(__file__).resolve().parents[2]
    meta = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model": route.model,
        "source": _git_revision(repo_root),
        "corpus_sha256": _sha256_file(cases_path),
        "harness_sha256": _sha256_file(Path(__file__)),
        "full_corpus_cases": len(all_cases),
        "selected_case_ids": [case["id"] for case in selected_cases],
        "arms": [
            {
                "name": arm.name,
                "guide_bundle_digest": (
                    (_guide_entry(catalogs[arm.name]) or {}).get("bundle_digest")
                ),
            }
            for arm in arms
        ],
    }
    _write_jsonl(output_dir / "results.jsonl", rows)
    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "run_meta.json", meta)
    return output_dir, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate managed App Guide routing and grounded answers."
    )
    parser.add_argument(
        "--cases",
        default=str(DEFAULT_CASES_PATH),
        help="held-out YAML corpus (default: eval/app_guide/cases.yaml)",
    )
    parser.add_argument(
        "--arm",
        action="append",
        help=(
            "repeatable: current, no-skill, or previous=/path/to/config/skills; "
            "defaults to current"
        ),
    )
    parser.add_argument("--case", action="append", default=[], help="run one case ID")
    parser.add_argument("--limit", type=int, help="run only the first N selected cases")
    parser.add_argument("--out", help="new output directory")
    parser.add_argument("--max-tool-rounds", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the full corpus without contacting a model",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.max_tool_rounds < 1:
        parser.error("--max-tool-rounds must be positive")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be positive")

    try:
        if args.validate_only:
            cases_path = Path(args.cases).resolve()
            cases = load_cases(cases_path)
            positives = sum(case["expected_trigger"] for case in cases)
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "cases": len(cases),
                        "positive": positives,
                        "negative": len(cases) - positives,
                        "corpus_sha256": _sha256_file(cases_path),
                    },
                    sort_keys=True,
                )
            )
            return 0
        output_dir, summary = asyncio.run(run(args))
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        print(f"app-guide eval error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"output": str(output_dir), "summary": summary}, indent=2))
    errors = sum(arm["errors"] for arm in summary["arms"].values())
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
