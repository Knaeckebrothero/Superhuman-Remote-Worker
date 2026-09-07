#!/usr/bin/env python3
"""Generate synthetic old-layout checkpoint bytes, never read production data.

Reads named definitions from a frozen checkout with AST extraction: loading the
entire old agent runtime is unnecessary. The state factory executes unchanged,
with its TypedDict constructor represented by dict (the same runtime result).
Model definitions retain the original module identity for negative fixtures.

  python scripts/flatten_checkpoint_fixture.py --baseline OLD --output FILE

The output has safe positive state/message records and explicit negative model
records. The latter demonstrate why successful decoding alone is not proof:
strict msgpack returns dictionaries for unallowlisted first-party model types.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import List

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _definitions(path: Path, names: set[str]) -> tuple[ast.Module, str]:
    raw = path.read_bytes()
    parsed = ast.parse(raw.decode(), filename=str(path))
    selected = [
        node
        for node in parsed.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    if {node.name for node in selected} != names:
        raise ValueError("frozen source does not contain the expected definitions")
    future = ast.parse("from __future__ import annotations\n").body
    return ast.Module(body=future + selected, type_ignores=[]), hashlib.sha256(
        raw
    ).hexdigest()


def generate(baseline: Path) -> dict:
    if version("langgraph-checkpoint") != "4.1.1":
        raise ValueError("fixture producer must use the pinned checkpoint 4.1.1")
    state_tree, state_hash = _definitions(
        baseline / "src/core/state.py", {"create_initial_state"}
    )
    state_namespace = {"UniversalAgentState": dict}
    exec(
        compile(ast.fix_missing_locations(state_tree), "frozen_state_factory", "exec"),
        state_namespace,
    )
    state = state_namespace["create_initial_state"](
        "synthetic-job", "/workspace/synthetic-job", {"synthetic": True}
    )
    messages = [
        SystemMessage(content="Synthetic fixture instructions.", id="synthetic-system"),
        HumanMessage(
            content=[{"type": "text", "text": "Read the synthetic note."}],
            additional_kwargs={"fixture_identity": "synthetic"},
            id="synthetic-user",
        ),
        AIMessage(
            content="",
            id="synthetic-assistant",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "note.txt"},
                    "id": "synthetic-tool",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content="Synthetic note contents.",
            id="synthetic-result",
            tool_call_id="synthetic-tool",
            name="read_file",
        ),
    ]
    state.update(
        messages=messages,
        initialized=True,
        is_strategic_phase=False,
        iteration=7,
        phase_number=2,
        worker_resume_id="synthetic-resume",
        delivered_reply_keys=["synthetic-reply"],
        instruction_read_receipts={"instructions.md": {"sha256": "0" * 64}},
        todos=[{"id": 1, "content": "Finish synthetic work", "status": "in_progress"}],
        freeze_data={
            "freeze_type": "human_review",
            "summary": "Synthetic review pause",
        },
        resume_feedback="Synthetic feedback",
        completion_report_payload={
            "should_stop": False,
            "goal_achieved": False,
            "error": None,
            "freeze_data": None,
        },
    )
    model_tree, model_hash = _definitions(
        baseline / "src/core/context.py", {"IdentityAnchor", "ConversationSummary"}
    )
    old_module = ModuleType("src.core.context")
    old_module.__dict__.update(
        BaseModel=BaseModel,
        ConfigDict=ConfigDict,
        Field=Field,
        model_validator=model_validator,
        List=List,
    )
    previous = sys.modules.get(old_module.__name__)
    sys.modules[old_module.__name__] = old_module
    try:
        exec(
            compile(
                ast.fix_missing_locations(model_tree), "frozen_summary_models", "exec"
            ),
            old_module.__dict__,
        )
        anchor = old_module.IdentityAnchor(
            agent_role="Synthetic worker",
            current_task="Synthetic task",
            active_constraints=["Retain synthetic state"],
        )
        summary = old_module.ConversationSummary(
            summary="Synthetic conversation summary",
            tasks_completed="Synthetic completed task",
            key_decisions="Synthetic decision",
            current_state="Synthetic active state",
            identity_anchor=anchor,
        )
        serializer = JsonPlusSerializer(
            allowed_msgpack_modules=None, pickle_fallback=False
        )
        records = []
        for name, value, expectation in (
            ("worker-state", state, "preserve"),
            ("pending-write-message", messages[2], "preserve"),
            (
                "pending-write-completion",
                state["completion_report_payload"],
                "preserve",
            ),
            ("old-identity-anchor", anchor, "first-party-type-requires-decision"),
            ("old-conversation-summary", summary, "first-party-type-requires-decision"),
        ):
            encoding, blob = serializer.dumps_typed(value)
            expected = (
                {
                    "messages": [message_to_dict(message) for message in messages],
                    "fields": {
                        key: val for key, val in state.items() if key != "messages"
                    },
                }
                if name == "worker-state"
                else message_to_dict(value)
                if name == "pending-write-message"
                else value.model_dump()
                if isinstance(value, BaseModel)
                else value
            )
            records.append(
                {
                    "name": name,
                    "expectation": expectation,
                    "encoding": encoding,
                    "blob_base64": base64.b64encode(blob).decode(),
                    "expected": expected,
                }
            )
    finally:
        if previous is None:
            sys.modules.pop(old_module.__name__, None)
        else:
            sys.modules[old_module.__name__] = previous
    persistence_source = (baseline / "src/subagents/persistence.py").read_bytes()
    persistence_tree = ast.parse(persistence_source.decode())
    seed_key = next(
        ast.literal_eval(node.value)
        for node in persistence_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "SUBAGENT_FORK_SEED_PROVIDER_KEY"
            for target in node.targets
        )
    )
    return {
        "version": 1,
        "synthetic_only": True,
        "producer_versions": {
            name: version(name)
            for name in (
                "langgraph-checkpoint",
                "langchain-core",
                "ormsgpack",
                "pydantic",
            )
        },
        "source_sha256": {
            "src/core/state.py": state_hash,
            "src/core/context.py": model_hash,
            "src/subagents/persistence.py": hashlib.sha256(
                persistence_source
            ).hexdigest(),
        },
        "records": records,
        "subagent_fork_seed": {
            "provider_raw": {seed_key: message_to_dict(messages[1])}
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(generate(args.baseline), indent=2, sort_keys=True) + "\n"
    )
    print("Wrote five synthetic checkpoint records; no production data read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
