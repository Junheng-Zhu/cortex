from base import Tool

class ToolRegistry:

    _tool={}


    def __init__(self):


    def register(self, tool:Tool ):

        self._tool[tool.name]=tool
#z这里为什么报错，应为缩进块
    def list_tool(self):
        return self._tool.keys()
        
    
    def get(self,name:str)->Tool:

        return self._tools.get(name)
    