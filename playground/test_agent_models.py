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

""" Traceback (most recent call last):
  File "d:\pyproject\cortex\playground\test_agent_models.py", line 14, in <module>
    from agent.models import LLMResponse, ToolCall
  File "d:\pyproject\cortex\agent\models.py", line 5, in <module>
    class ToolCall:
  File "D:\python 3.10.10\lib\dataclasses.py", line 1184, in dataclass
    return wrap(cls)
  File "D:\python 3.10.10\lib\dataclasses.py", line 1175, in wrap
    return _process_class(cls, init, repr, eq, order, unsafe_hash,
  File "D:\python 3.10.10\lib\dataclasses.py", line 1024, in _process_class
    _init_fn(all_init_fields,
  File "D:\python 3.10.10\lib\dataclasses.py", line 544, in _init_fn
    raise TypeError(f'non-default argument {f.name!r} '
TypeError: non-default argument 'name' follows default argument """