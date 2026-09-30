"""Single-instance lock files for the pipeline and the daemon.

A lock is a file created with O_EXCL that records the owner's pid and start
time. It is stale (and is taken over) when the owning process no longer exists
or the lock is older than `stale_after_seconds`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_STALE_SECONDS = 6 * 3600


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists. Never signals/kills anything."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # access denied => it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileLock:
    def __init__(self, path: Path, stale_after_seconds: float = DEFAULT_STALE_SECONDS):
        self.path = Path(path)
        self.stale_after_seconds = stale_after_seconds
        self.held = False

    def _read(self) -> Optional[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _is_stale(self, info: Optional[dict]) -> bool:
        if info is None:
            # Unreadable: only a leftover if it has been sitting there for a while.
            try:
                return time.time() - self.path.stat().st_mtime > 60
            except OSError:
                return False
        if time.time() - float(info.get("started", 0) or 0) > self.stale_after_seconds:
            return True
        return not pid_alive(int(info.get("pid", 0) or 0))

    def acquire(self) -> bool:
        """Take the lock without waiting. False when another live owner holds it."""
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = self._read()
                if not self._is_stale(info):
                    return False
                logger.warning("Removing stale lock %s (owner %s no longer active)", self.path.name,
                               (info or {}).get("pid", "?"))
                if self._read() != info:  # somebody replaced it meanwhile
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            except OSError as e:
                logger.error("Cannot create lock %s: %s", self.path, e)
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "started": time.time()}, f)
            self.held = True
            return True
        return False

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        info = self._read()
        if info is None or int(info.get("pid", 0) or 0) == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self):
        return self if self.acquire() else None

    def __exit__(self, *exc):
        self.release()
        return False
