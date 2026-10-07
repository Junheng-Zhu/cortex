"""Conservative paths and atomic file operations shared by V2 recovery."""
import os
from pathlib import Path, PureWindowsPath
from uuid import uuid4

PROTECTED = {'.git', '.cortex'}
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def safe_path(root: Path, relative: str) -> Path:
    parts = relative.split('/')
    if (not relative or '\\' in relative or PureWindowsPath(relative).drive or
            relative.startswith('/') or any(p in {'', '.', '..'} for p in parts) or
            any(p.casefold() in PROTECTED or ':' in p or p.endswith(('.', ' ')) or
                p.split('.')[0].upper() in WINDOWS_RESERVED for p in parts)):
        raise ValueError(f'unsafe or protected workspace path: {relative}')
    path = root / relative
    for parent in [root, *path.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError(f'symlink parent: {relative}')
        if parent.exists() and not parent.is_dir():
            raise ValueError(f'non-directory parent: {relative}')
    return path


def validate_manifest(root, files, blob):
    names = set()
    prefixes = {}
    files = tuple(files)
    file_names = {x.path.casefold() for x in files}
    for item in files:
        safe_path(root, item.path)
        key = item.path.casefold()
        if key in names:
            raise ValueError(f'case collision: {item.path}')
        names.add(key)
        parts = item.path.split('/')
        if any('/'.join(parts[:n]).casefold() in file_names for n in range(1, len(parts))):
            raise ValueError(f'directory/file collision: {item.path}')
        for length in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:length])
            folded = prefix.casefold()
            if folded in prefixes and prefixes[folded] != prefix:
                raise ValueError(f'case collision: {prefix}')
            prefixes[folded] = prefix
        if item.mode not in {'100644', '100755', '120000'}:
            raise ValueError(f'unsupported file mode: {item.mode}')
        if item.mode == '120000':
            target = blob(item.content_hash).decode()
            if (Path(target).is_absolute() or PureWindowsPath(target).drive or '\\' in target or
                    not (root / item.path).parent.joinpath(target).resolve().is_relative_to(root)):
                raise ValueError(f'escaping symlink: {item.path}')
            resolved = (root / item.path).parent.joinpath(target).resolve()
            if any(p.casefold() in PROTECTED for p in resolved.relative_to(root).parts):
                raise ValueError(f'protected symlink: {item.path}')


def write_file(root, relative, item, blob):
    path = safe_path(root, relative)
    if path.exists() and not path.is_symlink() and not path.is_file():
        raise ValueError(f'directory or special file collision: {relative}')
    if item is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / ('.cortex-write-' + uuid4().hex)
    try:
        data = blob(item.content_hash)
        if item.mode == '120000':
            os.symlink(data.decode(), tmp)
        else:
            with open(tmp, 'xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.chmod(0o755 if item.mode == '100755' else 0o644)
        os.replace(tmp, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)
