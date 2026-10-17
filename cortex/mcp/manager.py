from cortex.observability.tracer import RunRecorder
from .client import MCPConnection, MCPConnectionError
from .adapter import MCPToolAdapter
from .schema import UnsupportedSchema


class MCPManager:
    """Owns server sessions and only its own registry registrations."""
    def __init__(self, config, recorder=None):
        self.config = config
        self.recorder = recorder if recorder is not None else RunRecorder()
        self.connections = {}
        self.adapters = {}
        self.registry = None
        self.started = False

    async def start(self, registry, allowed_permissions):
        if self.started:
            raise RuntimeError('MCPManager already started')
        self.started = True
        self.registry = registry
        if not self.config.enabled:
            return
        try:
            for server in self.config.servers:
                if server.server_id not in self.config.allowed_servers:
                    continue
                connection = MCPConnection(server, self.recorder)
                self.connections[server.server_id] = connection
                try:
                    await connection.start()
                    if connection.state != 'ready':
                        raise MCPConnectionError('MCP server initialization failed')
                except MCPConnectionError:
                    if server.required:
                        raise
                    continue
                for remote in connection.tools:
                    grant = server.tools.get(remote.name)
                    if grant is None or grant.permission not in allowed_permissions:
                        connection.event('discovery', remote_tool_name=remote.name, status='permission_denied')
                        continue
                    try:
                        adapter = MCPToolAdapter(connection, remote, grant)
                    except UnsupportedSchema:
                        connection.event('discovery', remote_tool_name=remote.name, status='unsupported_schema')
                        continue
                    registry.register(adapter)
                    self.adapters[adapter.name] = adapter
        except BaseException:
            await self.aclose()
            raise

    def resolve(self, name):
        adapter = self.adapters[name]
        return adapter.server_id, adapter.remote_tool_name

    async def aclose(self):
        for connection in self.connections.values():
            await connection.aclose()
        if self.registry is not None:
            for name, adapter in self.adapters.items():
                self.registry.unregister(name, expected=adapter)
        self.adapters.clear()
