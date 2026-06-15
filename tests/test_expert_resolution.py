"""Unit tests for pure expert resolution/validation logic (Slice 1)."""
from src.core.loader import deep_merge


def test_deep_merge_does_not_alias_nested_base():
    """A base-only nested structure must be copied, not aliased, into the result
    (decision 25: fix base.copy() shallow aliasing before user fragments flow through)."""
    base = {"workspace": {"structure": ["archive/", "output/"]}}
    override = {"display_name": "Custom"}
    result = deep_merge(base, override)
    # Mutating the result's nested list must not touch the base.
    result["workspace"]["structure"].append("evil/")
    assert base["workspace"]["structure"] == ["archive/", "output/"]
