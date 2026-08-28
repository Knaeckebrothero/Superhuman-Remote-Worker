#!/usr/bin/env python3
"""Verify an application-E2E CI run before publishing its evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.app.harness import (
    RUN_DIRECTORY_MARKER,
    CommandResult,
    CommandRunner,
    StateStore,
    read_private_json,
    validate_run_id,
)

Probe = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class CiObservation:
    e2e_exit_code: int
    state_found: bool
    teardown_required: bool
    teardown_proven: bool
    qualifying_observation: bool
    run_id: str = "none"
    last_completed_layer: str = "none"
    browser_attempts: int | None = None
    authoritative: bool = False
    cleanup_status: str = "not-started"
    layer_timings: Mapping[str, object] | None = None
    teardown_problems: tuple[str, ...] = ()
    qualification_problems: tuple[str, ...] = ()


def _default_probe(argv: Sequence[str]) -> CommandResult:
    return CommandRunner().run(
        argv,
        check=False,
        timeout=30,
        label="CI teardown verification",
    )


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    value = read_private_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _discover_run_directory(state_root: Path) -> Path:
    candidates = [
        child
        for child in state_root.iterdir()
        if not child.is_symlink()
        and child.is_dir()
        and (child / RUN_DIRECTORY_MARKER).is_file()
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one owned E2E run directory, found {len(candidates)}"
        )
    return candidates[0]


def _cleanup_status(run_dir: Path, run_id: str) -> tuple[str, list[str]]:
    resource_path = run_dir / "browser/browser-resources.json"
    cleanup_path = run_dir / "cleanup.json"
    if not resource_path.exists():
        return "not-required", []
    problems: list[str] = []
    try:
        resources = _read_json_object(resource_path, "browser resource ledger")
        cleanup = _read_json_object(cleanup_path, "cleanup evidence")
        resource_rows = resources.get("resources")
        cleanup_rows = cleanup.get("resources")
        if cleanup.get("run_id") != run_id:
            problems.append("cleanup evidence does not belong to the owned CI run")
        if not isinstance(resources.get("run_id"), str):
            problems.append("browser resource ledger has no provider scenario run id")
        if not isinstance(resource_rows, list) or not isinstance(cleanup_rows, list):
            problems.append("cleanup evidence has an invalid resource list")
        else:
            resource_ids = [
                row.get("id") for row in resource_rows if isinstance(row, dict)
            ]
            cleanup_ids = [
                row.get("id") for row in cleanup_rows if isinstance(row, dict)
            ]
            valid_ids = all(
                isinstance(item, str) for item in resource_ids + cleanup_ids
            )
            if (
                not valid_ids
                or len(resource_ids) != len(resource_rows)
                or len(cleanup_ids) != len(cleanup_rows)
                or len(set(resource_ids)) != len(resource_ids)
                or set(resource_ids) != set(cleanup_ids)
            ):
                problems.append(
                    "cleanup evidence does not cover every exact resource id"
                )
        if resources.get("cleanup_complete") is not True:
            problems.append("browser resource ledger is not marked cleanup-complete")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        problems.append(f"cleanup evidence is unavailable: {type(exc).__name__}")
    return ("verified" if not problems else "failed"), problems


def inspect_ci_observation(
    state_root: Path,
    e2e_exit_code: int,
    *,
    probe: Probe = _default_probe,
) -> CiObservation:
    state_root = Path(os.path.abspath(state_root.expanduser()))
    if not state_root.exists():
        qualification = (
            ("E2E command succeeded without creating owned result metadata",)
            if e2e_exit_code == 0
            else ()
        )
        return CiObservation(
            e2e_exit_code=e2e_exit_code,
            state_found=False,
            teardown_required=False,
            teardown_proven=False,
            qualifying_observation=False,
            qualification_problems=qualification,
        )

    teardown_problems: list[str] = []
    qualification_problems: list[str] = []
    try:
        store = StateStore(state_root)
        store._require_owned_root()
        run_dir = _discover_run_directory(state_root)
        ledger = _read_json_object(run_dir / "ledger.json", "ownership ledger")
        store.validate(ledger)
    except (OSError, RuntimeError, ValueError) as exc:
        return CiObservation(
            e2e_exit_code=e2e_exit_code,
            state_found=True,
            teardown_required=True,
            teardown_proven=False,
            qualifying_observation=False,
            teardown_problems=(
                f"owned result metadata could not be validated: {type(exc).__name__}",
            ),
        )

    run_id = validate_run_id(str(ledger["run_id"]))
    cluster_name = str(ledger["cluster_name"])
    last_layer = str(ledger.get("last_completed_layer", "none"))
    browser_attempts_value = ledger.get("browser_attempts")
    browser_attempts = (
        browser_attempts_value
        if isinstance(browser_attempts_value, int)
        and not isinstance(browser_attempts_value, bool)
        else None
    )
    authoritative = ledger.get("authoritative") is True
    teardown_required = ledger.get("created_by_run") is True

    if (state_root / "active.json").exists():
        teardown_problems.append("active ownership marker still exists")
    if ledger.get("phase") != "down":
        teardown_problems.append("ownership ledger did not reach the down phase")
    if teardown_required and not isinstance(ledger.get("cluster_deleted_at"), str):
        teardown_problems.append("cluster deletion was not recorded")
    if not isinstance(ledger.get("finished_at"), str):
        teardown_problems.append("lifecycle completion was not recorded")

    credential_paths = (
        run_dir / "credentials.json",
        run_dir / "kubeconfig.yaml",
        run_dir / "browser.env",
        run_dir / "vm-ssh-key",
        run_dir / "vm-ssh-key.pub",
        run_dir / "browser/.auth/journey.json",
        run_dir / "browser/.auth/journey.json.candidate",
    )
    if any(path.exists() or path.is_symlink() for path in credential_paths):
        teardown_problems.append("one or more private credential files remain")

    docker_ready = probe(["docker", "version", "--format", "{{.Server.Version}}"])
    if docker_ready.returncode != 0 or not docker_ready.stdout.strip():
        teardown_problems.append("Docker daemon is unavailable for absence proof")
    else:
        container_inventory = probe(["docker", "ps", "-a", "--format", "{{.Names}}"])
        if container_inventory.returncode != 0:
            teardown_problems.append("Docker container inventory failed")
        else:
            forbidden_names = {
                f"srw-e2e-browser-{run_id}",
            }
            forbidden_prefix = f"k3d-{cluster_name}-"
            remaining_names = set(container_inventory.stdout.splitlines())
            if forbidden_names.intersection(remaining_names) or any(
                name.startswith(forbidden_prefix) for name in remaining_names
            ):
                teardown_problems.append("one or more exact run containers remain")

        for kind, argv in (
            (
                "network",
                ["docker", "network", "inspect", f"k3d-{cluster_name}"],
            ),
            (
                "image volume",
                ["docker", "volume", "inspect", f"k3d-{cluster_name}-images"],
            ),
        ):
            if probe(argv).returncode == 0:
                teardown_problems.append(f"exact run {kind} remains")

        images = ledger.get("images")
        if isinstance(images, dict):
            for image in images.values():
                if (
                    isinstance(image, str)
                    and probe(["docker", "image", "inspect", image]).returncode == 0
                ):
                    teardown_problems.append("one or more exact run image tags remain")
                    break

    cluster_inventory = probe(["k3d", "cluster", "list", "-o", "json"])
    if cluster_inventory.returncode != 0:
        teardown_problems.append("k3d cluster inventory failed")
    else:
        try:
            clusters = json.loads(cluster_inventory.stdout or "[]")
            if not isinstance(clusters, list):
                raise ValueError
            if any(
                isinstance(cluster, dict) and cluster.get("name") == cluster_name
                for cluster in clusters
            ):
                teardown_problems.append("exact owned k3d cluster remains")
        except (json.JSONDecodeError, ValueError):
            teardown_problems.append("k3d cluster inventory was not valid JSON")

    cleanup_status, cleanup_problems = _cleanup_status(run_dir, run_id)
    qualification_problems.extend(cleanup_problems)
    if e2e_exit_code == 0:
        if not authoritative or ledger.get("source_dirty") is not False:
            qualification_problems.append(
                "successful result is not clean-tree authoritative"
            )
        if last_layer != "playwright-complete":
            qualification_problems.append(
                "successful result did not complete Playwright"
            )
        if browser_attempts != 1:
            qualification_problems.append(
                "successful result was not a first-attempt pass"
            )
        if cleanup_status != "verified":
            qualification_problems.append(
                "successful result lacks exact cleanup evidence"
            )

    teardown_proven = not teardown_problems
    qualifying = (
        e2e_exit_code == 0
        and teardown_proven
        and not qualification_problems
        and authoritative
        and last_layer == "playwright-complete"
        and browser_attempts == 1
    )
    timings = ledger.get("layer_timings")
    return CiObservation(
        e2e_exit_code=e2e_exit_code,
        state_found=True,
        teardown_required=teardown_required,
        teardown_proven=teardown_proven,
        qualifying_observation=qualifying,
        run_id=run_id,
        last_completed_layer=last_layer,
        browser_attempts=browser_attempts,
        authoritative=authoritative,
        cleanup_status=cleanup_status,
        layer_timings=timings if isinstance(timings, dict) else {},
        teardown_problems=tuple(teardown_problems),
        qualification_problems=tuple(qualification_problems),
    )


def _append_outputs(path: Path, observation: CiObservation) -> None:
    values = {
        "teardown_required": str(observation.teardown_required).lower(),
        "teardown_proven": str(observation.teardown_proven).lower(),
        "qualifying_observation": str(observation.qualifying_observation).lower(),
        "run_id": observation.run_id,
        "last_completed_layer": observation.last_completed_layer,
        "browser_attempts": str(observation.browser_attempts or 0),
    }
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def _append_summary(path: Path, observation: CiObservation) -> None:
    result = "passed" if observation.e2e_exit_code == 0 else "failed"
    lines = [
        "## Application E2E observation",
        "",
        "| Field | Result |",
        "|---|---|",
        f"| E2E command | {result} (exit {observation.e2e_exit_code}) |",
        f"| Run id | `{observation.run_id}` |",
        f"| Last readiness layer | `{observation.last_completed_layer}` |",
        f"| Browser attempts | {observation.browser_attempts or 0} |",
        f"| Exact cleanup | {observation.cleanup_status} |",
        f"| Teardown proven | {str(observation.teardown_proven).lower()} |",
        f"| Qualifying scheduled observation | {str(observation.qualifying_observation).lower()} |",
        "",
    ]
    timings = observation.layer_timings or {}
    if timings:
        lines.extend(["### Layer timings", "", "| Layer | Elapsed |", "|---|---:|"])
        for layer, value in timings.items():
            elapsed = value.get("elapsed_ms") if isinstance(value, dict) else None
            rendered = f"{elapsed} ms" if isinstance(elapsed, int) else "unknown"
            lines.append(f"| `{layer}` | {rendered} |")
        lines.append("")
    problems = [
        *observation.teardown_problems,
        *observation.qualification_problems,
    ]
    if problems:
        lines.extend(["### Verification findings", ""])
        lines.extend(f"- {problem}" for problem in problems)
        lines.append("")
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--e2e-exit-code", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    observation = inspect_ci_observation(
        arguments.state_root,
        arguments.e2e_exit_code,
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        _append_outputs(Path(github_output), observation)
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        _append_summary(Path(github_summary), observation)

    if observation.teardown_problems:
        for problem in observation.teardown_problems:
            print(f"[e2e-ci] teardown verification failed: {problem}", file=sys.stderr)
        return 1
    if observation.e2e_exit_code == 0 and not observation.qualifying_observation:
        for problem in observation.qualification_problems:
            print(f"[e2e-ci] observation is not qualifying: {problem}", file=sys.stderr)
        return 1
    print(
        "[e2e-ci] teardown proven"
        if observation.teardown_proven
        else "[e2e-ci] no owned cluster state was created",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
