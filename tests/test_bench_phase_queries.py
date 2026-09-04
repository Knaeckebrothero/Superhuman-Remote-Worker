"""Drift guards for the U2 WP5 audit helper."""

import re
from pathlib import Path

from src.tools.registry import TOOL_REGISTRY


QUERY = Path("bench/queries/phase_illegal_calls.sql")


def test_phase_query_matches_single_phase_registry_tools():
    sql = QUERY.read_text()
    values = re.search(
        r"single_phase\(tool_name, allowed_phase\) AS \(\s*VALUES(?P<body>.*?)\n\),",
        sql,
        re.DOTALL,
    )
    assert values is not None
    query_pairs = set(
        re.findall(r"\('([^']+)', '(strategic|tactical)'\)", values["body"])
    )
    registry_pairs = {
        (name, phases[0])
        for name, metadata in TOOL_REGISTRY.items()
        if len(phases := metadata.get("phases", ["strategic", "tactical"])) == 1
    }
    assert query_pairs == registry_pairs


def test_phase_query_checks_gate_results_and_persistent_blocks():
    sql = QUERY.read_text()
    assert "unsafe_or_unclassified" in sql
    assert "result_payload->'tool'->>'success'" in sql
    assert "message->'additional_kwargs'->>'srw_protected' = 'true'" in sql
    assert "message->'additional_kwargs'->>'srw_instruction_path'" in sql
    assert "^skills/(strategic|tactical)-phase/SKILL[.]md$" in sql
    assert "message->>'content' LIKE" not in sql
    assert "zero_block_requests" in sql
    assert "multi_block_additions" in sql
