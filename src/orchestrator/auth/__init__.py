"""Cookie BFF for the cockpit.

See knowledge-base/knowledge/features/auth_bff_and_api_tokens.md.
"""

from orchestrator.auth.bff import router as bff_router

__all__ = ["bff_router"]
