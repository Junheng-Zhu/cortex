import asyncio
import hashlib
import time
from cortex.tools.base import Tool, ExecutionStrategy
from cortex.tools.executor import ToolResult
from .schema import input_model


def tool_name(server_id: str, remote_name: str) -> str:
    # Full tuple is retained on the adapter for reverse lookup. Hash avoids both
    # punctuation normalization collisions and provider name/length restrictions.
    digest = hashlib.sha256((server_id + '\0' + remote_name).encode()).hexdigest()[:32]
    return f'mcp_{server_id}_{digest}'


class MCPToolAdapter(Tool):
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED
    retryable = False
    max_retries = 0
    preserve_input_schema = True
    accepts_runtime_timeout = False

    def __init__(self, connection, remote, grant):
        self.connection = connection
        self.server_id = connection.config.server_id
        self.remote_tool_name = remote.name
        self.name = tool_name(self.server_id, remote.name)
        self.description = remote.description or ''
        self.input_model = input_model(remote.inputSchema)
        self.permission = grant.permission
        self.side_effect_policy = grant.side_effect_policy
        self.timeout = connection.config.call_timeout

    def on_permission_denied(self):
        self.connection.event("execution", remote_tool_name=self.remote_tool_name, status="permission_denied")

    def execute(self, input):
        raise RuntimeError('MCP tools require Agent.arun / ToolExecutor.aexecute')

    async def aexecute(self, input):
        return await self.call(input, self.timeout)

    async def aexecute_with_timeout(self, input, timeout):
        return await self.call(input, timeout)

    async def call(self, input, timeout):
        started = time.perf_counter()
        status = 'success'
        data = None
        error_type = error_message = None
        try:
            if self.connection.state != 'ready' or self.connection.session is None:
                raise RuntimeError('Unavailable')
            async with asyncio.timeout(min(timeout, self.timeout)):
                result = await self.connection.session.call_tool(self.remote_tool_name,
                    input.model_dump(exclude_unset=True))
            data = {'content': [c.model_dump(mode='json', by_alias=True, exclude_none=True) for c in result.content],
                    'structuredContent': result.structuredContent, 'isError': result.isError}
            if result.isError:
                status, error_type, error_message = 'tool_error', 'MCPToolError', 'Remote tool reported an error'
        except asyncio.CancelledError:
            status = 'cancelled'
            await self.connection.aclose()
            raise
        except TimeoutError:
            status, error_type, error_message = 'timeout', 'ToolTimeoutError', 'MCP tool timed out'
            await self.connection.aclose()
        except Exception:
            status, error_type, error_message = 'transport_error', 'MCPTransportError', 'MCP transport unavailable'
            await self.connection.aclose()
        finally:
            self.connection.event('execution', remote_tool_name=self.remote_tool_name,
                execution_duration_ms=int((time.perf_counter() - started) * 1000), status=status)
        # Explicit env values are never reflected in remote results.
        def redact(value):
            if isinstance(value, str):
                for secret in self.connection.config.env.values():
                    if secret.get_secret_value():
                        value = value.replace(secret.get_secret_value(), '<redacted>')
            elif isinstance(value, dict):
                return {redact(k): redact(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [redact(v) for v in value]
            return value
        return ToolResult(self.name, 1, int((time.perf_counter() - started) * 1000),
                          status == 'success', error_type, error_message, redact(data))
