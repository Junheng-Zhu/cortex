from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.file_tools import list_notes, read_note
from src.tools.exceptions import ToolFileNotFoundError, ToolSandboxError


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"PASS  {message}")


def main():
    notes = list_notes()
    check("calendar.txt" in notes, "calendar.txt is discoverable")

    content = read_note("calendar.txt")
    check("会议" in content, "calendar.txt contains 会议")
    check("开会" in content, "calendar.txt contains 开会")
    check("ppt" in content.lower(), "calendar.txt contains ppt")

    try:
        read_note("__cortex_eval_missing__.txt")
    except ToolFileNotFoundError:
        missing_is_controlled = True
    else:
        missing_is_controlled = False
    check(missing_is_controlled, "missing-file handling raises a controlled error")

    try:
        read_note("../.gitignore")
    except ToolSandboxError:
        traversal_is_rejected = True
    else:
        traversal_is_rejected = False
    check(traversal_is_rejected, "path traversal is rejected")

    print("EVAL GATE: PASSED")


if __name__ == "__main__":
    main()
