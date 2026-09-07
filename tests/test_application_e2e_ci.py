"""Structural contracts for the scheduled application-E2E observation lane."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.e2e.app import harness

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github/workflows/application-e2e.yml"


def workflow_document() -> tuple[str, dict]:
    raw = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.safe_load(raw)
    assert isinstance(document, dict)
    return raw, document


def test_application_e2e_is_a_separate_profiled_observation_lane() -> None:
    raw, document = workflow_document()
    triggers = document.get("on", document.get(True))

    assert set(triggers) == {"schedule", "workflow_dispatch", "push"}
    assert triggers["push"] == {"branches": ["develop"]}
    assert document["concurrency"]["cancel-in-progress"] is True
    assert set(document["jobs"]) == {"application-e2e"}
    job = document["jobs"]["application-e2e"]
    assert job["timeout-minutes"] == "${{ matrix.job_timeout_minutes }}"
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "include": [
                {
                    "profile": "pinned-virtual",
                    "job_timeout_minutes": 45,
                    "lifecycle_timeout": "38m",
                },
                {
                    "profile": "stateless-sandbox",
                    "job_timeout_minutes": 75,
                    "lifecycle_timeout": "68m",
                },
            ]
        },
    }
    assert "matrix.profile" in job["env"]["APP_E2E_STATE_DIR"]
    permissions = job.get("permissions", document["permissions"])
    assert permissions == {"contents": "read"}
    assert './scripts/e2e-app.sh run --profile "${{ matrix.profile }}"' in raw
    assert "APP_E2E_ALLOW_DIRTY" not in raw
    assert "pull_request:" not in raw


def test_workflow_pins_the_harness_toolchain_and_keeps_a_teardown_reserve() -> None:
    raw, document = workflow_document()

    assert document["env"]["K3D_VERSION"] == harness.K3D_VERSION
    assert harness.K3S_IMAGE.endswith("v1.31.5-k3s1")
    assert "version: v1.31.5" in raw
    assert "version: v3.17.0" in raw
    assert (
        'timeout --foreground --signal=INT --kill-after=4m "${{ matrix.lifecycle_timeout }}"'
        in raw
    )
    assert 'install -d -m 0700 "${APP_E2E_STATE_DIR%/*}"' in raw
    assert "python -m tests.e2e.app.ci_finalize" in raw
    assert "if: always()" in raw


def test_artifact_paths_are_explicit_and_browser_evidence_requires_teardown() -> None:
    raw, document = workflow_document()
    steps = {
        step.get("name"): step
        for step in document["jobs"]["application-e2e"]["steps"]
        if isinstance(step, dict) and step.get("name")
    }
    metadata = steps["Upload sanitized E2E metadata"]
    browser = steps["Upload browser evidence after proven teardown"]

    assert metadata["with"]["retention-days"] == 7
    assert browser["with"]["retention-days"] == 7
    assert "steps.finalize.outputs.teardown_proven == 'true'" in browser["if"]
    assert "browser/playwright-report/**" in browser["with"]["path"]
    assert "browser/artifacts/**" in browser["with"]["path"]
    assert "diagnostics/**" in metadata["with"]["path"]
    assert "credentials.json" not in raw
    assert "kubeconfig.yaml" not in raw
    assert "/.auth" not in raw
    assert "${{ env.APP_E2E_STATE_DIR }}/**" not in raw
    assert "matrix.profile" in metadata["with"]["name"]
    assert "matrix.profile" in browser["with"]["name"]
