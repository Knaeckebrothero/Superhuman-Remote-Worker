"""Small diagnostics operations with explicit renderers and workspace access."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, overload

from fastapi import HTTPException


class EmailPreviewRenderer(Protocol):
    def _build_system_notification_html(self, **kwargs: Any) -> str: ...
    def _build_agent_message_html(self, **kwargs: Any) -> str: ...
    def render_notification_html(self, body: str, cockpit_link: str) -> str: ...


class DiagnosticWorkspace(Protocol):
    @property
    def base_path(self) -> Path: ...
    @property
    def is_available(self) -> bool: ...


class EnvironmentReader(Protocol):
    @overload
    def __call__(self, name: str, default: str) -> str: ...

    @overload
    def __call__(self, name: str, default: None = None) -> str | None: ...


@dataclass(frozen=True)
class DiagnosticDependencies:
    workspace: DiagnosticWorkspace
    email_renderer: EmailPreviewRenderer
    getenv: EnvironmentReader


async def debug_email_index(*, dependencies: DiagnosticDependencies) -> str:
    """Preserve the existing diagnostic output and feature gate."""
    if dependencies.getenv("EMAIL_PREVIEW_ENABLED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(status_code=404)
    links = "".join(
        f'<li><a href="/debug/emails/{n}">{n}</a></li>'
        for n in ("system", "agent", "permission")
    )
    return f"<!DOCTYPE html><html><body><h1>Email previews</h1><ul>{links}</ul></body></html>"


async def debug_email_preview(
    name: str, *, dependencies: DiagnosticDependencies
) -> str:
    """Preserve the existing diagnostic output and feature gate."""
    if dependencies.getenv("EMAIL_PREVIEW_ENABLED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(status_code=404)
    link = "https://cockpit.example/preview"
    if name == "system":
        return dependencies.email_renderer._build_system_notification_html(
            to_name="Ada Lovelace",
            body_md=(
                "Your automation **Nightly ledger sync** was disabled after "
                "3 consecutive failures.\n\n"
                "The last run failed in `reconcile.py` with:\n\n"
                "```\nValueError: unbalanced ledger (delta 4.20)\n```\n\n"
                "Re-enable it from [Automations](https://cockpit.example/automations) "
                "once the job is fixed."
            ),
            cockpit_link=link,
        )
    if name == "agent":
        # The fixture deliberately exercises the markdown subset (headings,
        # emphasis, code, lists, tables, quotes) — this page is the only place
        # the rendering is looked at with human eyes before it reaches an inbox.
        return dependencies.email_renderer._build_agent_message_html(
            message_md=(
                "**Job `5706c684`** (`developer`) has completed and is "
                "awaiting review.\n\n"
                "## Summary\n\n"
                "Migrated the billing schema to the new ledger format. The "
                "`amount_cents` column is now `NOT NULL`, and every historical "
                "row was backfilled from `legacy_amount`.\n\n"
                "> One caveat: 14 rows in 2019 had no legacy value and were "
                "left at zero.\n\n"
                "**Deliverables:**\n\n"
                "- `migrations/app/0163_billing_ledger.sql`\n"
                "- `orchestrator/services/billing.py`\n"
                "  - new `LedgerEntry` dataclass\n"
                "  - `reconcile()` now returns a diff\n\n"
                "| Check | Result |\n"
                "| --- | --- |\n"
                "| `pytest tests/test_billing.py` | 42 passed |\n"
                "| `ruff check` | clean |\n\n"
                "Full diff: https://git.example/srw/compare/main...ledger\n\n"
                "---\n\n"
                "*Confidence: 92%*"
            ),
            job_description="Migrate the billing schema to the new ledger format",
            config_name="developer",
            phase_str="phase 2",
            cockpit_link=link,
            reply_to_addr="reply@example.com",
        )
    if name == "permission":
        # A permission gate is a feed notification now; its mail is the
        # notification template with the magic links as labeled bare URLs.
        body = (
            "**run_command** is waiting for your approval in session "
            "**Nightly build** (requested 4 min ago).\n\n"
            '```\n{"command": "rm -rf ./build"}\n```\n\n'
            f"Approve: {link}/magic/approve/preview-approve\n"
            f"Deny: {link}/magic/approve/preview-deny\n\n"
            "These links need no sign-in and expire in 30 minutes. "
            f"Session: {link}/sessions/preview"
        )
        return dependencies.email_renderer.render_notification_html(
            body, f"{link}/inbox?n=preview"
        )
    raise HTTPException(status_code=404, detail="unknown email preview")


async def workspace_status(*, dependencies: DiagnosticDependencies) -> dict[str, Any]:
    """Preserve the existing diagnostic output and feature gate."""

    base_path = dependencies.workspace.base_path
    is_available = dependencies.workspace.is_available

    # List top-level entries (workspace is a flat directory now, no job_* subdirs)
    entries = []
    if is_available:
        try:
            entries = [d.name for d in base_path.iterdir()][:20]
        except Exception:
            pass

    return {
        "configured_path": str(base_path),
        "resolved_path": str(base_path.resolve()) if base_path.exists() else None,
        "is_available": is_available,
        "env_workspace_path": dependencies.getenv("WORKSPACE_PATH"),
        "entries": entries,
    }
