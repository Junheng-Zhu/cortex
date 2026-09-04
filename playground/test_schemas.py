import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)


from src.tools.schemas import ReadNoteInput

# tool_Read=ReadNoteInput()
# tool_Read(path = "notes/python.md")
# tool_Read(path = 123)

tool_read = ReadNoteInput(filename="python.md")

print(tool_read)
print(tool_read.filename)
print(type(tool_read))
print(ReadNoteInput.model_json_schema())


# ReadNoteInput()


# ReadNoteInput(path=123)

