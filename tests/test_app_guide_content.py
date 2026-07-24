"""Focused content contracts for the managed SRW product guide.

These checks do not attempt to prove every sentence from implementation. They
pin the safety/actionability boundaries whose drift would make the guide teach
users an unsafe or impossible workflow, and force a coverage decision when a
live session control group gains a tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import yaml

from services.canvas import CanvasRenderer
from services.shared_browser_canvas import BrowserCapabilityReason
from src.core.capability_grants import CATALOG
from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES
from src.tools.canvas import CANVAS_TOOLS_METADATA
from src.tools.research.browser_direct import BROWSER_DIRECT_TOOLS_METADATA

_ROOT = Path(__file__).resolve().parents[1]
_GUIDE = _ROOT / "config" / "skills" / "app-guide"


def _focused_topic(topic: str) -> tuple[dict, str]:
    text = (_GUIDE / "references" / f"{topic}.md").read_text(encoding="utf-8")
    metadata_text, body = text.removeprefix("---\n").split("\n---\n", 1)
    return yaml.safe_load(metadata_text), " ".join(body.lower().split())


def test_focused_guides_keep_metadata_contracts():
    expected = {
        "automations": {
            "guide_id": "automations.schedule",
            "capability_ids": {"automations.manage"},
            "journey_ids": {"automations.create"},
        },
        "fleet-and-delegation": {
            "guide_id": "sessions.delegate",
            "capability_ids": {
                "sessions.delegate",
                "jobs.create",
                "jobs.review",
            },
            "journey_ids": {"sessions.delegate-job"},
        },
        "canvas-and-browser": {
            "guide_id": "canvas.present-and-browse",
            "capability_ids": {"canvas.files", "canvas.browser"},
            "journey_ids": {"canvas.present-file", "canvas.share-browser"},
        },
        "permissions-and-availability": {
            "guide_id": "sessions.permissions-and-workspaces",
            "capability_ids": {
                "sessions.permission-mode",
                "workspaces.select",
            },
            "journey_ids": {
                "sessions.configure-permissions",
                "sessions.choose-workspace",
            },
        },
    }

    for topic, wanted in expected.items():
        metadata, _ = _focused_topic(topic)
        assert metadata["guide_id"] == wanted["guide_id"]
        assert metadata["content_type"] == "how_to"
        assert set(metadata["capability_ids"]) == wanted["capability_ids"]
        assert set(metadata["journey_ids"]) == wanted["journey_ids"]


def test_automation_guide_keeps_current_safety_and_actionability_boundaries():
    _, body = _focused_topic("automations")

    assert "current built-in automation surface is schedule-only" in body
    assert "run now" in body and "does not move the next scheduled run" in body
    assert "automation-fired jobs do not attach connectors" in body
    assert "automatically pauses the automation" in body
    assert "catchup window" in body and "does not backfill every missed" in body
    assert "scheduled delivery is at-least-once" in body
    assert "pinned by its stable id" in body
    assert "unpinned `worker_base` resolves" in body
    assert (
        "default session group cannot save, enable, run, pause, edit, or delete" in body
    )
    assert "app guide itself does not grant workflow tools" in body


def test_fleet_guide_keeps_current_scope_and_fallback_boundaries():
    _, body = _focused_topic("fleet-and-delegation")

    assert "fleet management" in body
    assert "jobs → new job" in body
    assert "normally inherits this session's selected connectors" in body
    assert "app guide does not grant fleet management" in body
    assert "does not delete jobs" in body
    assert "does not" in body and "continuously monitor a job" in body
    assert "delegation" in body and "different ways srw can split work" in body
    assert "non-admin users need the delegation grant" in body


def test_canvas_guide_keeps_renderer_handoff_and_availability_boundaries():
    _, body = _focused_topic("canvas-and-browser")

    assert "canvas is not another place where srw stores a copy" in body
    assert "svg, pdf, mermaid" in body and "are not supported" in body
    assert "scripts, forms, animations" in body
    assert "html-interactive" in body and "`auto` never selects it" in body
    assert "opaque, no-network sandbox" in body
    assert "every css, script, image, and font" in body
    assert "live preview" in body and "deployment enables" in body
    assert "closing the pane is local" in body
    assert "does not delete the file" in body
    assert "take control" in body and "release control" in body
    assert "user_is_driving" in body
    assert "same dom, cookies, and authenticated state" in body
    assert "closing the canvas pane merely detaches your viewer" in body
    assert "not available on the jobs view" in body
    assert "do not promise vm support" in body
    assert "shared browser is off on this server" in body
    assert "intentionally hidden" in body
    assert "app guide itself grants neither" in body


def test_permissions_guide_keeps_current_policy_and_workspace_boundaries():
    _, body = _focused_topic("permissions-and-availability")

    assert "static app guide" in body and "cannot inspect every" in body
    assert "supervised" in body and "before every tool call" in body
    assert "auto-accept" in body
    for tool_name in ("run_command", "shell_execute", "shell_read"):
        assert tool_name in body
    assert "autonomous" in body and "without per-call approval" in body
    assert "permission mode does not add tools" in body
    assert "built-in non-admin ceiling allows auto-accept" in body
    assert "autonomous needs an explicit grant" in body
    assert "global, project, and user scope" in body
    assert "most restrictive result wins" in body
    assert "the platform default is virtual" in body
    for tier in ("virtual", "container", "none", "vm"):
        assert f"| **{tier}**" in body
    assert "workspace changes are upgrade-only" in body
    for group in (
        "canvas",
        "fleet management",
        "experts & skills",
        "automations & loops",
    ):
        assert f"**{group}**" in body
    assert "admin → grants" in body
    assert "do not recommend switching to autonomous" in body


def test_permissions_guide_covers_the_current_grant_catalog():
    _, body = _focused_topic("permissions-and-availability")

    assert CATALOG["permission_mode"]["default"] == "auto_accept"
    for key in CATALOG:
        assert f"`{key}`" in body, key


def test_canvas_and_direct_browser_tool_inventories_have_guide_coverage():
    canvas_coverage = {
        "canvas-and-browser": {
            "get_canvas",
            "set_canvas",
            "clear_canvas",
        }
    }
    browser_coverage = {
        "canvas-and-browser": {
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_select",
            "browser_scroll",
            "browser_screenshot",
            "browser_back",
            "browser_close",
        }
    }
    renderer_coverage = {
        "canvas-and-browser": {
            "auto",
            "markdown",
            "text",
            "html",
            "html-interactive",
            "image",
        }
    }

    canvas_names = set().union(*canvas_coverage.values())
    assert canvas_names == set(SESSION_TOOL_OVERRIDE_NAMES["canvas"])
    assert canvas_names == set(CANVAS_TOOLS_METADATA)
    assert set().union(*browser_coverage.values()) == set(BROWSER_DIRECT_TOOLS_METADATA)
    # The guide may describe a schema-advertised opt-in renderer before every
    # supported deployment has it; every renderer in this build still needs an
    # explicit coverage decision.
    assert set(get_args(CanvasRenderer)) <= set().union(*renderer_coverage.values())


def test_canvas_guide_covers_every_shared_browser_reason_code():
    _, body = _focused_topic("canvas-and-browser")

    for reason in get_args(BrowserCapabilityReason):
        assert f"`{reason}`" in body, reason


def test_every_live_workflow_tool_has_an_explicit_guide_topic():
    coverage = {
        "automations": {
            "list_automations",
            "get_automation",
            "list_automation_runs",
            "propose_automation",
        },
        "projects-and-loops": {
            "get_project_loop",
            "list_project_loop_jobs",
            "explain_project_loop",
        },
    }

    assert set().union(*coverage.values()) == set(
        SESSION_TOOL_OVERRIDE_NAMES["workflows"]
    )


def test_every_selectable_live_fleet_tool_has_an_explicit_guide_topic():
    coverage = {
        "fleet-and-delegation": {
            "get_session_context",
            "create_worker_job",
            "list_worker_jobs",
            "get_worker_job",
            "get_job_workspace_file",
            "approve_worker_job",
            "resume_worker_job",
            "cancel_worker_job",
            "pause_worker_job",
            "get_current_project",
            "list_project_jobs",
            "list_project_repositories",
            "get_default_project_repository",
        }
    }

    assert set().union(*coverage.values()) == set(
        SESSION_TOOL_OVERRIDE_NAMES["orchestrator"]
    )
