"""Array-shaped tool arguments a weak model got wrong are repaired, not rejected.

Live evidence behind this (2026-08-16, main dev): a commissioned officer on the
default brain could not tag a backlog ticket. Eight consecutive
``kb_write``/``kb_update`` calls failed pydantic validation, alternating between
a JSON-encoded string and an ``{'item': [...]}`` wrapper, and it finally dropped
the argument and wrote an untagged note — a silent wrong answer.
"""

from typing import Dict, List, Optional

import pytest
from pydantic import BaseModel

from src.shared.tool_arg_coercion import coerce_tool_args


class _Schema(BaseModel):
    title: str
    tags: Optional[List[str]] = None
    links: Optional[List[dict]] = None
    counts: Optional[Dict[str, int]] = None


class TestShapesModelsActuallyProduce:
    def test_a_json_encoded_array_becomes_the_array(self):
        args, repaired = coerce_tool_args(
            _Schema, {"title": "t", "tags": '["category:researcher", "ready"]'}
        )
        assert args["tags"] == ["category:researcher", "ready"]
        assert repaired == ["tags"]

    def test_a_wrapped_array_is_unwrapped(self):
        args, repaired = coerce_tool_args(
            _Schema, {"title": "t", "tags": {"item": ["ready", "bp05"]}}
        )
        assert args["tags"] == ["ready", "bp05"]
        assert repaired == ["tags"]

    @pytest.mark.parametrize("key", ["item", "items", "values", "list", "array"])
    def test_every_known_wrapper_key_is_unwrapped(self, key):
        args, repaired = coerce_tool_args(_Schema, {"title": "t", "tags": {key: ["a"]}})
        assert args["tags"] == ["a"]
        assert repaired == ["tags"]

    def test_an_array_that_lost_its_brackets_is_reassembled_in_order(self):
        args, repaired = coerce_tool_args(
            _Schema, {"title": "t", "tags": {"1": "second", "0": "first"}}
        )
        assert args["tags"] == ["first", "second"]

    def test_list_of_objects_is_repaired_too(self):
        args, _ = coerce_tool_args(_Schema, {"title": "t", "links": '[{"a": 1}]'})
        assert args["links"] == [{"a": 1}]


class TestItRefusesToInventData:
    def test_a_bare_string_stays_a_string(self):
        """Guessing ``["ready"]`` here would fabricate a tag the model never sent."""
        args, repaired = coerce_tool_args(_Schema, {"title": "t", "tags": "ready"})
        assert args["tags"] == "ready"
        assert repaired == []

    def test_malformed_json_is_left_for_the_real_error(self):
        args, repaired = coerce_tool_args(_Schema, {"title": "t", "tags": '["a", '})
        assert args["tags"] == '["a", '
        assert repaired == []

    def test_a_genuine_mapping_field_is_never_flattened(self):
        args, repaired = coerce_tool_args(
            _Schema, {"title": "t", "counts": {"0": 1, "1": 2}}
        )
        assert args["counts"] == {"0": 1, "1": 2}
        assert repaired == []

    def test_a_string_field_is_never_touched(self):
        args, repaired = coerce_tool_args(_Schema, {"title": '["not", "a", "list"]'})
        assert args["title"] == '["not", "a", "list"]'
        assert repaired == []

    def test_a_multi_key_object_is_not_a_wrapper(self):
        payload = {"item": ["a"], "other": 1}
        args, repaired = coerce_tool_args(_Schema, {"title": "t", "tags": payload})
        assert args["tags"] == payload
        assert repaired == []


class TestPassthrough:
    def test_correct_arguments_are_returned_unchanged(self):
        original = {"title": "t", "tags": ["a", "b"]}
        args, repaired = coerce_tool_args(_Schema, original)
        assert args is original
        assert repaired == []

    def test_explicit_none_is_left_alone(self):
        args, repaired = coerce_tool_args(_Schema, {"title": "t", "tags": None})
        assert args["tags"] is None
        assert repaired == []

    def test_a_tool_without_a_schema_is_a_no_op(self):
        original = {"tags": '["a"]'}
        args, repaired = coerce_tool_args(None, original)
        assert args is original
        assert repaired == []

    def test_non_dict_arguments_do_not_raise(self):
        args, repaired = coerce_tool_args(_Schema, "not a mapping")
        assert args == {}
        assert repaired == []
