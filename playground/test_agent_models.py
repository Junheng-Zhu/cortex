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

llm_response = LLMResponse()
llm_response.from_dict(fake_response)
print(llm_response.tool_calls)
for item in llm_response.tool_calls:
    print(f"Tool Name: {item.name}, Arguments: {item.arguments}")

""" [ToolCall(name='read_note', arguments={'filename': 'python.md'}, tool_call_id=None), ToolCall(name='delete_note', arguments={'filename': 'python.md'}, tool_call_id=None)]
Tool Name: read_note, Arguments: {'filename': 'python.md'}
Tool Name: delete_note, Arguments: {'filename': 'python.md'} """