"""Tests for create_initial_state and UniversalAgentState.

Verifies all default values, metadata handling, and type correctness.
"""


from src.core.state import create_initial_state


class TestCreateInitialState:
    """Tests for create_initial_state."""

    def test_returns_typed_dict(self):
        """Should return a dict matching UniversalAgentState."""
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert isinstance(state, dict)

    def test_job_id_set(self):
        state = create_initial_state(job_id="abc-123", workspace_path="/ws")
        assert state["job_id"] == "abc-123"

    def test_workspace_path_set(self):
        state = create_initial_state(job_id="j1", workspace_path="/my/workspace")
        assert state["workspace_path"] == "/my/workspace"

    def test_messages_empty(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["messages"] == []

    def test_initialized_false(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["initialized"] is False

    def test_phase_complete_false(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["phase_complete"] is False

    def test_goal_achieved_false(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["goal_achieved"] is False

    def test_is_strategic_phase_true(self):
        """Agent should start in strategic mode."""
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["is_strategic_phase"] is True

    def test_phase_number_starts_at_one(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["phase_number"] == 1

    def test_is_final_phase_false(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["is_final_phase"] is False

    def test_iteration_zero(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["iteration"] == 0

    def test_should_stop_false(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["should_stop"] is False

    def test_consecutive_llm_errors_zero(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["consecutive_llm_errors"] == 0

    def test_workspace_memory_empty(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["workspace_memory"] == ""

    def test_error_is_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["error"] is None

    def test_context_stats_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["context_stats"] is None

    def test_tool_retry_state_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["tool_retry_state"] is None

    def test_phase_transition_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["phase_transition"] is None

    def test_todo_next_id_one(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["todo_next_id"] == 1

    def test_todos_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["todos"] is None

    def test_staged_todos_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["staged_todos"] is None

    def test_resume_feedback_none(self):
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["resume_feedback"] is None

    def test_metadata_defaults_to_empty(self):
        """None metadata should become empty dict."""
        state = create_initial_state(job_id="j1", workspace_path="/ws")
        assert state["metadata"] == {}

    def test_metadata_preserves_passed_dict(self):
        """Passed metadata should be stored."""
        md = {"document_path": "/data/doc.pdf", "prompt": "Extract data"}
        state = create_initial_state(job_id="j1", workspace_path="/ws", metadata=md)
        assert state["metadata"] == md
        assert state["metadata"]["document_path"] == "/data/doc.pdf"

    def test_metadata_none_explicit(self):
        """Explicitly passing None should produce empty dict."""
        state = create_initial_state(job_id="j1", workspace_path="/ws", metadata=None)
        assert state["metadata"] == {}
