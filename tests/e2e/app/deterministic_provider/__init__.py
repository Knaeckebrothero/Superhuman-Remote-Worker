"""Deterministic OpenAI-compatible provider for full-stack E2E journeys.

This package is deliberately test-owned.  It is deployed only by the application
E2E harness and is never imported by the production orchestrator or agent runtime.
"""

from tests.e2e.app.deterministic_provider.provider import (
    CHAT_MODEL_ID,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_ID,
    RERANK_MODEL_ID,
    ScenarioStore,
    create_control_app,
    create_inference_app,
)

__all__ = [
    "CHAT_MODEL_ID",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL_ID",
    "RERANK_MODEL_ID",
    "ScenarioStore",
    "create_control_app",
    "create_inference_app",
]
