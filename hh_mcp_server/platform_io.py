"""Cross-platform primitives for private files and local process locking."""
from __future__ import annotations

import os
from pathlib import Path
import time


IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def private_rw_flags() -> int:
    """Return secure read/write-create flags supported by the current platform."""
    return os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)


def _reject_symlink_without_nofollow(path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise OSError(f"Refusing to follow symlink for private file: {path}")


def set_private_fd_mode(fd: int, mode: int = 0o600) -> None:
    """Apply POSIX owner-only permissions when the platform supports them."""
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)


def set_private_path_mode(path: Path, mode: int) -> None:
    """Apply POSIX modes; Windows relies on the user's profile ACL instead."""
    if not IS_WINDOWS:
        path.chmod(mode)


def open_private_rw(path: Path) -> int:
    _reject_symlink_without_nofollow(path)
    fd = os.open(path, private_rw_flags(), 0o600)
    set_private_fd_mode(fd)
    return fd


def acquire_exclusive_lock(
    path: Path, *, wait_seconds: float = 0.0, poll_interval: float = 0.1
) -> int:
    """Acquire a local-process lock, optionally waiting for transient contention."""
    if wait_seconds < 0:
        raise ValueError("wait_seconds must be non-negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")

    deadline = time.monotonic() + wait_seconds
    while True:
        fd = open_private_rw(path)
        try:
            if IS_WINDOWS:
                # msvcrt.locking locks a byte range from the current file position.
                # Keep one durable byte in the lock file so the range always exists.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise BlockingIOError("Lock is already held") from error
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise


def fsync_directory(path: Path) -> None:
    """Persist directory metadata where directory fsync is supported."""
    if IS_WINDOWS:
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
