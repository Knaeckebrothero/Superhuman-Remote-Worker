"""Async email service for authentication emails (verification, password reset).

Reads SMTP configuration from environment variables. Designed for use with
the Proton Mail Bridge on the K3s cluster or any standard SMTP relay.

Environment variables:
    SMTP_HOST              — SMTP server hostname (required for production auth)
    SMTP_PORT              — SMTP port (default: 587, use 1025 for Proton Bridge)
    SMTP_USER              — SMTP username (bridge credentials)
    SMTP_PASSWORD           — SMTP password (bridge credentials)
    SMTP_USE_TLS           — Use STARTTLS (default: true)
    SMTP_TRUST_SELF_SIGNED — Accept self-signed certs (default: false, true for Proton Bridge)
    SMTP_FROM              — Sender email address (default: noreply@h4ll.app)
"""

import logging
import os
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

logger = logging.getLogger(__name__)


class EmailService:
    """Async SMTP email sender for auth-related emails."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
        self.trust_self_signed = os.getenv("SMTP_TRUST_SELF_SIGNED", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        self.from_address = os.getenv("SMTP_FROM", "noreply@h4ll.app")

    @property
    def is_configured(self) -> bool:
        """Whether SMTP is configured (host is set)."""
        return bool(self.host)

    def _get_tls_context(self) -> ssl.SSLContext | None:
        """Build TLS context, optionally trusting self-signed certs (Proton Bridge)."""
        if not self.use_tls:
            return None
        ctx = ssl.create_default_context()
        if self.trust_self_signed:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _send(self, to: str, subject: str, body_text: str, body_html: str) -> bool:
        """Send an email via SMTP.

        Returns True on success, False on failure (logs the error).
        """
        if not self.is_configured:
            logger.warning("SMTP not configured — cannot send email")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        try:
            tls_context = self._get_tls_context()
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.user or None,
                password=self.password or None,
                start_tls=self.use_tls,
                tls_context=tls_context,
            )
            logger.info(f"Email sent to {to}: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to}: {e}")
            return False

    async def send_verification_email(self, to: str, code: str, display_name: str) -> bool:
        """Send an email verification code."""
        subject = f"SRW — Verify your email ({code})"
        body_text = (
            f"Hi {display_name},\n\n"
            f"Your verification code is: {code}\n\n"
            f"Enter this code in the cockpit to complete your registration.\n"
            f"This code expires in 30 minutes.\n\n"
            f"If you didn't create an account, ignore this email."
        )
        body_html = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px;">
  <h2 style="color: #cba6f7; margin-bottom: 8px;">SRW Cockpit</h2>
  <p style="color: #a6adc8; margin-bottom: 24px;">Email Verification</p>
  <p>Hi {display_name},</p>
  <p>Your verification code is:</p>
  <div style="background: #1e1e2e; border: 1px solid #313244; border-radius: 8px; padding: 16px; text-align: center; margin: 24px 0;">
    <span style="font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #cba6f7;">{code}</span>
  </div>
  <p>Enter this code in the cockpit to complete your registration.</p>
  <p style="color: #6c7086; font-size: 13px;">This code expires in 30 minutes. If you didn't create an account, ignore this email.</p>
</div>"""
        return await self._send(to, subject, body_text, body_html)

    async def send_password_reset_email(self, to: str, code: str, display_name: str) -> bool:
        """Send a password reset code."""
        subject = f"SRW — Password reset code ({code})"
        body_text = (
            f"Hi {display_name},\n\n"
            f"Your password reset code is: {code}\n\n"
            f"Enter this code in the cockpit to reset your password.\n"
            f"This code expires in 30 minutes.\n\n"
            f"If you didn't request a password reset, ignore this email."
        )
        body_html = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px;">
  <h2 style="color: #cba6f7; margin-bottom: 8px;">SRW Cockpit</h2>
  <p style="color: #a6adc8; margin-bottom: 24px;">Password Reset</p>
  <p>Hi {display_name},</p>
  <p>Your password reset code is:</p>
  <div style="background: #1e1e2e; border: 1px solid #313244; border-radius: 8px; padding: 16px; text-align: center; margin: 24px 0;">
    <span style="font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #cba6f7;">{code}</span>
  </div>
  <p>Enter this code in the cockpit to set a new password.</p>
  <p style="color: #6c7086; font-size: 13px;">This code expires in 30 minutes. If you didn't request this, ignore this email.</p>
</div>"""
        return await self._send(to, subject, body_text, body_html)


# Module-level singleton
email_service = EmailService()
