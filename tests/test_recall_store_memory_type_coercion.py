"""An LLM-authored memory_type must never discard the memory.

Regression guard for Defect 7 of
knowledge-base/knowledge/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

Job c6dd288d's extractor emitted ``factial`` (a typo for ``factual``) and the
whole extraction was lost to::

    CheckViolationError: new row for relation "memories" violates check
    constraint "valid_memory_type"

``memory_type`` comes straight from the extractor's ``mem.type``, so it is
untrusted input feeding a CHECK constraint. Coerce and log; never drop.
"""

import logging

import pytest

from shared.runtime.services.recall_store import (
    DEFAULT_MEMORY_TYPE,
    VALID_MEMORY_TYPES,
    coerce_memory_type,
)


class TestCoerceMemoryType:
    def test_the_incident_value_is_coerced(self):
        assert coerce_memory_type("factial") == "factual"

    @pytest.mark.parametrize("valid", sorted(VALID_MEMORY_TYPES))
    def test_valid_types_pass_through_unchanged(self, valid):
        assert coerce_memory_type(valid) == valid

    @pytest.mark.parametrize("bad", ["", "FACTUAL", "unknown", None, "factual "])
    def test_out_of_set_values_fall_back_to_the_default(self, bad):
        assert coerce_memory_type(bad) == DEFAULT_MEMORY_TYPE

    def test_coercion_is_logged_at_warning_with_the_rejected_value(self, caplog):
        with caplog.at_level(logging.WARNING):
            coerce_memory_type("factial")
        assert any(
            r.levelno == logging.WARNING and "factial" in r.getMessage()
            for r in caplog.records
        ), "a silent coercion hides a real extractor bug"

    def test_valid_types_do_not_log(self, caplog):
        with caplog.at_level(logging.WARNING):
            coerce_memory_type("procedural")
        assert not caplog.records


class TestConstantMatchesSchema:
    def test_python_constant_matches_the_sql_check_constraint(self):
        """The constant exists to mirror SQL — assert it actually does.

        Reads the schema rather than restating the values, so a migration that
        adds a memory type fails here instead of at runtime.
        """
        from pathlib import Path

        schema = Path("src/orchestrator/database/vector_schema.sql").read_text()
        line = next(
            ln
            for ln in schema.splitlines()
            if "valid_memory_type" in ln and "CHECK" in ln
        )
        in_sql = {
            token.strip().strip("'")
            for token in line.split("(")[-1].rstrip(") ,").split(",")
        }
        assert in_sql == set(VALID_MEMORY_TYPES), (
            f"Python constant {sorted(VALID_MEMORY_TYPES)} has drifted from the "
            f"SQL constraint {sorted(in_sql)}"
        )
