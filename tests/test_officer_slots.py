"""Officer slot roster tests — S5b of knowledge-base/knowledge/features/centurion.md.

Pure-module coverage for services/officer_slots.py: provision-time
validation, funnel admission (flat cap vs roster, auto-select, stamping,
actionable 409 messages), and the sitrep capacity line. The funnel wiring
itself (advisory lock + GROUP BY + deep-merge order) is k3d-smoke
territory, same split as the rest of the officer suite.
"""

import pytest

from services.officer_slots import (
    SlotAdmissionError,
    admit,
    capacity_lines,
    validate_slots_spec,
)

ROSTER_META = {
    "enabled": True,
    "slots": {
        "line": {"count": 2, "model": "MiniMax-M3", "backend": "sandbox"},
        "heavy": {"count": 1, "model": "gpt-5.6-sol", "backend": "vm"},
    },
}


class TestValidateSlotsSpec:
    def test_valid_spec_normalizes(self):
        cleaned = validate_slots_spec(
            {
                "line": {"count": "2", "model": " MiniMax-M3 ", "backend": "sandbox"},
                "heavy": {"count": 1},
            }
        )
        assert cleaned["line"] == {
            "count": 2,
            "model": "MiniMax-M3",
            "backend": "sandbox",
        }
        assert cleaned["heavy"] == {"count": 1}

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            {},
            [],
            {"line": {"count": 1, "nope": True}},
            {"line": {"model": "x"}},  # missing count
            {"line": {"count": -1}},
            {"line": {"count": 99}},
            {"line": {"count": 1, "backend": "warp-drive"}},
            {"line": {"count": 1, "model": ""}},
            {"BAD NAME": {"count": 1}},
            {"unslotted": {"count": 1}},  # reserved
        ],
    )
    def test_rejects_bad_specs(self, bad):
        with pytest.raises(ValueError):
            validate_slots_spec(bad)

    def test_rejects_too_many_slots(self):
        with pytest.raises(ValueError):
            validate_slots_spec({f"s{i}": {"count": 1} for i in range(9)})


class TestAdmitFlatCap:
    def test_admits_under_cap_with_no_patch(self):
        name, patch = admit({"max_concurrent_workers": 2}, None, {None: 1})
        assert name is None
        assert patch == {}

    def test_rejects_at_cap_counting_all_stamps(self):
        with pytest.raises(SlotAdmissionError, match="2/2"):
            admit({"max_concurrent_workers": 2}, None, {None: 1, "old": 1})

    def test_requested_slot_ignored_without_roster(self):
        name, patch = admit({"max_concurrent_workers": 3}, "heavy", {})
        assert name is None and patch == {}


class TestAdmitRoster:
    def test_named_slot_admits_and_stamps(self):
        name, patch = admit(ROSTER_META, "heavy", {"line": 2})
        assert name == "heavy"
        assert patch == {
            "llm": {"model": "gpt-5.6-sol"},
            "workspace": {"backend": "vm"},
        }

    def test_single_slot_roster_auto_selects(self):
        meta = {"slots": {"line": {"count": 2, "model": "MiniMax-M3"}}}
        name, patch = admit(meta, None, {})
        assert name == "line"
        assert patch == {"llm": {"model": "MiniMax-M3"}}
        assert "workspace" not in patch  # backend unset → job default stands

    def test_multi_slot_roster_requires_a_name(self):
        with pytest.raises(SlotAdmissionError, match="name one via the slot"):
            admit(ROSTER_META, None, {})

    def test_unknown_slot_lists_roster(self):
        with pytest.raises(SlotAdmissionError, match="Unknown slot 'cavalry'"):
            admit(ROSTER_META, "cavalry", {})

    def test_full_slot_rejects_with_alternatives(self):
        with pytest.raises(SlotAdmissionError, match="Slot 'heavy' is full: 1/1"):
            admit(ROSTER_META, "heavy", {"heavy": 1})

    def test_slot_counts_are_independent(self):
        # heavy full does not block line.
        name, _ = admit(ROSTER_META, "line", {"heavy": 1, "line": 1})
        assert name == "line"

    def test_zero_count_slot_is_always_full(self):
        meta = {"slots": {"reserve": {"count": 0}}}
        with pytest.raises(SlotAdmissionError, match="Slot 'reserve' is full: 0/0"):
            admit(meta, "reserve", {})

    def test_malformed_roster_degrades_to_flat_cap(self):
        meta = {"slots": {"line": "not-a-dict"}, "max_concurrent_workers": 1}
        name, patch = admit(meta, None, {})
        assert name is None and patch == {}


class TestCapacityLines:
    def test_flat_line(self):
        assert (
            capacity_lines({"max_concurrent_workers": 3}, {None: 1})
            == "Capacity: 1/3 worker slots in use."
        )

    def test_roster_line_with_stray(self):
        line = capacity_lines(ROSTER_META, {"line": 1, None: 1})
        assert line == (
            "Capacity: heavy 0/1, line 1/2 worker slots in use (+1 unslotted)."
        )
