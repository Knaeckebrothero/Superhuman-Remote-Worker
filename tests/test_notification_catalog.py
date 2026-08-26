"""The notification catalog is data; these pin the invariants the rest of the
system relies on (unified notification system, D5/D7/D8)."""

import uuid
from datetime import datetime, timezone

import pytest

from services import notification_catalog as cat


class TestCategories:
    def test_every_category_is_well_formed(self):
        for name, spec in cat.CATEGORIES.items():
            assert spec.name == name
            assert spec.severity in cat.SEVERITIES
            assert spec.actions, f"{name} declares no actions"
            for action in spec.actions:
                assert action.label_key.startswith("notifications.actions.")
                assert action.style in cat.ACTION_STYLES

    def test_unknown_category_is_loud(self):
        with pytest.raises(ValueError, match="unknown notification category"):
            cat.category_spec("nope")

    def test_slice_one_classes_keep_mailing(self):
        # Behaviour parity: every class that mails today mails immediately;
        # only `low` is in-app only.
        for severity in ("critical", "high", "normal"):
            channels = {
                s.channel
                for s in cat.steps_for(cat.category_spec("incident"), severity)
            }
            assert "email" in channels
            assert all(s.immediate for s in cat.SEVERITY_CLASSES[severity])
        assert cat.SEVERITY_CLASSES["low"] == ()

    def test_only_critical_bypasses_quiet_hours_by_default(self):
        spec = cat.category_spec("review_queue")
        assert cat.bypasses_quiet_hours(spec, "critical") is True
        assert cat.bypasses_quiet_hours(spec, "high") is False

    def test_severity_override_must_be_valid(self):
        spec = cat.category_spec("review_queue")
        assert cat.normalize_severity(spec, None) == "normal"
        assert cat.normalize_severity(spec, "low") == "low"
        with pytest.raises(ValueError):
            cat.normalize_severity(spec, "urgent")


class TestSpecs:
    def test_action_input_and_name_go_together(self):
        with pytest.raises(ValueError):
            cat.ActionSpec("x", "notifications.actions.x", input="text")
        with pytest.raises(ValueError):
            cat.ActionSpec("x", "notifications.actions.x", input_name="reason")

    def test_action_label_key_namespace_enforced(self):
        with pytest.raises(ValueError):
            cat.ActionSpec("x", "inbox.sudoDetail.approve")

    def test_step_rejects_in_app_and_unknown_channels(self):
        with pytest.raises(ValueError):
            cat.StepSpec("in_app")
        with pytest.raises(ValueError):
            cat.StepSpec("carrier_pigeon")

    def test_serialize_actions_merges_params_into_every_action(self):
        spec = cat.category_spec("vm_upgrade")
        actions = cat.serialize_actions(spec, {"request_id": "r1", "job_id": "j1"})
        assert [a["type"] for a in actions] == [
            "approve_upgrade",
            "resume_without_vm",
            "deny",
        ]
        assert all(a["params"] == {"request_id": "r1", "job_id": "j1"} for a in actions)
        deny = actions[-1]
        assert deny["input"] == "text" and deny["input_name"] == "reason"


class TestIds:
    def test_notification_id_is_deterministic(self):
        a = cat.notification_id("user", "u1", "freeze_notification:cmd")
        b = cat.notification_id("user", "u1", "freeze_notification:cmd")
        assert a == b
        assert a != cat.notification_id("user", "u2", "freeze_notification:cmd")
        assert a != cat.notification_id("user", "u1", "freeze_notification:other")

    def test_notification_id_matches_postgres_uuid_generate_v5(self):
        # the slice-3 backfill mints ids in SQL with uuid_generate_v5(uuid_ns_url(), ...);
        # the Python side must produce byte-identical uuids from the same string.
        key = f"{cat.ID_PREFIX}:user:u1:k"
        assert cat.notification_id("user", "u1", "k") == uuid.uuid5(
            uuid.NAMESPACE_URL, key
        )


class TestSerialization:
    def test_serialize_notification_shape(self):
        now = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)
        row = {
            "id": uuid.UUID(int=1),
            "category": "review_queue",
            "severity": "normal",
            "subject": "s",
            "body": "b",
            "source_kind": "job",
            "source_id": "j1",
            "actions": [{"type": "approve"}],
            "payload": {"job_id": "j1"},
            "created_at": now,
            "seen_at": None,
            "read_at": None,
            "interacted_at": None,
            "resolved_at": now,
            "resolved_by": "user:u1",
            "archived_at": None,
        }
        wire = cat.serialize_notification(row)
        assert wire["id"] == str(uuid.UUID(int=1))
        assert wire["source_ref"] == {"kind": "job", "id": "j1"}
        assert wire["created_at"] == now.isoformat()
        assert wire["seen_at"] is None
        assert wire["resolved_by"] == "user:u1"
        assert cat.cursor_for(row) == f"{now.isoformat()}|{uuid.UUID(int=1)}"

    def test_source_ref_is_null_without_source(self):
        wire = cat.serialize_notification(
            {
                "id": "x",
                "category": "incident",
                "severity": "critical",
                "created_at": None,
            }
        )
        assert wire["source_ref"] is None
        assert wire["actions"] == [] and wire["payload"] == {}


class TestRegistries:
    def test_register_action_rejects_undeclared_action(self):
        with pytest.raises(ValueError, match="declares no action"):
            cat.register_action("review_queue", "teleport")

    def test_register_and_lookup(self):
        saved = dict(cat._ACTION_HANDLERS)
        try:

            @cat.register_action("incident", "open")
            async def _handler(ctx):
                return cat.ActionResult(result={"ok": True})

            assert cat.action_handler("incident", "open") is _handler
            assert cat.action_handler("incident", "nope") is None
        finally:
            cat._ACTION_HANDLERS.clear()
            cat._ACTION_HANDLERS.update(saved)
