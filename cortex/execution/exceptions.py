"""Backend-independent execution failures."""


class ExecutionError(RuntimeError):
    """An execution backend could not execute a request."""


class ExecutionUnavailableError(ExecutionError):
    """The selected execution environment is unavailable."""


class ExecutionTimeoutError(ExecutionError):
    """A request exceeded its deadline."""
