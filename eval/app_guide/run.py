"""Held-out routing and live-capability evaluation for SRW's managed App Guide.

The harness intentionally lives outside ``config/skills/app-guide`` so the
runtime skill cannot see held-out questions or expectations. Both suites
exercise the real managed catalog, production menu fencing, and production
``read_product_guide`` implementation. The M2 suite additionally validates
synthetic snapshots through the production capability output contract and
scores guide → capability → operation order.

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

from eval.app_guide.capability_fixtures import (
    CAPABILITY_FIXTURE_NAMES,
    capability_fixture_json,
)
from src.core.expert_resolution import fence_skills_menu
from src.core.product_capabilities import CAPABILITY_REGISTRY
from src.core.skill_resolution import (
    APP_GUIDE_LOADER_TOOL,
    APP_GUIDE_SKILL,
    add_persistent_system_skills,
    managed_product_guide_system_floor,
    managed_product_guide_turn_boundary,
)
from src.tools.context import ToolContext
from src.tools.product_capabilities import (
    PRODUCT_CAPABILITIES_TOOL_NAME,
    CapabilityToolRequest,
)
from src.tools.product_help import create_product_help_tools

SCHEMA_VERSION = 1
DEFAULT_CASES_PATH = Path(__file__).with_name("cases.yaml")
DEFAULT_CAPABILITY_CASES_PATH = Path(__file__).with_name("capability_cases.yaml")
DEFAULT_RUNS_ROOT = Path(__file__).with_name("runs")
ROUTING_SUITE = "routing"
CAPABILITY_SUITE = "capability"
ALLOWED_CATEGORIES = frozenset(
    {"broad", "workflow", "availability", "near_miss", "honest_gap"}
)
ALLOWED_CAPABILITY_CATEGORIES = frozenset(
    {"stable", "near_miss", "dynamic", "failure", "action", "rollback"}
)
ALLOWED_CRITICALITIES = frozenset({"standard", "critical"})
ALLOWED_SEVERITIES = frozenset({"standard", "critical"})
REQUIRED_NEAR_MISS_TAGS = frozenset(
    {"repository_onboarding", "application_code", "generic_advice", "similar_term"}
)
CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
PHRASE_TOKEN_RE = re.compile(r"[a-z0-9]+")
MAX_REQUIRED_FACT_TOKEN_GAP = 3
REQUIRED_FACT_POLARITY_BLOCKERS = frozenset(
    {"not", "never", "no", "cannot", "unable", "without", "hardly", "fail", "fails"}
)
AFFIRMATIVE_FACT_BLOCKERS = REQUIRED_FACT_POLARITY_BLOCKERS | frozenset(
    {
        "unknown",
        "uncertain",
        "unclear",
        "determine",
        "whether",
        "maybe",
        "perhaps",
        "possibly",
        "might",
        "may",
        "could",
        "if",
    }
)
CLAUSE_SPLIT_RE = re.compile(r"[.!?;,\n]+")

SYSTEM_FRAME = """\
You are an assistant running inside Superhuman Remote Worker (SRW). Answer the
user's actual request concisely. A listed skill is relevant only when its
description matches the request. Do not load an irrelevant skill, invent
product UI, or imply that explaining a feature proves it is enabled in this
deployment or callable in this session.

{skills_menu}
{managed_floor}"""


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
        affirmative = entry.get("affirmative", False)
        if not isinstance(affirmative, bool):
            raise ValueError(f"{item_location}.affirmative must be bool")
        if forbidden and affirmative:
            raise ValueError(f"{item_location} forbidden claim cannot be affirmative")
        if affirmative and any(
            AFFIRMATIVE_FACT_BLOCKERS.intersection(PHRASE_TOKEN_RE.findall(item))
            for item in alternatives
        ):
            raise ValueError(
                f"{item_location}.any_of contains a non-affirmative alternative"
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


def validate_capability_corpus(
    document: Any,
    *,
    known_topics: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate the small M2 live-state/trajectory corpus."""

    _expect_type(document, dict, "capability_corpus")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"capability_corpus.schema_version must be {SCHEMA_VERSION}")
    if document.get("suite") != "app_guide_capabilities":
        raise ValueError("capability_corpus.suite must be 'app_guide_capabilities'")
    cases = document.get("cases")
    _expect_type(cases, list, "capability_corpus.cases")
    if len(cases) < 8:
        raise ValueError("capability corpus must contain at least eight cases")

    topics = known_topics if known_topics is not None else current_topic_ids()
    known_capability_ids = set(CAPABILITY_REGISTRY)
    required_fields = {
        "id",
        "prompt",
        "category",
        "criticality",
        "expected_topic",
        "capability_tool",
        "capability_fixture",
        "expected_capability_ids",
        "expected_operation",
        "required_facts",
        "forbidden_claims",
    }
    seen_ids: set[str] = set()
    seen_prompts: set[str] = set()
    categories: Counter[str] = Counter()

    for index, case in enumerate(cases):
        location = f"capability_corpus.cases[{index}]"
        _expect_type(case, dict, location)
        missing = required_fields - set(case)
        if missing:
            raise ValueError(f"{location} missing {', '.join(sorted(missing))}")

        case_id = case["id"]
        if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
            raise ValueError(f"{location}.id has invalid format")
        if case_id in seen_ids:
            raise ValueError(f"duplicate capability case id {case_id!r}")
        seen_ids.add(case_id)

        prompt = case["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{location}.prompt must be a non-empty string")
        normalized_prompt = " ".join(prompt.casefold().split())
        if normalized_prompt in seen_prompts:
            raise ValueError(f"duplicate capability prompt in {case_id!r}")
        seen_prompts.add(normalized_prompt)

        category = case["category"]
        if category not in ALLOWED_CAPABILITY_CATEGORIES:
            raise ValueError(
                f"{location}.category must be one of "
                f"{sorted(ALLOWED_CAPABILITY_CATEGORIES)}"
            )
        categories[category] += 1
        if case["criticality"] not in ALLOWED_CRITICALITIES:
            raise ValueError(
                f"{location}.criticality must be one of {sorted(ALLOWED_CRITICALITIES)}"
            )

        expected_topic = case["expected_topic"]
        if not isinstance(expected_topic, str) or expected_topic not in topics:
            raise ValueError(f"{location}.expected_topic is not a current topic")

        capability_mode = case["capability_tool"]
        if capability_mode not in {"enabled", "absent"}:
            raise ValueError(
                f"{location}.capability_tool must be 'enabled' or 'absent'"
            )
        fixture = case["capability_fixture"]
        expected_ids = case["expected_capability_ids"]
        if expected_ids is not None:
            if (
                not isinstance(expected_ids, list)
                or any(not isinstance(item, str) for item in expected_ids)
                or len(expected_ids) != len(set(expected_ids))
            ):
                raise ValueError(
                    f"{location}.expected_capability_ids must be null or a unique list"
                )
            unknown_ids = sorted(set(expected_ids) - known_capability_ids)
            if unknown_ids:
                raise ValueError(
                    f"{location}.expected_capability_ids contains unknown IDs: "
                    + ", ".join(unknown_ids)
                )
        if capability_mode == "absent":
            if fixture is not None or expected_ids is not None:
                raise ValueError(
                    f"{location} absent capability tool cannot have a fixture "
                    "or expected call"
                )
        elif fixture not in CAPABILITY_FIXTURE_NAMES:
            raise ValueError(f"{location}.capability_fixture is unknown")
        else:
            # Construction uses the production output contract and catches
            # fixture drift during offline corpus validation.
            capability_fixture_json(fixture)

        operation = case["expected_operation"]
        if operation not in {None, "email_send"}:
            raise ValueError(
                f"{location}.expected_operation must be null or 'email_send'"
            )
        if operation is not None and expected_ids is None:
            raise ValueError(
                f"{location} operation case requires a capability observation"
            )
        if category in {"dynamic", "failure", "action"} and expected_ids is None:
            raise ValueError(f"{location} live-state case requires a capability call")
        if category in {"stable", "near_miss", "rollback"} and expected_ids is not None:
            raise ValueError(f"{location} must remain guide-only")

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
        if not case["required_facts"]:
            raise ValueError(f"{location} needs required facts")

    missing_categories = ALLOWED_CAPABILITY_CATEGORIES - set(categories)
    if missing_categories:
        raise ValueError(
            "capability corpus missing categories: "
            + ", ".join(sorted(missing_categories))
        )
    return cases


def load_capability_cases(
    path: Path = DEFAULT_CAPABILITY_CASES_PATH,
) -> list[dict[str, Any]]:
    """Load and validate the held-out M2 capability corpus."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return validate_capability_corpus(document)


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


def capability_tool_schema() -> dict[str, Any]:
    """Return the production request shape for the live capability tool."""

    return {
        "type": "function",
        "function": {
            "name": PRODUCT_CAPABILITIES_TOOL_NAME,
            "description": (
                "Check current SRW build, deployment, user, session, and "
                "actionability state. The snapshot is advisory, not "
                "authorization."
            ),
            "parameters": CapabilityToolRequest.model_json_schema(),
        },
    }


def email_send_eval_tool_schema() -> dict[str, Any]:
    """Return the safe synthetic operation shape used by one trajectory case."""

    return {
        "type": "function",
        "function": {
            "name": "email_send",
            "description": (
                "Attempt the requested email send using current operation-time "
                "policy. This synthetic evaluation never contacts SMTP."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": ["subject", "body", "to"],
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
        .replace("can't", "cannot")
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
            skipped = answer_tokens[position + 1 : position + 1 + relative]
            if REQUIRED_FACT_POLARITY_BLOCKERS.intersection(skipped):
                break
            position += relative + 1
        else:
            return True
    return False


def _contains_affirmative_fact(normalized_answer: str, alternative: str) -> bool:
    """Require a positive fact in a clause without denial or uncertainty."""

    for clause in CLAUSE_SPLIT_RE.split(normalized_answer):
        if AFFIRMATIVE_FACT_BLOCKERS.intersection(PHRASE_TOKEN_RE.findall(clause)):
            continue
        if _contains_required_fact(clause, alternative):
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

        def matches(alternative: str) -> bool:
            if forbidden:
                return _normalized_text(alternative) in normalized
            if expectation.get("affirmative", False):
                return _contains_affirmative_fact(normalized, alternative)
            return _contains_required_fact(normalized, alternative)

        match = next(
            (
                alternative
                for alternative in expectation["any_of"]
                if matches(alternative)
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


def score_capability_case(
    case: dict[str, Any],
    calls: Sequence[dict[str, Any]],
    answer: str,
) -> dict[str, Any]:
    """Score guide → capability → operation trajectories without an LLM judge."""

    reader_calls = [call for call in calls if call.get("name") == APP_GUIDE_LOADER_TOOL]
    capability_calls = [
        call for call in calls if call.get("name") == PRODUCT_CAPABILITIES_TOOL_NAME
    ]
    operation_calls = [call for call in calls if call.get("name") == "email_send"]
    expected_topic = case["expected_topic"]
    observed_topics = [
        call["topic_id"]
        for call in reader_calls
        if isinstance(call.get("topic_id"), str)
    ]
    unexpected_topics = [
        topic for topic in observed_topics if topic not in {expected_topic, "index"}
    ]
    reader_pass = expected_topic in observed_topics and not unexpected_topics

    expected_ids = case["expected_capability_ids"]
    if expected_ids is None:
        capability_pass = not capability_calls
    else:
        capability_pass = (
            len(capability_calls) == 1
            and capability_calls[0].get("argument_status") == "valid"
            and capability_calls[0].get("topic") is None
            and capability_calls[0].get("capability_ids") == sorted(expected_ids)
        )

    expected_operation = case["expected_operation"]
    if expected_operation is None:
        operation_pass = not operation_calls
    else:
        operation_pass = (
            len(operation_calls) == 1
            and operation_calls[0].get("argument_status") == "valid"
            and set(operation_calls[0].get("argument_keys", []))
            == {"subject", "body", "to"}
        )

    order_pass = True
    if capability_calls:
        order_pass = bool(reader_calls) and max(
            int(call["round"]) for call in reader_calls
        ) < min(int(call["round"]) for call in capability_calls)
    if operation_calls:
        order_pass = (
            order_pass
            and bool(capability_calls)
            and max(int(call["round"]) for call in capability_calls)
            < min(int(call["round"]) for call in operation_calls)
        )

    allowed_tool_names = {
        APP_GUIDE_LOADER_TOOL,
        PRODUCT_CAPABILITIES_TOOL_NAME,
        "email_send",
    }
    unexpected_tools = sorted(
        {
            str(call.get("name"))
            for call in calls
            if call.get("name") not in allowed_tool_names
        }
    )
    tool_set_pass = not unexpected_tools

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
    trajectory_pass = (
        reader_pass
        and capability_pass
        and operation_pass
        and order_pass
        and tool_set_pass
    )
    grounding_pass = not missing_required and not forbidden_hits
    return {
        "trajectory": {
            "expected_trigger": True,
            "observed_trigger": bool(reader_calls),
            "trigger_pass": bool(reader_calls),
            "expected_topic": expected_topic,
            "allowed_topics": [],
            "observed_topics": observed_topics,
            "unexpected_topics": unexpected_topics,
            "topic_pass": reader_pass,
            "expected_capability_ids": expected_ids,
            "observed_capability_calls": len(capability_calls),
            "capability_pass": capability_pass,
            "expected_operation": expected_operation,
            "observed_operation_calls": len(operation_calls),
            "operation_pass": operation_pass,
            "strict_order_pass": order_pass,
            "unexpected_tools": unexpected_tools,
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
    try:
        status = json.loads(result).get("status")
    except (AttributeError, json.JSONDecodeError, TypeError):
        status = None
    if status in {"ready", "partial", "unavailable"}:
        return str(status)
    if "binding changed" in lowered or "not sent" in lowered:
        return "refused"
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
    capability_result: str | None = None,
    operation_result: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Run one fresh-context case and return answer plus bounded trajectory."""

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(catalog)},
        {"role": "user", "content": prompt},
    ]
    turn_boundary = managed_product_guide_turn_boundary(
        catalog,
        [APP_GUIDE_LOADER_TOOL],
    )
    tool_schemas: list[dict[str, Any]] = []
    if reader is not None:
        tool_schemas.append(product_tool_schema(reader))
    if capability_result is not None:
        tool_schemas.append(capability_tool_schema())
    if operation_result is not None:
        tool_schemas.append(email_send_eval_tool_schema())
    calls: list[dict[str, Any]] = []
    total_usage: Counter[str] = Counter()

    for round_index in range(max_tool_rounds + 1):
        kwargs: dict[str, Any] = {
            "model": route.model,
            "messages": [
                *messages,
                *(
                    [{"role": "user", "content": turn_boundary}]
                    if turn_boundary
                    else []
                ),
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if tool_schemas:
            kwargs["tools"] = tool_schemas
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
            capability_topic = None
            capability_ids: list[str] = []
            argument_keys: list[str] = []
            argument_status = "valid"
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
                argument_status = "invalid_json"
            if isinstance(arguments, dict):
                argument_keys = sorted(str(key) for key in arguments)
            elif argument_status == "valid":
                argument_status = "invalid_shape"
                arguments = {}

            if (
                name == APP_GUIDE_LOADER_TOOL
                and reader is not None
                and argument_status == "valid"
            ):
                raw_topic = arguments.get("topic_id")
                if isinstance(raw_topic, str):
                    topic_id = raw_topic
                    result = str(reader.invoke({"topic_id": topic_id}))
                else:
                    argument_status = "invalid_shape"
                    result = (
                        "The managed SRW product guide reader received an "
                        "invalid topic. Do not guess product behavior."
                    )
            elif name == APP_GUIDE_LOADER_TOOL:
                result = (
                    "The managed SRW product guide reader is unavailable or "
                    "received an invalid topic. Do not guess product behavior."
                )
            elif (
                name == PRODUCT_CAPABILITIES_TOOL_NAME
                and capability_result is not None
                and argument_status == "valid"
            ):
                try:
                    request = CapabilityToolRequest.model_validate(arguments)
                except (TypeError, ValueError):
                    argument_status = "invalid_shape"
                    result = (
                        '{"status":"unavailable","error_code":"invalid_request",'
                        '"summary":"The capability request was invalid."}'
                    )
                else:
                    capability_topic = request.topic
                    capability_ids = list(request.capability_ids)
                    result = capability_result
            elif name == PRODUCT_CAPABILITIES_TOOL_NAME:
                result = (
                    '{"status":"unavailable","error_code":"endpoint_unavailable",'
                    '"summary":"Current capability state cannot be inspected."}'
                )
            elif (
                name == "email_send"
                and operation_result is not None
                and argument_status == "valid"
            ):
                valid_operation = (
                    set(arguments) == {"subject", "body", "to"}
                    and isinstance(arguments.get("subject"), str)
                    and isinstance(arguments.get("body"), str)
                    and isinstance(arguments.get("to"), list)
                    and bool(arguments["to"])
                    and all(isinstance(item, str) for item in arguments["to"])
                )
                if valid_operation:
                    result = operation_result
                else:
                    argument_status = "invalid_shape"
                    result = "Error: invalid synthetic email operation. Not sent."
            else:
                result = "Unknown evaluation tool. Continue without inventing output."

            calls.append(
                {
                    "round": round_index + 1,
                    "name": name,
                    "topic_id": topic_id,
                    "topic": capability_topic,
                    "capability_ids": capability_ids,
                    "argument_keys": argument_keys,
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


async def run_capability_case(
    *,
    case: dict[str, Any],
    arm: Arm,
    catalog: dict[str, Any],
    route: LLMRoute,
    timeout: float,
    max_tool_rounds: int,
    max_tokens: int,
) -> dict[str, Any]:
    """Execute one synthetic M2 trajectory with validated capability output."""

    started = time.perf_counter()
    reader = reader_for_catalog(catalog)
    capability_result = (
        capability_fixture_json(case["capability_fixture"])
        if case["capability_tool"] == "enabled"
        else None
    )
    operation_result = (
        "Error: the email connector binding changed after the snapshot. "
        "No email was sent. Retry on the next turn with current state."
        if case["expected_operation"] == "email_send"
        else None
    )
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
                capability_result=capability_result,
                operation_result=operation_result,
            )
        error_type = None
    except Exception as exc:
        answer, calls, usage = "", [], {}
        error_type = type(exc).__name__

    score = score_capability_case(case, calls, answer)
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": CAPABILITY_SUITE,
        "arm": arm.name,
        "case_id": case["id"],
        "category": case["category"],
        "criticality": case["criticality"],
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


def summarize_capability_results(
    rows: Sequence[dict[str, Any]],
    *,
    full_corpus_size: int,
) -> dict[str, Any]:
    """Aggregate the targeted M2 capability-routing matrix."""

    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)

    arms: dict[str, dict[str, Any]] = {}
    for arm, arm_rows in sorted(by_arm.items()):
        by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in arm_rows:
            by_category[row["category"]].append(row)
        summary = {
            "cases": len(arm_rows),
            "complete_corpus": len(arm_rows) == full_corpus_size,
            "passed": sum(bool(row["passed"]) for row in arm_rows),
            "trajectory_passed": sum(
                bool(row["trajectory"]["pass"]) for row in arm_rows
            ),
            "grounding_passed": sum(
                bool(row["answer_score"]["grounding_pass"]) for row in arm_rows
            ),
            "strict_order_passed": sum(
                bool(row["trajectory"]["strict_order_pass"]) for row in arm_rows
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
        summary["release_gate_pass"] = bool(
            summary["complete_corpus"]
            and summary["passed"] == summary["cases"]
            and summary["critical_forbidden_count"] == 0
            and summary["errors"] == 0
        )
        arms[arm] = summary
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": CAPABILITY_SUITE,
        "arms": arms,
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
    default_cases = (
        DEFAULT_CAPABILITY_CASES_PATH
        if args.suite == CAPABILITY_SUITE
        else DEFAULT_CASES_PATH
    )
    cases_path = Path(args.cases or default_cases).resolve()
    all_cases = (
        load_capability_cases(cases_path)
        if args.suite == CAPABILITY_SUITE
        else load_cases(cases_path)
    )
    selected_cases = _select_cases(
        all_cases,
        selected_ids=args.case,
        limit=args.limit,
    )
    arms = [parse_arm_spec(spec) for spec in (args.arm or ["current"])]
    if len({arm.name for arm in arms}) != len(arms):
        raise ValueError("arm names must be unique in one run")
    if args.suite == CAPABILITY_SUITE and any(arm.name == "no-skill" for arm in arms):
        raise ValueError("the capability suite requires the managed guide")

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
            runner = run_capability_case if args.suite == CAPABILITY_SUITE else run_case
            row = await runner(
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

    summary = (
        summarize_capability_results(rows, full_corpus_size=len(all_cases))
        if args.suite == CAPABILITY_SUITE
        else summarize_results(rows, full_corpus_size=len(all_cases))
    )
    repo_root = Path(__file__).resolve().parents[2]
    meta = {
        "schema_version": SCHEMA_VERSION,
        "suite": args.suite,
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
        description=(
            "Evaluate managed App Guide routing or live-capability trajectories."
        )
    )
    parser.add_argument(
        "--suite",
        choices=[ROUTING_SUITE, CAPABILITY_SUITE],
        default=ROUTING_SUITE,
        help="routing (M1 corpus) or capability (M2 targeted matrix)",
    )
    parser.add_argument(
        "--cases",
        help="held-out YAML corpus (defaults to the selected suite's corpus)",
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
            default_cases = (
                DEFAULT_CAPABILITY_CASES_PATH
                if args.suite == CAPABILITY_SUITE
                else DEFAULT_CASES_PATH
            )
            cases_path = Path(args.cases or default_cases).resolve()
            cases = (
                load_capability_cases(cases_path)
                if args.suite == CAPABILITY_SUITE
                else load_cases(cases_path)
            )
            result: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "suite": args.suite,
                "cases": len(cases),
                "corpus_sha256": _sha256_file(cases_path),
            }
            if args.suite == CAPABILITY_SUITE:
                result["categories"] = dict(
                    sorted(Counter(case["category"] for case in cases).items())
                )
            else:
                positives = sum(case["expected_trigger"] for case in cases)
                result.update(
                    {
                        "positive": positives,
                        "negative": len(cases) - positives,
                    }
                )
            print(json.dumps(result, sort_keys=True))
            return 0
        output_dir, summary = asyncio.run(run(args))
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        print(f"app-guide eval error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"output": str(output_dir), "summary": summary}, indent=2))
    errors = sum(arm["errors"] for arm in summary["arms"].values())
    failed_complete_gate = any(
        arm["complete_corpus"] and not arm["release_gate_pass"]
        for arm in summary["arms"].values()
    )
    return 1 if errors or failed_complete_gate else 0


if __name__ == "__main__":
    raise SystemExit(main())
