"""Writing shared files safely. The review server's request threads and the jobs it starts as child processes
read, change and write the same files (people.json, voiceprints.json, a recording's sidecar and transcript):

- `atomic_write_text` replaces a file in one step (a reader never sees half a file; a failed write leaves the old one),
- `registry_lock` keeps one load → change → save of the people and voice print registry at a time, across threads
  and processes, so two of them do not lose each other's changes.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterator

try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

log = logging.getLogger(__name__)

LOCK_TIMEOUT_S = 30.0  # then go on without the lock (and say so) rather than hang a request or a job
LOCK_FILE = ".lock"  # in <out_dir>/_speakers, next to people.json and voiceprints.json


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8", tries: int = 20, wait_s: float = 0.05) -> None:
    """Write a temporary file next to `path` and put it in place. Windows refuses the replace for a moment while
    another process has the target open (a reader, an antivirus): try again briefly before failing."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with open(fd, "w", encoding=encoding) as f:
            f.write(text)
        for i in range(tries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == tries - 1:
                    raise
                time.sleep(wait_s)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_guards: dict[str, threading.RLock] = {}
_guards_lock = threading.Lock()
_held = threading.local()  # {lock file: (depth, os handle)} of this thread: the lock is re-entrant


def _os_lock(path: Path, deadline: float) -> int | None:
    """The lock file locked by this process (byte 0), or None after the deadline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    while True:
        try:
            if msvcrt:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _os_unlock(fd: int) -> None:
    try:
        if msvcrt:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        os.close(fd)  # releases a flock too


@contextmanager
def file_lock(path: Path, timeout_s: float | None = None) -> Iterator[None]:
    """Hold `path` as a lock: one thread of one process at a time, re-entrant within a thread. After the timeout
    the block runs anyway with a warning (a crashed holder must not stop everything; the OS frees its lock)."""
    key = os.path.normcase(os.path.abspath(path))
    with _guards_lock:
        guard = _guards.setdefault(key, threading.RLock())
    deadline = time.monotonic() + (LOCK_TIMEOUT_S if timeout_s is None else timeout_s)
    held = _held.__dict__.setdefault("locks", {})
    got = guard.acquire(timeout=max(0.0, deadline - time.monotonic()))
    depth, fd = held.get(key, (0, None))
    if got and depth == 0:
        fd = _os_lock(path, deadline)
        if fd is None:
            got = False
            guard.release()
    if not got:
        log.warning("%s: still locked after %.1f s, going on without it", path, LOCK_TIMEOUT_S if timeout_s is None
                    else timeout_s)
        yield
        return
    held[key] = (depth + 1, fd)
    try:
        yield
    finally:
        if depth == 0:
            del held[key]
            _os_unlock(fd)
        else:
            held[key] = (depth, fd)
        guard.release()


def registry_lock(out_dir: Path, timeout_s: float | None = None):
    """The people and voice print registry (<out_dir>/_speakers) for one load → change → save."""
    return file_lock(out_dir / "_speakers" / LOCK_FILE, timeout_s)


def registry_locked(fn):
    """`registry_lock` around a whole function whose first argument is the Config."""
    @wraps(fn)
    def run(cfg, *args, **kwargs):
        with registry_lock(cfg.out_dir):
            return fn(cfg, *args, **kwargs)
    return run
