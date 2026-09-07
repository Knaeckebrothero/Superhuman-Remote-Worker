"""Prompt and configuration override request contracts."""

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class ConfigOverrideCreate(BaseModel):
    """Request body for creating or replacing a config override.

    ``kind`` selects the config subsection. Text kinds (prompts, instructions)
    populate ``content``; structured kinds (settings, guardrails) populate
    ``value_json``. ``name`` is the resolver entry_type / settings leaf (dotted
    for limits, e.g. 'limits.context_threshold_tokens'). ``family=None`` means a
    global default.
    """

    family: str | None = Field(None, max_length=64)
    kind: Literal["prompts", "instructions", "settings", "guardrails"]
    name: str = Field(..., min_length=1, max_length=128)
    content: str | None = Field(None, min_length=1)
    content_format: Literal["text", "markdown", "jinja", "yaml"] = "text"
    value_json: Any = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> "ConfigOverrideCreate":
        """Enforce the content/value_json XOR by kind (mirrors the DB check)."""
        if self.kind in ("prompts", "instructions"):
            if self.content is None:
                raise ValueError(f"{self.kind} override requires 'content'")
            if self.value_json is not None:
                raise ValueError(f"{self.kind} override must not set 'value_json'")
        else:  # settings, guardrails
            if self.value_json is None:
                raise ValueError(f"{self.kind} override requires 'value_json'")
            if self.content is not None:
                raise ValueError(f"{self.kind} override must not set 'content'")
        return self


class ConfigOverrideUpdate(BaseModel):
    """Update an existing override's payload; family/kind/name are immutable.

    The acting kind is taken from the stored row, so the route picks ``content``
    (text kinds) or ``value_json`` (structured kinds).
    """

    content: str | None = Field(None, min_length=1)
    content_format: Literal["text", "markdown", "jinja", "yaml"] = "text"
    value_json: Any = None
    notes: str | None = None
