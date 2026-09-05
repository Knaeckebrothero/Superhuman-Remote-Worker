"""Email datasource (IMAP/SMTP managed connector) — orchestrator-side helpers.

Pure validation logic for ``type='email'`` datasources, plus the synchronous
connectivity probe behind ``POST /api/datasources/{id}/test``. No FastAPI/DB
imports — unit-testable without ``orchestrator.main``
(tests/test_email_datasource_validation.py).

Tier→tool selection is NOT here: the tier-keyed ``EMAIL_TIER_TOOLS`` map and
``email_effective_access()`` live in ``src/core/datasource_setup.py`` (the
shared source of truth for both trust boundaries); this module imports them
so validation and dispatch-config normalization cannot drift from selection.

Spec: knowledge-base/knowledge/features/email_datasource.md ("Trust and permission model").
"""

from __future__ import annotations

import imaplib
import smtplib
from typing import Any

from src.shared.datasource_policy import (
    EMAIL_TIER_ORDER,
    email_effective_access,
)

DEFAULT_EMAIL_ACCESS = "draft"
DEFAULT_DRAFTS_FOLDER = "Drafts"

_ALLOWED_CONFIG_KEYS = {
    "access",
    "folders",
    "drafts_folder",
    "from_address",
    "recipient_allowlist",
    "unattended_send",
}

_ALLOWED_CREDENTIAL_KEYS = {"backend", "username", "password", "imap", "smtp"}

_VALID_SECURITY = ("ssl", "starttls")

_DEFAULT_PORTS = {
    "imap": {"ssl": 993, "starttls": 143},
    "smtp": {"ssl": 465, "starttls": 587},
}


def _str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise ValueError(f"email config {field} must be a list of non-empty strings")
    return [v.strip() for v in value]


def validate_email_config(
    config: dict[str, Any] | None, owner_has_send_grant: bool
) -> dict[str, Any]:
    """Validate + normalize the non-secret config of an email datasource.

    Returns the normalized config (defaults applied, every key present).
    Raises ``ValueError`` with a user-facing message — callers map it to 400.
    """
    raw = dict(config or {})
    unknown = sorted(set(raw) - _ALLOWED_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"Unknown email config field(s): {', '.join(unknown)}")

    access = raw.get("access", DEFAULT_EMAIL_ACCESS)
    if access not in EMAIL_TIER_ORDER:
        raise ValueError(
            f"email config access must be one of: {', '.join(EMAIL_TIER_ORDER)}"
        )

    folders = _str_list(raw.get("folders"), "folders")
    recipient_allowlist = _str_list(
        raw.get("recipient_allowlist"), "recipient_allowlist"
    )

    drafts_folder = raw.get("drafts_folder", DEFAULT_DRAFTS_FOLDER)
    if not isinstance(drafts_folder, str) or not drafts_folder.strip():
        raise ValueError("email config drafts_folder must be a non-empty string")

    from_address = raw.get("from_address")
    if from_address is not None:
        if not isinstance(from_address, str) or not from_address.strip():
            raise ValueError("email config from_address must be a non-empty string")
        from_address = from_address.strip()

    unattended_send = raw.get("unattended_send", False)
    if not isinstance(unattended_send, bool):
        raise ValueError("email config unattended_send must be a boolean")

    if access == "send" and not folders:
        raise ValueError(
            "access='send' requires a non-empty folder allowlist (folders): "
            "the allowlist is the share boundary for a mailbox the agent can "
            "send from"
        )
    if unattended_send and not owner_has_send_grant:
        raise ValueError(
            "unattended_send requires the 'email_autonomous_send' capability "
            "grant; without it email_send pauses for human approval"
        )

    return {
        "access": access,
        "folders": folders,
        "drafts_folder": drafts_folder.strip(),
        "from_address": from_address,
        "recipient_allowlist": recipient_allowlist,
        "unattended_send": unattended_send,
    }


def _validate_server_block(block: Any, kind: str) -> dict[str, Any]:
    if not isinstance(block, dict):
        raise ValueError(
            f"email credentials {kind} must be an object with host/port/security"
        )
    unknown = sorted(set(block) - {"host", "port", "security"})
    if unknown:
        raise ValueError(
            f"Unknown email credentials {kind} field(s): {', '.join(unknown)}"
        )
    host = block.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"email credentials {kind}.host is required")
    security = block.get("security", "ssl")
    if security not in _VALID_SECURITY:
        raise ValueError(
            f"email credentials {kind}.security must be one of: "
            f"{', '.join(_VALID_SECURITY)}"
        )
    port = block.get("port", _DEFAULT_PORTS[kind][security])
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ValueError(f"email credentials {kind}.port must be a port number")
    return {"host": host.strip(), "port": port, "security": security}


def validate_email_credentials(
    credentials: dict[str, Any] | None, *, access: str
) -> dict[str, Any]:
    """Validate + normalize email credentials BEFORE encryption at rest.

    The smtp block is required only when ``access='send'`` (draft tier files
    drafts over IMAP APPEND, no SMTP). Raises ``ValueError`` with a
    user-facing message; never includes the password in it.
    """
    creds = dict(credentials or {})
    if not creds:
        raise ValueError(
            "email credentials are required: username, password (app password) "
            "and an imap server block"
        )
    unknown = sorted(set(creds) - _ALLOWED_CREDENTIAL_KEYS)
    if unknown:
        raise ValueError(f"Unknown email credentials field(s): {', '.join(unknown)}")

    backend = creds.get("backend", "imap_smtp")
    if backend != "imap_smtp":
        raise ValueError(
            "email credentials backend must be 'imap_smtp' (the only v1 backend)"
        )
    username = creds.get("username")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("email credentials username is required")
    password = creds.get("password")
    if not isinstance(password, str) or not password:
        raise ValueError("email credentials password is required")

    smtp = creds.get("smtp")
    if access == "send" and smtp is None:
        raise ValueError(
            "access='send' requires an smtp block in credentials "
            "(host/port/security for the submission server)"
        )

    normalized: dict[str, Any] = {
        "backend": "imap_smtp",
        "username": username.strip(),
        "password": password,
        "imap": _validate_server_block(creds.get("imap"), "imap"),
    }
    if smtp is not None:
        normalized["smtp"] = _validate_server_block(smtp, "smtp")
    return normalized


def email_dispatch_config(
    config: dict[str, Any] | None,
    *,
    project_read_only: bool,
    owner_can_autonomous_send: bool,
) -> dict[str, Any]:
    """Dispatch-time copy of an email config for the agent payload.

    - ``access`` floored to 'read' by a read-only project link (the email
      analogue of the managed-connector credential withholding, from which
      email is exempt),
    - ``unattended_send`` re-checked against the owner's CURRENT
      ``email_autonomous_send`` grant — fail closed against a revocation
      after create/update-time validation.
    """
    raw = dict(config or {})
    folders = [v.strip() for v in raw.get("folders") or [] if isinstance(v, str)]
    allowlist = [
        v.strip() for v in raw.get("recipient_allowlist") or [] if isinstance(v, str)
    ]
    drafts_folder = raw.get("drafts_folder")
    if not isinstance(drafts_folder, str) or not drafts_folder.strip():
        drafts_folder = DEFAULT_DRAFTS_FOLDER
    from_address = raw.get("from_address")
    if not isinstance(from_address, str) or not from_address.strip():
        from_address = None
    access = email_effective_access(
        {"config": raw, "project_read_only": project_read_only}
    )
    return {
        "access": access,
        "folders": [v for v in folders if v],
        "drafts_folder": drafts_folder.strip(),
        "from_address": from_address,
        "recipient_allowlist": [v for v in allowlist if v],
        "unattended_send": bool(raw.get("unattended_send"))
        and owner_can_autonomous_send,
    }


def _quote_mailbox(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def probe_email_connection(
    credentials: dict[str, Any] | None,
    config: dict[str, Any] | None,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Synchronous IMAP (+ SMTP for the send tier) connectivity probe.

    Blocking by design — the /test endpoint runs it via ``asyncio.to_thread``
    under an outer ``asyncio.wait_for``. Returns the endpoint's
    ``{'status', 'message'}`` shape with actionable messages (bad auth vs
    missing folder vs connect failure) and never echoes the password.
    """
    creds = credentials or {}
    conf = config or {}
    access = conf.get("access", DEFAULT_EMAIL_ACCESS)
    folders = [f.strip() for f in conf.get("folders") or [] if isinstance(f, str)]
    folders = [f for f in folders if f]

    if access == "send" and not folders:
        return {
            "status": "error",
            "message": "access='send' requires a non-empty folder allowlist (folders)",
        }

    username = creds.get("username")
    password = creds.get("password")
    imap_cfg = creds.get("imap") or {}
    imap_host = imap_cfg.get("host")
    if not (username and password and imap_host):
        return {
            "status": "error",
            "message": (
                "Email credentials are incomplete: username, password, and "
                "imap.host are required"
            ),
        }

    imap_security = imap_cfg.get("security", "ssl")
    imap_port = imap_cfg.get("port") or _DEFAULT_PORTS["imap"].get(imap_security, 993)

    try:
        if imap_security == "starttls":
            imap = imaplib.IMAP4(imap_host, imap_port, timeout=timeout)
            imap.starttls()
        else:
            imap = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=timeout)
    except (OSError, imaplib.IMAP4.error) as e:
        return {
            "status": "error",
            "message": f"IMAP connection to {imap_host}:{imap_port} failed: {e}",
        }

    missing: list[str] = []
    try:
        try:
            imap.login(username, password)
        except imaplib.IMAP4.error as e:
            return {
                "status": "error",
                "message": f"IMAP login failed (check username/app password): {e}",
            }
        for folder in folders:
            try:
                status, _ = imap.status(_quote_mailbox(folder), "(MESSAGES)")
            except imaplib.IMAP4.error:
                status = "NO"
            if status != "OK":
                missing.append(folder)
    except OSError as e:
        return {
            "status": "error",
            "message": f"IMAP connection to {imap_host}:{imap_port} failed: {e}",
        }
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    if missing:
        return {
            "status": "error",
            "message": f"IMAP folder(s) not found: {', '.join(missing)}",
        }

    smtp_ok = False
    if access == "send":
        smtp_cfg = creds.get("smtp") or {}
        smtp_host = smtp_cfg.get("host")
        if not smtp_host:
            return {
                "status": "error",
                "message": "access='send' requires an smtp block in credentials",
            }
        smtp_security = smtp_cfg.get("security", "ssl")
        smtp_port = smtp_cfg.get("port") or _DEFAULT_PORTS["smtp"].get(
            smtp_security, 465
        )
        try:
            if smtp_security == "starttls":
                smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
                smtp.starttls()
            else:
                smtp = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout)
            try:
                smtp.ehlo()
                smtp.login(username, password)
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except smtplib.SMTPAuthenticationError as e:
            return {
                "status": "error",
                "message": f"SMTP auth failed (check username/app password): {e}",
            }
        except (OSError, smtplib.SMTPException) as e:
            return {
                "status": "error",
                "message": f"SMTP connection to {smtp_host}:{smtp_port} failed: {e}",
            }
        smtp_ok = True

    message = f"Connected to IMAP mailbox; verified {len(folders)} folder(s)"
    if smtp_ok:
        message += "; SMTP auth ok"
    return {"status": "ok", "message": message}
