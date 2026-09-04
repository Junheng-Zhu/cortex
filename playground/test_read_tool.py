import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

sys.path.insert(0, PROJECT_ROOT)


from src.tools.file_tools import ReadNoteTool

read=ReadNoteTool()
result=read.execute("python.md")
print(result)

result=read.execute("not_exist.md")
print(result)

result=read.execute(123)
print(result)