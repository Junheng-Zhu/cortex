import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)

from src.tools.registry import ToolRegistry
from src.tools.executor import ToolExecutor
from src.tools.file_tools import *
from src.tools.permission import Permission


registry = ToolRegistry()
read_note_tool = ReadNoteTool()
delete_note_tool = DeleteNoteTool()
slow_tool = SlowTool()

allowed_permissions = {Permission.READ, Permission.WRITE, Permission.DELETE}

executor = ToolExecutor(allowed_permissions,registry)

registry.register(read_note_tool)
registry.register(delete_note_tool)
registry.register(slow_tool)

fake_response = {
    "name":"read_note",
    "arguments":{
        "filename":"python.md"
    }
}
result=executor.execute(
    fake_response["name"],
    fake_response["arguments"]
)
print(result)
""" PS D:\pyproject> & d:/pyproject/cortex/venv/Scripts/python.exe d:/pyproject/cortex/playground/test_fake_llm.py
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "D:\python 3.10.10\lib\multiprocessing\spawn.py", line 116, in spawn_main
    exitcode = _main(fd, parent_sentinel)
  File "D:\python 3.10.10\lib\multiprocessing\spawn.py", line 125, in _main
    prepare(preparation_data)
  File "D:\python 3.10.10\lib\multiprocessing\spawn.py", line 236, in prepare
    _fixup_main_from_path(data['init_main_from_path'])
  File "D:\python 3.10.10\lib\multiprocessing\spawn.py", line 287, in _fixup_main_from_path
    main_content = runpy.run_path(main_path,
  File "D:\python 3.10.10\lib\runpy.py", line 289, in run_path
    return _run_module_code(code, init_globals, run_name,
  File "D:\python 3.10.10\lib\runpy.py", line 96, in _run_module_code
    _run_code(code, mod_globals, init_globals,
  File "D:\python 3.10.10\lib\runpy.py", line 86, in _run_code
    exec(code, run_globals)
  File "d:\pyproject\cortex\playground\test_fake_llm.py", line 34, in <module>
    result=executor.execute(
  File "d:\pyproject\cortex\src\tools\executor.py", line 167, in execute
    tool_result = self._execute_with_retry(tool, validated_input)
  File "d:\pyproject\cortex\src\tools\executor.py", line 100, in _execute_with_retry
    p.start()
  File "D:\python 3.10.10\lib\multiprocessing\process.py", line 121, in start
    self._popen = self._Popen(self)
  File "D:\python 3.10.10\lib\multiprocessing\context.py", line 224, in _Popen
    return _default_context.get_context().Process._Popen(process_obj)
  File "D:\python 3.10.10\lib\multiprocessing\context.py", line 336, in _Popen
    return Popen(process_obj)
  File "D:\python 3.10.10\lib\multiprocessing\popen_spawn_win32.py", line 45, in __init__
    prep_data = spawn.get_preparation_data(process_obj._name)
  File "D:\python 3.10.10\lib\multiprocessing\spawn.py", line 154, in get_preparation_data
    _check_not_importing_main()
  File "D:\python 3.10.10\lib\multiprocessing\spawn.py", line 134, in _check_not_importing_main
    raise RuntimeError('''
RuntimeError: 
        An attempt has been made to start a new process before the
        current process has finished its bootstrapping phase.

        This probably means that you are not using fork to start your
        child processes and you have forgotten to use the proper idiom
        in the main module:

            if __name__ == '__main__':
                freeze_support()
                ...

        The "freeze_support()" line can be omitted if the program
        is not going to be frozen to produce an executable.
Traceback (most recent call last):
  File "d:\pyproject\cortex\playground\test_fake_llm.py", line 34, in <module>
    result=executor.execute(
  File "d:\pyproject\cortex\src\tools\executor.py", line 167, in execute
    tool_result = self._execute_with_retry(tool, validated_input)
  File "d:\pyproject\cortex\src\tools\executor.py", line 112, in _execute_with_retry
    outcome = tool_queue.get()
  File "D:\python 3.10.10\lib\multiprocessing\queues.py", line 103, in get
    res = self._recv_bytes()
  File "D:\python 3.10.10\lib\multiprocessing\connection.py", line 216, in recv_bytes
    buf = self._recv_bytes(maxlength)
  File "D:\python 3.10.10\lib\multiprocessing\connection.py", line 305, in _recv_bytes
    waitres = _winapi.WaitForMultipleObjects(
KeyboardInterrupt """