"""Prevent official SDK diagnostics from reflecting untrusted wire data."""
import logging


class _SafeSDKDiagnostic(logging.Filter):
    def filter(self, record):
        record.msg = 'MCP SDK diagnostic (wire data and exception details withheld)'
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def protect_sdk_logs():
    # SDK parse failures include raw JSON in Pydantic exception tracebacks.
    # Apply only to SDK loggers, preserving the application's logger behavior.
    for name in ('mcp.client.stdio', 'mcp.client.session', 'mcp.shared.session',
                 'mcp.os.posix.utilities', 'mcp.os.win32.utilities'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _SafeSDKDiagnostic) for f in logger.filters):
            logger.addFilter(_SafeSDKDiagnostic())
