"""Subscription authorization request contracts."""

from pydantic import BaseModel, Field


class CodexCallbackRequest(BaseModel):
    """Request body for manually completing a Codex OAuth callback (legacy)."""

    url: str | None = Field(
        None, description="Full callback URL from browser address bar"
    )
    code: str | None = Field(None, description="OAuth authorization code")
    state: str | None = Field(None, description="OAuth state parameter")


class SubscriptionLoginStart(BaseModel):
    """Request body for starting a subscription authorization flow."""

    provider: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "SRW provider key from GET /api/subscriptions/status "
            "(e.g. 'openai-codex', 'anthropic-claude-code')."
        ),
    )


class SubscriptionCallbackSubmit(BaseModel):
    """Request body for completing a browser OAuth flow by pasted URL.

    The URL is parsed for its ``code``/``state`` server-side; it is never used
    as a request destination.
    """

    url: str | None = Field(
        None, description="Full callback URL from the browser address bar"
    )
    code: str | None = Field(None, description="OAuth authorization code")
    state: str | None = Field(None, description="OAuth state parameter")
