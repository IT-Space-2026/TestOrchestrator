"""KRE platform integration adapter.

Placeholder seam pending the corresponding specification sections. Web request
handlers must not perform this work inline; dispatch to a worker process.
"""


class NotConfiguredError(RuntimeError):
    """Raised when the integration is invoked before it is configured."""


class Adapter:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    def status(self) -> str:
        return "not-configured"
