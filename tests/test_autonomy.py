"""Unit tests for the autonomy level system.

Tests:
- should_freeze_at_boundary() for all 5 levels x phase types x phase numbers
- Config parsing: autonomy is parsed from YAML, invalid values default to "partial"
- Config serialization round-trip through serialize_resolved_config / load_config_from_resolved
- freeze_for_review() produces correct output
- finalize_job() handles full vs other autonomy levels
- approve_frozen_job() handles freeze_type: phase_boundary vs job_complete
"""

import json
import pytest
import shutil
import subprocess
from unittest.mock import MagicMock, patch

from agent.core.phase import (  # noqa: E402
    should_freeze_at_boundary,
    freeze_for_review,
    finalize_job,
)
from shared.runtime.core.loader import (  # noqa: E402
    AgentConfig,
    VALID_AUTONOMY_LEVELS,
    load_agent_config_from_dict,
)
from agent.tools.core.job import _final_phase_data  # noqa: E402


# =============================================================================
# Fixtures
# =============================================================================


def make_config(autonomy: str = "partial") -> AgentConfig:
    """Create a minimal AgentConfig with given autonomy level."""
    return AgentConfig(
        agent_id="test",
        display_name="Test Agent",
        autonomy=autonomy,
    )


def make_state(job_id: str = "test-job", phase_number: int = 1) -> dict:
    """Create a minimal agent state dict."""
    return {
        "job_id": job_id,
        "phase_number": phase_number,
        "is_strategic_phase": True,
        "messages": [],
    }


# =============================================================================
# should_freeze_at_boundary() tests
# =============================================================================


class TestShouldFreezeAtBoundary:
    """Test the core freeze decision function for all autonomy levels."""

    # --- full: never freezes at boundaries ---

    def test_full_strategic_phase_1(self):
        config = make_config("full")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1)
            is False
        )

    def test_full_strategic_phase_3(self):
        config = make_config("full")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=3)
            is False
        )

    def test_full_tactical_phase_2(self):
        config = make_config("full")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is False
        )

    # --- review: never freezes at boundaries (only at job_complete, handled separately) ---

    def test_review_strategic_phase_1(self):
        config = make_config("review")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1)
            is False
        )

    def test_review_strategic_phase_5(self):
        config = make_config("review")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=5)
            is False
        )

    def test_review_tactical_phase_2(self):
        config = make_config("review")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is False
        )

    # --- partial: freezes only at first strategic phase boundary ---

    def test_partial_strategic_phase_0(self):
        """phase_number=0 is the actual first strategic phase (set by init_workspace)."""
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=0) is True
        )

    def test_partial_strategic_phase_1(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_partial_strategic_phase_2(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=2)
            is False
        )

    def test_partial_strategic_phase_3(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=3)
            is False
        )

    def test_partial_tactical_phase_1(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=1)
            is False
        )

    def test_partial_tactical_phase_2(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is False
        )

    # --- guided: freezes at every strategic phase boundary ---

    def test_guided_strategic_phase_1(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_guided_strategic_phase_2(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=2) is True
        )

    def test_guided_strategic_phase_5(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=5) is True
        )

    def test_guided_tactical_phase_1(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=1)
            is False
        )

    def test_guided_tactical_phase_3(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=3)
            is False
        )

    # --- dependent: freezes at every phase boundary ---

    def test_dependent_strategic_phase_1(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_dependent_strategic_phase_3(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=3) is True
        )

    def test_dependent_tactical_phase_1(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=1)
            is True
        )

    def test_dependent_tactical_phase_2(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is True
        )

    # --- Edge cases ---

    def test_missing_autonomy_attribute(self):
        """Config without autonomy attr defaults to partial behavior."""
        config = MagicMock(spec=[])  # No attributes
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_unknown_autonomy_value(self):
        """Unknown autonomy value returns False (safe fallback)."""
        config = MagicMock()
        config.autonomy = "unknown_value"
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1)
            is False
        )


# =============================================================================
# freeze_for_review() tests
# =============================================================================


class TestFreezeForReview:
    """Test the phase boundary freeze function."""

    def test_freeze_writes_json(self):
        """freeze_for_review writes job_frozen.json with correct freeze_type."""
        state = make_state()
        workspace = MagicMock()
        todo_manager = MagicMock()

        result = freeze_for_review(state, workspace, todo_manager, "strategic", 1)

        assert result.success is True
        assert result.state_updates["should_stop"] is True
        assert result.state_updates["goal_achieved"] is False

        # Verify workspace.write_file was called with correct path and data
        workspace.write_file.assert_called_once()
        call_args = workspace.write_file.call_args
        assert call_args[0][0] == "output/job_frozen.json"
        frozen_data = json.loads(call_args[0][1])
        assert frozen_data["freeze_type"] == "phase_boundary"
        assert frozen_data["phase_type"] == "strategic"
        assert frozen_data["phase_number"] == 1
        assert frozen_data["status"] == "pending_review"

    def test_freeze_tactical_phase(self):
        """freeze_for_review works for tactical phases too."""
        state = make_state(phase_number=3)
        workspace = MagicMock()
        todo_manager = MagicMock()

        freeze_for_review(state, workspace, todo_manager, "tactical", 3)

        call_args = workspace.write_file.call_args
        frozen_data = json.loads(call_args[0][1])
        assert frozen_data["phase_type"] == "tactical"
        assert frozen_data["phase_number"] == 3

    def test_freeze_archives_todos(self):
        """freeze_for_review archives todos."""
        state = make_state()
        workspace = MagicMock()
        todo_manager = MagicMock()

        freeze_for_review(state, workspace, todo_manager, "strategic", 1)

        todo_manager.archive.assert_called_once_with("phase_1_strategic")


# =============================================================================
# Config parsing tests
# =============================================================================


class TestConfigParsing:
    """Test autonomy level parsing from config data."""

    def test_default_autonomy(self):
        """AgentConfig defaults to 'partial' autonomy."""
        config = AgentConfig(agent_id="test", display_name="Test")
        assert config.autonomy == "partial"

    def test_parse_valid_autonomy_from_dict(self):
        """load_agent_config_from_dict parses autonomy correctly."""
        for level in VALID_AUTONOMY_LEVELS:
            data = {
                "agent_id": "test",
                "display_name": "Test",
                "autonomy": level,
            }
            config = load_agent_config_from_dict(data)
            assert config.autonomy == level, f"Failed for level: {level}"

    def test_invalid_autonomy_defaults_to_partial(self):
        """Invalid autonomy value defaults to 'partial'."""
        data = {
            "agent_id": "test",
            "display_name": "Test",
            "autonomy": "invalid_level",
        }
        config = load_agent_config_from_dict(data)
        assert config.autonomy == "partial"

    def test_missing_autonomy_defaults_to_partial(self):
        """Missing autonomy key defaults to 'partial'."""
        data = {
            "agent_id": "test",
            "display_name": "Test",
        }
        config = load_agent_config_from_dict(data)
        assert config.autonomy == "partial"

    def test_autonomy_not_in_extra(self):
        """Autonomy field should NOT leak into extra dict."""
        data = {
            "agent_id": "test",
            "display_name": "Test",
            "autonomy": "guided",
        }
        config = load_agent_config_from_dict(data)
        assert "autonomy" not in config.extra

    def test_autonomy_survives_dataclass_asdict(self):
        """Autonomy field is captured by dataclasses.asdict()."""
        import dataclasses

        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            autonomy="guided",
        )
        d = dataclasses.asdict(config)
        assert d["autonomy"] == "guided"


# =============================================================================
# Serialization round-trip tests
# =============================================================================


class TestSerializationRoundTrip:
    """Test that autonomy survives serialize_resolved_config / load_config_from_resolved."""

    def test_round_trip(self):
        """Autonomy is preserved through serialize → load round-trip."""
        from shared.runtime.core.loader import (
            serialize_resolved_config,
            load_config_from_resolved,
        )

        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            autonomy="guided",
        )

        # serialize_resolved_config needs prompt files, so we mock the resolvers
        with (
            patch("shared.runtime.core.loader.PromptMatrixResolver") as mock_prompt,
            patch("shared.runtime.core.loader.InstructionMatrixResolver") as mock_instr,
        ):
            # Mock resolver to return dummy content
            mock_prompt_instance = MagicMock()
            mock_prompt_instance.load.return_value = "dummy prompt"
            mock_prompt.return_value = mock_prompt_instance

            mock_instr_instance = MagicMock()
            mock_instr_instance.load.return_value = "dummy instruction"
            mock_instr.return_value = mock_instr_instance

            resolved = serialize_resolved_config(config, model="gpt-4o")

        # Verify autonomy is in the serialized dict
        assert resolved["agent"]["autonomy"] == "guided"

        # Load back
        restored = load_config_from_resolved(resolved)
        assert restored.autonomy == "guided"

    def test_round_trip_default(self):
        """Default autonomy 'partial' is preserved through round-trip."""
        from shared.runtime.core.loader import load_config_from_resolved

        # Simulate a resolved config dict
        resolved = {
            "agent": {
                "agent_id": "test",
                "display_name": "Test",
                "autonomy": "partial",
                "llm": {"model": "gpt-4o"},
                "workspace": {},
                "tools": {},
                "connections": {},
                "limits": {},
                "context_management": {},
                "phase_settings": {},
                "instruction_files": [],
                "extra": {},
            },
            "prompts": {},
            "instructions": {},
        }

        restored = load_config_from_resolved(resolved)
        assert restored.autonomy == "partial"


# =============================================================================
# finalize_job() head_commit tests (Task 6 amendment item 1)
# =============================================================================


class TestFinalizeJobHeadCommit:
    """``head_commit`` must reach the completion freeze.

    Nothing else writes ``head_commit`` into a job's ``freeze_data``.
    Without this, ``_verification_gate_decision``'s no-progress
    short-circuit (``orchestrator/main.py``) can never fire in production —
    the exact defect the amendment identified: unit tests for the decision
    function pass ``head_commit`` in explicitly and stay green while the
    real wiring is dead. See knowledge-base/knowledge/superpowers/specs/
    2026-07-27-verification-fail-closed-design.md.
    """

    def setup_method(self):
        _final_phase_data.clear()

    @staticmethod
    def _seed_final_data(job_id: str = "test-job") -> None:
        _final_phase_data[job_id] = {
            "summary": "done",
            "deliverables": ["output/report.md"],
            "confidence": 0.9,
            "job_id": job_id,
        }

    def test_partial_autonomy_freeze_carries_head_commit(self):
        """The 'freeze for human review' path (non-full autonomy)."""
        state = make_state()
        ws = MagicMock()
        ws.git_manager = None
        ws.get_head_commit = MagicMock(return_value="abc1234")
        tm = MagicMock()
        self._seed_final_data()

        result = finalize_job(state, ws, tm, config=make_config("partial"))

        assert result.freeze_data["head_commit"] == "abc1234"

    def test_full_autonomy_completion_carries_head_commit(self):
        """The auto-complete path (autonomy=full) — a DIFFERENT dict literal
        in finalize_job than the partial-autonomy freeze above. Verification
        can still gate a full-autonomy target (autonomy only decides what
        happens AFTER a critic approves), so this freeze needs head_commit
        too — not just the pending_review one."""
        state = make_state()
        ws = MagicMock()
        ws.git_manager = None
        ws.get_head_commit = MagicMock(return_value="def5678")
        tm = MagicMock()
        self._seed_final_data()

        result = finalize_job(state, ws, tm, config=make_config("full"))

        assert result.freeze_data["head_commit"] == "def5678"

    def test_head_commit_failure_is_swallowed_and_completion_still_succeeds(self):
        """Best-effort: a git failure at freeze time must never break job
        completion — it must only cost the no-progress detection a round."""
        state = make_state()
        ws = MagicMock()
        ws.git_manager = None
        ws.get_head_commit = MagicMock(side_effect=RuntimeError("no repo"))
        tm = MagicMock()
        self._seed_final_data()

        result = finalize_job(state, ws, tm, config=make_config("partial"))

        assert result.success is True
        assert result.freeze_data["head_commit"] is None


class TestFinalizeJobContentTree:
    """``content_tree`` is what the no-progress guard actually compares.

    Driven against a REAL git repo and a REAL ``TodoManager``, for three
    rounds, with nothing changed in between. All three recorded values must be
    EQUAL, or the guard is inert again.

    **Every collaborator that writes to the workspace must be real here.** This
    guard has now been found inert three times, and each time the test that was
    supposed to catch it had stubbed out whatever moved the tree:

    1. ``head_commit`` was read BEFORE a ``commit(..., allow_empty=True)`` that
       runs unconditionally — hidden behind a hand-built freeze dict.
    2. ``rev-parse HEAD^{tree}`` counted ``output/job_frozen.json``, whose
       ``timestamp`` is fresh every round — hidden behind a two-round test.
    3. ``TodoManager.archive`` writes ``archive/todos_phase_{N}_{type}_{TS}.md``
       — a NEW filename every round — from ``finalize_job`` itself, AFTER the
       hash is captured, so round N's archive is staged by round N+1's
       ``git add -A``. Hidden behind ``tm = MagicMock()``.

    So: no mocks on the workspace side, and three rounds rather than two,
    because (3) only shows up from the second round onward.
    """

    def setup_method(self):
        _final_phase_data.clear()

    @staticmethod
    def _seed_final_data(job_id: str = "test-job") -> None:
        _final_phase_data[job_id] = {
            "summary": "done",
            "deliverables": ["output/report.md"],
            "confidence": 0.9,
            "job_id": job_id,
        }

    @staticmethod
    def _make_real_workspace(base):
        from agent.core.workspace import WorkspaceManager, WorkspaceManagerConfig
        from tests._fs_backend import FilesystemTestBackend

        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["output/", "archive/"],
                git_versioning=True,
            ),
            base_path=base,
            backend=FilesystemTestBackend(base),
        )
        ws.initialize()
        ws.write_file("output/report.md", "the deliverable")
        ws.git_manager.commit("deliverable")
        return ws

    @staticmethod
    def _real_todo_manager(ws):
        from agent.managers.todo import TodoManager

        return TodoManager(ws)

    @pytest.mark.skipif(
        shutil.which("git") is None, reason="Git not available on system"
    )
    @pytest.mark.parametrize("autonomy", ["partial", "full"])
    def test_three_rounds_with_no_content_change_record_the_same_value(
        self, tmp_path, autonomy
    ):
        ws = self._make_real_workspace(tmp_path)
        tm = self._real_todo_manager(ws)

        trees = []
        heads = []
        for _ in range(3):
            # What a real agent leaves behind before completing: todos that
            # finalize_job will archive into the workspace.
            tm.add("do the thing")
            tm.add("do the other thing")
            self._seed_final_data()
            freeze = finalize_job(
                make_state(), ws, tm, config=make_config(autonomy)
            ).freeze_data
            trees.append(freeze["content_tree"])
            heads.append(freeze["head_commit"])

        assert all(trees), "no content_tree recorded at all"
        assert len(set(trees)) == 1, (
            "the no-progress guard is inert: three rounds over identical "
            f"content recorded different values: {trees}"
        )
        # Contrast: the commit SHA moves every round because both freeze
        # branches commit with allow_empty=True. That is why the guard cannot
        # key on it.
        assert len(set(heads)) == 3

    @pytest.mark.skipif(
        shutil.which("git") is None, reason="Git not available on system"
    )
    def test_a_todo_archive_alone_does_not_look_like_progress(self, tmp_path):
        """Narrow regression for defect (3): the ONLY thing that changes
        between these two rounds is a todo archive.

        Round 1 has todos, so ``finalize_job`` archives them — but it does so
        AFTER its own commit, so the archive is still untracked when round 1's
        hash is taken. Round 2 adds no todos and changes no content; its
        ``git add -A`` is what stages round 1's archive. The two hashes
        therefore differ by exactly one ``archive/`` entry and nothing else.
        """
        ws = self._make_real_workspace(tmp_path)
        tm = self._real_todo_manager(ws)

        tm.add("a todo that will be archived into the workspace")
        self._seed_final_data()
        first = finalize_job(
            make_state(), ws, tm, config=make_config("partial")
        ).freeze_data

        self._seed_final_data()
        second = finalize_job(
            make_state(), ws, tm, config=make_config("partial")
        ).freeze_data

        # Sanity: the archive really did land in the repo, so this test is
        # exercising the exclusion rather than a workspace where nothing
        # happened at all.
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=ws.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert any(p.startswith("archive/") for p in listing.splitlines()), (
            "no todo archive was committed — this test would pass vacuously"
        )

        assert first["content_tree"] == second["content_tree"]

    @staticmethod
    def _simulate_return_resume(ws, open_findings):
        """The step every earlier test of this guard was missing.

        A *returned* verification round does not just loop ``finalize_job``:
        the orchestrator resumes the target with feedback
        (``_internal_resume_job`` in orchestrator/main.py), and the resume node
        writes that feedback into the workspace ROOT as ``feedback.md``
        (``restore_from_feedback`` in src/graph.py). The next round's
        ``git add -A`` then commits it.

        Both string templates are replicated verbatim from those two call
        sites. Byte-exactness is not what the assertion depends on — any file
        at a new tracked path moves the hash — but realistic content keeps the
        test honest about what production actually writes.
        """
        # orchestrator/main.py, _handle_critic_verdict_on_complete
        lines = ["## Open findings", ""]
        for f in sorted(open_findings, key=lambda x: x.get("id", "")):
            lines.append(
                f"- **{f['id']}** [{f.get('severity', 'unknown')}]: "
                f"{f.get('claim', '')}"
            )
        feedback = "\n".join(lines)

        # src/graph.py, restore_from_feedback step 2
        ws.write_file(
            "feedback.md",
            f"# Human Feedback\n\n"
            f"Received when resuming frozen job.\n\n"
            f"## Feedback\n\n"
            f"{feedback}\n",
        )

    @pytest.mark.skipif(
        shutil.which("git") is None, reason="Git not available on system"
    )
    def test_a_full_return_round_does_not_look_like_progress(self, tmp_path):
        """THE round shape the guard actually has to survive.

        complete -> capture -> critic RETURNS -> resume writes feedback.md ->
        complete -> capture, with the deliverable byte-identical throughout.

        Every previous test of this guard looped ``finalize_job`` directly and
        never performed the resume, which is precisely why ``feedback.md``
        survived three rounds of fixes: it is written between the two captures,
        so it is a NEW tracked path in the second one. With it counted, T1 was
        never equal to T2 and the guard could not fire on the first repeat —
        only from the second consecutive identical return onward.
        """
        findings = [{"id": "F1", "severity": "high", "claim": "missing tests"}]
        ws = self._make_real_workspace(tmp_path)
        tm = self._real_todo_manager(ws)

        trees = []
        for _ in range(3):
            tm.add("attempt to address the feedback")
            self._seed_final_data()
            trees.append(
                finalize_job(
                    make_state(), ws, tm, config=make_config("partial")
                ).freeze_data["content_tree"]
            )
            # The critic returns; the target is resumed with the open findings.
            self._simulate_return_resume(ws, findings)

        # Sanity: feedback.md really is tracked, or this passes vacuously.
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=ws.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert "feedback.md" in listing, (
            "feedback.md was never committed — this test would pass vacuously"
        )

        assert trees[0] == trees[1], (
            "the guard cannot fire on the FIRST repeated return: the resume "
            f"wrote a file that moved the hash. {trees[0]} != {trees[1]}"
        )
        assert len(set(trees)) == 1, f"hashes diverged across rounds: {trees}"

    @pytest.mark.skipif(
        shutil.which("git") is None, reason="Git not available on system"
    )
    def test_a_real_content_change_moves_the_value(self, tmp_path):
        """Contrast case: an implementation returning a constant would pass
        the test above."""
        ws = self._make_real_workspace(tmp_path)
        tm = MagicMock()

        self._seed_final_data()
        first = finalize_job(
            make_state(), ws, tm, config=make_config("partial")
        ).freeze_data

        ws.write_file("output/report.md", "the deliverable, now improved")

        self._seed_final_data()
        second = finalize_job(
            make_state(), ws, tm, config=make_config("partial")
        ).freeze_data

        assert first["content_tree"] != second["content_tree"]

    def test_content_tree_failure_is_swallowed(self):
        """Best-effort, exactly like head_commit: a git failure at freeze time
        must never break job completion."""
        state = make_state()
        ws = MagicMock()
        ws.git_manager = None
        ws.get_head_commit = MagicMock(return_value="abc1234")
        ws.get_content_tree = MagicMock(side_effect=RuntimeError("no repo"))
        tm = MagicMock()
        self._seed_final_data()

        result = finalize_job(state, ws, tm, config=make_config("partial"))

        assert result.success is True
        assert result.freeze_data["content_tree"] is None


# =============================================================================
# Comprehensive autonomy matrix test
# =============================================================================


class TestAutonomyMatrix:
    """Exhaustive test of the autonomy decision matrix from the design doc.

    | Level      | After 1st Strategic | After Nth Strategic | After Tactical | After job_complete |
    |------------|:---:|:---:|:---:|:---:|
    | full       | -   | -   | -   | auto-complete      |
    | review     | -   | -   | -   | freeze             |
    | partial    | freeze | - | -   | freeze             |
    | guided     | freeze | freeze | - | freeze           |
    | dependent  | freeze | freeze | freeze | freeze       |
    """

    @pytest.mark.parametrize(
        "level,is_strategic,phase_number,expected",
        [
            # full: never freeze at boundaries
            ("full", True, 0, False),
            ("full", True, 1, False),
            ("full", True, 2, False),
            ("full", True, 5, False),
            ("full", False, 1, False),
            ("full", False, 3, False),
            # review: never freeze at boundaries
            ("review", True, 1, False),
            ("review", True, 3, False),
            ("review", False, 1, False),
            ("review", False, 2, False),
            # partial: only first strategic (phase_number 0 or 1)
            ("partial", True, 0, True),  # init_workspace sets phase_number=0
            ("partial", True, 1, True),  # state default is phase_number=1
            ("partial", True, 2, False),
            ("partial", True, 5, False),
            ("partial", False, 0, False),
            ("partial", False, 1, False),
            ("partial", False, 3, False),
            # guided: all strategic
            ("guided", True, 1, True),
            ("guided", True, 2, True),
            ("guided", True, 5, True),
            ("guided", False, 1, False),
            ("guided", False, 3, False),
            # dependent: all phases
            ("dependent", True, 1, True),
            ("dependent", True, 3, True),
            ("dependent", False, 1, True),
            ("dependent", False, 3, True),
        ],
    )
    def test_autonomy_decision(self, level, is_strategic, phase_number, expected):
        config = make_config(level)
        result = should_freeze_at_boundary(config, is_strategic, phase_number)
        assert result is expected, (
            f"level={level}, is_strategic={is_strategic}, "
            f"phase_number={phase_number}: expected {expected}, got {result}"
        )
