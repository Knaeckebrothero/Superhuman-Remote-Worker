"""Focused content contracts for the managed SRW product guide.

These checks do not attempt to prove every sentence from implementation. They
pin the safety/actionability boundaries whose drift would make the guide teach
users an unsafe or impossible workflow, and force a coverage decision when a
live session control group gains a tool.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES

_ROOT = Path(__file__).resolve().parents[1]
_GUIDE = _ROOT / "config" / "skills" / "app-guide"


def _focused_topic(topic: str) -> tuple[dict, str]:
    text = (_GUIDE / "references" / f"{topic}.md").read_text(encoding="utf-8")
    metadata_text, body = text.removeprefix("---\n").split("\n---\n", 1)
    return yaml.safe_load(metadata_text), " ".join(body.lower().split())


def test_automation_and_fleet_guides_keep_metadata_contracts():
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
