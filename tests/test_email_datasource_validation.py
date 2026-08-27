"""Unit tests for the email-datasource validation service (P0 plumbing).

Covers ``orchestrator/services/email_datasource.py`` — pure config/credential
validation and dispatch-time config normalization — without importing
``orchestrator.main``. Tier→tool selection itself lives in
``src/core/datasource_setup.py``; a contract test here pins the shared
tier→tool-name map both trust boundaries depend on.

Spec: knowledge-base/knowledge/features/email_datasource.md.
"""

import pytest

from orchestrator.services.email_datasource import (
    email_dispatch_config,
    probe_email_connection,
    validate_email_config,
    validate_email_credentials,
)
from src.core.datasource_setup import EMAIL_TIER_ORDER, EMAIL_TIER_TOOLS


def _creds(access_smtp: bool = False) -> dict:
    creds = {
        "backend": "imap_smtp",
        "username": "user@example.com",
        "password": "app-password",
        "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
    }
    if access_smtp:
        creds["smtp"] = {"host": "smtp.example.com", "port": 465, "security": "ssl"}
    return creds


class TestTierToolContract:
    """Pin the shared tier→tool-name contract (cumulative ladder)."""

    def test_tier_order(self):
        assert EMAIL_TIER_ORDER == ("read", "read_write", "draft", "send")

    def test_exact_tool_names_per_tier(self):
        read = ["email_list_folders", "email_list", "email_search", "email_read"]
        assert EMAIL_TIER_TOOLS["read"] == read
        assert EMAIL_TIER_TOOLS["read_write"] == read + ["email_move", "email_flag"]
        assert EMAIL_TIER_TOOLS["draft"] == EMAIL_TIER_TOOLS["read_write"] + [
            "email_draft"
        ]
        assert EMAIL_TIER_TOOLS["send"] == EMAIL_TIER_TOOLS["draft"] + ["email_send"]

    def test_tiers_are_cumulative(self):
        previous: list[str] = []
        for tier in EMAIL_TIER_ORDER:
            tools = EMAIL_TIER_TOOLS[tier]
            assert tools[: len(previous)] == previous, tier
            previous = tools


class TestValidateEmailConfig:
    def test_empty_config_gets_defaults(self):
        normalized = validate_email_config(None, False)
        assert normalized == {
            "access": "draft",
            "folders": [],
            "drafts_folder": "Drafts",
            "from_address": None,
            "recipient_allowlist": [],
            "unattended_send": False,
        }

    @pytest.mark.parametrize("access", ["read", "read_write", "draft"])
    def test_happy_path_non_send_tiers(self, access):
        normalized = validate_email_config({"access": access}, False)
        assert normalized["access"] == access
        assert normalized["folders"] == []

    def test_happy_path_send_tier(self):
        normalized = validate_email_config(
            {
                "access": "send",
                "folders": ["AI", "AI/Processed"],
                "drafts_folder": "Entwürfe",
                "from_address": "user@example.com",
                "recipient_allowlist": ["boss@example.com", "example.org"],
            },
            False,
        )
        assert normalized["access"] == "send"
        assert normalized["folders"] == ["AI", "AI/Processed"]
        assert normalized["drafts_folder"] == "Entwürfe"
        assert normalized["from_address"] == "user@example.com"
        assert normalized["recipient_allowlist"] == ["boss@example.com", "example.org"]

    def test_send_with_empty_folders_rejected(self):
        with pytest.raises(ValueError, match="folder allowlist"):
            validate_email_config({"access": "send"}, False)

    def test_bad_access_enum_rejected(self):
        with pytest.raises(ValueError, match="access must be one of"):
            validate_email_config({"access": "admin"}, False)

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValueError, match="Unknown email config field"):
            validate_email_config({"acess": "read"}, False)

    def test_folders_must_be_non_empty_strings(self):
        with pytest.raises(ValueError, match="folders"):
            validate_email_config({"folders": ["AI", ""]}, False)
        with pytest.raises(ValueError, match="folders"):
            validate_email_config({"folders": "AI"}, False)
        with pytest.raises(ValueError, match="folders"):
            validate_email_config({"folders": [1]}, False)

    def test_recipient_allowlist_must_be_strings(self):
        with pytest.raises(ValueError, match="recipient_allowlist"):
            validate_email_config({"recipient_allowlist": [None]}, False)

    def test_folder_values_are_stripped(self):
        normalized = validate_email_config({"folders": ["  AI  "]}, False)
        assert normalized["folders"] == ["AI"]

    def test_drafts_folder_must_be_non_empty(self):
        with pytest.raises(ValueError, match="drafts_folder"):
            validate_email_config({"drafts_folder": "  "}, False)

    def test_from_address_must_be_non_empty_when_set(self):
        with pytest.raises(ValueError, match="from_address"):
            validate_email_config({"from_address": ""}, False)

    def test_unattended_send_must_be_bool(self):
        with pytest.raises(ValueError, match="unattended_send"):
            validate_email_config({"unattended_send": "yes"}, False)

    def test_unattended_send_without_grant_rejected(self):
        with pytest.raises(ValueError, match="email_autonomous_send"):
            validate_email_config(
                {"access": "send", "folders": ["AI"], "unattended_send": True},
                False,
            )

    def test_unattended_send_with_grant_accepted(self):
        normalized = validate_email_config(
            {"access": "send", "folders": ["AI"], "unattended_send": True},
            True,
        )
        assert normalized["unattended_send"] is True


class TestValidateEmailCredentials:
    def test_happy_path_draft_tier_no_smtp(self):
        normalized = validate_email_credentials(_creds(), access="draft")
        assert normalized["backend"] == "imap_smtp"
        assert normalized["imap"] == {
            "host": "imap.example.com",
            "port": 993,
            "security": "ssl",
        }
        assert "smtp" not in normalized

    def test_send_tier_requires_smtp_block(self):
        with pytest.raises(ValueError, match="smtp block"):
            validate_email_credentials(_creds(), access="send")

    def test_send_tier_with_smtp_ok(self):
        normalized = validate_email_credentials(_creds(access_smtp=True), access="send")
        assert normalized["smtp"]["host"] == "smtp.example.com"

    def test_optional_smtp_block_validated_on_non_send_tiers(self):
        creds = _creds()
        creds["smtp"] = {"host": ""}
        with pytest.raises(ValueError, match="smtp.host"):
            validate_email_credentials(creds, access="read")

    def test_empty_credentials_rejected(self):
        with pytest.raises(ValueError, match="credentials are required"):
            validate_email_credentials(None, access="read")
        with pytest.raises(ValueError, match="credentials are required"):
            validate_email_credentials({}, access="read")

    def test_unknown_backend_rejected(self):
        creds = _creds()
        creds["backend"] = "gmail_api"
        with pytest.raises(ValueError, match="imap_smtp"):
            validate_email_credentials(creds, access="read")

    def test_backend_defaults_to_imap_smtp(self):
        creds = _creds()
        del creds["backend"]
        normalized = validate_email_credentials(creds, access="read")
        assert normalized["backend"] == "imap_smtp"

    def test_missing_username_password_rejected(self):
        creds = _creds()
        creds["username"] = " "
        with pytest.raises(ValueError, match="username"):
            validate_email_credentials(creds, access="read")
        creds = _creds()
        del creds["password"]
        with pytest.raises(ValueError, match="password"):
            validate_email_credentials(creds, access="read")

    def test_missing_imap_block_rejected(self):
        creds = _creds()
        del creds["imap"]
        with pytest.raises(ValueError, match="imap"):
            validate_email_credentials(creds, access="read")

    def test_port_defaults_follow_security(self):
        creds = _creds()
        creds["imap"] = {"host": "imap.example.com", "security": "starttls"}
        normalized = validate_email_credentials(creds, access="read")
        assert normalized["imap"]["port"] == 143

        creds = _creds(access_smtp=True)
        creds["smtp"] = {"host": "smtp.example.com", "security": "starttls"}
        normalized = validate_email_credentials(creds, access="send")
        assert normalized["smtp"]["port"] == 587

    def test_bad_security_rejected(self):
        creds = _creds()
        creds["imap"]["security"] = "plain"
        with pytest.raises(ValueError, match="security"):
            validate_email_credentials(creds, access="read")

    def test_bad_port_rejected(self):
        creds = _creds()
        creds["imap"]["port"] = 0
        with pytest.raises(ValueError, match="port"):
            validate_email_credentials(creds, access="read")
        creds["imap"]["port"] = True
        with pytest.raises(ValueError, match="port"):
            validate_email_credentials(creds, access="read")

    def test_unknown_keys_rejected(self):
        creds = _creds()
        creds["token"] = "x"
        with pytest.raises(ValueError, match="Unknown email credentials field"):
            validate_email_credentials(creds, access="read")
        creds = _creds()
        creds["imap"]["hostname"] = "x"
        with pytest.raises(ValueError, match="Unknown email credentials imap field"):
            validate_email_credentials(creds, access="read")

    def test_error_messages_never_contain_password(self):
        creds = _creds()
        creds["imap"]["port"] = -1
        with pytest.raises(ValueError) as exc_info:
            validate_email_credentials(creds, access="read")
        assert "app-password" not in str(exc_info.value)


class TestEmailDispatchConfig:
    def test_project_read_only_floors_access_to_read(self):
        conf = email_dispatch_config(
            {"access": "send", "folders": ["AI"]},
            project_read_only=True,
            owner_can_autonomous_send=True,
        )
        assert conf["access"] == "read"
        # The clamp is expressed via the access floor ONLY — folders and the
        # rest of the config survive (credentials are never emptied either).
        assert conf["folders"] == ["AI"]

    def test_access_passes_through_when_not_read_only(self):
        conf = email_dispatch_config(
            {"access": "send", "folders": ["AI"]},
            project_read_only=False,
            owner_can_autonomous_send=False,
        )
        assert conf["access"] == "send"

    def test_unknown_access_fails_closed_to_read(self):
        conf = email_dispatch_config(
            {"access": "root"},
            project_read_only=False,
            owner_can_autonomous_send=False,
        )
        assert conf["access"] == "read"

    def test_unattended_send_forced_off_without_owner_grant(self):
        conf = email_dispatch_config(
            {"access": "send", "folders": ["AI"], "unattended_send": True},
            project_read_only=False,
            owner_can_autonomous_send=False,
        )
        assert conf["unattended_send"] is False

    def test_unattended_send_kept_with_owner_grant(self):
        conf = email_dispatch_config(
            {"access": "send", "folders": ["AI"], "unattended_send": True},
            project_read_only=False,
            owner_can_autonomous_send=True,
        )
        assert conf["unattended_send"] is True

    def test_defaults_applied_to_empty_config(self):
        conf = email_dispatch_config(
            None, project_read_only=False, owner_can_autonomous_send=False
        )
        assert conf == {
            "access": "draft",
            "folders": [],
            "drafts_folder": "Drafts",
            "from_address": None,
            "recipient_allowlist": [],
            "unattended_send": False,
        }

    def test_tampered_values_are_tolerated(self):
        # Dispatch normalization must not raise on a DB-tampered row.
        conf = email_dispatch_config(
            {"folders": ["AI", 3, None], "drafts_folder": 7, "from_address": 1},
            project_read_only=False,
            owner_can_autonomous_send=False,
        )
        assert conf["folders"] == ["AI"]
        assert conf["drafts_folder"] == "Drafts"
        assert conf["from_address"] is None


class TestEmailConnectionProbeGuards:
    """Pure (no-network) guard paths of the /test probe."""

    def test_send_with_empty_folders_short_circuits(self):
        result = probe_email_connection(_creds(access_smtp=True), {"access": "send"})
        assert result["status"] == "error"
        assert "folder allowlist" in result["message"]

    def test_incomplete_credentials_short_circuit(self):
        result = probe_email_connection({"username": "u"}, {"access": "read"})
        assert result["status"] == "error"
        assert "incomplete" in result["message"]
        assert "app-password" not in result["message"]
