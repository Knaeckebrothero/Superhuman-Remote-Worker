"""Cookie BFF for the cockpit.

See docs/features/auth_bff_and_api_tokens.md.
"""

from auth.bff import router as bff_router

__all__ = ["bff_router"]
