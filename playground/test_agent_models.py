import sys
import os
# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from agent.loop import Agent
from agent.fake_llm import FakeLLM
from src.tools.executor import ToolExecutor

from src.tools.registry import ToolRegistry
from src.tools.file_tools import DeleteNoteTool, ReadNoteTool, SlowTool
from src.tools.permission import Permission

from agent.models import LLMResponse, ToolCall

fake_response = {
"tool_calls":[
 {
  "name":"read_note",
  "arguments":'{"filename":"python.md"}'
 },
 {
   "name":"delete_note",
   "arguments":'{"filename":"python.md"}'
  }
]
}

llm_response = LLMResponse(fake_response)
print(llm_response.tool_calls)
for item in llm_response.tool_calls:
    print(f"Tool Name: {item.name}, Arguments: {item.arguments}")

""" Traceback (most recent call last):
  File "d:\pyproject\cortex\playground\test_agent_models.py", line 29, in <module>
    llm_response = LLMResponse(fake_response)
  File "d:\pyproject\cortex\agent\models.py", line 23, in __init__
    self.tool_calls.append(tool_call)
AttributeError: 'LLMResponse' object has no attribute 'tool_calls'. Did you mean: 'is_tool_call'? """