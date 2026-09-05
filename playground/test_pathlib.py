import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)

from src.tools.file_tools import read_note
from src.tools.exceptions import *

# ① python.md
try : print(read_note("python.md"))
except ToolFileNotFoundError as e:
    print(str(e))
    

# ② not_exist.md
# read_note("not_exist.md")
try : print(read_note("not_exist.md"))
except ToolFileNotFoundError as e:
    print(str(e))

# ③ ../secret.txt
# read_note("../secret.txt")
try : print(read_note("../secret.txt"))
except ToolAccessPermissionError as e:
    print(str(e))

# ④ ../../secret.txt
# read_note("../../secret.txt")
try : print(read_note("../../secret.txt"))
except ToolAccessPermissionError as e:
    print(str(e))

""" 工具名称 '<function read_note at 0x00000201B6D72320>' 文件地址：'D:\pyproject\cortex\notes\not_exist.md'失败原因: 文件不存在
Traceback (most recent call last):
  File "d:\pyproject\cortex\playground\test_pathlib.py", line 26, in <module>
    try : print(read_note("../secret.txt"))
  File "d:\pyproject\cortex\src\tools\file_tools.py", line 42, in read_note
    raise ToolFileNotFoundError("文件不存在",read_note,filepath)
src.tools.exceptions.ToolFileNotFoundError: 工具名称 '<function read_note at 0x00000201B6D72320>' 文件地址：'D:\pyproject\cortex\notes\..\secret.txt'失败原因: 文件不存在 """

# ⑤ 绝对路径
# read_note("")




