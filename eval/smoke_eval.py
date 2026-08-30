from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.file_tools import list_notes, read_note


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

    missing = read_note("__cortex_eval_missing__.txt")
    check("找不到文件" in missing, "missing-file handling returns a controlled error")

    traversal = read_note("../.gitignore")
    check("不允许访问该路径" in traversal, "path traversal is rejected")

    print("EVAL GATE: PASSED")


if __name__ == "__main__":
    main()
