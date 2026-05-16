"""Tests for orchestrator.services.email — recipient validation and the
RFC 6761 reserved-TLD blocklist that keeps undeliverable envelopes from
being recorded as `delivery_status='sent'`.

The live SMTP round-trip is out of scope here (dev SMTP bridge handles
that during smoke). These tests exercise the syntactic guard alone.
"""

from unittest.mock import AsyncMock, patch

import pytest

import orchestrator.services.email as email_mod


# =============================================================================
# Section 1 — _is_undeliverable_recipient predicate
# =============================================================================


class TestIsUndeliverableRecipient:
    @pytest.mark.parametrize(
        "addr",
        [
            "alice@example.invalid",
            "bob@host.test",
            "carol@subdomain.example",
            "dave@localhost",
            "ALICE@EXAMPLE.INVALID",  # case-insensitive TLD
            "edge@deep.sub.invalid",  # TLD-only check, not full FQDN
        ],
    )
    def test_blocks_rfc6761_reserved_tlds(self, addr):
        assert email_mod._is_undeliverable_recipient(addr) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "alice@example.com",
            "bob@mail.example.com",
            "carol@anthropic.com",
            "dave@localhost.example.com",  # localhost not the TLD
        ],
    )
    def test_allows_normal_addresses(self, addr):
        assert email_mod._is_undeliverable_recipient(addr) is False

    @pytest.mark.parametrize(
        "addr",
        [
            "",
            "not-an-email",
            "@no-localpart.com",
            "no-domain@",
            "user@.leading-dot.com",
            "user@double..dot.com",
        ],
    )
    def test_rejects_malformed(self, addr):
        assert email_mod._is_undeliverable_recipient(addr) is True


# =============================================================================
# Section 2 — _send refuses undeliverable recipients before opening SMTP
# =============================================================================
#
# The point of the guard is twofold:
#   1. Don't waste an SMTP RTT on something we know will fail.
#   2. More importantly, return False so callers (notably
#      send_permission_pending_email) record delivery_status='failed'
#      instead of 'sent', which is the truthful outcome.
#
# We assert both: aiosmtplib.send is never reached, and the return is False.


class TestSendRefusesUndeliverable:
    def _service_with_smtp(self):
        """Build a service instance with SMTP configured. Real
        construction; only aiosmtplib.send is patched at call time."""
        svc = email_mod.EmailService.__new__(email_mod.EmailService)
        svc.host = "smtp.example.com"
        svc.port = 587
        svc.user = ""
        svc.password = ""
        svc.use_tls = True
        svc.trust_self_signed = False
        svc.from_address = "noreply@example.com"
        svc.cockpit_url = "http://cockpit"
        svc.agent_email = ""
        svc.mail_domain = ""
        return svc

    @pytest.mark.asyncio
    async def test_invalid_recipient_returns_false_without_sending(self):
        svc = self._service_with_smtp()
        send_mock = AsyncMock()
        with patch.object(email_mod.aiosmtplib, "send", send_mock):
            ok = await svc._send(
                to="smoke-tester@example.invalid",
                subject="x",
                body_text="x",
                body_html="<p>x</p>",
            )
        assert ok is False
        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_bad_in_list_aborts_whole_send(self):
        """A single undeliverable in a list aborts the whole envelope —
        we don't half-send."""
        svc = self._service_with_smtp()
        send_mock = AsyncMock()
        with patch.object(email_mod.aiosmtplib, "send", send_mock):
            ok = await svc._send(
                to=["real@example.com", "bogus@x.invalid"],
                subject="x",
                body_text="x",
                body_html="<p>x</p>",
            )
        assert ok is False
        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_recipient_proceeds_to_smtp(self):
        svc = self._service_with_smtp()
        send_mock = AsyncMock(return_value=None)
        with patch.object(email_mod.aiosmtplib, "send", send_mock):
            ok = await svc._send(
                to="user@example.com",
                subject="x",
                body_text="x",
                body_html="<p>x</p>",
            )
        assert ok is True
        send_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_smtp_unconfigured_short_circuits_before_validation(self):
        """Pre-existing behavior: SMTP unconfigured → False, even for
        valid recipients. The new validation must not change this."""
        svc = self._service_with_smtp()
        svc.host = ""  # unconfigured
        send_mock = AsyncMock()
        with patch.object(email_mod.aiosmtplib, "send", send_mock):
            ok = await svc._send(
                to="user@example.com",
                subject="x",
                body_text="x",
                body_html="<p>x</p>",
            )
        assert ok is False
        send_mock.assert_not_awaited()
