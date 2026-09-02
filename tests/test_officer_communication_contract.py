"""The officer's communication contract has an inbound half
(officer_visibility_streamline.md §3.4): a Legate message is never ambient
context, and answering it is part of the wake's work."""

import re
from pathlib import Path

from src.tools.core.officer import OFFICER_TOOLS_METADATA

PERSONA = Path("config/experts/centurion/persona.txt")


def _contract() -> str:
    text = PERSONA.read_text()
    match = re.search(
        r"<communication_contract>(.*?)</communication_contract>", text, re.S
    )
    assert match, "persona lost its <communication_contract> block"
    return match.group(1)


class _AnyKey(dict):
    """format_map helper: every placeholder renders as ''."""

    def __missing__(self, key):
        return ""


class TestInboundContract:
    def test_the_contract_has_an_inbound_clause(self):
        body = _contract()
        assert "Inbound" in body
        assert "never ambient context" in body
        assert "before you file your sleep" in body

    def test_a_question_owes_an_answer_the_same_wake(self):
        assert "A question owes an answer the same wake" in _contract()

    def test_the_clause_names_both_answer_paths(self):
        body = _contract()
        assert "Answer in your session when the Legate is live there" in body
        assert "notify_user" in body

    def test_the_outbound_tiers_survive(self):
        body = _contract()
        for tier in ("log", "digest", "page"):
            assert f"- {tier}" in body

    def test_persona_still_formats(self):
        # The persona is .format()-ed at boot; a stray brace in the new
        # clause would crash every officer.
        PERSONA.read_text().format_map(_AnyKey())


class TestToolCatalogText:
    def test_notify_user_description_no_longer_promises_a_budget(self):
        description = OFFICER_TOOLS_METADATA["notify_user"]["description"].lower()
        assert "budget" not in description

    def test_notify_user_description_names_answering_the_legate(self):
        description = OFFICER_TOOLS_METADATA["notify_user"]["description"].lower()
        assert "answer" in description
