"""The self-hosted runner routing policy holds.

This repository is public and its CI runs on self-hosted runners; a
`pull_request` routed to one would execute a stranger's code inside the cluster.
``scripts/check_ci_runners.py`` is the merge-time gate on that, and
``.github/workflows/ci-policy.yml`` is what actually blocks a merge.

This test is a convenience, NOT the gate: ``test-python`` is change-based on
develop and would skip entirely on a workflow-only diff. Its job is to fail fast
locally and in the normal test run, so nobody discovers a routing mistake only
after pushing. The hard gate is the required ``runner routing policy`` check.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_ci_runners.py"


def _load_checker():
    """Import the checker by path.

    ``scripts/`` is not on sys.path (conftest.py adds the project root and
    ``orchestrator/`` only), and adding it session-wide to reach one module would
    put every script name into the global import namespace.
    """
    spec = importlib.util.spec_from_file_location("check_ci_runners", CHECKER)
    assert spec and spec.loader, f"cannot load {CHECKER}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves its annotations through
    # sys.modules[cls.__module__], so a module that executes while absent from
    # sys.modules raises AttributeError on the first dataclass it defines.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestCiRunnerPolicy:
    @pytest.fixture(scope="class")
    def checker(self):
        if not CHECKER.is_file():
            pytest.fail(
                f"{CHECKER} is missing — it is the merge-time half of the "
                f"self-hosted runner fork defence."
            )
        return _load_checker()

    def test_policy_holds(self, checker):
        errors = checker.check()
        assert errors == [], "CI runner-routing policy violated:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    def test_runtime_guard_contract_is_documented(self, checker):
        """The run-time half must stay documented here.

        The guard itself lives in Scripts-and-Notebooks
        (devops/github-actions-runner/) — outside every repository it protects,
        which is what makes it uneditable by a pull request. So this repo can
        only check that the contract is still written down, not that it is still
        enforced; the enforcement proof runs against the built image and gates
        its push. check() covers this too, but calling it out separately means a
        vanished contract reads as its own failure rather than one line in a list.
        """
        doc = REPO_ROOT / "policy" / "ci_self_hosted_runners.md"
        assert doc.is_file(), f"{doc} is missing — the runner contract is undocumented"
        text = doc.read_text(encoding="utf-8")
        missing = [n for n in checker.RUNNER_CONTRACT if n not in text]
        assert not missing, f"runner contract drifted; doc no longer mentions {missing}"

    def test_every_workflow_is_registered(self, checker):
        """A new workflow file must be a deliberate routing decision."""
        on_disk = {
            p.name
            for p in (REPO_ROOT / ".github" / "workflows").iterdir()
            if p.is_file() and p.suffix in {".yml", ".yaml"}
        }
        unregistered = on_disk - checker.WORKFLOWS.keys()
        assert not unregistered, (
            f"unregistered workflow(s): {sorted(unregistered)}. Add a Policy entry "
            f"to scripts/check_ci_runners.py declaring triggers and routing."
        )
