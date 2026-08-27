"""The two EmailService bodies render through the shared layout, escaped."""

from services import brand
from services.email import EmailService


def test_system_notification_escapes_recipient_name() -> None:
    svc = EmailService()
    html = svc._build_system_notification_html(
        to_name="<script>alert(1)</script>",
        body_md="hello",
        cockpit_link="https://cockpit.test/",
    )
    assert "<script>" not in html
    assert brand.TRAVERTINE["panel-bg"] in html
    assert "#1e1e2e" not in html  # Catppuccin is gone


def test_agent_message_escapes_job_description_and_config_name() -> None:
    svc = EmailService()
    html = svc._build_agent_message_html(
        message_md="body text",
        job_description="<img src=x onerror=alert(1)>",
        config_name="<b>agent</b>",
        phase_str="phase 1",
        cockpit_link="https://cockpit.test/",
        reply_to_addr=None,
    )
    assert "<img src=x" not in html
    assert "<b>agent</b>" not in html
    assert "#1e1e2e" not in html
