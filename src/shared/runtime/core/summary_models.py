"""Structured summary schemas shared by compaction and auxiliary LLM tasks."""

from typing import List

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IdentityAnchor(BaseModel):
    """Identity persistence payload for deterministic compaction stitching."""

    model_config = ConfigDict(extra="forbid")

    agent_role: str = ""
    current_task: str = ""
    active_constraints: List[str] = Field(default_factory=list)


class ConversationSummary(BaseModel):
    """Structured summary — forces the model to stop after valid JSON.

    All string fields accept List[str] as well, because some models
    (MiniMax, Kimi K2, etc.) return arrays for list-like content.
    A model_validator coerces any remaining list/dict values to strings
    as a catch-all safety net.
    """

    summary: str | List[str] = Field(
        description="General overview of the conversation and what happened"
    )
    tasks_completed: str | List[str] = Field(
        description="Bullet-point list of completed tasks"
    )
    tasks_in_progress: str | List[str] = Field(
        default="", description="Tasks started but not finished"
    )
    key_decisions: str | List[str] = Field(description="Important decisions made")
    current_state: str | List[str] = Field(
        description="Current progress and immediate next steps"
    )
    blockers: str | List[str] = Field(
        default="", description="Errors or blockers encountered, empty if none"
    )
    critical_facts: str | List[str] = Field(
        default="",
        description="Exact identifiers, file paths, error messages, URLs, version numbers, and configuration values that must survive compression verbatim",
    )
    state_changes: str | List[str] = Field(
        default="", description="Files created, modified, or deleted during this period"
    )
    pinned_instructions: str | List[str] = Field(
        default="", description="Rules from instructions/config that must persist"
    )
    identity_anchor: IdentityAnchor | str | List[str] = Field(
        default="",
        description="Agent role, current task, and active constraints for identity persistence",
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_all_fields(cls, data):
        """Catch-all: coerce any list/unexpected type to string.

        Some models ignore the JSON schema and return arrays instead of
        strings. Rather than enumerating every field in separate validators,
        handle all string fields uniformly. Dicts on identity_anchor pass
        through since they have special downstream handling.
        """
        if not isinstance(data, dict):
            return data
        for key, value in data.items():
            if key == "identity_anchor" and isinstance(value, dict):
                data[key] = IdentityAnchor(**value)
                continue
            if isinstance(value, list):
                data[key] = "\n".join(
                    f"- {item}" if isinstance(item, str) else f"- {item}"
                    for item in value
                )
        return data
