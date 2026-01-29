"""Custom exceptions for LLM layer."""


class ContextOverflowError(Exception):
    """Raised when HTTP request body exceeds model context limit.

    This exception is raised at the httpx layer when the actual request
    being sent to the LLM API would exceed the model's context window.
    It's the lowest-level safety check that catches overflow including
    tool definitions and request overhead.

    Attributes:
        token_count: Actual tokens counted in the request
        limit: Maximum allowed tokens
        request_size_bytes: Size of request body in bytes
    """

    def __init__(
        self,
        token_count: int,
        limit: int,
        request_size_bytes: int = 0,
        message: str = None,
    ):
        self.token_count = token_count
        self.limit = limit
        self.request_size_bytes = request_size_bytes
        self.message = message or (
            f"Request body has {token_count:,} tokens, "
            f"exceeds model limit of {limit:,}"
        )
        super().__init__(self.message)
