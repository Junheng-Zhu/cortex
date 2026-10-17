"""Optional MCP external tool provider (official SDK, stdio only)."""
from .config import MCPConfig, ServerConfig, ToolGrant, load_config
from .manager import MCPManager
from .adapter import MCPToolAdapter

__all__ = ['MCPConfig', 'ServerConfig', 'ToolGrant', 'load_config', 'MCPManager', 'MCPToolAdapter']
