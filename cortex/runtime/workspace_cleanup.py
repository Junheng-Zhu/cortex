"""Workspace tree removal, including Windows read-only Git object files."""

import os
from pathlib import Path
import shutil
import stat


def _remove_tree(path: str | Path) -> None:
    """Remove a workspace tree; repair permission failures, propagate other errors.

    Use onerror for Python 3.11 compatibility. Never chmod through a symlink,
    suppress a failed retry, or treat a still-existing path as already removed.
    """
    root = Path(path)
    retried = set()

    def remove(entry):
        try:
            entry.lstat()
        except FileNotFoundError:
            return
        shutil.rmtree(entry, onerror=retry)

    def retry(function, failed_path, exc_info):
        error = exc_info[1]
        if isinstance(error, FileNotFoundError) and not os.path.lexists(failed_path):
            return
        if not isinstance(error, PermissionError):
            raise error
        key = (function, os.fspath(failed_path))
        if key in retried:
            raise error
        retried.add(key)
        entry = Path(failed_path)
        info = entry.lstat()
        mode = info.st_mode
        if (stat.S_ISLNK(mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise error
        if function not in (os.unlink, os.remove, os.rmdir, os.scandir, os.listdir, os.open):
            raise error
        writable = stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(mode):
            writable |= stat.S_IXUSR
        entry.chmod(writable)
        if function in (os.unlink, os.remove, os.rmdir):
            function(failed_path)
        elif function in (os.scandir, os.listdir, os.open):
            # Traversal callbacks do not resume walking after a successful retry.
            # Complete that subtree instead of merely reopening its directory.
            remove(entry)
        else:
            raise error

    remove(root)
