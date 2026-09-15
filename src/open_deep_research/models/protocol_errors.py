"""Framework-independent model protocol errors shared by both engines."""


class MessageCodecError(ValueError):
    """Raised when a message cannot be represented by Gateway Wire V2."""


class ModelGatewayError(RuntimeError):
    """Sanitized terminal Gateway failure after LiteLLM has exhausted policy."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        provider_error_code: str | None = None,
    ) -> None:
        """Create a stable error without copying upstream response content."""
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        # Whitelisted identifier extracted from the upstream error payload (or
        # the canonical token-limit marker); never free-form provider text.
        self.provider_error_code = provider_error_code
