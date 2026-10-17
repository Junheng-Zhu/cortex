"""Official SDK stdio session with a single task owning its context managers."""
import asyncio
import os
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .logging import protect_sdk_logs


class MCPConnectionError(RuntimeError):
    pass


class MCPConnection:
    def __init__(self, config, recorder=None):
        protect_sdk_logs()
        self.config = config
        self.recorder = recorder
        self.state = 'new'
        self.session = None
        self.tools = []
        self.task = None
        self.stop = asyncio.Event()
        self.ready = None
        self.connection_duration_ms = 0

    def event(self, event_type, **data):
        def safe(value):
            if isinstance(value, str):
                for secret in self.config.env.values():
                    if secret.get_secret_value():
                        value = value.replace(secret.get_secret_value(), '<redacted>')
            return value
        data = {key: safe(value) for key, value in data.items()}
        if self.recorder:
            self.recorder.record('mcp_' + event_type, server_id=self.config.server_id, **data)

    async def start(self):
        if self.state != 'new':
            raise MCPConnectionError('MCP connection cannot be restarted')
        self.state = 'connecting'
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._own_session(), name='mcp-' + self.config.server_id)
        try:
            if not await asyncio.wait_for(asyncio.shield(self.ready), self.config.connect_timeout):
                raise MCPConnectionError("MCP server initialization failed")
        except BaseException as exc:
            await self.aclose()
            self.state = 'failed'
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.event('connection', status='timeout' if isinstance(exc, TimeoutError) else 'transport_error')
            raise MCPConnectionError('MCP server initialization failed') from None

    async def _own_session(self):
        started = time.perf_counter()
        try:
            # SDK uses a small platform-specific inherited environment, plus these
            # explicit overrides. Raw stderr never enters logs/traces.
            params = StdioServerParameters(command=self.config.command, args=self.config.args,
                env={k: v.get_secret_value() for k, v in self.config.env.items()}, cwd=self.config.cwd)
            with open(os.devnull, 'w') as errlog:
                async with stdio_client(params, errlog=errlog) as streams:
                    async with ClientSession(*streams) as session:
                        self.session = session
                        await session.initialize()
                        cursor = None
                        seen = set()
                        names = set()
                        while True:
                            page = await session.list_tools(cursor=cursor)
                            for tool in page.tools:
                                if tool.name in names:
                                    raise MCPConnectionError('Duplicate remote tool')
                                names.add(tool.name)
                                self.tools.append(tool)
                            cursor = page.nextCursor
                            if not cursor:
                                break
                            if cursor in seen:
                                raise MCPConnectionError('Repeated discovery cursor')
                            seen.add(cursor)
                        self.connection_duration_ms = int((time.perf_counter() - started) * 1000)
                        self.state = 'ready'
                        self.event('connection', status='ready', discovery_count=len(self.tools),
                                   connection_duration_ms=self.connection_duration_ms)
                        self.ready.set_result(True)
                        await self.stop.wait()
        except BaseException:
            self.state = 'failed'
            if not self.ready.done():
                self.ready.set_result(False)
            self.event('connection', status='transport_error')
        finally:
            self.session = None
            if not self.ready.done():
                self.ready.set_result(False)

    async def aclose(self):
        if self.task is None:
            self.state = 'closed'
            return
        self.stop.set()
        # All SDK context exits occur in _own_session, never in another task.
        if not self.ready.done():
            self.task.cancel()
        await asyncio.shield(self.task)
        self.session = None
        self.state = 'closed'
