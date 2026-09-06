"""scripts/select_affected_tests.py — the thing that decides what CI runs.

A selector that under-selects turns a red suite green, so the property under test
is *never miss*, and the anchor is a real change rather than a hypothetical: the
integration test at the bottom replays commit a1d92680's file list and requires
all nine test files that actually failed. Six of those nine share no name with any
changed file, which is the entire reason this is a graph and not a naming rule.

Unit tests build a synthetic repo so they stay fast and can assert exact sets;
the real-repo test is deliberately one test, because the graph build is ~9s.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from select_affected_tests import ALL, Graph, select  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


# =============================================================================
# Synthetic repo — exact sets, no real-graph noise
# =============================================================================
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A miniature of this repo's shape: editable application packages, data files, and tests."""
    (tmp_path / "src" / "agent" / "core").mkdir(parents=True)
    (tmp_path / "src" / "agent" / "tools").mkdir(parents=True)
    (tmp_path / "src" / "orchestrator" / "services").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "config").mkdir()

    (tmp_path / "src" / "shared").mkdir()

    # shared leaf <- agent mid <- agent hub, plus an unrelated island
    (tmp_path / "src" / "shared" / "leaf.py").write_text("VALUE = 1\n")
    (tmp_path / "src" / "agent" / "core" / "mid.py").write_text(
        "from shared.leaf import VALUE\n"
    )
    (tmp_path / "src" / "agent" / "tools" / "hub.py").write_text(
        "from agent.core.mid import VALUE\n"
    )
    (tmp_path / "src" / "agent" / "core" / "island.py").write_text("OTHER = 2\n")

    # Both applications consume the same shared leaf through qualified imports.
    (tmp_path / "src" / "orchestrator" / "services" / "thing.py").write_text(
        "from shared.leaf import VALUE\n"
    )
    (tmp_path / "src" / "orchestrator" / "main.py").write_text(
        "from orchestrator.services.thing import VALUE\n"
    )

    (tmp_path / "tests" / "test_leaf.py").write_text(
        "from shared.leaf import VALUE\n\ndef test_x(): assert VALUE\n"
    )
    (tmp_path / "tests" / "test_hub.py").write_text(
        "from agent.tools.hub import VALUE\n\ndef test_x(): assert VALUE\n"
    )
    (tmp_path / "tests" / "test_island.py").write_text(
        "from agent.core.island import OTHER\n\ndef test_x(): assert OTHER\n"
    )
    (tmp_path / "tests" / "test_via_main.py").write_text(
        "import orchestrator.main as main\n\ndef test_x(): assert main\n"
    )
    # asserts on a data file, imports nothing interesting
    (tmp_path / "tests" / "test_data.py").write_text(
        "from pathlib import Path\n"
        "def test_x():\n"
        "    assert Path('config/thing.yaml').exists()\n"
    )
    (tmp_path / "config" / "thing.yaml").write_text("a: 1\n")
    return tmp_path


def sel(changed, repo: Path):
    out = select(changed, repo)
    return out if out == ALL else set(out)


class TestImportEdges:
    def test_a_leaf_selects_every_transitive_importer(self, repo: Path):
        got = sel(["src/shared/leaf.py"], repo)
        assert got == {
            "tests/test_leaf.py",
            "tests/test_hub.py",  # via agent.tools.hub -> agent.core.mid
            "tests/test_via_main.py",  # via main -> services.thing
        }

    def test_an_island_selects_only_its_own_test(self, repo: Path):
        assert sel(["src/agent/core/island.py"], repo) == {"tests/test_island.py"}

    def test_the_orchestrator_package_resolves(self, repo: Path):
        """Qualified orchestrator imports resolve through the editable source root."""
        assert sel(["src/orchestrator/main.py"], repo) == {"tests/test_via_main.py"}

    def test_an_intermediate_module_selects_its_dependents_not_its_deps(
        self, repo: Path
    ):
        got = sel(["src/agent/core/mid.py"], repo)
        assert "tests/test_hub.py" in got
        # test_leaf imports leaf only; mid is downstream of it, not upstream.
        assert "tests/test_leaf.py" not in got


class TestDataEdges:
    def test_a_config_file_selects_the_test_that_reads_it(self, repo: Path):
        assert sel(["config/thing.yaml"], repo) == {"tests/test_data.py"}

    def test_a_data_file_nobody_reads_selects_nothing(self, repo: Path):
        (repo / "config" / "unread.yaml").write_text("b: 2\n")
        assert sel(["config/unread.yaml"], repo) == set()

    def test_a_literal_naming_a_parent_directory_still_matches(self, repo: Path):
        (repo / "tests" / "test_dir_scan.py").write_text(
            "from pathlib import Path\n"
            "def test_x(): assert list(Path('config').glob('*.yaml'))\n"
        )
        (repo / "config" / "deep.yaml").write_text("c: 3\n")
        assert "tests/test_dir_scan.py" in sel(["config/deep.yaml"], repo)


class TestFailOpen:
    """Every ambiguity must widen the run, never narrow it."""

    @pytest.mark.parametrize(
        "changed",
        [
            "tests/conftest.py",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements/constraints.txt",
            "requirements/locks/orchestrator-py311.txt",
            "requirements/locks/vm-controller-py312.txt",
            "scripts/lock_dependencies.py",
            "src/orchestrator/requirements.txt",
            "pyproject.toml",
            "scripts/select_affected_tests.py",
        ],
    )
    def test_environment_and_selector_changes_run_everything(
        self, changed: str, repo: Path
    ):
        assert sel([changed], repo) == ALL

    def test_a_deleted_source_file_runs_everything(self, repo: Path):
        (repo / "src" / "shared" / "leaf.py").unlink()
        assert sel(["src/shared/leaf.py"], repo) == ALL

    def test_a_python_file_outside_the_scanned_trees_runs_everything(self, repo: Path):
        (repo / "generator.py").write_text("x = 1\n")
        # Top-level .py IS scanned; a nested unscanned tree is not.
        (repo / "vm").mkdir()
        (repo / "vm" / "helper.py").write_text("y = 2\n")
        assert sel(["vm/helper.py"], repo) == ALL

    def test_an_unparseable_file_runs_everything(self, repo: Path):
        (repo / "src" / "agent" / "core" / "broken.py").write_text("def (\n")
        assert sel(["src/shared/leaf.py"], repo) == ALL

    def test_unobserved_application_source_runs_everything(self, repo: Path):
        (repo / "src" / "agent" / "entry.py").write_text("VALUE = 1\n")
        assert sel(["src/agent/entry.py"], repo) == ALL

    def test_no_changed_files_selects_nothing_rather_than_everything(self, repo: Path):
        """An empty diff is not an ambiguity — there is nothing to test."""
        assert sel([], repo) == set()


class TestChangedTests:
    def test_a_changed_test_runs_itself(self, repo: Path):
        assert sel(["tests/test_island.py"], repo) == {"tests/test_island.py"}

    def test_a_changed_test_that_nothing_imports_is_still_selected(self, repo: Path):
        (repo / "tests" / "test_brand_new.py").write_text("def test_x(): pass\n")
        assert "tests/test_brand_new.py" in sel(["tests/test_brand_new.py"], repo)


class TestNonTestFilesUnderTests:
    """Only ``test_*.py`` may be handed to pytest as a target.

    Everything else under ``tests/`` — helpers, package inits, the fixture
    container's entrypoint — is a dependency. Emitting one makes CI import a
    module nobody wrote to be collected, and the run dies at collection with a
    ModuleNotFoundError that has nothing to do with the change.
    """

    @pytest.fixture
    def repo(self, repo: Path) -> Path:
        (repo / "tests" / "support").mkdir()
        (repo / "tests" / "__init__.py").write_text("")
        (repo / "tests" / "support" / "__init__.py").write_text("")
        (repo / "tests" / "support" / "harness.py").write_text("HARNESS = 1\n")
        # Runs only inside its own image, where `harness` is top-level.
        (repo / "tests" / "support" / "run.py").write_text(
            "from harness import HARNESS\n"
        )
        (repo / "tests" / "test_harness.py").write_text(
            "from tests.support.harness import HARNESS\n\ndef test_x(): assert HARNESS\n"
        )
        return repo

    def test_a_changed_helper_selects_its_importers_not_itself(self, repo: Path):
        got = sel(["tests/support/harness.py"], repo)
        assert "tests/support/harness.py" not in got
        assert "tests/test_harness.py" in got

    def test_a_helper_nobody_imports_is_never_a_target(self, repo: Path):
        """run.py's own import only resolves inside its image, never under pytest."""
        assert "tests/support/run.py" not in sel(["tests/support/run.py"], repo)

    def test_a_changed_package_init_selects_the_tests_that_import_through_it(
        self, repo: Path
    ):
        got = sel(["tests/support/__init__.py"], repo)
        assert "tests/support/__init__.py" not in got
        assert "tests/test_harness.py" in got

    def test_a_source_package_init_selects_its_dependents(self, repo: Path):
        (repo / "src" / "shared" / "__init__.py").write_text("")
        got = sel(["src/shared/__init__.py"], repo)
        assert "tests/test_leaf.py" in got
        assert "tests/test_hub.py" in got  # via agent.tools.hub -> agent.core.mid


class TestGraphInternals:
    def test_shared_change_selects_all_application_consumers(self, repo: Path):
        (repo / "src" / "mcp_server").mkdir()
        (repo / "src" / "mcp_server" / "server.py").write_text(
            "from shared.leaf import VALUE\n"
        )
        (repo / "tests" / "test_mcp.py").write_text(
            "from mcp_server.server import VALUE\n"
        )
        assert {
            "tests/test_leaf.py",
            "tests/test_hub.py",
            "tests/test_via_main.py",
            "tests/test_mcp.py",
        } <= sel(["src/shared/leaf.py"], repo)

    def test_relative_initializer_exports_keep_leaf_dependencies(self, repo: Path):
        (repo / "src" / "shared" / "__init__.py").write_text(
            "from .leaf import VALUE\n"
        )
        (repo / "tests" / "test_package_api.py").write_text(
            "from shared import VALUE\n"
        )
        assert "tests/test_package_api.py" in sel(["src/shared/leaf.py"], repo)

    def test_relative_imports_resolve_within_a_package(self, tmp_path: Path):
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
        (tmp_path / "src" / "pkg" / "a.py").write_text("A = 1\n")
        (tmp_path / "src" / "pkg" / "b.py").write_text("from .a import A\n")
        (tmp_path / "tests" / "test_b.py").write_text("from pkg.b import A\n")
        graph = Graph(tmp_path)
        assert graph.reaches(
            tmp_path / "tests" / "test_b.py", tmp_path / "src" / "pkg" / "a.py"
        )

    def test_a_module_does_not_reach_itself(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "src" / "solo.py").write_text("import solo\n")
        graph = Graph(tmp_path)
        assert (tmp_path / "src" / "solo.py") not in graph._closure(
            tmp_path / "src" / "solo.py"
        )


# =============================================================================
# The anchor: a real change, and the tests it really broke
# =============================================================================
class TestAgainstTheRealRepo:
    """One test, because building the real graph costs ~9s."""

    #: Commit a1d92680 paths mapped to the current application packages.
    CHANGED = [
        "config/schema.json",
        "config/session_base.yaml",
        "config/skills/app-guide/references/permissions-and-availability.md",
        "src/orchestrator/main.py",
        "src/shared/runtime/core/capability_grants.py",
        "src/shared/runtime/core/loader.py",
        "src/shared/runtime/core/session_tool_overrides.py",
        "src/shared/runtime/core/tool_policy.py",
        "src/shared/runtime/core/tool_report.py",
        "src/agent/tools/orchestrator/catalog.py",
        "src/agent/tools/orchestrator/workflows.py",
        "src/agent/tools/registry.py",
    ]

    #: Observed failures from the full-suite run on that change. The starred six
    #: have NO filename relationship to any changed file — they are why a naming
    #: rule was rejected.
    MUST_SELECT = [
        "tests/test_capability_grants.py",
        "tests/test_tool_report.py",
        "tests/test_tool_policy.py",
        "tests/test_tool_grant_classification.py",  # *
        "tests/test_app_guide_content.py",  # *
        "tests/test_orchestrator_catalog_tool.py",  # *
        "tests/test_orchestrator_workflows_tool.py",  # *
        "tests/test_persistent_session.py",  # *
        "tests/test_session_tool_groups_endpoint.py",  # *
    ]

    def test_selects_every_test_that_really_failed(self):
        got = select(self.CHANGED, REPO)
        assert got != ALL, "should narrow on this change, not bail out"
        missing = [t for t in self.MUST_SELECT if t not in got]
        assert not missing, (
            f"selector would SKIP tests that this change really broke: {missing}. "
            f"A missed test turns a red suite green — widen the graph, do not "
            f"relax this list."
        )

    def test_a_leaf_change_narrows_hard(self):
        """The saving has to be real, or selection is only added risk."""
        # Workspace tools execute the eager tools/API package initializers;
        # they are not leaves once those dependency edges are resolved correctly.
        got = select(["src/vm_controller/lifecycle_auth.py"], REPO)
        assert got != ALL
        assert "tests/test_vm_lifecycle_auth.py" in got
        total = len(list((REPO / "tests").glob("test_*.py")))
        assert len(got) < total * 0.25, (
            f"a leaf module selected {len(got)}/{total} test files; if even leaf "
            f"changes cannot narrow, the graph is not buying anything"
        )

    def test_never_emits_a_path_pytest_cannot_collect(self):
        """The 2026-08-25 CI break: the e2e fixture's entrypoint was a target.

        ``tests/e2e/app/deterministic_provider/run.py`` imports ``provider`` as
        its container image lays it out, so importing it from the repo root
        raises ModuleNotFoundError and takes the whole run down at collection.
        """
        got = select(
            [
                "tests/e2e/__init__.py",
                "tests/e2e/app/harness.py",
                "tests/e2e/app/deterministic_provider/provider.py",
                "tests/e2e/app/deterministic_provider/run.py",
            ],
            REPO,
        )
        assert got != ALL
        assert "tests/e2e/app/deterministic_provider/run.py" not in got
        uncollectable = [p for p in got if not Path(p).name.startswith("test_")]
        assert not uncollectable, (
            f"selector emitted paths pytest will not collect on its own: "
            f"{uncollectable}"
        )
        # The helper's own tests still run — this narrows, it does not drop.
        assert "tests/e2e/app/test_harness.py" in got
        assert "tests/e2e/app/deterministic_provider/test_provider.py" in got

    def test_workspace_change_keeps_eager_consumers_and_always_run_tripwires(self):
        got = select(["src/agent/tools/workspace/files.py"], REPO)
        # Importing a module executes its eager package initializers. Losing
        # these edges made workspace tools look like a much narrower leaf.
        assert "tests/citation_engine/test_integration_postgres.py" in got
        assert "tests/cloud_mount/test_cloud_scan_guard.py" in got
        assert "tests/test_config_tool_grants_snapshot.py" in got
        assert "tests/test_tool_policy.py" in got
        assert "tests/test_endpoint_inventory.py" in got
        assert "tests/test_notification_producer_manifest.py" in got
        assert "tests/test_runtime_coordinate_inventory.py" in got
