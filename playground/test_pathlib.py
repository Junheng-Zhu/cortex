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

""" 
工具名称 <function read_note at 0x000001A8A10E2320> 文件地址：'D:\pyproject\cortex\notes\not_exist.md'失败原因: 文件不存在
工具名称 <function read_note at 0x000001A8A10E2320> 访问地址：'D:\pyproject\cortex\secret.txt'失败原因: 读取其他路径
工具名称 <function read_note at 0x000001A8A10E2320> 访问地址：'D:\pyproject\secret.txt'失败原因: 读取其他路径
 """

# ⑤ 绝对路径
# read_note("")
try : print(read_note("D:\\pyproject\\cortex\\README.md"))
except ToolAccessPermissionError as e:
    print(str(e))
try : print(read_note("D:\\pyproject\\cortex\\notes\\python.md"))
except ToolAccessPermissionError as e:
    print(str(e))
# 为什么这里也会读取成功，我前面不是还好拼接上文件夹的地址吗

