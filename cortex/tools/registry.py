from .base import Tool
from .base import ToolNotFoundError


class ToolRegistry:

    def __init__(self):
        self._tools = {}

    def register(self, tool: Tool):

        self._tools[tool.name] = tool

    def list_tools(self):
        return self._tools.keys()

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)

        if tool is None:
            raise ToolNotFoundError("工具未找到", name)

        return tool
