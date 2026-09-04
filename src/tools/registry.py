from .base import Tool


class ToolRegistry:

    def __init__(self):
        self._tools = {}

    def register(self, tool: Tool):

        self._tools[tool.name] = tool

    def list_tools(self):
        return self._tools.keys()

    def get(self, name: str) -> Tool:

        return self._tools.get(name)
