"""File store helpers: safe names, safe paths, mime types, disk-space checks.

Layout: <data dir>/files/<task-id>/<sanitized name>. The data dir is the directory holding the database,
so a temporary FTASK_DB never touches the real file store.
"""
from __future__ import annotations

import errno
import mimetypes
import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path

CHUNK = 1024 * 1024
MAX_NAME_BYTES = 200
SPACE_RESERVE = 128 * 1024 * 1024  # always leave this much free on the disk

# C0/C1 controls, DEL, bidi overrides/isolates, zero-width and BOM characters
_BAD_CHARS = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")


def files_root(db_path) -> Path:
    return Path(db_path).parent / "files"


def sanitize_name(name) -> str:
    """Return a file name that is safe as a single path component (never empty, no separators, not hidden)."""
    if not isinstance(name, str):
        name = ""
    name = unicodedata.normalize("NFC", name)
    name = _BAD_CHARS.sub("_", name)
    name = re.split(r"[\\/]", name)[-1]  # keep only the last path component, for both separators
    name = name.strip().lstrip(".").rstrip(". ")
    if not name:
        return "file"
    raw = name.encode("utf-8")
    if len(raw) > MAX_NAME_BYTES:
        stem, ext = os.path.splitext(name)
        ext = ext if len(ext.encode("utf-8")) <= 20 else ""
        budget = MAX_NAME_BYTES - len(ext.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(". ")
        name = (stem or "file") + ext
    return name


def guess_mime(name, client=None) -> str:
    guessed, _ = mimetypes.guess_type(name, strict=False)
    if guessed and _MIME_RE.match(guessed):
        return guessed.lower()
    if isinstance(client, str) and _MIME_RE.match(client.strip()):
        return client.strip().lower()
    return "application/octet-stream"


def format_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"


def is_enospc(e: OSError) -> bool:
    return e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1), errno.EFBIG)


def free_space(path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(str(p)).free


def ensure_task_dir(root: Path, task_id: str) -> Path:
    """Create <root>/<task-id> (0700) and make sure it is a real directory, not a symlink."""
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", task_id):
        raise ValueError("bad task id")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)  # the root itself may be a symlink the admin set up
    d = root / task_id
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"{d} is not a plain directory (symlink?); refusing to use it")
    return d


def reserve_name(directory: Path, name: str, tmp_path: Path) -> Path:
    """Atomically move tmp_path to directory/name, adding ' (2)', ' (3)'... on collision. Never overwrites."""
    stem, ext = os.path.splitext(name)
    for n in range(1, 10000):
        cand = name if n == 1 else f"{stem} ({n}){ext}"
        if len(cand.encode("utf-8")) > 250:
            cand = f"{stem[:80]} ({n}){ext}"
        target = directory / cand
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        try:
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
        return target
    raise FileExistsError("could not find a free file name")


def resolve_stored(root: Path, rel: str) -> Path:
    """Absolute path for a stored relative path, validated: exactly <task-id>/<name>, no tricks."""
    parts = rel.split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts):
        raise PermissionError("bad stored path")
    return root / parts[0] / parts[1]


def open_stored(root: Path, rel: str):
    """Open a stored file for reading, refusing symlinks and non-regular files. Returns (file object, size)."""
    path = resolve_stored(root, rel)
    dst = os.lstat(path.parent)
    if not stat.S_ISDIR(dst.st_mode):
        raise PermissionError("task directory is not a plain directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError("not a regular file")
        return os.fdopen(fd, "rb"), st.st_size
    except BaseException:
        os.close(fd)
        raise


def delete_stored(root: Path, rel: str) -> bool:
    """Unlink a stored file (only if it is a plain file inside the store). Remove the task dir when empty."""
    try:
        path = resolve_stored(root, rel)
        st = os.lstat(path)
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            return False
        os.unlink(path)  # unlinking a symlink removes only the link
    except (OSError, PermissionError):
        return False
    try:
        os.rmdir(path.parent)
    except OSError:
        pass
    return True


def content_disposition(name: str) -> str:
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\%]', "_", name) or "file"
    from urllib.parse import quote
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
