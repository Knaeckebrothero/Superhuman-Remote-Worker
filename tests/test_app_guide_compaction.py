"""The managed product guide remains callable after message compaction."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.context import ContextConfig, ContextManager
from src.core.expert_resolution import fence_skills_menu
from src.core.skill_resolution import (
    APP_GUIDE_LOADER_TOOL,
    add_persistent_system_skills,
)
from src.tools.context import ToolContext
from src.tools.product_help import create_product_help_tools


def test_app_guide_can_be_loaded_again_after_old_result_is_compacted(monkeypatch):
    monkeypatch.delenv("APP_GUIDE_BREAK_GLASS_DISABLED", raising=False)
    catalog = add_persistent_system_skills({})
    reader = create_product_help_tools(
        ToolContext(config={"_resolved_skills": catalog})
    )[0]
    old_guide_result = reader.invoke({"topic_id": "overview"})

    messages = [
        HumanMessage(content="What is SRW?", id="human-old"),
        AIMessage(
            content="",
            id="assistant-old",
            tool_calls=[
                {
                    "id": "guide-old",
                    "name": APP_GUIDE_LOADER_TOOL,
                    "args": {"topic_id": "overview"},
                }
            ],
        ),
        ToolMessage(
            content=old_guide_result,
            tool_call_id="guide-old",
            name=APP_GUIDE_LOADER_TOOL,
            id="tool-old",
        ),
        HumanMessage(content="What time is it?", id="human-recent"),
        AIMessage(
            content="",
            id="assistant-recent",
            tool_calls=[
                {
                    "id": "time-recent",
                    "name": "get_time",
                    "args": {},
                }
            ],
        ),
        ToolMessage(
            content="12:00 UTC",
            tool_call_id="time-recent",
            name="get_time",
            id="tool-recent",
        ),
    ]
    manager = ContextManager(
        ContextConfig(
            compaction_threshold_tokens=100_000,
            summarization_threshold_tokens=100_000,
            keep_recent_tool_results=1,
            keep_recent_messages=6,
            max_tool_result_length=50_000,
        )
    )

    compacted = manager.prepare_messages_for_llm(messages, aggressive=True)

    compacted_old = next(
        message
        for message in compacted
        if isinstance(message, ToolMessage) and message.tool_call_id == "guide-old"
    )
    assert compacted_old.content == manager.config.placeholder_text
    assert old_guide_result not in "\n".join(
        str(message.content) for message in compacted
    )
    assert manager.state.total_tool_results_cleared == 1

    # Production builds this fenced catalog block afresh for each model call;
    # it is not dependent on retaining an old ToolMessage in conversation.
    current_menu = fence_skills_menu(catalog["menu"])
    assert "- app-guide [load with read_product_guide(topic_id)]" in current_menu

    current_jobs = reader.invoke({"topic_id": "jobs"})
    assert "[product guide topic: jobs]" in current_jobs
    assert "Some pause reasons are retried or redispatched" in current_jobs
    assert "Folder allowlist" not in current_jobs
    assert "OKF Root Path" not in current_jobs
