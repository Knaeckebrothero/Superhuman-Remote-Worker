from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tests.e2e.app import ci_finalize
from tests.e2e.app.harness import CommandResult, StateStore, utc_now, write_private_json

RUN_ID = "20260827-120000-abcdef12"


def absent_runtime_probe(argv: Sequence[str]) -> CommandResult:
    if list(argv[:2]) == ["docker", "version"]:
        return CommandResult(0, stdout="29.0.0\n")
    if list(argv[:4]) == ["docker", "ps", "-a", "--format"]:
        return CommandResult(0, stdout="unrelated-container\n")
    if list(argv[:4]) == ["k3d", "cluster", "list", "-o"]:
        return CommandResult(0, stdout="[]")
    return CommandResult(1)


def completed_state(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "state"
    store = StateStore(root)
    ledger = store.initialize(RUN_ID)
    ledger.update(
        {
            "created_by_run": True,
            "source_revision": "a" * 40,
            "source_dirty": False,
            "authoritative": True,
            "cluster_deleted_at": utc_now(),
            "last_completed_layer": "playwright-complete",
            "browser_attempts": 1,
            "layer_timings": {"cluster-api": {"elapsed_ms": 1200}},
        }
    )
    run_dir = Path(str(ledger["run_dir"]))
    thread_id = "123e4567-e89b-42d3-a456-426614174000"
    write_private_json(
        run_dir / "browser/browser-resources.json",
        {
            "schema": 1,
            "run_id": "browser-scenario-run",
            "resources": [{"kind": "thread", "id": thread_id}],
            "finalized": True,
            "cleanup_complete": True,
        },
    )
    write_private_json(
        run_dir / "cleanup.json",
        {
            "run_id": RUN_ID,
            "resources": [{"kind": "thread", "id": thread_id, "status": "404"}],
        },
    )
    store.persist(ledger)
    store.clear_active(ledger)
    return root, ledger


def test_completed_clean_run_is_a_qualifying_observation(tmp_path: Path) -> None:
    root, _ledger = completed_state(tmp_path)

    result = ci_finalize.inspect_ci_observation(
        root,
        0,
        probe=absent_runtime_probe,
    )

    assert result.teardown_proven is True
    assert result.qualifying_observation is True
    assert result.cleanup_status == "verified"
    assert result.browser_attempts == 1


def test_active_cluster_state_refuses_browser_artifact_release(tmp_path: Path) -> None:
    root, ledger = completed_state(tmp_path)
    store = StateStore(root)
    # Re-publish a valid active claim to model teardown that did not finish.
    write_private_json(store.active_path, ledger)

    result = ci_finalize.inspect_ci_observation(
        root,
        1,
        probe=absent_runtime_probe,
    )

    assert result.teardown_proven is False
    assert "active ownership marker still exists" in result.teardown_problems


def test_remaining_exact_container_fails_teardown_proof(tmp_path: Path) -> None:
    root, ledger = completed_state(tmp_path)

    def remaining_container(argv: Sequence[str]) -> CommandResult:
        if list(argv[:4]) == ["docker", "ps", "-a", "--format"]:
            return CommandResult(
                0,
                stdout=f"k3d-{ledger['cluster_name']}-server-0\n",
            )
        return absent_runtime_probe(argv)

    result = ci_finalize.inspect_ci_observation(root, 1, probe=remaining_container)

    assert result.teardown_proven is False
    assert "one or more exact run containers remain" in result.teardown_problems


def test_failed_journey_with_proven_teardown_is_not_qualifying(tmp_path: Path) -> None:
    root, _ledger = completed_state(tmp_path)

    result = ci_finalize.inspect_ci_observation(
        root,
        1,
        probe=absent_runtime_probe,
    )

    assert result.teardown_proven is True
    assert result.qualifying_observation is False


def test_success_without_exact_resource_cleanup_is_not_qualifying(
    tmp_path: Path,
) -> None:
    root, ledger = completed_state(tmp_path)
    run_dir = Path(str(ledger["run_dir"]))
    (run_dir / "browser/browser-resources.json").unlink()
    (run_dir / "cleanup.json").unlink()

    result = ci_finalize.inspect_ci_observation(
        root,
        0,
        probe=absent_runtime_probe,
    )

    assert result.teardown_proven is True
    assert result.cleanup_status == "not-required"
    assert result.qualifying_observation is False
    assert (
        "successful result lacks exact cleanup evidence"
        in result.qualification_problems
    )


def test_failure_before_state_creation_requires_no_teardown(tmp_path: Path) -> None:
    result = ci_finalize.inspect_ci_observation(
        tmp_path / "absent",
        1,
        probe=absent_runtime_probe,
    )

    assert result.state_found is False
    assert result.teardown_required is False
    assert result.teardown_proven is False


def test_github_outputs_and_summary_publish_only_sanitized_status(
    tmp_path: Path,
) -> None:
    root, _ledger = completed_state(tmp_path)
    result = ci_finalize.inspect_ci_observation(
        root,
        0,
        probe=absent_runtime_probe,
    )
    outputs = tmp_path / "github-output"
    summary = tmp_path / "github-summary"

    ci_finalize._append_outputs(outputs, result)
    ci_finalize._append_summary(summary, result)

    output_text = outputs.read_text(encoding="utf-8")
    summary_text = summary.read_text(encoding="utf-8")
    assert "teardown_proven=true" in output_text
    assert "qualifying_observation=true" in output_text
    assert f"run_id={RUN_ID}" in output_text
    assert "Application E2E observation" in summary_text
    assert "cluster-api" in summary_text
    assert "1200 ms" in summary_text
