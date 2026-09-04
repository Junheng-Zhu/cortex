import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

sys.path.insert(0, PROJECT_ROOT)

from src.tools.registry import ToolRegistry
from src.tools.executor import ToolExecutor
from src.tools.file_tools import ReadNoteTool

registry=ToolRegistry()
read_note_tool=ReadNoteTool()
executor=ToolExecutor(registry)

registry.register(read_note_tool)




# ① 正常 Tool
#    "read_note" + {"filename": "python.md"}
""" result=executor.execute("read_note",{"filename": "python.md"})
print(result) """

# ② 缺参数
#    "read_note" + {}
""" result=executor.execute("read_note",{})
print(result) """

""" Traceback (most recent call last):
  File "d:\pyproject\cortex\playground\test_executor.py", line 31, in <module>
    result=executor.execute("read_note",{})
  File "d:\pyproject\cortex\src\tools\executor.py", line 13, in execute
    validated_input=tool.input_model(**arguments)
  File "D:\pyproject\cortex\venv\lib\site-packages\pydantic\main.py", line 263, in __init__
    validated_self = self.__pydantic_validator__.validate_python(data, self_instance=self)
pydantic_core._pydantic_core.ValidationError: 1 validation error for ReadNoteInput
filename
  Field required [type=missing, input_value={}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing """

# ③ 错误 Tool
#    "xxx" + {...}
""" result=executor.execute("read",{"filename": "python.md"})
print(result) """

""" Traceback (most recent call last):
  File "d:\pyproject\cortex\playground\test_executor.py", line 37, in <module>
    result=executor.execute("read",{"filename": "python.md"})
  File "d:\pyproject\cortex\src\tools\executor.py", line 13, in execute
    validated_input=tool.input_model(**arguments)
AttributeError: 'NoneType' object has no attribute 'input_model' """


# ④ 文件不存在
#    "read_note" + {"filename": "not_exist.md"}
result=executor.execute("read_note",{"filename": "not_exist.md"})
print(result)

# 错误：找不到文件 not_exist.md